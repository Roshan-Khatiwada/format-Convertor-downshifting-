"""
Core layout-aware PDF extractor.

Uses PyMuPDF page.get_text("dict") to extract content blocks in their
exact visual reading order (top-to-bottom, left-to-right).

Integrates:
- Text extraction with font metadata
- Figure extraction (FigureExtractor)
- Table extraction (TableExtractor)
- OCR fallback for scanned pages
- Section detection (section_detector)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from .config import Config
from .figure_extractor import FigureExtractor
from .ocr import ocr_page, page_has_text_layer
from .section_detector import FontProfile, build_font_profile
from .table_extractor import TableExtractor
from .utils import sanitize_filename

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

_READING_ORDER_COLS = 2  # Number of columns to detect multi-column layout


class PDFExtractor:
    """
    Extracts raw layout blocks from a PDF in visual reading order.

    Returns a flat list of block dicts that the JSONBuilder will
    assemble into the final JSON structure.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._figure_extractor = FigureExtractor(
            figures_dir=config.figures_dir,
            min_width=config.min_image_width,
            min_height=config.min_image_height,
            min_bytes=config.min_image_size,
        )

    def extract(self, pdf_path: Path) -> list[dict]:
        """
        Main entry point. Returns all blocks in reading order.
        """
        logger.info("Extracting: %s", pdf_path.name)
        doc_slug = sanitize_filename(pdf_path.stem).lower()

        doc = fitz.open(str(pdf_path))
        table_extractor = TableExtractor(pdf_path, strategy=self.config.table_strategy)

        # Extract all tables up-front for bbox matching
        all_tables = table_extractor.extract_all_tables()
        table_index = _build_table_index(all_tables)

        all_blocks: list[dict] = []
        font_profile: Optional[FontProfile] = None

        # First pass: gather all text blocks to build font profile
        raw_text_pass: list[list[dict]] = []
        for page in doc:
            if page_has_text_layer(page):
                page_blocks = self._extract_text_blocks(page)
            else:
                page_blocks = ocr_page(page, dpi=self.config.ocr_dpi, language=self.config.ocr_language)
            raw_text_pass.append(page_blocks)

        # Build global font profile from all text blocks
        flat_text = [b for pg in raw_text_pass for b in pg]
        font_profile = build_font_profile(flat_text)
        logger.debug("Global font profile: body=%.1fpt", font_profile.body_size)

        # Second pass: assemble full block list with figures interleaved
        for page in doc:
            page_num = page.number + 1
            text_blocks = raw_text_pass[page.number]
            figure_blocks = self._figure_extractor.extract_page_figures(doc, page, doc_slug)
            page_table_blocks = table_index.get(page_num, [])

            # Merge all blocks and sort by visual reading order
            merged = _merge_and_sort(text_blocks, figure_blocks, page_table_blocks)

            # Annotate each block with font profile info
            for blk in merged:
                blk["_font_profile"] = font_profile

            all_blocks.extend(merged)

        doc.close()

        # Link captions to adjacent figures
        _link_captions(all_blocks)

        logger.info(
            "Extracted %d blocks from '%s'.",
            len(all_blocks),
            pdf_path.name,
        )
        return all_blocks

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _extract_text_blocks(page: fitz.Page) -> list[dict]:
        """
        Extract text blocks from a page using PyMuPDF dict mode.
        Returns blocks with content, position, and font metadata.
        """
        page_num = page.number + 1
        page_dict = page.get_text("dict", sort=True)
        blocks: list[dict] = []

        for blk in page_dict.get("blocks", []):
            btype = blk.get("type", -1)
            if btype == 1:
                # Image block in text stream — will be handled by figure extractor
                continue
            if btype != 0:
                continue

            lines_text: list[str] = []
            max_font_size: float = 0.0
            is_bold = False
            is_italic = False

            for line in blk.get("lines", []):
                line_parts: list[str] = []
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    line_parts.append(text)
                    sz = span.get("size", 0.0)
                    if sz > max_font_size:
                        max_font_size = sz

                    flags = span.get("flags", 0)
                    # bit 4 = bold, bit 1 = italic (PyMuPDF flags)
                    if flags & (1 << 4):
                        is_bold = True
                    if flags & (1 << 1):
                        is_italic = True

                if line_parts:
                    lines_text.append(" ".join(line_parts))

            content = "\n".join(lines_text).strip()
            if not content:
                continue

            bbox = blk.get("bbox", (0, 0, 0, 0))
            blocks.append({
                "type": "text",
                "content": content,
                "page": page_num,
                "x0": bbox[0],
                "y0": bbox[1],
                "x1": bbox[2],
                "y1": bbox[3],
                "font_size": max_font_size,
                "bold": is_bold,
                "italic": is_italic,
                "source": "native",
            })

        return blocks


