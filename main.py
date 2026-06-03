"""
main.py — PDF-to-JSON Extraction System

Entry point for batch processing.

Usage:
    python main.py                    # process all PDFs in input/
    python main.py --input /path      # custom input directory
    python main.py --file doc.pdf     # single file
    python main.py --workers 8        # parallel workers
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from src.config import Config
from src.extractor import PDFExtractor
from src.json_builder import JSONBuilder
from src.utils import setup_logging

logger = logging.getLogger(__name__)


# ── Worker function (must be module-level for multiprocessing) ───────────────

def process_pdf(pdf_path_str: str, config_dict: dict) -> tuple[str, bool, str]:
    """
    Process a single PDF file.
    Returns (pdf_path, success, message).
    Run in a subprocess so PyMuPDF/PaddleOCR don't leak state.
    """
    pdf_path = Path(pdf_path_str)

    # Rebuild config from dict (dataclass can't cross process boundary easily)
    cfg = Config(
        input_dir=Path(config_dict["input_dir"]),
        output_dir=Path(config_dict["output_dir"]),
    )
    cfg.ocr_enabled = config_dict["ocr_enabled"]
    cfg.ocr_language = config_dict["ocr_language"]
    cfg.ocr_dpi = config_dict["ocr_dpi"]
    cfg.table_strategy = config_dict["table_strategy"]
    cfg.min_image_size = config_dict["min_image_size"]
    cfg.min_image_width = config_dict["min_image_width"]
    cfg.min_image_height = config_dict["min_image_height"]

    setup_logging(config_dict["log_level"])

    try:
        extractor = PDFExtractor(cfg)
        builder = JSONBuilder(pdf_path, cfg.output_dir)

        raw_blocks = extractor.extract(pdf_path)
        result = builder.build(raw_blocks)

        output_path = cfg.output_dir / (pdf_path.stem + ".json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        return pdf_path_str, True, f"→ {output_path}"

    except Exception as exc:
        logger.exception("Failed to process %s", pdf_path)
        return pdf_path_str, False, str(exc)


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PDF-to-JSON extraction system for CTD/eCTD and technical documents."
    )
    p.add_argument("--input", "-i", type=Path, help="Input directory (overrides config)")
    p.add_argument("--output", "-o", type=Path, help="Output directory (overrides config)")
    p.add_argument("--file", "-f", type=Path, help="Process a single PDF file")
    p.add_argument("--workers", "-w", type=int, help="Parallel worker count")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    cfg = Config()
    if args.input:
        cfg.input_dir = args.input
    if args.output:
        cfg.output_dir = args.output
        cfg.figures_dir = cfg.output_dir / "figures"
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        cfg.figures_dir.mkdir(parents=True, exist_ok=True)
    if args.workers:
        cfg.max_workers = args.workers

    # Collect PDFs to process
    if args.file:
        if not args.file.is_file():
            logger.error("File not found: %s", args.file)
            return 1
        pdf_files = [args.file]
    else:
        pdf_files = sorted(cfg.input_dir.glob("*.pdf"))
        if not pdf_files:
            logger.warning("No PDF files found in: %s", cfg.input_dir)
            return 0

    logger.info("Found %d PDF file(s) to process.", len(pdf_files))

    # Serialize config for subprocess
    config_dict = {
        "input_dir": str(cfg.input_dir),
        "output_dir": str(cfg.output_dir),
        "ocr_enabled": cfg.ocr_enabled,
        "ocr_language": cfg.ocr_language,
        "ocr_dpi": cfg.ocr_dpi,
        "table_strategy": cfg.table_strategy,
        "min_image_size": cfg.min_image_size,
        "min_image_width": cfg.min_image_width,
        "min_image_height": cfg.min_image_height,
        "log_level": args.log_level,
    }

    success_count = 0
    fail_count = 0

    if cfg.max_workers > 1 and len(pdf_files) > 1:
        with ProcessPoolExecutor(max_workers=cfg.max_workers) as executor:
            futures = {
                executor.submit(process_pdf, str(p), config_dict): p
                for p in pdf_files
            }
            for future in as_completed(futures):
                pdf_path, success, message = future.result()
                _log_result(pdf_path, success, message)
                if success:
                    success_count += 1
                else:
                    fail_count += 1
    else:
        for pdf_path in pdf_files:
            _, success, message = process_pdf(str(pdf_path), config_dict)
            _log_result(str(pdf_path), success, message)
            if success:
                success_count += 1
            else:
                fail_count += 1

    logger.info(
        "Done. %d succeeded, %d failed.",
        success_count,
        fail_count,
    )
    return 0 if fail_count == 0 else 1


def _log_result(pdf_path: str, success: bool, message: str) -> None:
    name = Path(pdf_path).name
    if success:
        logger.info("✓ %-60s %s", name, message)
    else:
        logger.error("✗ %-60s %s", name, message)


if __name__ == "__main__":
    sys.exit(main())
