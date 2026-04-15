"""
validator.py — IS 5568 / PDF/UA-1 compliance validation.

Two validators:

  StructValidator   — validates a List[StructElement] (pre-export, fast)
                      Integrates SemanticValidator hard-fail gates:
                      any hard fail forces score ≤ 45 and status = non_compliant.
  FileValidator     — validates an exported PDF file with pikepdf (post-export)

Both return ValidationResult with score (0-100), status, errors, warnings.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import StructElement, ValidationResult

# ---------------------------------------------------------------------------
# Scoring weights  (must sum to 100)
# ---------------------------------------------------------------------------
_WEIGHTS: Dict[str, int] = {
    "has_struct_tree":   25,   # StructTreeRoot present
    "has_text_layer":    35,   # text layer / ActualText readable
    "has_lang":          20,   # /Lang at Root level (he-IL)
    "has_title":         10,   # /Title in metadata
    "has_pdfua_xmp":      5,   # pdfuaid:part=1 in XMP
    "has_markinfo":       5,   # /MarkInfo/Marked=true
}

# PDF/UA structural sub-checks (do not add to score, but affect errors/warnings)
_STRUCT_CHECKS = {
    "has_headings":         20,   # at least one H1-H6
    "heading_hierarchy":    10,   # no level-skipping
    "lists_tagged":         10,   # list items wrapped in L/LI/LBody
    "tables_tagged":        10,   # tables have TH with Scope
    "reading_order":        10,   # page numbers are monotonic
}


# ---------------------------------------------------------------------------
# StructValidator  (pre-export)
# ---------------------------------------------------------------------------

class StructValidator:
    """
    Validate a list of StructElement objects before the PDF is written.
    Returns a ValidationResult whose score uses the file-validator weights
    (we optimistically assume the export step will set lang/title/XMP/MarkInfo
    correctly if the caller passes the right parameters).
    """

    def validate(
        self,
        elements: List[StructElement],
        lang: str = "he-IL",
        title: str = "",
        # detector candidate counts — required for hard-fail gate
        heading_candidates: int = 0,
        list_candidates: int = 0,
        table_candidates: int = 0,
        kv_candidates: int = 0,
        doc_type=None,
        is_scanned: bool = False,
        page_texts: Optional[Dict[int, str]] = None,
        page_confidences: Optional[Dict[int, float]] = None,
    ) -> ValidationResult:
        errors:   List[str] = []
        warnings: List[str] = []
        components: Dict[str, int] = {}

        flat = _flatten(elements)

        # ── Run hard-fail semantic gate first ────────────────────────────────
        try:
            from .semantic_validator import SemanticValidator
            gate = SemanticValidator().run(
                elements=elements,
                doc_type=doc_type,
                lang=lang,
                heading_candidates=heading_candidates,
                list_candidates=list_candidates,
                table_candidates=table_candidates,
                kv_candidates=kv_candidates,
                is_scanned=is_scanned,
                page_texts=page_texts,
                page_confidences=page_confidences,
            )
            # Hard fails are errors; review/warnings demoted to warnings
            errors.extend(f.message for f in gate.hard_fails)
            warnings.extend(f.message for f in gate.needs_review)
            warnings.extend(f.message for f in gate.warnings)
        except Exception as _gate_err:
            warnings.append(f"SemanticValidator לא זמין: {_gate_err}")
            gate = None

        # ── Baseline structural scoring ──────────────────────────────────────

        # — StructTreeRoot (always true at struct stage) —
        components["has_struct_tree"] = _WEIGHTS["has_struct_tree"]

        # — Text layer: any element has non-empty text? —
        has_text = any(e.text.strip() for e in flat)
        components["has_text_layer"] = _WEIGHTS["has_text_layer"] if has_text else 0
        if not has_text:
            errors.append("אין שכבת טקסט — WCAG 1.4.5 יכשל")

        # — Language —
        lang_ok = lang and lang.lower() in ("he-il", "he", "iw")
        components["has_lang"] = (
            _WEIGHTS["has_lang"] if lang_ok
            else (_WEIGHTS["has_lang"] // 2 if lang else 0)
        )
        if not lang:
            errors.append("שפת המסמך לא מוגדרת — WCAG 3.1.1")
        elif not lang_ok:
            warnings.append(f"שפה מוגדרת אך לא עברית: {lang}")

        # — Title —
        components["has_title"] = _WEIGHTS["has_title"] if title.strip() else 0
        if not title.strip():
            warnings.append("כותרת המסמך לא מוגדרת — PDF/UA §7.4")

        # — XMP / MarkInfo: optimistic (will be set by tag_builder) —
        components["has_pdfua_xmp"] = _WEIGHTS["has_pdfua_xmp"]
        components["has_markinfo"]  = _WEIGHTS["has_markinfo"]

        # — Structural sub-checks (informational — gate already covers hard cases) —
        headings = [e for e in flat if e.elem_type in
                    ("H1","H2","H3","H4","H5","H6")]
        if not headings:
            warnings.append("אין כותרות מתויגות (H1-H3) — WCAG 1.3.1")
        elif not _heading_hierarchy_ok(headings):
            warnings.append("היררכיית כותרות שגויה (פסיחת רמה) — PDF/UA §7.5")

        lists   = [e for e in flat if e.elem_type == "L"]
        lbodies = [e for e in flat if e.elem_type == "LBody"]
        if lists and not lbodies:
            warnings.append("רשימות קיימות אך חסר LBody — WCAG 1.3.1")

        tables = [e for e in flat if e.elem_type == "Table"]
        ths    = [e for e in flat if e.elem_type == "TH"]
        if tables and not ths:
            warnings.append("טבלאות קיימות אך חסרות כותרות עמודות TH — IS 5568 §7.2")

        if not _reading_order_ok(elements):
            warnings.append("סדר קריאה עלול להיות שגוי — WCAG 1.3.2")

        # ── Score computation ────────────────────────────────────────────────
        score = min(100, sum(components.values()))

        # Hard-fail gate overrides: cap score and force non_compliant
        if gate is not None and gate.hard_fails:
            score = min(score, 45)

        status = _score_to_status(score)

        return ValidationResult(
            score=score, status=status,
            errors=errors, warnings=warnings,
            components=components,
        )


# ---------------------------------------------------------------------------
# FileValidator  (post-export, uses pikepdf)
# ---------------------------------------------------------------------------

class FileValidator:
    """
    Validates an actual PDF file.
    Mirrors the logic in app.py:validate_pdf_accessibility() but is
    standalone and returns a ValidationResult.
    """

    def validate(self, pdf_path: str) -> ValidationResult:
        errors:   List[str] = []
        warnings: List[str] = []
        components: Dict[str, int] = {}

        try:
            import pikepdf
            from pdfminer.high_level import extract_text
        except ImportError as e:
            return ValidationResult(
                score=0, status="error",
                errors=[f"חסרות תלויות: {e}"],
            )

        try:
            with pikepdf.open(pdf_path) as pdf:
                total_pages = len(pdf.pages)

                # — Text layer —
                text_score = _check_text_layer(pdf_path, pdf, total_pages)
                components["has_text_layer"] = text_score
                if text_score == 0:
                    errors.append("אין שכבת טקסט — WCAG 1.4.5")
                elif text_score < _WEIGHTS["has_text_layer"]:
                    warnings.append("שכבת טקסט חלקית")

                # — StructTreeRoot —
                if "/StructTreeRoot" in pdf.Root:
                    components["has_struct_tree"] = _WEIGHTS["has_struct_tree"]
                    # Deeper structural checks
                    struct_errs, struct_warns = _check_struct_tree(pdf)
                    errors.extend(struct_errs)
                    warnings.extend(struct_warns)
                else:
                    components["has_struct_tree"] = 0
                    errors.append("אין StructTreeRoot — המסמך אינו tagged — IS 5568 §7.2")

                # — Language —
                root_lang = str(pdf.Root.get("/Lang", "")).strip().strip('"\'')
                if root_lang:
                    components["has_lang"] = _WEIGHTS["has_lang"]
                else:
                    meta_lang = str(pdf.docinfo.get("/Lang", "")).strip().strip('"\'')
                    components["has_lang"] = (
                        _WEIGHTS["has_lang"] // 2 if meta_lang else 0
                    )
                    (errors if not meta_lang else warnings).append(
                        "שפה לא מוגדרת ב-Root — WCAG 3.1.1"
                    )

                # — Title —
                raw_title = str(pdf.docinfo.get("/Title", "")).strip().strip('"\'')
                components["has_title"] = _WEIGHTS["has_title"] if raw_title else 0
                if not raw_title:
                    warnings.append("כותרת המסמך לא מוגדרת — PDF/UA §7.4")

                # — PDF/UA XMP —
                try:
                    with pdf.open_metadata() as meta:
                        pdfua = str(meta.get("pdfuaid:part", "")).strip()
                    components["has_pdfua_xmp"] = (
                        _WEIGHTS["has_pdfua_xmp"] if pdfua == "1" else 0
                    )
                    if pdfua != "1":
                        warnings.append("חסר מזהה PDF/UA-1 ב-XMP — ISO 14289-1 §6.2")
                except Exception:
                    components["has_pdfua_xmp"] = 0

                # — MarkInfo —
                mi = pdf.Root.get("/MarkInfo")
                marked = mi.get("/Marked") if mi else None
                components["has_markinfo"] = (
                    _WEIGHTS["has_markinfo"] if (marked is not None and bool(marked))
                    else 0
                )
                if not marked:
                    warnings.append("MarkInfo/Marked לא מוגדר — PDF/UA §7.3")

        except Exception as exc:
            return ValidationResult(
                score=0, status="error",
                errors=[f"שגיאה בבדיקת הקובץ: {exc}"],
            )

        score  = min(100, sum(components.values()))
        status = _score_to_status(score)
        return ValidationResult(
            score=score, status=status,
            errors=errors, warnings=warnings,
            components=components,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(elements: List[StructElement]) -> List[StructElement]:
    result: List[StructElement] = []

    def walk(e: StructElement):
        result.append(e)
        for child in e.children:
            walk(child)

    for e in elements:
        walk(e)
    return result


def _heading_hierarchy_ok(headings: List[StructElement]) -> bool:
    level_map = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}
    levels = [level_map.get(h.elem_type, 0) for h in headings]
    for i in range(1, len(levels)):
        if levels[i] > levels[i - 1] + 1:
            return False
    return True


def _reading_order_ok(elements: List[StructElement]) -> bool:
    pages = [e.page_num for e in elements if e.page_num > 0]
    for i in range(1, len(pages)):
        if pages[i] < pages[i - 1] - 1:
            return False
    return True


def _score_to_status(score: int) -> str:
    if score >= 85:
        return "compliant"
    if score >= 60:
        return "needs_review"
    return "non_compliant"


def _check_text_layer(pdf_path: str, pdf, total_pages: int) -> int:
    """Return the text-layer score component."""
    try:
        from pdfminer.high_level import extract_text
        sample = list(range(min(3, total_pages)))
        text   = extract_text(pdf_path, page_numbers=sample) or ""
        if len(text.strip()) > 20:
            return 35
        if len(text.strip()) > 5:
            return 15
    except Exception:
        pass

    # Fallback: look for Tj/TJ operators in raw content streams
    import pikepdf
    for page in list(pdf.pages)[:3]:
        try:
            raw_obj = page.obj.get("/Contents")
            if raw_obj is None:
                continue
            if hasattr(raw_obj, "read_bytes"):
                raw = raw_obj.read_bytes()
            elif isinstance(raw_obj, pikepdf.Array):
                raw = b"".join(
                    x.read_bytes() for x in raw_obj
                    if hasattr(x, "read_bytes")
                )
            else:
                raw = b""
            if b"Tj" in raw or b"TJ" in raw:
                return 35
        except Exception:
            pass
    return 0


def _check_struct_tree(pdf) -> tuple:
    """Return (errors, warnings) from a structural tree inspection."""
    import pikepdf
    errors:   List[str] = []
    warnings: List[str] = []

    str_root = pdf.Root.get("/StructTreeRoot")
    if not str_root:
        return errors, warnings

    # Collect all StructElem types
    types_found: List[str] = []

    def walk(obj, depth=0):
        if depth > 50:   # guard against circular refs
            return
        try:
            if isinstance(obj, pikepdf.Dictionary):
                otype = str(obj.get("/Type", "")).lstrip("/")
                if otype == "StructElem":
                    s = str(obj.get("/S", "")).lstrip("/")
                    types_found.append(s)
                k = obj.get("/K")
                if isinstance(k, pikepdf.Array):
                    for child in k:
                        walk(child, depth + 1)
                elif isinstance(k, pikepdf.Dictionary):
                    walk(k, depth + 1)
            elif isinstance(obj, pikepdf.Array):
                for item in obj:
                    walk(item, depth + 1)
        except Exception:
            pass

    doc_k = str_root.get("/K")
    if doc_k is not None:
        walk(doc_k)

    headings = [t for t in types_found if t in ("H1","H2","H3","H4","H5","H6","H")]
    tables   = [t for t in types_found if t == "Table"]
    ths      = [t for t in types_found if t == "TH"]
    lists    = [t for t in types_found if t == "L"]

    if not headings:
        warnings.append("אין כותרות בעץ המבנה — WCAG 1.3.1")
    if tables and not ths:
        warnings.append("טבלאות קיימות בעץ המבנה אך חסרות TH")
    if lists:
        lbodies = [t for t in types_found if t == "LBody"]
        if not lbodies:
            warnings.append("רשימות בעץ המבנה חסרות LBody")

    return errors, warnings
