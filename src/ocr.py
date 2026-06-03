"""
OCR fallback module for scanned or image-heavy PDF pages.

Strategy:
1. Try PaddleOCR if available (best layout preservation).
2. Fall back to pytesseract.
3. Return raw text blocks with approximate bounding boxes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# ── Lazy imports – don't crash at import time if optional deps are absent ──

def _try_import_paddle():
    try:
        from paddleocr import PaddleOCR  # type: ignore
        return PaddleOCR
    except ImportError:
        return None


def _try_import_tesseract():
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        return pytesseract, Image
    except ImportError:
        return None


# ── Public API ─────────────────────────────────────────────────────────────

def page_has_text_layer(page: fitz.Page, min_chars: int = 20) -> bool:
    """
    Return True if the page has a meaningful text layer.
    Uses a conservative threshold to avoid treating sparse/watermark
    text as a real text layer.
    """
    text = page.get_text("text")
    return len(text.strip()) >= min_chars


def ocr_page(page: fitz.Page, dpi: int = 300, language: str = "en") -> list[dict]:
    """
    OCR a single PyMuPDF page and return a list of text blocks.

    Each block has the shape:
    {
        "type": "text",
        "content": str,
        "page": int,
        "x0": float, "y0": float, "x1": float, "y1": float,
        "font_size": float,
        "bold": False,
        "italic": False,
        "source": "ocr"
    }
    """
    page_num = page.number + 1

    # Render page to image
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    clip = page.rect
    pix = page.get_pixmap(matrix=mat, clip=clip)
    img_bytes = pix.tobytes("png")

    blocks = _ocr_with_paddle(img_bytes, page_num, pix.width, pix.height, clip, dpi)

    if blocks is None:
        blocks = _ocr_with_tesseract(img_bytes, page_num, pix.width, pix.height, clip, dpi)

    if blocks is None:
        logger.warning("Page %d: OCR unavailable. No text extracted.", page_num)
        blocks = []

    return blocks


# ── Internal helpers ────────────────────────────────────────────────────────

def _scale_bbox(
    bbox: tuple[float, float, float, float],
    img_w: int,
    img_h: int,
    page_rect: fitz.Rect,
) -> tuple[float, float, float, float]:
    """
    Scale from image pixel coords back to PDF page coordinates.
    """
    pw = page_rect.width or 1
    ph = page_rect.height or 1
    x0, y0, x1, y1 = bbox
    return (
        x0 / img_w * pw + page_rect.x0,
        y0 / img_h * ph + page_rect.y0,
        x1 / img_w * pw + page_rect.x0,
        y1 / img_h * ph + page_rect.y0,
    )


def _ocr_with_paddle(
    img_bytes: bytes,
    page_num: int,
    img_w: int,
    img_h: int,
    page_rect: fitz.Rect,
    dpi: int,
) -> Optional[list[dict]]:
    """Run PaddleOCR on raw PNG bytes. Returns None if not available."""
    PaddleOCR = _try_import_paddle()
    if PaddleOCR is None:
        return None

    try:
        import numpy as np
        from PIL import Image as PILImage
        import io

        ocr_engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        image = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
        arr = np.array(image)
        result = ocr_engine.ocr(arr, cls=True)

        blocks: list[dict] = []
        if not result or not result[0]:
            return blocks

        for line in result[0]:
            if not line:
                continue
            poly, (text, confidence) = line
            if confidence < 0.5 or not text.strip():
                continue

            # poly: [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            px0, py0, px1, py1 = min(xs), min(ys), max(xs), max(ys)
            x0, y0, x1, y1 = _scale_bbox((px0, py0, px1, py1), img_w, img_h, page_rect)

            blocks.append({
                "type": "text",
                "content": text.strip(),
                "page": page_num,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "font_size": max(8.0, (y1 - y0) * 0.75),
                "bold": False,
                "italic": False,
                "source": "ocr_paddle",
            })

        logger.info("Page %d: PaddleOCR extracted %d blocks.", page_num, len(blocks))
        return blocks

    except Exception as exc:
        logger.warning("PaddleOCR failed on page %d: %s", page_num, exc)
        return None


def _ocr_with_tesseract(
    img_bytes: bytes,
    page_num: int,
    img_w: int,
    img_h: int,
    page_rect: fitz.Rect,
    dpi: int,
) -> Optional[list[dict]]:
    """Run pytesseract on raw PNG bytes. Returns None if not available."""
    result = _try_import_tesseract()
    if result is None:
        return None

    pytesseract, PILImage = result

    try:
        import io
        import pandas as pd

        image = PILImage.open(io.BytesIO(img_bytes))
        tsv = pytesseract.image_to_data(
            image,
            output_type=pytesseract.Output.DATAFRAME,
            config=f"--dpi {dpi}",
        )

        blocks: list[dict] = []
        # Group words by block_num + par_num + line_num
        tsv = tsv[tsv["conf"] > 30].dropna(subset=["text"])
        tsv["text"] = tsv["text"].astype(str).str.strip()
        tsv = tsv[tsv["text"] != ""]

        group_cols = ["block_num", "par_num", "line_num"]
        for _, grp in tsv.groupby(group_cols):
            text = " ".join(grp["text"].tolist())
            if not text.strip():
                continue
            px0 = grp["left"].min()
            py0 = grp["top"].min()
            px1 = (grp["left"] + grp["width"]).max()
            py1 = (grp["top"] + grp["height"]).max()

            x0, y0, x1, y1 = _scale_bbox((px0, py0, px1, py1), img_w, img_h, page_rect)

            blocks.append({
                "type": "text",
                "content": text,
                "page": page_num,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "font_size": max(8.0, (y1 - y0) * 0.75),
                "bold": False,
                "italic": False,
                "source": "ocr_tesseract",
            })

        logger.info("Page %d: Tesseract extracted %d blocks.", page_num, len(blocks))
        return blocks

    except Exception as exc:
        logger.warning("Tesseract failed on page %d: %s", page_num, exc)
        return None
