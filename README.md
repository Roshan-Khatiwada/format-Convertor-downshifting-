# PDF-to-JSON Extraction System

A production-grade system that converts regulatory, pharmaceutical, CTD/eCTD dossier, and technical PDF documents into structured, AI-ready JSON — preserving exact visual reading order, section hierarchy, tables, figures, and captions.

---

## Features

| Capability | Detail |
|---|---|
| **Reading order** | Uses PyMuPDF `get_text("dict")` + coordinate sorting — exact visual order guaranteed |
| **Section detection** | Detects CTD/eCTD codes (`P5.3`, `3.2.S`, `3.2.P.3`), numbered headings, font-size/bold heuristics |
| **Table extraction** | pdfplumber → camelot → OCR fallback; output is structured JSON rows/columns, never markdown |
| **Figure extraction** | SHA-256 de-duplication; every unique image saved once; bbox-aware placement |
| **Caption linking** | Figures immediately adjacent to caption text are auto-linked |
| **OCR fallback** | PaddleOCR (preferred) → pytesseract — scanned pages handled transparently |
| **Batch processing** | Parallel `ProcessPoolExecutor`; progress tracking |
| **Config** | `.env` file or environment variables |

---

## Project Structure

```
pdf_to_json/
│
├── input/                   ← Place your PDF files here
├── output/
│   ├── figures/             ← Extracted images saved here
│   └── *.json               ← One JSON per PDF
│
├── src/
│   ├── __init__.py
│   ├── config.py            ← Configuration dataclass + .env loading
│   ├── extractor.py         ← Core layout-aware extraction engine
│   ├── figure_extractor.py  ← Image extraction + SHA-256 dedup
│   ├── table_extractor.py   ← pdfplumber/camelot table extraction
│   ├── ocr.py               ← PaddleOCR + pytesseract fallback
│   ├── section_detector.py  ← Font-size/pattern heading classification
│   ├── json_builder.py      ← Assembles final JSON from raw blocks
│   └── utils.py             ← Hashing, normalization, caption detection
│
├── main.py                  ← CLI entry point
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Installation

### 1. Clone and create virtual environment

```bash
git clone <repo-url> pdf_to_json
cd pdf_to_json
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
```

### 2. Install core dependencies

```bash
pip install -r requirements.txt
```

### 3. (Optional) Install PaddleOCR for best OCR quality

```bash
# CPU-only
pip install paddlepaddle PaddleOCR

# GPU (CUDA 11.x)
pip install paddlepaddle-gpu PaddleOCR
```

### 4. (Optional) Install Camelot for bordered-table extraction

```bash
pip install "camelot-py[cv]"
```

### 5. (Optional) Tesseract binary (for pytesseract fallback)

```bash
# Ubuntu / Debian
sudo apt-get install tesseract-ocr

# macOS
brew install tesseract

# Windows: download installer from https://github.com/UB-Mannheim/tesseract/wiki
```

### 6. Configure

```bash
cp .env.example .env
# Edit .env as needed
```

---

## Usage

### Process all PDFs in `input/`

```bash
python main.py
```

### Process a single file

```bash
python main.py --file "input/P 5.3 Validation.pdf"
```

### Custom directories

```bash
python main.py --input /data/pdfs --output /data/json
```

### Parallel processing (8 workers)

```bash
python main.py --workers 8
```

### Debug logging

```bash
python main.py --log-level DEBUG
```

---

## Output JSON Format

Each PDF produces a JSON file:

```json
{
  "section_code": "p5.3",
  "dossier_type": "product",
  "_instruction": "Generate P5.3 content from the SECTION DATA below...",
  "components": [
    {
      "type": "section_header",
      "section_title": "Validation of Analytical Procedures",
      "section_number": "P 5.3",
      "sn": 1,
      "content": null
    },
    {
      "type": "text",
      "section_title": "Validation of Analytical Procedures",
      "sn": 2,
      "content": "The following validation studies were conducted..."
    },
    {
      "type": "table",
      "section_title": "Analytical Method Validation Summary",
      "sn": 3,
      "content": {
        "columns": ["Parameter", "Acceptance Criteria", "Result", "Conclusion"],
        "rows": [
          {
            "Parameter": "Specificity",
            "Acceptance Criteria": "No interference",
            "Result": "No interference observed",
            "Conclusion": "Pass"
          }
        ]
      }
    },
    {
      "type": "figure",
      "sn": 4,
      "page": 3,
      "caption": "Figure 1: HPLC Chromatogram of Reference Standard",
      "content": {
        "file": "output/figures/p5.3_p3_1.png"
      }
    },
    {
      "type": "list",
      "sn": 5,
      "content": [
        "Method is specific for the analyte",
        "LOD: 0.05 μg/mL",
        "LOQ: 0.15 μg/mL"
      ]
    }
  ]
}
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `INPUT_DIR` | `input` | Directory containing source PDFs |
| `OUTPUT_DIR` | `output` | Directory for JSON output |
| `OCR_ENABLED` | `true` | Enable OCR for scanned pages |
| `OCR_LANGUAGE` | `en` | OCR language code |
| `OCR_DPI` | `300` | Rendering DPI for OCR |
| `TABLE_STRATEGY` | `auto` | `auto` \| `pdfplumber` \| `camelot` |
| `MIN_IMAGE_SIZE` | `1000` | Skip images smaller than N bytes |
| `MIN_IMAGE_WIDTH` | `50` | Skip images narrower than N pixels |
| `MIN_IMAGE_HEIGHT` | `50` | Skip images shorter than N pixels |
| `MAX_WORKERS` | `4` | Parallel processing workers |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Architecture Notes

### Reading Order Guarantee

The extractor uses `PyMuPDF page.get_text("dict", sort=True)` to get blocks in top-to-bottom, left-to-right order. Figure blocks are inserted at their exact Y-coordinate position, not appended at the end. Table blocks suppress overlapping text blocks to avoid duplication.

### Figure De-duplication

Every image is SHA-256 hashed before saving. If the same image hash has been seen before (e.g. a repeated logo), the existing file path is reused and no duplicate figure entry is created.

### OCR Fallback

Each page is checked with `page.get_text()` — if fewer than 20 characters are found, the page is flagged as scanned and OCR is invoked. PaddleOCR is tried first (better layout), falling back to pytesseract. OCR results are integrated into the same block stream.

### Section Code Normalization

- `"P 5.3"` → `"p5.3"`
- `"3.2.S.4.1"` → `"3.2.s.4.1"`
- `"P2.4 Manufacturing"` → `"p2.4"`

---

## Supported Section Codes

The system recognizes and normalizes:

- `P x.x` — Product sections
- `S x.x` — Substance sections  
- `A x.x` — Appendix sections
- `3.2.P.x.x` — CTD Module 3 product
- `3.2.S.x.x` — CTD Module 3 substance
- `M x.x` — Module references
- Standard numeric (`1.2.3`) headings

---

## License

MIT
