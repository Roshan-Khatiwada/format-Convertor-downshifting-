"""
Utility functions for PDF-to-JSON extraction system.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """Configure logging for the application."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def hash_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """Return hex digest of bytes."""
    h = hashlib.new(algorithm)
    h.update(data)
    return h.hexdigest()


def sanitize_filename(name: str) -> str:
    """
    Convert a string to a safe filename stem.
    Removes non-alphanumeric chars (except dots, underscores, hyphens).
    """
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\s\-.]", "_", name)
    name = re.sub(r"[\s]+", "_", name)
    name = name.strip("._-")
    return name or "document"


def normalize_section_code(raw: str) -> str:
    """
    Normalize section codes to lowercase compact form.

    Examples:
        "P 5.3"  -> "p5.3"
        "P5.3"   -> "p5.3"
        "3.2.S"  -> "3.2.s"
        "3.2.P.3" -> "3.2.p.3"
    """
    normalized = re.sub(r"\s+", "", raw).lower()
    return normalized


# Pattern to detect CTD/eCTD section codes
SECTION_CODE_PATTERN = re.compile(
    r"""
    (?:
        (?:P|S|A|M|Q|R)\s*[\d.]+   # P 5.3, S 1.2, etc.
        |
        3\.2\.[SPAQRM](?:\.\d+)*    # 3.2.S, 3.2.P.3, etc.
        |
        \d+\.\d+(?:\.\d+)*          # 1.2, 1.2.3
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def detect_section_code(text: str) -> Optional[str]:
    """
    Try to find a CTD/eCTD section code in a block of text.
    Returns the normalized code or None.
    """
    match = SECTION_CODE_PATTERN.search(text.strip())
    if match:
        return normalize_section_code(match.group(0))
    return None


def infer_dossier_type(section_code: str) -> str:
    """
    Infer dossier type from section code.
    """
    code = section_code.lower()
    if code.startswith("p") or "3.2.p" in code:
        return "product"
    if code.startswith("s") or "3.2.s" in code:
        return "substance"
    if code.startswith("a") or "3.2.a" in code:
        return "appendix"
    if code.startswith("m"):
        return "module"
    return "unknown"


def build_instruction(section_code: str, dossier_type: str) -> str:
    """
    Build a default LLM instruction string for the JSON envelope.
    """
    return (
        f"Generate {section_code.upper()} content from the SECTION DATA below. "
        f"This is a {dossier_type} dossier section. "
        "Preserve all tables, figures, and structured content. "
        "Do not summarize or omit any data."
    )


def is_caption_text(text: str) -> bool:
    """
    Heuristic: does this text look like a figure/table caption?
    """
    text = text.strip()
    return bool(
        re.match(
            r"^(figure|fig\.?|table|tbl\.?|chart|scheme|diagram)\s*[\d\w\-]*[:\.]",
            text,
            re.IGNORECASE,
        )
    )


def merge_close_text_blocks(blocks: list[dict], y_threshold: float = 5.0) -> list[dict]:
    """
    Merge text blocks that are very close vertically (same visual line).
    Blocks must already be sorted by (page, y0, x0).
    """
    if not blocks:
        return blocks

    merged: list[dict] = []
    current = dict(blocks[0])

    for blk in blocks[1:]:
        same_page = blk.get("page") == current.get("page")
        close_y = abs(blk.get("y0", 0) - current.get("y0", 0)) < y_threshold
        if same_page and close_y and current.get("type") == "text" == blk.get("type"):
            # Merge
            current["content"] = (current.get("content") or "") + " " + (blk.get("content") or "")
        else:
            merged.append(current)
            current = dict(blk)

    merged.append(current)
    return merged


def extension_for_colorspace(colorspace: int, components: int) -> str:
    """Return a file extension appropriate for a PyMuPDF pixmap."""
    if colorspace == 8:  # CMYK
        return "jpg"
    return "png"
