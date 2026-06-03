"""
Merge extracted PDF JSON into Format/output templates.

Final JSON keeps the exact format structure (headers, prompts, columns).
PDF data is placed in `content` on each data component (text / table / figure).
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

# Format slot section_code -> keywords to find content in parent PDF extract
_SLOT_HINTS: dict[str, list[str]] = {
    "description": ["light yellow", "film coated", "round shaped", "appearance", "description"],
    "dosage_form": ["dosage form", "tablets", "capsule", "film-coated"],
    "reconstitution_diluent": ["reconstitution", "diluent", "not applicable"],
    "container_closure": ["blister", "alu/alu", "packing", "carton", "container", "closure"],
    "diluent_container_closure": ["diluent", "not applicable"],
    "composition_summary": ["each film", "each ", "contains", "excipients", "q.s"],
    "unit_formula": ["ingredients", "dry mix", "unit formula", "qualitative"],
    "formula_table": ["ingredients", "dry mix", "unit formula"],
    "abbreviations": ["where as", "british pharmacopoeia", "bp:", "ih:"],
    "packaging": ["packaging", "blister", "container"],
    "packaging_table": ["packaging", "component", "material"],
    "coa_intro": ["certificate of analysis", "enclosed"],
    "standards_table": ["working standard", "reference standard", "expiry"],
    "finished_product_spec": ["specification", "enclosed"],
    "spec_table": ["test parameters", "sr. no", "specifications"],
    "be_study": ["be study", "bcs class", "not applicable", "bioequivalence"],
    "cdp_study": ["cdp", "enclosed"],
    "proof_intro": ["proof of purchase", "invoice", "enclosed"],
    "proof_figures": ["invoice", "figure"],
}

_SKIP_TEXT = re.compile(
    r"^(confidential!|page\s+\d+|leben life sciences)",
    re.IGNORECASE,
)

_SLOT_NEGATIVE: dict[str, list[str]] = {
    "description": ["blister", "carton", "packing", "alu/alu", "qualitative", "ingredients"],
    "dosage_form": ["blister", "carton", "packing", "alu", "light yellow", "film coated"],
    "container_closure": ["ingredients", "dry mix", "solifenacin succinate\nbp"],
}

# Prefer exact label lines from P1-style dossiers
_SLOT_PREFERRED: dict[str, list[str]] = {
    "dosage_form": [r"dosage form:\s*(\w+)", r"c\.\s*dosage form:\s*(.+)"],
}


def _text_of(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c.strip()
    return ""


def _table_content(block: dict) -> Optional[dict]:
    c = block.get("content")
    if isinstance(c, dict) and "rows" in c:
        return c
    return None


def _column_overlap(table: dict, expected: list[str]) -> float:
    cols = [str(c).lower() for c in table.get("columns", [])]
    if not cols or not expected:
        return 0.0
    exp = [str(c).lower() for c in expected]
    hits = sum(1 for e in exp if any(e in c or c in e for c in cols))
    return hits / max(len(exp), 1)


def _pool_extracted(components: list[dict]) -> dict[str, list[dict]]:
    pool: dict[str, list[dict]] = {"text": [], "table": [], "figure": [], "other": []}
    for blk in components:
        t = blk.get("type") or "other"
        if t in pool:
            pool[t].append(blk)
        else:
            pool["other"].append(blk)
    return pool


def _pop_text(
    pool: dict[str, list[dict]],
    keywords: list[str],
    slot: str = "",
) -> Optional[str]:
    negatives = _SLOT_NEGATIVE.get(slot, [])
    preferred_patterns = _SLOT_PREFERRED.get(slot, [])

    for pattern in preferred_patterns:
        for i, blk in enumerate(pool["text"]):
            text = _text_of(blk)
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        for blk in pool["table"]:
            tbl = _table_content(blk)
            if not tbl:
                continue
            for row in tbl.get("rows", []):
                for val in row.values():
                    m = re.search(pattern, str(val), re.IGNORECASE)
                    if m:
                        return m.group(1).strip()

    best_idx = None
    best_score = 0
    for i, blk in enumerate(pool["text"]):
        text = _text_of(blk)
        if not text or _SKIP_TEXT.match(text.split("\n")[0]):
            continue
        lower = text.lower()
        if any(n in lower for n in negatives):
            continue
        score = sum(1 for kw in keywords if kw in lower)
        if score > best_score:
            best_score = score
            best_idx = i
    if best_idx is not None and best_score > 0:
        return _text_of(pool["text"].pop(best_idx))

    # Second pass: allow negatives only if no better match exists
    for i, blk in enumerate(pool["text"]):
        text = _text_of(blk)
        if not text or _SKIP_TEXT.match(text.split("\n")[0]):
            continue
        lower = text.lower()
        score = sum(1 for kw in keywords if kw in lower)
        if slot == "dosage_form" and "tablet" in lower and "blister" not in lower:
            score += 2
        if score > best_score:
            best_score = score
            best_idx = i
    if best_idx is not None and best_score > 0:
        return _text_of(pool["text"].pop(best_idx))
    return None


def _pop_table(
    pool: dict[str, list[dict]],
    expected_columns: Optional[list[str]] = None,
    keywords: Optional[list[str]] = None,
    *,
    min_column_overlap: float = 0.35,
) -> Optional[dict]:
    best_idx = None
    best_score = -1.0
    for i, blk in enumerate(pool["table"]):
        tbl = _table_content(blk)
        if not tbl:
            continue
        col_overlap = _column_overlap(tbl, expected_columns or []) if expected_columns else 0.0
        if expected_columns and col_overlap < min_column_overlap:
            continue
        score = col_overlap * 3
        if keywords:
            blob = str(tbl).lower()
            score += sum(0.5 for kw in keywords if kw in blob)
        if score > best_score:
            best_score = score
            best_idx = i
    if best_idx is not None and best_score > 0:
        tbl = _table_content(pool["table"].pop(best_idx))
        return copy.deepcopy(tbl) if tbl else None
    return None


def _build_composition_summary_table(pool: dict[str, list[dict]]) -> Optional[dict]:
    """Build Label/Value summary from 'Each ... contains' text lines in P1 PDFs."""
    rows: list[dict] = []
    product_name = ""
    each_line = ""
    api_line = ""
    excipients_line = ""
    to_remove: list[dict] = []

    for blk in pool["text"]:
        text = _text_of(blk)
        if not text:
            continue
        lower = text.lower()
        if "product name" in lower or "solifenacin" in lower and "tablets" in lower:
            product_name = text.split("\n")[-1].strip() if "\n" in text else text
            to_remove.append(blk)
        elif "each" in lower and "contains" in lower:
            each_line = text
            to_remove.append(blk)
        elif "excipients" in lower and "q.s" in lower:
            excipients_line = text
            to_remove.append(blk)
        elif re.search(r"\d+\s*mg", text) and "succinate" in lower:
            api_line = text.replace("\n", " ").strip()
            to_remove.append(blk)

    if not (each_line or api_line):
        return None

    rows.append({"Label": "Product Name", "Value": product_name or "Solifenacin Succinate Tablets BP 5 mg"})
    rows.append({"Label": each_line or "Each Film coated tablet contains:", "Value": ""})
    if api_line:
        rows.append({"Label": "Active ingredient", "Value": api_line})
    rows.append({"Label": "Excipients", "Value": excipients_line or "q.s."})

    for blk in to_remove:
        if blk in pool["text"]:
            pool["text"].remove(blk)

    return {"columns": ["Label", "Value"], "rows": rows}


def _pop_figure(pool: dict[str, list[dict]]) -> Optional[dict]:
    if not pool["figure"]:
        return None
    blk = pool["figure"].pop(0)
    fig: dict[str, Any] = {}
    for key in ("file", "path", "page", "caption", "width", "height"):
        if key in blk:
            fig[key] = blk[key]
    if "content" in blk and isinstance(blk["content"], dict):
        fig.update(blk["content"])
    return fig or None


def _fill_slot(
    fmt_comp: dict,
    pool: dict[str, list[dict]],
    *,
    default_na: Optional[str] = None,
) -> dict:
    """Return format component with `content` filled from PDF extract pool."""
    comp = copy.deepcopy(fmt_comp)
    ctype = comp.get("type")
    slot = (comp.get("section_code") or "").lower()
    hints = _SLOT_HINTS.get(slot, [])
    title_hints = (comp.get("title") or comp.get("section_title") or "").lower().split()
    keywords = [h for h in hints + title_hints if len(h) > 2]

    if ctype == "section_header":
        return comp

    if ctype == "text":
        content = _pop_text(pool, keywords, slot=slot)
        if content is None and slot == "dosage_form":
            for blk in pool["text"]:
                t = _text_of(blk)
                if re.search(r"\btablets\b", t, re.I) and "blister" not in t.lower():
                    content = "Tablets"
                    pool["text"].remove(blk)
                    break
        if content is None and default_na and slot in (
            "reconstitution_diluent",
            "diluent_container_closure",
        ):
            content = default_na
        if content is None:
            content = _pop_any_text(pool)
        if content:
            comp["content"] = content
        return comp

    if ctype == "table":
        cols = comp.get("columns") or []
        if slot == "composition_summary":
            content = _pop_table(
                pool,
                expected_columns=cols,
                keywords=keywords,
                min_column_overlap=0.5,
            )
            if content is None:
                content = _build_composition_summary_table(pool)
        else:
            content = _pop_table(pool, expected_columns=cols, keywords=keywords)
        if content:
            comp["content"] = content
        return comp

    if ctype == "figure":
        if pool["figure"]:
            if slot == "proof_figures" and len(pool["figure"]) > 1:
                comp["content"] = []
                while pool["figure"]:
                    fig = _pop_figure(pool)
                    if fig:
                        comp["content"].append(fig)
            else:
                fig = _pop_figure(pool)
                if fig:
                    comp["content"] = fig
        return comp

    return comp


def _pop_any_text(pool: dict[str, list[dict]]) -> Optional[str]:
    while pool["text"]:
        text = _text_of(pool["text"].pop(0))
        if text and not _SKIP_TEXT.match(text.split("\n")[0]):
            return text
    return None


def merge_content_with_format(
    extracted_content: dict,
    format_template: dict,
) -> dict:
    """
    Build final JSON: exact format template + PDF data in content fields only.
    """
    result = {
        "section_code": format_template.get("section_code"),
        "dossier_type": format_template.get("dossier_type"),
    }
    if "_instruction" in format_template:
        result["_instruction"] = format_template["_instruction"]

    pool = _pool_extracted(extracted_content.get("components") or [])
    filled: list[dict] = []

    for fmt_comp in format_template.get("components") or []:
        default_na = None
        slot = (fmt_comp.get("section_code") or "").lower()
        if slot in ("reconstitution_diluent", "diluent_container_closure"):
            default_na = "Not applicable."
        filled.append(_fill_slot(fmt_comp, pool, default_na=default_na))

    # Remaining extract blocks appended only if format had no matching slot
    leftovers = pool["text"] + pool["table"] + pool["figure"]
    if leftovers and not any(c.get("content") for c in filled if c.get("type") != "section_header"):
        for blk in leftovers:
            entry = {"type": blk.get("type"), "content": blk.get("content")}
            if blk.get("page"):
                entry["page"] = blk["page"]
            filled.append(entry)

    result["components"] = filled
    return result


# Extract keys reserved for specific formats only (no parent fallback)
_EXCLUSIVE_EXTRACT: dict[str, set[str]] = {
    "p7": {"p6.1"},
}


def find_extract_for_format(
    format_section_code: str,
    extracts_by_code: dict[str, dict],
    aliases: Optional[dict[str, str]] = None,
) -> Optional[dict]:
    """
    Resolve which extracted PDF JSON feeds a format template.
    Exact code, alias, then longest parent prefix (p1.1 -> p1).
    """
    code = format_section_code.lower()
    aliases = aliases or {}

    if code in aliases:
        src = aliases[code].lower()
        return extracts_by_code.get(src)

    if code in extracts_by_code:
        return extracts_by_code[code]

    parts = code.split(".")
    for end in range(len(parts) - 1, 0, -1):
        parent = ".".join(parts[:end])
        if parent not in extracts_by_code:
            continue
        allowed = _EXCLUSIVE_EXTRACT.get(parent)
        if allowed is not None and code not in allowed:
            continue
        return extracts_by_code[parent]

    return None