# ── Sorting & merging ────────────────────────────────────────────────────────

def _merge_and_sort(
    text_blocks: list[dict],
    figure_blocks: list[dict],
    table_blocks: list[dict],
) -> list[dict]:
    """
    Merge text, figure, and table blocks for a single page.
    Sort by visual reading order: top-to-bottom, left-to-right.
    Text blocks that spatially overlap with a table are suppressed
    (replaced by the structured table).
    """
    # Remove text blocks that are inside table bounding boxes
    table_bboxes = [(t["x0"], t["y0"], t["x1"], t["y1"]) for t in table_blocks]
    filtered_text = [
        b for b in text_blocks
        if not _any_bbox_contains(b["x0"], b["y0"], b["x1"], b["y1"], table_bboxes)
    ]

    all_blocks = filtered_text + figure_blocks + table_blocks

    # Sort: primary key = y0 (top), secondary = x0 (left)
    all_blocks.sort(key=lambda b: (b.get("y0", 0), b.get("x0", 0)))
    return all_blocks


def _any_bbox_contains(
    x0: float, y0: float, x1: float, y1: float,
    bboxes: list[tuple[float, float, float, float]],
    overlap_threshold: float = 0.6,
) -> bool:
    """
    Return True if the given bbox significantly overlaps with any bbox in list.
    """
    bw = x1 - x0
    bh = y1 - y0
    area = bw * bh
    if area <= 0:
        return False

    for tx0, ty0, tx1, ty1 in bboxes:
        ix0 = max(x0, tx0)
        iy0 = max(y0, ty0)
        ix1 = min(x1, tx1)
        iy1 = min(y1, ty1)
        iw = max(0.0, ix1 - ix0)
        ih = max(0.0, iy1 - iy0)
        intersection = iw * ih
        if intersection / area >= overlap_threshold:
            return True
    return False


def _build_table_index(tables: list[dict]) -> dict[int, list[dict]]:
    """Group tables by page number for fast lookup."""
    idx: dict[int, list[dict]] = {}
    for t in tables:
        pg = t.get("page", 1)
        idx.setdefault(pg, []).append(t)
    return idx


# ── Caption linking ──────────────────────────────────────────────────────────

def _link_captions(blocks: list[dict], proximity_y: float = 30.0) -> None:
    """
    In-place: find caption text blocks adjacent to figure blocks and
    attach the caption text to the figure's "caption" field.

    Looks for caption blocks immediately before or after a figure,
    on the same page, within proximity_y points vertically.
    """
    from .utils import is_caption_text

    n = len(blocks)
    for i, blk in enumerate(blocks):
        if blk.get("type") != "figure":
            continue

        fig_page = blk.get("page")
        fig_y0 = blk.get("y0", 0)
        fig_y1 = blk.get("y1", 0)

        # Check block immediately above
        if i > 0:
            prev = blocks[i - 1]
            if (
                prev.get("type") == "text"
                and prev.get("page") == fig_page
                and is_caption_text(prev.get("content", ""))
                and abs(prev.get("y1", 0) - fig_y0) < proximity_y
            ):
                blk["caption"] = prev["content"].strip()
                prev["_absorbed"] = True
                continue

        # Check block immediately below
        if i + 1 < n:
            nxt = blocks[i + 1]
            if (
                nxt.get("type") == "text"
                and nxt.get("page") == fig_page
                and is_caption_text(nxt.get("content", ""))
                and abs(nxt.get("y0", 0) - fig_y1) < proximity_y
            ):
                blk["caption"] = nxt["content"].strip()
                nxt["_absorbed"] = True
