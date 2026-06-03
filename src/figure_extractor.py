"""
Figure extraction module.

Extracts images from PDF pages using PyMuPDF.
De-duplicates images via SHA-256 hashing.
Returns figure blocks with file references and bounding boxes.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


class FigureExtractor:
    """
    Extracts and de-duplicates images from PDF documents.

    Two levels of deduplication are applied:

    1. GLOBAL hash dedup — same image bytes across the whole document
       are saved to disk only once (e.g. repeated logos).

    2. PER-PAGE hash + bbox dedup — if the same image XObject is
       referenced multiple times on ONE page (thumbnails, soft-masks,
       Form XObject re-use), only the largest / most prominent instance
       is emitted as a JSON block.  This is the fix for the "one figure
       in the PDF but N entries in the JSON" bug.
    """

    def __init__(
        self,
        figures_dir: Path,
        min_width: int = 50,
        min_height: int = 50,
        min_bytes: int = 1000,
    ) -> None:
        self.figures_dir = figures_dir
        self.min_width = min_width
        self.min_height = min_height
        self.min_bytes = min_bytes

        # Global: hash -> saved file path (deduplicates the saved file)
        self._seen: dict[str, Path] = {}
        # Track hashes already emitted to JSON to avoid cross-page duplicates
        self._emitted_hashes: set[str] = set()

        self.figures_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────

    def extract_page_figures(
        self,
        doc: fitz.Document,
        page: fitz.Page,
        doc_slug: str,
    ) -> list[dict]:
        """
        Extract all figures from a single PDF page.

        Returns a list of de-duplicated figure dicts, each with positional
        data so they can be inserted into the reading-order stream.

        Shape:
        {
            "type": "figure",
            "page": int,
            "x0": float, "y0": float, "x1": float, "y1": float,
            "file": str,          # relative path as string
            "caption": None,      # filled later by extractor
            "hash": str,
        }
        """
        page_num = page.number + 1
        image_list = page.get_images(full=True)
        if not image_list:
            return []

        # Map xref -> bbox on this page
        xref_to_bbox = self._get_image_bboxes(page)

        # --- Pass 1: collect all candidate figures for this page ----------
        candidates: list[dict] = []
        img_index = 0

        for img_info in image_list:
            xref = img_info[0]
            img_index += 1

            try:
                raw, ext = self._extract_raw_image(doc, xref)
            except Exception as exc:
                logger.debug("Page %d xref %d: extraction failed: %s", page_num, xref, exc)
                continue

            if not raw or len(raw) < self.min_bytes:
                continue

            img_hash = hashlib.sha256(raw).hexdigest()

            # Save file globally (once per unique hash)
            if img_hash in self._seen:
                saved_path = self._seen[img_hash]
            else:
                filename = f"{doc_slug}_p{page_num}_{img_index}.{ext}"
                saved_path = self.figures_dir / filename
                saved_path.write_bytes(raw)
                self._seen[img_hash] = saved_path
                logger.debug("Saved figure: %s", saved_path)

            bbox = xref_to_bbox.get(xref)
            if bbox is None:
                bbox = (0.0, 0.0, float(page.rect.width), float(page.rect.height))

            x0, y0, x1, y1 = bbox
            w, h = x1 - x0, y1 - y0

            if w < self.min_width or h < self.min_height:
                logger.debug("Page %d: skipping tiny image (%.0fx%.0f)", page_num, w, h)
                continue

            # Skip full-page background images (likely watermarks/backgrounds)
            page_area = float(page.rect.width) * float(page.rect.height)
            img_area = w * h
            if img_area > page_area * 0.8:
                logger.debug("Page %d: skipping full-page background image", page_num)
                continue

            candidates.append({
                "type": "figure",
                "page": page_num,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "file": str(saved_path),
                "caption": None,
                "hash": img_hash,
                "_area": w * h,
            })

        # --- Pass 2: per-page deduplication --------------------------------
        # Problem: the same XObject can appear multiple times on one page as:
        #   • the real figure  +  a tiny soft-mask / thumbnail copy
        #   • a Form XObject that re-embeds the image at a different scale
        #
        # Strategy:
        #   a) Group candidates by hash.
        #   b) Within each hash-group, keep only the LARGEST instance
        #      (highest area), since that is the actual content figure.
        #   c) Additionally suppress any candidate whose bbox is almost
        #      entirely contained inside another candidate's bbox (catches
        #      the case where two different xrefs have the same visual area
        #      but different hashes, e.g. CMYK vs RGB versions).

        deduped = _deduplicate_candidates(candidates)

        logger.debug(
            "Page %d: %d raw candidates → %d after dedup.",
            page_num, len(candidates), len(deduped),
        )
        # Strip internal _area key and filter cross-page duplicates
        result = []
        for blk in deduped:
            blk.pop("_area", None)
            # Skip if this hash was already emitted on a previous page
            if blk["hash"] not in self._emitted_hashes:
                self._emitted_hashes.add(blk["hash"])
                result.append(blk)
            else:
                logger.debug(
                    "Page %d: skipping cross-page duplicate (hash %s)",
                    page_num, blk["hash"][:8],
                )

        return result

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _extract_raw_image(doc: fitz.Document, xref: int) -> tuple[bytes, str]:
        """
        Extract raw image bytes and determine file extension.
        Converts CMYK/exotic colorspaces to RGB PNG.
        """
        img_dict = doc.extract_image(xref)
        raw: bytes = img_dict["image"]
        ext: str = img_dict.get("ext", "png").lower()
        colorspace: str = img_dict.get("colorspace", 3)

        # Handle CMYK or unusual colorspaces
        n = img_dict.get("n", 3)
        if n == 4 or ext not in ("png", "jpg", "jpeg", "bmp", "gif", "tiff"):
            pix = fitz.Pixmap(doc, xref)
            if pix.n - (pix.alpha or 0) > 3:  # CMYK
                pix = fitz.Pixmap(fitz.csRGB, pix)
            raw = pix.tobytes("png")
            ext = "png"

        return raw, ext

    @staticmethod
    def _get_image_bboxes(page: fitz.Page) -> dict[int, tuple[float, float, float, float]]:
        """
        Build a mapping from image xref -> bounding box on the page.
        Uses page.get_image_rects() when available (PyMuPDF >= 1.18).
        Falls back to iterating page blocks.
        """
        xref_bbox: dict[int, tuple[float, float, float, float]] = {}

        # Method 1: get_image_rects (preferred, accurate)
        try:
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                rects = page.get_image_rects(xref)
                if rects:
                    r = rects[0]
                    xref_bbox[xref] = (r.x0, r.y0, r.x1, r.y1)
            return xref_bbox
        except AttributeError:
            pass  # older PyMuPDF

        # Method 2: Iterate blocks from get_text("dict")
        try:
            page_dict = page.get_text("dict")
            for blk in page_dict.get("blocks", []):
                if blk.get("type") == 1:  # image block
                    xref = blk.get("xref", -1)
                    if xref > 0:
                        b = blk["bbox"]
                        xref_bbox[xref] = (b[0], b[1], b[2], b[3])
        except Exception:
            pass

        return xref_bbox


# ── Module-level helpers ─────────────────────────────────────────────────────

def _deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """
    Remove duplicate figure candidates for a single page.

    Step A — Hash grouping:
        Group by SHA-256 hash. Within each group keep only the instance
        with the LARGEST bounding-box area. This handles the common case
        where one logical figure produces multiple XObject references
        (e.g. the real image + a soft-mask copy at a smaller scale).

    Step B — Spatial containment:
        After Step A, if candidate A's bbox is almost entirely contained
        inside candidate B's bbox, remove A. This catches stacked images
        (vector overlay on raster background) where only the outer one
        should appear in the JSON.
    """
    if not candidates:
        return []

    from collections import defaultdict

    # ── Step A: keep largest-area instance per hash ───────────────────────
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_hash[c["hash"]].append(c)

    after_a: list[dict] = []
    for group in by_hash.values():
        best = max(group, key=lambda c: c["_area"])
        after_a.append(best)

    if len(after_a) <= 1:
        return after_a

    # ── Step B: remove spatially-contained duplicates ─────────────────────
    after_a.sort(key=lambda c: c["_area"], reverse=True)  # largest first

    kept: list[dict] = []
    for candidate in after_a:
        dominated = any(
            _bbox_contained_by(
                (candidate["x0"], candidate["y0"], candidate["x1"], candidate["y1"]),
                (other["x0"], other["y0"], other["x1"], other["y1"]),
                threshold=0.85,
            )
            for other in kept
        )
        if not dominated:
            kept.append(candidate)

    # Restore visual reading order (top-to-bottom, left-to-right)
    kept.sort(key=lambda c: (c["y0"], c["x0"]))
    return kept


def _bbox_contained_by(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
    threshold: float = 0.85,
) -> bool:
    """
    Return True if `inner` bbox is at least `threshold` fraction
    covered by `outer` bbox.
    """
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer

    inner_area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inner_area <= 0:
        return True  # zero-area → discard

    cx0 = max(ix0, ox0)
    cy0 = max(iy0, oy0)
    cx1 = min(ix1, ox1)
    cy1 = min(iy1, oy1)
    intersection = max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)

    return (intersection / inner_area) >= threshold
