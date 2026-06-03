"""
Section and heading detection for PDF documents.

Uses font size, weight, positioning, and text patterns to classify
text spans as section headers, subsections, captions, or body text.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Heading number patterns
# -------------------------------------------------------------------------
_NUMBERED_HEADING = re.compile(
    r"""
    ^
    (?:
        \d+\.\d+(?:\.\d+)*          # 1.2, 1.2.3, 1.2.3.4
        | [A-Z]\s*\d+(?:\.\d+)*     # P 5.3, S 1.1
        | 3\.2\.[SPAQRM](?:\.\d+)*  # 3.2.S, 3.2.P.3
        | [IVXLCDM]+\.              # Roman numerals (I. II.)
        | \d+\.                     # Simple 1.
    )
    \s+\S
    """,
    re.VERBOSE | re.IGNORECASE,
)

_CAPTION_PATTERN = re.compile(
    r"^(figure|fig\.?|table|tbl\.?|chart|scheme|diagram)\s*[\d\w\-]*[:\.]",
    re.IGNORECASE,
)

_ALL_CAPS_MIN_WORDS = 3  # Minimum words for ALL_CAPS heuristic


@dataclass
class FontProfile:
    """Aggregated font statistics from a page or document."""
    body_size: float = 10.0
    header_sizes: list[float] = field(default_factory=list)

    def is_header_size(self, size: float, threshold_factor: float = 1.15) -> bool:
        return size >= self.body_size * threshold_factor

    def header_level(self, size: float) -> int:
        """
        Return a heading level (1-4) based on relative size.
        Returns 0 for body text.
        """
        if not self.is_header_size(size):
            return 0
        ratio = size / self.body_size
        if ratio >= 2.0:
            return 1
        if ratio >= 1.6:
            return 2
        if ratio >= 1.3:
            return 3
        return 4


def build_font_profile(blocks: list[dict]) -> FontProfile:
    """
    Scan raw layout blocks to build a font size profile for the document.
    Identifies typical body text size as the statistical mode.
    """
    sizes: list[float] = []

    for blk in blocks:
        if blk.get("type") != "text":
            continue
        size = blk.get("font_size")
        if size and size > 4:
            sizes.append(round(size, 1))

    if not sizes:
        return FontProfile()

    # Mode via frequency count
    from collections import Counter

    freq = Counter(sizes)
    body_size = freq.most_common(1)[0][0]

    # Collect unique header sizes (significantly larger than body)
    header_sizes = sorted(
        {s for s in set(sizes) if s >= body_size * 1.15},
        reverse=True,
    )

    profile = FontProfile(body_size=body_size, header_sizes=header_sizes)
    logger.debug("Font profile — body: %.1f, headers: %s", body_size, header_sizes[:5])
    return profile


def classify_block(block: dict, profile: FontProfile) -> str:
    """
    Classify a text block as one of:
      - "section_header"
      - "caption"
      - "text"
      - "list_item"

    Returns the classification string.
    """
    text: str = (block.get("content") or "").strip()
    if not text:
        return "text"

    font_size: float = block.get("font_size", profile.body_size)
    bold: bool = block.get("bold", False)
    italic: bool = block.get("italic", False)

    # ── Caption detection ─────────────────────────────────────────────────
    if _CAPTION_PATTERN.match(text):
        return "caption"

    # ── List item detection ───────────────────────────────────────────────
    if re.match(r"^[\u2022\u2013\u2014\-\*•]\s+", text) or re.match(r"^\d+[\)\.]\s+\S", text):
        return "list_item"

    # ── Exclude document headers/footers/metadata ──────────────────────────
    # Skip very short single-word texts or common labels
    if len(text.split()) <= 1 or re.match(r"^(confidential|draft|page|proprietary|copyright)\b", text, re.IGNORECASE):
        return "text"
    # Skip texts with colons at beginning (often metadata like "Product Name: ...")
    if text.startswith("Product ") or text.startswith("Company "):
        return "text"
    # Skip corporate/company names (often on document cover)
    if re.search(r"(pvt|ltd|inc|corp|llc|ltd\.|pvt\.)\s*\.?\s*$", text, re.IGNORECASE):
        return "text"

    # ── Header detection ──────────────────────────────────────────────────
    level = profile.header_level(font_size)
    is_numbered = bool(_NUMBERED_HEADING.match(text))
    words = text.split()
    is_all_caps = (
        len(words) >= _ALL_CAPS_MIN_WORDS
        and text == text.upper()
        and re.search(r"[A-Z]", text)
    )

    # Section headers must meet stricter criteria:
    # 1. Have a section number pattern AND be at least level 1 or bold, OR
    # 2. Be significantly larger (level >= 2), OR
    # 3. Be all caps with sufficient words
    if is_numbered and (level >= 1 or bold):
        return "section_header"

    if level >= 2:
        return "section_header"

    if is_all_caps and len(words) >= _ALL_CAPS_MIN_WORDS:
        return "section_header"

    # Smaller bold short text can still be a header if numbered
    if bold and font_size >= profile.body_size and len(text) < 80 and not italic and is_numbered:
        return "section_header"

    return "text"


def extract_section_number(text: str) -> Optional[str]:
    """
    Extract a structured section number from header text.
    E.g. "3.2.P.3 Drug Product" -> "3.2.P.3"
    """
    match = re.match(
        r"^((?:\d+\.)+\d*[A-Za-z]?(?:\.\d+)*|[A-Za-z]\s*\d+(?:\.\d+)*|3\.2\.[SPAQRM](?:\.\d+)*)\b",
        text.strip(),
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return None


def extract_section_title(text: str) -> str:
    """
    Strip the section number prefix and return a clean title.
    """
    section_num = extract_section_number(text)
    if section_num:
        title = text[len(section_num):].strip(" :.–-")
        return title if title else text
    return text
