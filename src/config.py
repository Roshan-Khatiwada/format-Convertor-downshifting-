"""
Configuration module for PDF-to-JSON extraction system.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Paths
    input_dir: Path = Path(os.getenv("INPUT_DIR", "input"))
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "output"))
    figures_dir: Path = field(init=False)

    # OCR
    ocr_enabled: bool = os.getenv("OCR_ENABLED", "true").lower() == "true"
    ocr_language: str = os.getenv("OCR_LANGUAGE", "en")
    ocr_dpi: int = int(os.getenv("OCR_DPI", "300"))

    # Table extraction
    table_strategy: str = os.getenv("TABLE_STRATEGY", "auto")  # auto | pdfplumber | camelot | ocr

    # Figure extraction
    min_image_size: int = int(os.getenv("MIN_IMAGE_SIZE", "1000"))  # bytes
    min_image_width: int = int(os.getenv("MIN_IMAGE_WIDTH", "50"))  # pixels
    min_image_height: int = int(os.getenv("MIN_IMAGE_HEIGHT", "50"))  # pixels

    # Section detection
    header_font_size_threshold: float = float(os.getenv("HEADER_FONT_SIZE_THRESHOLD", "11.0"))

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "extraction.log")

    # Processing
    max_workers: int = int(os.getenv("MAX_WORKERS", "4"))

    def __post_init__(self) -> None:
        self.figures_dir = self.output_dir / "figures"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)


# Global config singleton
config = Config()
