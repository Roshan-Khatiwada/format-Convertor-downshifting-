"""
Batch processor: mirror Format/output structure in final_output/ and fill
each format.json with data from matching PDFs in input/ (via output/ extracts).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.format_merger import find_extract_for_format, merge_content_with_format
from src.utils import setup_logging

logger = logging.getLogger(__name__)

# PDF section code -> extract key when filename numbering differs from format
FORMAT_SOURCE_ALIASES: Dict[str, str] = {
    "p6.1": "p7",  # P 7 Container Closure PDF -> P6.1 container format
}


@dataclass
class FormatEntry:
    """One format template under Format/output."""

    format_path: Path
    section_code: str
    section_name: str
    parent_dir: str


def _section_key_from_stem(stem: str) -> Optional[str]:
    match = re.match(r"P\s+([\d.]+)\s+", stem)
    if not match:
        return None
    return f"p{match.group(1).replace(' ', '')}".lower()


def list_format_templates(format_dir: Path) -> List[FormatEntry]:
    """All format.json files under Format/output (user-uploaded structure)."""
    entries: List[FormatEntry] = []
    for fmt_file in sorted(format_dir.glob("*/*/format.json")):
        section_dir = fmt_file.parent
        with open(fmt_file, encoding="utf-8") as f:
            data = json.load(f)
        code = (data.get("section_code") or section_dir.name.split()[0]).lower()
        entries.append(
            FormatEntry(
                format_path=fmt_file,
                section_code=code,
                section_name=section_dir.name,
                parent_dir=section_dir.parent.name,
            )
        )
    return entries


def load_extracts_by_code(extracted_dir: Path) -> Dict[str, dict]:
    """Index output/*.json by section code from filename."""
    extracts: Dict[str, dict] = {}
    for json_file in extracted_dir.glob("*.json"):
        code = _section_key_from_stem(json_file.stem)
        if not code:
            logger.warning("Skipping extract without section code: %s", json_file.name)
            continue
        with open(json_file, encoding="utf-8") as f:
            extracts[code] = json.load(f)
        logger.debug("Loaded extract %s <- %s", code, json_file.name)
    return extracts


def save_final_format_output(
    entry: FormatEntry,
    formatted: dict,
    final_output_base: Path,
) -> Path:
    """Mirror Format/output path; write JSON named after format folder."""
    out_dir = final_output_base / entry.parent_dir / entry.section_name
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"{entry.section_name}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(formatted, f, ensure_ascii=False, indent=2)

    # Copy prompt.txt when present (keeps folder identical to Format/output)
    prompt_src = entry.format_path.parent / "prompt.txt"
    if prompt_src.is_file():
        shutil.copy2(prompt_src, out_dir / "prompt.txt")

    return out_file


def copy_figures_to_final(source_dir: Path, target_dir: Path) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    if source_dir.exists():
        for fig_file in source_dir.glob("*"):
            if fig_file.is_file():
                (target_dir / fig_file.name).write_bytes(fig_file.read_bytes())
                count += 1
    return count


def process_all_pdfs() -> None:
    setup_logging("INFO")

    main_dir = Path.cwd()
    format_output_dir = main_dir / "Format" / "output"
    final_output_dir = main_dir / "final_output"
    temp_output_dir = main_dir / "output"

    final_output_dir.mkdir(parents=True, exist_ok=True)
    (final_output_dir / "figures").mkdir(parents=True, exist_ok=True)
    temp_output_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(output_dir=temp_output_dir)
    cfg.figures_dir = final_output_dir / "figures"
    cfg.figures_dir.mkdir(parents=True, exist_ok=True)

    format_entries = list_format_templates(format_output_dir)
    if not format_entries:
        logger.error("No format templates in %s", format_output_dir)
        return

    extracts = load_extracts_by_code(temp_output_dir)
    if not extracts:
        logger.error("No extracted JSON in %s — run: python main.py", temp_output_dir)
        return

    logger.info("Found %d format templates, %d PDF extracts", len(format_entries), len(extracts))

    success = 0
    skipped = 0

    for entry in format_entries:
        extracted = find_extract_for_format(
            entry.section_code,
            extracts,
            aliases=FORMAT_SOURCE_ALIASES,
        )
        if not extracted:
            logger.warning(
                "No PDF data for format %s (%s) — add matching PDF to input/",
                entry.section_code,
                entry.section_name,
            )
            skipped += 1
            continue

        with open(entry.format_path, encoding="utf-8") as f:
            format_template = json.load(f)

        formatted = merge_content_with_format(extracted, format_template)
        out_path = save_final_format_output(entry, formatted, final_output_dir)
        logger.info("Saved %s", out_path.relative_to(main_dir))
        success += 1

    fig_count = copy_figures_to_final(temp_output_dir / "figures", final_output_dir / "figures")

    logger.info("\n%s", "=" * 70)
    logger.info("BATCH PROCESSING COMPLETE")
    logger.info("%s", "=" * 70)
    logger.info("Formats written: %d", success)
    logger.info("Formats skipped (no PDF): %d", skipped)
    logger.info("Figures copied: %d", fig_count)
    logger.info("Output mirrors Format/output: %s", final_output_dir)
    logger.info("%s", "=" * 70)


if __name__ == "__main__":
    process_all_pdfs()
