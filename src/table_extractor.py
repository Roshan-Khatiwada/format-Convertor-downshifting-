"""
Table extraction module.

Extraction priority:
1. pdfplumber (best for most PDFs, layout-aware)
2. camelot  (lattice mode for bordered tables)
3. OCR fallback (for scanned tables — basic)

All tables are returned as structured JSON dicts, NOT markdown.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Lazy optional imports ───────────────────────────────────────────────────

def _try_import_pdfplumber():
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except ImportError:
        return None


def _try_import_camelot():
    try:
        import camelot  # type: ignore
        return camelot
    except ImportError:
        return None


# ── Data classes ────────────────────────────────────────────────────────────

def _normalize_cell(val: Any) -> str:
    """Convert a cell value to a clean string."""
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val)).strip()


def _table_to_json(raw_rows: list[list[Any]]) -> Optional[dict]:
    """
    Convert a 2-D list of cell values to the JSON table schema.

    {
        "columns": [...],
        "rows": [{col: val, ...}, ...]
    }

    Returns None if the table is empty or has no usable rows.
    """
    if not raw_rows or len(raw_rows) < 1:
        return None

    # Clean all cells
    cleaned = [[_normalize_cell(c) for c in row] for row in raw_rows]

    # Detect header row (first row)
    header = cleaned[0]
    data_rows = cleaned[1:]

    # Skip tables where header is entirely blank
    if not any(h for h in header):
        # Try to use first data row as header
        if data_rows:
            header = data_rows[0]
            data_rows = data_rows[1:]
        else:
            return None

    # Generate unique column names (avoid blanks/duplicates)
    columns: list[str] = []
    seen: dict[str, int] = {}
    for h in header:
        name = h or "column"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)

    # Build row dicts
    rows: list[dict[str, str]] = []
    for raw_row in data_rows:
        # Pad or truncate to match column count
        padded = list(raw_row) + [""] * max(0, len(columns) - len(raw_row))
        row_dict = {col: padded[i] for i, col in enumerate(columns)}
        # Skip entirely-empty rows
        if any(v for v in row_dict.values()):
            rows.append(row_dict)

    if not rows:
        return None

    return {"columns": columns, "rows": rows}


# ── Page-level table extraction ─────────────────────────────────────────────

class TableExtractor:
    """
    Extracts tables from PDF pages and returns structured JSON.

    Each table result:
    {
        "type": "table",
        "page": int,
        "x0": float, "y0": float, "x1": float, "y1": float,
        "content": { "columns": [...], "rows": [...] },
        "section_title": None,   # filled by json_builder
    }
    """

    def __init__(self, pdf_path: Path, strategy: str = "auto") -> None:
        self.pdf_path = pdf_path
        self.strategy = strategy

    def extract_all_tables(self) -> list[dict]:
        """
        Extract all tables from the PDF.
        Returns a flat list of table dicts with page/bbox info.
        """
        if self.strategy in ("pdfplumber", "auto"):
            tables = self._extract_with_pdfplumber()
            if tables is not None:
                return tables

        if self.strategy in ("camelot", "auto"):
            tables = self._extract_with_camelot()
            if tables is not None:
                return tables

        logger.warning("No table extractor succeeded for %s", self.pdf_path.name)
        return []

    # ── pdfplumber ──────────────────────────────────────────────────────────

    def _extract_with_pdfplumber(self) -> Optional[list[dict]]:
        pdfplumber = _try_import_pdfplumber()
        if pdfplumber is None:
            logger.debug("pdfplumber not available.")
            return None

        results: list[dict] = []
        try:
            with pdfplumber.open(str(self.pdf_path)) as pdf:
                for page_obj in pdf.pages:
                    page_num = page_obj.page_number
                    tables = page_obj.extract_tables()
                    if not tables:
                        continue
                    for tbl in tables:
                        content = _table_to_json(tbl)
                        if not content:
                            continue
                        # Get approximate bbox from page
                        # pdfplumber doesn't expose per-table bbox easily;
                        # we use the table's cell bounding boxes
                        bbox = self._pdfplumber_table_bbox(page_obj, tbl)
                        results.append({
                            "type": "table",
                            "page": page_num,
                            "x0": bbox[0], "y0": bbox[1],
                            "x1": bbox[2], "y1": bbox[3],
                            "content": content,
                            "section_title": None,
                        })
        except Exception as exc:
            logger.warning("pdfplumber table extraction error: %s", exc)
            return None

        logger.info("pdfplumber: extracted %d tables from %s", len(results), self.pdf_path.name)
        return results

    @staticmethod
    def _pdfplumber_table_bbox(
        page_obj: Any,
        raw_table: list[list[Any]],
    ) -> tuple[float, float, float, float]:
        """
        Attempt to find the bounding box of a table on a pdfplumber page.
        Falls back to full-page bbox.
        """
        try:
            settings = {"snap_tolerance": 3, "join_tolerance": 3}
            tables = page_obj.find_tables(table_settings=settings)
            if tables:
                t = tables[0]
                return (t.bbox[0], t.bbox[1], t.bbox[2], t.bbox[3])
        except Exception:
            pass
        # Full page fallback
        return (0, 0, float(page_obj.width), float(page_obj.height))

    # ── camelot ────────────────────────────────────────────────────────────

    def _extract_with_camelot(self) -> Optional[list[dict]]:
        camelot = _try_import_camelot()
        if camelot is None:
            logger.debug("camelot not available.")
            return None

        results: list[dict] = []
        try:
            for flavor in ("lattice", "stream"):
                try:
                    tables = camelot.read_pdf(
                        str(self.pdf_path),
                        pages="all",
                        flavor=flavor,
                    )
                except Exception:
                    continue
                for tbl in tables:
                    raw = tbl.df.values.tolist()
                    content = _table_to_json(raw)
                    if not content:
                        continue
                    # camelot uses (x1,y1,x2,y2) in points
                    x1, y1, x2, y2 = tbl._bbox
                    results.append({
                        "type": "table",
                        "page": tbl.page,
                        "x0": x1, "y0": y1, "x1": x2, "y1": y2,
                        "content": content,
                        "section_title": None,
                    })

                if results:
                    break  # use first flavor that works

        except Exception as exc:
            logger.warning("camelot table extraction error: %s", exc)
            return None

        logger.info("camelot: extracted %d tables from %s", len(results), self.pdf_path.name)
        return results
