"""
validator.py — IS 5568 / PDF/UA-1 compliance validation.

Two validators:

  StructValidator   — validates a List[StructElement] (pre-export, fast)
  FileValidator     — validates an exported PDF file with pikepdf (post-export)

PAC 2024 integration (optional):
  run_pac_check(pdf_path) runs PAC CLI when PAC_PATH is set in environment.
  Install PAC from https://pac.pdf-accessibility.org and set:
    PAC_PATH=C:/Program Files/PAC 2024/PAC.exe
  in your .env file. Without PAC_PATH the function returns None gracefully.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .models import StructElement, ValidationResult


# ---------------------------------------------------------------------------
# PAC 2024 integration
# ---------------------------------------------------------------------------

_PAC_SEARCH_PATHS = [
    r"C:\Program Files\PAC 2024\PAC.exe",
    r"C:\Program Files (x86)\PAC 2024\PAC.exe",
    r"C:\Program Files\PDF Accessibility Checker\PAC.exe",
    r"C:\Program Files (x86)\PDF Accessibility Checker\PAC.exe",
]


def _find_pac() -> Optional[str]:
    """Return path to PAC.exe or None if not found."""
    explicit = os.environ.get("PAC_PATH", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    for p in _PAC_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def run_pac_check(pdf_path: str) -> Optional[dict]:
    """
    Run PAC 2024 on pdf_path and return a structured result dict, or None
    if PAC is not installed / check fails.

    PAC CLI:  PAC.exe "<pdf>" /report:"<output.xml>"
    Returns:
        {
          "pac_available": True,
          "passed": bool,
          "errors": [str, ...],
          "warnings": [str, ...],
          "raw_xml": str,          # full XML output for archiving
        }
    or None when PAC is not installed.
    """
    pac_exe = _find_pac()
    if not pac_exe:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = os.path.join(tmpdir, "pac_report.xml")
        try:
            result = subprocess.run(
                [pac_exe, pdf_path, f'/report:{report_path}'],
                capture_output=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return {"pac_available": True, "passed": False,
                    "errors": ["PAC הסתיים בחריגת זמן או שגיאה"], "warnings": [], "raw_xml": ""}

        if not os.path.isfile(report_path):
            # Try alternate CLI syntax (older PAC versions)
            try:
                result = subprocess.run(
                    [pac_exe, f'--input={pdf_path}', f'--report={report_path}'],
                    capture_output=True, timeout=120,
                )
            except Exception:
                pass

        if not os.path.isfile(report_path):
            return {"pac_available": True, "passed": False,
                    "errors": [f"PAC לא יצר דוח (קוד יציאה: {result.returncode})"],
                    "warnings": [], "raw_xml": ""}

        try:
            raw_xml = open(report_path, encoding="utf-8", errors="replace").read()
        except Exception:
            raw_xml = ""

        errors: List[str] = []
        warnings: List[str] = []
        passed = True

        try:
            root = ET.fromstring(raw_xml)
            # PAC XML schema: <ResultCollection> → <Result type="Error|Warning" ...>
            for elem in root.iter():
                tag = elem.tag.split("}")[-1].lower()   # strip namespace
                if tag in ("result", "check", "issue"):
                    rtype = (elem.get("type") or elem.get("Type") or "").lower()
                    msg   = (elem.get("description") or elem.get("Description")
                             or elem.text or "").strip()
                    if not msg:
                        continue
                    if "error" in rtype or "fail" in rtype:
                        errors.append(msg)
                        passed = False
                    elif "warn" in rtype:
                        warnings.append(msg)
        except ET.ParseError:
            # If XML is malformed, treat returncode 0 as pass
            passed = (result.returncode == 0)

        return {
            "pac_available": True,
            "passed": passed,
            "errors": errors[:30],     # cap for storage
            "warnings": warnings[:30],
            "raw_xml": raw_xml[:8000],  # cap for DB storage
        }

# ---------------------------------------------------------------------------
# Scoring weights  (must sum to 100)
# ---------------------------------------------------------------------------
_WEIGHTS: Dict[str, int] = {
    "has_text_layer":    30,   # text layer / ActualText readable
    "has_struct_tree":   20,   # StructTreeRoot present
    "has_lang":          15,   # /Lang at Root level (he-IL)
    "heading_quality":   10,   # H1 + hierarchy ok (IS 5568 §7.5 / WCAG 1.3.1)
    "has_title":         10,   # /Title in metadata
    "reading_order":      5,   # logical reading order (WCAG 1.3.2)
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

        # — Heading quality (scored): H1 present + hierarchy ok —
        headings = [e for e in flat if e.elem_type in
                    ("H1","H2","H3","H4","H5","H6")]
        if not headings:
            components["heading_quality"] = 0
            warnings.append("אין כותרות מתויגות (H1-H3) — WCAG 1.3.1")
        elif not any(e.elem_type == "H1" for e in headings):
            components["heading_quality"] = _WEIGHTS["heading_quality"] // 2
            warnings.append("חסר H1 — כותרת ראשית אחת נדרשת — IS 5568 §7.5")
        elif not _heading_hierarchy_ok(headings):
            components["heading_quality"] = _WEIGHTS["heading_quality"] // 2
            warnings.append("היררכיית כותרות שגויה (פסיחת רמה) — PDF/UA §7.5")
        else:
            components["heading_quality"] = _WEIGHTS["heading_quality"]

        # — Reading order (scored) —
        if _reading_order_ok(elements):
            components["reading_order"] = _WEIGHTS["reading_order"]
        else:
            components["reading_order"] = 0
            warnings.append("סדר קריאה עלול להיות שגוי — WCAG 1.3.2")

        # — Lists / Tables: informational only —
        lists   = [e for e in flat if e.elem_type == "L"]
        lbodies = [e for e in flat if e.elem_type == "LBody"]
        if lists and not lbodies:
            warnings.append("רשימות קיימות אך חסר LBody — WCAG 1.3.1")

        tables = [e for e in flat if e.elem_type == "Table"]
        ths    = [e for e in flat if e.elem_type == "TH"]
        if tables and not ths:
            warnings.append("טבלאות קיימות אך חסרות כותרות עמודות TH — IS 5568 §7.2")
        elif ths:
            ths_without_scope = [e for e in ths if not e.attrs.get("Scope")]
            if ths_without_scope:
                warnings.append(
                    f"{len(ths_without_scope)} כותרות TH ללא Scope — PDF/UA §7.5 / WCAG 1.3.1"
                )

        # ── Score computation ────────────────────────────────────────────────
        score = min(100, sum(components.values()))

        # Gate overrides: hard_fail → cap 45, needs_review → cap 69
        if gate is not None and gate.hard_fails:
            score = min(score, 45)
        elif gate is not None and gate.needs_review:
            score = min(score, 69)

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

                # — Heading quality (scored from struct tree) —
                head_types, ro_ok = _check_heading_and_order(pdf)
                if not head_types:
                    components["heading_quality"] = 0
                    warnings.append("אין כותרות בעץ המבנה — WCAG 1.3.1")
                elif "H1" not in head_types:
                    components["heading_quality"] = _WEIGHTS["heading_quality"] // 2
                    warnings.append("חסר H1 — כותרת ראשית אחת נדרשת — IS 5568 §7.5")
                else:
                    components["heading_quality"] = _WEIGHTS["heading_quality"]

                # — Reading order (scored) —
                components["reading_order"] = _WEIGHTS["reading_order"] if ro_ok else 0
                if not ro_ok:
                    warnings.append("סדר קריאה עלול להיות שגוי — WCAG 1.3.2")

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


def _check_heading_and_order(pdf) -> tuple:
    """Return (heading_types_set, reading_order_ok) from struct tree."""
    import pikepdf
    heading_types: set = set()
    page_nums: list = []

    str_root = pdf.Root.get("/StructTreeRoot")
    if not str_root:
        return heading_types, True

    def walk(obj, depth=0):
        if depth > 50:
            return
        try:
            if isinstance(obj, pikepdf.Dictionary):
                otype = str(obj.get("/Type", "")).lstrip("/")
                if otype == "StructElem":
                    s = str(obj.get("/S", "")).lstrip("/")
                    if s in ("H1","H2","H3","H4","H5","H6"):
                        heading_types.add(s)
                    pg = obj.get("/Pg")
                    if pg is not None:
                        try:
                            page_nums.append(int(str(pg.objgen[0])))
                        except Exception:
                            pass
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

    # Reading order: no large backward jumps
    ro_ok = True
    for i in range(1, len(page_nums)):
        if page_nums[i] < page_nums[i - 1] - 2:
            ro_ok = False
            break

    return heading_types, ro_ok


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
        warnings.append("טבלאות קיימות בעץ המבנה אך חסרות TH — IS 5568 §7.2")
    if ths:
        ths_no_scope = _count_th_without_scope(str_root)
        if ths_no_scope > 0:
            warnings.append(
                f"{ths_no_scope} כותרות TH ללא Scope — PDF/UA §7.5 / WCAG 1.3.1"
            )
    if lists:
        lbodies = [t for t in types_found if t == "LBody"]
        if not lbodies:
            warnings.append("רשימות בעץ המבנה חסרות LBody")

    return errors, warnings


def _count_th_without_scope(str_root) -> int:
    """Count TH StructElems that lack a /Scope attribute in their /A dict."""
    import pikepdf
    count = 0

    def walk(obj, depth=0):
        nonlocal count
        if depth > 50:
            return
        try:
            if isinstance(obj, pikepdf.Dictionary):
                if str(obj.get("/S", "")).lstrip("/") == "TH":
                    a = obj.get("/A")
                    has_scope = False
                    if isinstance(a, pikepdf.Dictionary):
                        has_scope = "/Scope" in a
                    elif isinstance(a, pikepdf.Array):
                        has_scope = any(
                            "/Scope" in item
                            for item in a
                            if isinstance(item, pikepdf.Dictionary)
                        )
                    if not has_scope:
                        count += 1
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
    return count
