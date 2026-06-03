"""
JSON builder module.

Takes the flat list of raw blocks from PDFExtractor and produces the
final structured JSON output:

{
    "section_code": "p5.3",
    "dossier_type": "product",
    "_instruction": "...",
    "components": [ ... ]
}

Handles:
- Block classification (header, text, table, figure, list)
- Section hierarchy tracking
- Sequential numbering (sn)
- Skipping absorbed caption blocks
- List item grouping
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from .section_detector import (
    FontProfile,
    build_font_profile,
    classify_block,
    extract_section_number,
    extract_section_title,
)
from .utils import (
    build_instruction,
    detect_section_code,
    infer_dossier_type,
    is_caption_text,
    normalize_section_code,
)

logger = logging.getLogger(__name__)


class JSONBuilder:
    """
    Converts flat raw blocks into the final document JSON.
    """

    def __init__(self, pdf_path: Path, output_dir: Path) -> None:
        self.pdf_path = pdf_path
        self.output_dir = output_dir

    def build(self, raw_blocks: list[dict]) -> dict:
        """
        Main entry point. Builds and returns the final JSON dict.
        """
        # Get font profile (already attached to blocks, reuse first one)
        profile: FontProfile = FontProfile()
        for blk in raw_blocks:
            if "_font_profile" in blk:
                profile = blk["_font_profile"]
                break

        # Detect section code from filename or first few text blocks
        section_code = self._detect_section_code(raw_blocks)
        dossier_type = infer_dossier_type(section_code)
        instruction = build_instruction(section_code, dossier_type)

        components: list[dict] = []
        sn = 1  # Serial number counter
        current_section_title = ""
        pending_list_items: list[str] = []

        for blk in raw_blocks:
            # Skip absorbed captions
            if blk.get("_absorbed"):
                continue

            btype = blk.get("type", "text")

            # ── Figure ────────────────────────────────────────────────────
            if btype == "figure":
                # Flush any pending list
                if pending_list_items:
                    components.append(self._build_list(sn, pending_list_items))
                    sn += 1
                    pending_list_items = []

                component = self._build_figure(blk, sn)
                if component:
                    components.append(component)
                    sn += 1
                continue

            # ── Table ─────────────────────────────────────────────────────
            if btype == "table":
                if pending_list_items:
                    components.append(self._build_list(sn, pending_list_items))
                    sn += 1
                    pending_list_items = []

                component = self._build_table(blk, sn, current_section_title)
                if component:
                    components.append(component)
                    sn += 1
                continue

            # ── Text ──────────────────────────────────────────────────────
            if btype == "text":
                content: str = (blk.get("content") or "").strip()
                if not content:
                    continue

                classification = classify_block(blk, profile)

                if classification == "section_header":
                    if pending_list_items:
                        components.append(self._build_list(sn, pending_list_items))
                        sn += 1
                        pending_list_items = []

                    section_num = extract_section_number(content)
                    section_title = extract_section_title(content)
                    current_section_title = content

                    components.append({
                        "type": "section_header",
                        "section_title": section_title,
                        "section_number": section_num,
                        "sn": sn,
                        "content": None,
                    })
                    sn += 1

                elif classification == "caption":
                    # Standalone caption (not absorbed by a figure) → text
                    if pending_list_items:
                        components.append(self._build_list(sn, pending_list_items))
                        sn += 1
                        pending_list_items = []
                    components.append(self._build_text(content, sn, current_section_title))
                    sn += 1

                elif classification == "list_item":
                    # Strip list marker and accumulate
                    cleaned = re.sub(r"^[\u2022\u2013\u2014\-\*•]\s+", "", content).strip()
                    cleaned = re.sub(r"^\d+[\)\.]\s+", "", cleaned).strip()
                    pending_list_items.append(cleaned)

                else:  # plain text
                    if pending_list_items:
                        components.append(self._build_list(sn, pending_list_items))
                        sn += 1
                        pending_list_items = []
                    components.append(self._build_text(content, sn, current_section_title))
                    sn += 1

        # Flush any remaining list
        if pending_list_items:
            components.append(self._build_list(sn, pending_list_items))

        return {
            "section_code": section_code,
            "dossier_type": dossier_type,
            "_instruction": instruction,
            "components": components,
        }

    # ── Component builders ──────────────────────────────────────────────────

    @staticmethod
    def _build_text(content: str, sn: int, section_title: str) -> dict:
        return {
            "type": "text",
            "section_title": section_title or None,
            "sn": sn,
            "content": content,
        }

    @staticmethod
    def _build_list(sn: int, items: list[str]) -> dict:
        return {
            "type": "list",
            "sn": sn,
            "content": items,
        }

    @staticmethod
    def _build_figure(blk: dict, sn: int) -> Optional[dict]:
        file_path = blk.get("file")
        if not file_path:
            return None
        return {
            "type": "figure",
            "sn": sn,
            "page": blk.get("page"),
            "caption": blk.get("caption"),
            "content": {"file": file_path},
        }

    @staticmethod
    def _build_table(blk: dict, sn: int, section_title: str) -> Optional[dict]:
        content = blk.get("content")
        if not content:
            return None
        return {
            "type": "table",
            "section_title": blk.get("section_title") or section_title or None,
            "sn": sn,
            "content": content,
        }

    # ── Section code detection ──────────────────────────────────────────────

    def _detect_section_code(self, blocks: list[dict]) -> str:
        """
        Try to detect a section code from:
        1. The PDF filename
        2. The first few text blocks (title area)
        """
        # From filename
        code = detect_section_code(self.pdf_path.stem)
        if code:
            logger.debug("Section code from filename: %s", code)
            return code

        # From first 20 text blocks
        checked = 0
        for blk in blocks:
            if blk.get("type") != "text":
                continue
            content = blk.get("content", "")
            code = detect_section_code(content)
            if code:
                logger.debug("Section code from text: %s", code)
                return code
            checked += 1
            if checked >= 20:
                break

        # Fallback: slugify filename
        stem = re.sub(r"[^a-z0-9.]", "", self.pdf_path.stem.lower())
        return stem or "unknown"
