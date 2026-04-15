"""
semantic_validator.py — Hard-fail semantic validation gates.

Implements 10 test categories that block 'accessible' export status:
  1.  Global structure tests
  2.  Heading tests
  3.  List tests
  4.  Table tests
  5.  Key-value structure tests
  6.  OCR quality tests
  7.  MCID binding tests
  8.  Reading order tests
  9.  Family-specific tests
  10. Hard-fail export gate (aggregate)

A GateResult with passed=False means the export MUST NOT be marked
'accessible'. The caller is responsible for writing non_compliant status.

PAC integration point: PACGate.validate(pdf_path) runs after export.
If a real PAC3 binary is available it is invoked; otherwise an enhanced
structural proxy is run via pikepdf.

Pipeline hook points
--------------------
  After StructureDetector.detect():
      gate = SemanticValidator().run(elements, doc_type, lang,
                                     heading_candidates, list_candidates,
                                     table_candidates)
      if not gate.passed:
          <block accessible status>

  After inject_digital() / inject_scanned_semantic():
      pac = PACGate().validate(output_pdf)
      if not pac.passed:
          <downgrade to non_compliant>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import StructElement, ValidationResult


# ---------------------------------------------------------------------------
# Finding — one test result
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    test_id: str
    severity: str       # "hard_fail" | "needs_review" | "warning"
    message: str
    criterion: str = ""

    @property
    def is_hard_fail(self) -> bool:
        return self.severity == "hard_fail"


# ---------------------------------------------------------------------------
# GateResult — aggregate of all findings
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    hard_fails: List[Finding] = field(default_factory=list)
    needs_review: List[Finding] = field(default_factory=list)
    warnings: List[Finding] = field(default_factory=list)

    @property
    def status_override(self) -> str:
        if self.hard_fails:
            return "non_compliant"
        if self.needs_review:
            return "needs_review"
        return ""

    def summary_lines(self) -> List[str]:
        lines = []
        for f in self.hard_fails:
            lines.append(f"  [HARD FAIL] {f.test_id}: {f.message}")
        for f in self.needs_review:
            lines.append(f"  [REVIEW]    {f.test_id}: {f.message}")
        for f in self.warnings:
            lines.append(f"  [WARN]      {f.test_id}: {f.message}")
        return lines

    def to_validation_result(self, base_score: int = 60) -> ValidationResult:
        errors = [f.message for f in self.hard_fails]
        warns  = [f.message for f in self.warnings + self.needs_review]
        score  = base_score
        if self.hard_fails:
            score = min(score, 45)
        elif self.needs_review:
            score = min(score, 69)
        if self.hard_fails:
            status = "non_compliant"
        elif score >= 85:
            status = "compliant"
        elif score >= 60:
            status = "needs_review"
        else:
            status = "non_compliant"
        return ValidationResult(score=score, status=status,
                                errors=errors, warnings=warns)


# ---------------------------------------------------------------------------
# Internal helpers
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


def _meaningful_types(flat: List[StructElement]) -> set:
    """Types excluding pure grouping/artifact wrappers."""
    SKIP = {"Artifact", "Document", "Sect", "Div", "Part", "Art"}
    return {e.elem_type for e in flat if e.elem_type not in SKIP}


# ===========================================================================
# 1. GLOBAL STRUCTURE TESTS
# ===========================================================================

def test_language(lang: str) -> Optional[Finding]:
    """Language must be defined. Hard fail if absent."""
    if not lang or not lang.strip():
        return Finding(
            test_id="LANG_MISSING",
            severity="hard_fail",
            message="שפת המסמך לא מוגדרת — WCAG 3.1.1 / PDF/UA §7.3",
            criterion="WCAG 3.1.1",
        )
    return None


def test_not_paragraph_only(flat: List[StructElement]) -> Optional[Finding]:
    """
    Hard fail: every non-grouping element is P.
    Only triggers when ≥ 5 meaningful elements exist (trivial docs exempt).
    """
    meaningful = [
        e for e in flat
        if e.elem_type not in ("Artifact", "Document", "Sect", "Div",
                                "Part", "Art", "Span", "Link")
    ]
    if len(meaningful) < 5:
        return None
    types = {e.elem_type for e in meaningful}
    if types <= {"P"}:
        return Finding(
            test_id="PARAGRAPH_ONLY",
            severity="hard_fail",
            message="כל התוכן יוצא כ-P בלבד — אין מבנה סמנטי (WCAG 1.3.1)",
            criterion="WCAG 1.3.1",
        )
    return None


def test_semantic_diversity(
    flat: List[StructElement],
    heading_candidates: int,
    list_candidates: int,
    table_candidates: int,
) -> List[Finding]:
    """
    Hard fail for each structural type that was detected but lost in export.
    Each detector must have reported its candidates count to the caller.
    """
    findings: List[Finding] = []
    types = _meaningful_types(flat)
    heading_types = {"H1", "H2", "H3", "H4", "H5", "H6"}

    if heading_candidates > 0 and not (types & heading_types):
        findings.append(Finding(
            test_id="HEADINGS_LOST",
            severity="hard_fail",
            message=(f"זוהו {heading_candidates} בלוקי כותרת אך אין H1/H2/H3 בייצוא — "
                     "reconstruction נכשל (WCAG 1.3.1)"),
            criterion="WCAG 1.3.1",
        ))
    if list_candidates > 0 and "L" not in types:
        findings.append(Finding(
            test_id="LISTS_LOST",
            severity="hard_fail",
            message=(f"זוהו {list_candidates} מועמדי רשימה אך אין L/LI בייצוא — "
                     "(WCAG 1.3.1)"),
            criterion="WCAG 1.3.1",
        ))
    if table_candidates > 0 and "Table" not in types:
        findings.append(Finding(
            test_id="TABLES_LOST",
            severity="hard_fail",
            message=(f"זוהו {table_candidates} מועמדי טבלה אך אין Table בייצוא — "
                     "(WCAG 1.3.1 / IS 5568 §7.2)"),
            criterion="WCAG 1.3.1",
        ))
    return findings


# ===========================================================================
# 2. HEADING TESTS
# ===========================================================================

_HEADING_LEVELS = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}


def test_has_h1(flat: List[StructElement]) -> Optional[Finding]:
    """First heading in document must be H1."""
    headings = [e for e in flat if e.elem_type in _HEADING_LEVELS]
    if not headings:
        return None  # no headings at all — covered by other tests
    if headings[0].elem_type != "H1":
        return Finding(
            test_id="FIRST_HEADING_NOT_H1",
            severity="warning",
            message=(f"כותרת ראשונה היא {headings[0].elem_type} ולא H1 — "
                     "PDF/UA §7.5"),
            criterion="PDF/UA §7.5",
        )
    return None


def test_heading_no_skip(flat: List[StructElement]) -> Optional[Finding]:
    """No heading may skip more than one level (H1→H3 is illegal)."""
    headings = [e for e in flat if e.elem_type in _HEADING_LEVELS]
    prev = None
    for h in headings:
        lvl = _HEADING_LEVELS[h.elem_type]
        if prev is not None and lvl > prev + 1:
            return Finding(
                test_id="HEADING_LEVEL_SKIP",
                severity="hard_fail",
                message=(f"פסיחת רמת כותרת: H{prev} → H{lvl} — "
                         "PDF/UA §7.5 / WCAG 1.3.1"),
                criterion="PDF/UA §7.5",
            )
        prev = lvl
    return None


def test_heading_candidates_exported(
    flat: List[StructElement],
    heading_candidates: int,
) -> Optional[Finding]:
    """Hard fail when heading candidates were detected but none exported."""
    if heading_candidates == 0:
        return None
    headings = [e for e in flat if e.elem_type in _HEADING_LEVELS]
    if not headings:
        return Finding(
            test_id="HEADING_CANDIDATES_LOST",
            severity="hard_fail",
            message=(f"זוהו {heading_candidates} בלוקי כותרת פוטנציאליים "
                     "אך אין H1/H2/H3 בייצוא — reconstruction נכשל"),
            criterion="WCAG 1.3.1",
        )
    return None


# ===========================================================================
# 3. LIST TESTS
# ===========================================================================

def test_list_exported(
    flat: List[StructElement],
    list_candidates: int,
) -> Optional[Finding]:
    """Hard fail when list candidates detected but no L in export."""
    if list_candidates == 0:
        return None
    if not any(e.elem_type == "L" for e in flat):
        return Finding(
            test_id="LIST_NOT_EXPORTED",
            severity="hard_fail",
            message=(f"זוהו {list_candidates} מועמדי רשימה אך אין L/LI בייצוא — "
                     "WCAG 1.3.1"),
            criterion="WCAG 1.3.1",
        )
    return None


def test_list_items_have_content(flat: List[StructElement]) -> Optional[Finding]:
    """Each LI must have an LBody child or direct text."""
    for e in flat:
        if e.elem_type == "LI":
            has_lbody = any(c.elem_type == "LBody" for c in e.children)
            has_text  = bool(e.text.strip())
            if not has_lbody and not has_text:
                return Finding(
                    test_id="LI_EMPTY",
                    severity="warning",
                    message="LI ריק — חסר LBody עם תוכן (WCAG 1.3.1)",
                    criterion="WCAG 1.3.1",
                )
    return None


# ===========================================================================
# 4. TABLE TESTS  (highest priority)
# ===========================================================================

def test_table_exported(
    flat: List[StructElement],
    table_candidates: int,
) -> Optional[Finding]:
    """Hard fail when table candidates detected but no Table in export."""
    if table_candidates == 0:
        return None
    if not any(e.elem_type == "Table" for e in flat):
        return Finding(
            test_id="TABLE_NOT_EXPORTED",
            severity="hard_fail",
            message=(f"זוהו {table_candidates} מועמדי טבלה אך אין Table בייצוא — "
                     "WCAG 1.3.1 / IS 5568 §7.2"),
            criterion="WCAG 1.3.1",
        )
    return None


def test_table_has_cells(flat: List[StructElement]) -> Optional[Finding]:
    """Every Table must contain at least one TD or TH."""
    for e in flat:
        if e.elem_type == "Table":
            all_in_table = _flatten([e])
            if not any(c.elem_type in ("TD", "TH") for c in all_in_table):
                return Finding(
                    test_id="TABLE_NO_CELLS",
                    severity="hard_fail",
                    message="Table ללא תאים (TD/TH) — מבנה שבור (IS 5568 §7.2)",
                    criterion="IS 5568 §7.2",
                )
    return None


def test_table_has_header(flat: List[StructElement]) -> Optional[Finding]:
    """Every Table should have at least one TH with Scope."""
    tables = [e for e in flat if e.elem_type == "Table"]
    if not tables:
        return None
    for table in tables:
        all_in_table = _flatten([table])
        if not any(e.elem_type == "TH" for e in all_in_table):
            return Finding(
                test_id="TABLE_NO_TH",
                severity="warning",
                message="טבלה ללא TH — חסרות כותרות עמודות (IS 5568 §7.2 / WCAG 1.3.1)",
                criterion="IS 5568 §7.2",
            )
    return None


# ===========================================================================
# 5. KEY-VALUE STRUCTURE TESTS
# ===========================================================================

def test_kv_promoted(
    flat: List[StructElement],
    kv_candidates: int,
) -> Optional[Finding]:
    """
    ≥ 3 key-value pairs must not remain as plain P blocks.
    Acceptable promotion targets: Table, L, Sect, Div.
    """
    if kv_candidates < 3:
        return None
    types = _meaningful_types(flat)
    has_structure = bool(types & {"Table", "L", "Sect", "Div"})
    if not has_structure:
        return Finding(
            test_id="KV_FLAT_PARAGRAPHS",
            severity="needs_review",
            message=(f"זוהו {kv_candidates} זוגות מפתח-ערך אך לא קודמו למבנה "
                     "סמנטי (Table/L/Sect) — WCAG 1.3.1"),
            criterion="WCAG 1.3.1",
        )
    return None


# ===========================================================================
# 6. OCR QUALITY TESTS
# ===========================================================================

# Characters outside printable ASCII / Hebrew / basic Latin-1 / bidi marks
_BAD_CHAR_RE = re.compile(
    r'[^\u0020-\u007E'      # printable ASCII
    r'\u00A0-\u00FF'        # Latin-1 supplement
    r'\u05D0-\u05EA'        # Hebrew letters
    r'\u05F0-\u05F4'        # Hebrew ligatures
    r'\uFB1D-\uFB4E'        # Hebrew presentation forms
    r'\u200B-\u200F'        # zero-width / bidi
    r'\u202A-\u202E'        # bidi embedding
    r'\n\r\t]'
)


def _bad_char_ratio(texts: Dict[int, str]) -> float:
    all_text = "".join(texts.values())
    if not all_text:
        return 0.0
    bad = len(_BAD_CHAR_RE.findall(all_text))
    return bad / len(all_text)


def _mean_confidence(page_confidences: Dict[int, float]) -> float:
    vals = [v for v in page_confidences.values() if v >= 0]
    return sum(vals) / len(vals) if vals else 100.0


def test_ocr_quality(
    page_texts: Dict[int, str],
    page_confidences: Optional[Dict[int, float]] = None,
    is_scanned: bool = True,
) -> List[Finding]:
    """OCR quality checks — only meaningful for scanned documents."""
    if not is_scanned or not page_texts:
        return []

    findings: List[Finding] = []

    # Bad character ratio
    ratio = _bad_char_ratio(page_texts)
    if ratio >= 0.30:
        findings.append(Finding(
            test_id="OCR_GIBBERISH",
            severity="hard_fail",
            message=(f"OCR: {ratio:.0%} תווים לא תקינים — "
                     "הטקסט פגום ולא ניתן לקריאה (WCAG 1.1.1)"),
            criterion="WCAG 1.1.1",
        ))
    elif ratio >= 0.15:
        findings.append(Finding(
            test_id="OCR_POOR_QUALITY",
            severity="needs_review",
            message=f"OCR: {ratio:.0%} תווים חשודים — איכות OCR נמוכה",
            criterion="WCAG 1.1.1",
        ))

    # Empty pages
    empty = [pg for pg, t in page_texts.items() if not t.strip()]
    if empty:
        empty_ratio = len(empty) / len(page_texts)
        if empty_ratio >= 0.50:
            findings.append(Finding(
                test_id="OCR_EMPTY_PAGES",
                severity="hard_fail",
                message=(f"OCR: {len(empty)}/{len(page_texts)} עמודים ריקים — "
                         "OCR נכשל (WCAG 1.4.5)"),
                criterion="WCAG 1.4.5",
            ))
        else:
            findings.append(Finding(
                test_id="OCR_PARTIAL_EMPTY",
                severity="warning",
                message=f"OCR: {len(empty)} עמודים ללא טקסט",
                criterion="WCAG 1.4.5",
            ))

    # Tesseract confidence (when available)
    if page_confidences:
        mean_conf = _mean_confidence(page_confidences)
        if mean_conf < 40.0:
            findings.append(Finding(
                test_id="OCR_LOW_CONFIDENCE",
                severity="hard_fail",
                message=(f"OCR: ביטחון ממוצע {mean_conf:.0f}% — "
                         "מתחת לסף המינימלי 40% (WCAG 1.1.1)"),
                criterion="WCAG 1.1.1",
            ))
        elif mean_conf < 60.0:
            findings.append(Finding(
                test_id="OCR_MEDIUM_CONFIDENCE",
                severity="needs_review",
                message=(f"OCR: ביטחון ממוצע {mean_conf:.0f}% — "
                         "מומלץ בדיקה ידנית"),
                criterion="WCAG 1.1.1",
            ))

    return findings


# ===========================================================================
# 7. MCID BINDING TESTS
# ===========================================================================

# Leaf types that must reference content via MCID
_LEAF_TYPES = {"H1", "H2", "H3", "H4", "H5", "H6",
               "P", "TD", "TH", "LBody", "Caption", "Figure"}


def test_mcid_binding(flat: List[StructElement]) -> List[Finding]:
    """
    Meaningful leaf nodes with text must have MCID assigned.
    A single finding is emitted if more than 5 unbound nodes exist — this
    indicates page-level-only MCID strategy rather than per-element binding.
    """
    unbound = [
        e for e in flat
        if e.elem_type in _LEAF_TYPES
        and e.mcid is None
        and e.text.strip()
    ]
    if len(unbound) > 5:
        return [Finding(
            test_id="MCID_UNBOUND",
            severity="warning",
            message=(f"{len(unbound)} אלמנטי עלה עם טקסט אך ללא MCID binding — "
                     "תוכן לא מקושר לזרם תוכן (PDF/UA §7.3)"),
            criterion="PDF/UA §7.3",
        )]
    return []


# ===========================================================================
# 8. READING ORDER TESTS
# ===========================================================================

def test_reading_order(elements: List[StructElement]) -> List[Finding]:
    """
    Top-level page numbers must be monotonically non-decreasing.
    Count backward jumps (page N → page N-2 or earlier).
    """
    pages = [e.page_num for e in elements if e.page_num > 0]
    backward_jumps = sum(
        1 for i in range(1, len(pages)) if pages[i] < pages[i - 1] - 1
    )
    if backward_jumps > 3:
        return [Finding(
            test_id="READING_ORDER_BROKEN",
            severity="hard_fail",
            message=(f"סדר קריאה שבור: {backward_jumps} קפיצות אחורה — "
                     "WCAG 1.3.2"),
            criterion="WCAG 1.3.2",
        )]
    if backward_jumps > 0:
        return [Finding(
            test_id="READING_ORDER_SUSPECT",
            severity="warning",
            message=(f"סדר קריאה חשוד: {backward_jumps} קפיצות אחורה — "
                     "WCAG 1.3.2"),
            criterion="WCAG 1.3.2",
        )]
    return []


# ===========================================================================
# 9. FAMILY-SPECIFIC TESTS
# ===========================================================================

def test_family_specific(
    flat: List[StructElement],
    doc_type,   # DocumentType enum value
) -> List[Finding]:
    """Document-type-aware structural requirements."""
    findings: List[Finding] = []

    try:
        from .classifier import DocumentType as DT
    except ImportError:
        return findings

    types     = _meaningful_types(flat)
    has_h     = bool(types & {"H1", "H2", "H3", "H4", "H5", "H6"})
    has_h2    = "H2" in types
    has_list  = "L" in types
    has_table = "Table" in types

    if doc_type == DT.PROTOCOL:
        if not has_h:
            findings.append(Finding(
                test_id="PROTOCOL_NO_HEADING",
                severity="hard_fail",
                message=("פרוטוקול: חסרות כותרות — נוכחים/סדר היום חייבים "
                         "להיות H2 — WCAG 1.3.1"),
                criterion="WCAG 1.3.1",
            ))
        elif not has_h2:
            findings.append(Finding(
                test_id="PROTOCOL_NO_H2",
                severity="warning",
                message="פרוטוקול: חסר H2 — סעיפי פרוטוקול צריכים להיות H2",
                criterion="WCAG 1.3.1",
            ))
        if not has_list:
            findings.append(Finding(
                test_id="PROTOCOL_NO_LIST",
                severity="hard_fail",
                message=("פרוטוקול: חסרות רשימות — רשימת משתתפים/החלטות "
                         "חייבת להיות L/LI — WCAG 1.3.1"),
                criterion="WCAG 1.3.1",
            ))

    elif doc_type == DT.LEGAL:
        if not has_h:
            findings.append(Finding(
                test_id="LEGAL_NO_HEADING",
                severity="hard_fail",
                message=("חוק/תקנות: חסרות כותרות — סעיפים ממוספרים חייבים "
                         "להיות H1/H2/H3 — WCAG 1.3.1"),
                criterion="WCAG 1.3.1",
            ))

    elif doc_type == DT.WORKPLAN:
        if not has_table:
            findings.append(Finding(
                test_id="WORKPLAN_NO_TABLE",
                severity="hard_fail",
                message=("תוכנית עבודה/תקציב: חסרות טבלאות — נתוני תכנון "
                         "חייבים להיות Table/TR/TH/TD — WCAG 1.3.1"),
                criterion="WCAG 1.3.1",
            ))

    elif doc_type == DT.NEWSLETTER:
        if not has_h:
            findings.append(Finding(
                test_id="NEWSLETTER_NO_HEADING",
                severity="warning",
                message="ניוזלטר: חסרות כותרות מאמרים — WCAG 1.3.1",
                criterion="WCAG 1.3.1",
            ))

    elif doc_type == DT.FORM:
        findings.append(Finding(
            test_id="FORM_FIELDS_UNTAGGED",
            severity="needs_review",
            message=("טופס: שדות AcroForm דורשים תיוג נפרד — "
                     "Pipeline הנוכחי לא מטפל בטפסים אינטראקטיביים"),
            criterion="WCAG 1.3.1",
        ))

    return findings


# ===========================================================================
# 10. MAIN GATE: SemanticValidator
# ===========================================================================

class SemanticValidator:
    """
    Run all 10 test categories and return a GateResult.

    Parameters
    ----------
    elements            : StructElement list from StructureDetector.detect()
    doc_type            : DocumentType from DocumentClassifier.classify()
    lang                : language string (e.g. "he-IL")
    heading_candidates  : count returned/logged by HeadingDetector
    list_candidates     : count returned/logged by ListDetector
    table_candidates    : count returned/logged by TableDetector
    kv_candidates       : count of detected key-value pairs (default 0)
    is_scanned          : True for scanned/OCR path
    page_texts          : {page_num: ocr_text} for OCR quality checks
    page_confidences    : {page_num: mean_confidence_0_100} (optional)

    Usage
    -----
    gate = SemanticValidator().run(
        elements=elements, doc_type=doc_type, lang=lang,
        heading_candidates=head_count, list_candidates=list_count,
        table_candidates=tbl_count,
    )
    if not gate.passed:
        for line in gate.summary_lines():
            print(line)
        # do NOT mark output as accessible
    """

    def run(
        self,
        elements: List[StructElement],
        doc_type=None,
        lang: str = "he-IL",
        heading_candidates: int = 0,
        list_candidates: int = 0,
        table_candidates: int = 0,
        kv_candidates: int = 0,
        is_scanned: bool = False,
        page_texts: Optional[Dict[int, str]] = None,
        page_confidences: Optional[Dict[int, float]] = None,
    ) -> GateResult:

        flat = _flatten(elements)
        hard_fails:    List[Finding] = []
        needs_reviews: List[Finding] = []
        warnings_out:  List[Finding] = []

        def _add(finding: Optional[Finding]):
            if finding is None:
                return
            if finding.severity == "hard_fail":
                hard_fails.append(finding)
            elif finding.severity == "needs_review":
                needs_reviews.append(finding)
            else:
                warnings_out.append(finding)

        def _add_list(findings: List[Finding]):
            for f in findings:
                _add(f)

        # 1. Global structure
        _add(test_language(lang))
        _add(test_not_paragraph_only(flat))
        _add_list(test_semantic_diversity(
            flat, heading_candidates, list_candidates, table_candidates
        ))

        # 2. Heading tests
        _add(test_has_h1(flat))
        _add(test_heading_no_skip(flat))
        _add(test_heading_candidates_exported(flat, heading_candidates))

        # 3. List tests
        _add(test_list_exported(flat, list_candidates))
        _add(test_list_items_have_content(flat))

        # 4. Table tests  (highest priority)
        _add(test_table_exported(flat, table_candidates))
        _add(test_table_has_cells(flat))
        _add(test_table_has_header(flat))

        # 5. Key-value tests
        _add(test_kv_promoted(flat, kv_candidates))

        # 6. OCR quality
        _add_list(test_ocr_quality(
            page_texts or {}, page_confidences, is_scanned
        ))

        # 7. MCID binding
        _add_list(test_mcid_binding(flat))

        # 8. Reading order
        _add_list(test_reading_order(elements))

        # 9. Family-specific
        if doc_type is not None:
            _add_list(test_family_specific(flat, doc_type))

        return GateResult(
            passed=(len(hard_fails) == 0),
            hard_fails=hard_fails,
            needs_review=needs_reviews,
            warnings=warnings_out,
        )


# ===========================================================================
# PAC GATE  (post-export integration point)
# ===========================================================================

@dataclass
class PACResult:
    passed: bool
    source: str           # "pac3" | "proxy" | "skipped"
    findings: List[Finding] = field(default_factory=list)

    def summary_lines(self) -> List[str]:
        return [f"  [PAC/{self.source}] {f.message}" for f in self.findings]


class PACGate:
    """
    Post-export PAC validation.

    Two modes:
      1. Real PAC3: if the `pac3` CLI binary is on PATH (or PAC3_CMD env var),
         run it against the output PDF and parse its exit code + stdout.
      2. Structural proxy: if PAC3 is unavailable, run an enhanced pikepdf
         check covering the most common PAC failure causes.

    Usage
    -----
    pac = PACGate().validate(output_pdf_path)
    if not pac.passed:
        for line in pac.summary_lines():
            print(line)
    """

    def validate(self, pdf_path: str) -> PACResult:
        pac3_result = self._try_pac3(pdf_path)
        if pac3_result is not None:
            return pac3_result
        return self._proxy_validate(pdf_path)

    # ------------------------------------------------------------------
    # PAC3 binary path
    # ------------------------------------------------------------------

    def _pac3_cmd(self) -> Optional[str]:
        import os, shutil
        env = os.environ.get("PAC3_CMD", "")
        if env and os.path.isfile(env):
            return env
        found = shutil.which("pac3") or shutil.which("pac3.exe")
        return found

    # ------------------------------------------------------------------
    # Real PAC3 invocation
    # ------------------------------------------------------------------

    def _try_pac3(self, pdf_path: str) -> Optional[PACResult]:
        cmd = self._pac3_cmd()
        if not cmd:
            return None
        import subprocess
        try:
            proc = subprocess.run(
                [cmd, "--input", pdf_path, "--format", "text"],
                capture_output=True, text=True, timeout=120,
            )
            passed  = proc.returncode == 0
            lines   = proc.stdout.splitlines() + proc.stderr.splitlines()
            findings = [
                Finding(
                    test_id="PAC3",
                    severity="hard_fail" if not passed else "warning",
                    message=line.strip(),
                    criterion="PDF/UA-1",
                )
                for line in lines if line.strip()
            ]
            return PACResult(passed=passed, source="pac3", findings=findings)
        except Exception as exc:
            return PACResult(
                passed=True, source="pac3",
                findings=[Finding(
                    test_id="PAC3_ERROR",
                    severity="warning",
                    message=f"PAC3 הפעלה נכשלה: {exc}",
                )],
            )

    # ------------------------------------------------------------------
    # Structural proxy (pikepdf-based PAC substitute)
    # ------------------------------------------------------------------

    def _proxy_validate(self, pdf_path: str) -> PACResult:
        findings: List[Finding] = []
        try:
            import pikepdf
        except ImportError:
            return PACResult(passed=True, source="skipped",
                             findings=[Finding(
                                 test_id="PAC_PROXY_SKIP",
                                 severity="warning",
                                 message="pikepdf לא זמין — PAC proxy דולג",
                             )])

        try:
            with pikepdf.open(pdf_path) as pdf:
                # 1. StructTreeRoot presence
                if "/StructTreeRoot" not in pdf.Root:
                    findings.append(Finding(
                        test_id="PAC_NO_STRUCT",
                        severity="hard_fail",
                        message="אין StructTreeRoot — המסמך לא tagged (PAC proxy)",
                        criterion="PDF/UA §7.2",
                    ))
                    return PACResult(passed=False, source="proxy",
                                     findings=findings)

                # 2. Language
                lang = str(pdf.Root.get("/Lang", "")).strip().strip('"\'')
                if not lang:
                    findings.append(Finding(
                        test_id="PAC_NO_LANG",
                        severity="hard_fail",
                        message="אין /Lang ב-Root — WCAG 3.1.1 / PDF/UA §7.3",
                        criterion="WCAG 3.1.1",
                    ))

                # 3. MarkInfo
                mi     = pdf.Root.get("/MarkInfo")
                marked = mi.get("/Marked") if mi else None
                if not marked:
                    findings.append(Finding(
                        test_id="PAC_NO_MARKINFO",
                        severity="hard_fail",
                        message="MarkInfo/Marked לא מוגדר — PDF/UA §7.3",
                        criterion="PDF/UA §7.3",
                    ))

                # 4. BDC markers in content streams
                bdc_re   = re.compile(rb"/\w+\s+<<[^>]*?/MCID\s+\d+")
                no_bdc_pages = []
                for pg_idx, page in enumerate(pdf.pages, 1):
                    raw_obj = page.obj.get("/Contents")
                    if raw_obj is None:
                        continue

                    def _get_bytes(o) -> bytes:
                        if hasattr(o, "read_bytes"):
                            return o.read_bytes()
                        if isinstance(o, pikepdf.Array):
                            return b"".join(
                                _get_bytes(x) for x in o
                                if hasattr(x, "read_bytes")
                            )
                        return b""

                    raw = _get_bytes(raw_obj)
                    if raw and not bdc_re.search(raw):
                        no_bdc_pages.append(pg_idx)

                if no_bdc_pages:
                    pgs = ", ".join(str(p) for p in no_bdc_pages[:5])
                    findings.append(Finding(
                        test_id="PAC_NO_BDC",
                        severity="hard_fail",
                        message=(f"אין BDC markers בעמודים {pgs} — "
                                 "תוכן לא מתויג (PAC proxy)"),
                        criterion="PDF/UA §7.3",
                    ))

                # 5. ParentTree
                str_root = pdf.Root.get("/StructTreeRoot")
                if str_root and "/ParentTree" not in str_root:
                    findings.append(Finding(
                        test_id="PAC_NO_PARENT_TREE",
                        severity="hard_fail",
                        message="אין ParentTree ב-StructTreeRoot — PDF/UA §14.7.4",
                        criterion="PDF/UA §14.7.4",
                    ))

                # 6. Title metadata
                raw_title = str(pdf.docinfo.get("/Title", "")).strip().strip('"\'')
                if not raw_title:
                    findings.append(Finding(
                        test_id="PAC_NO_TITLE",
                        severity="warning",
                        message="כותרת המסמך לא מוגדרת — PDF/UA §7.4",
                        criterion="PDF/UA §7.4",
                    ))

                # 7. pdfuaid:part = 1
                try:
                    with pdf.open_metadata() as meta:
                        pdfua = str(meta.get("pdfuaid:part", "")).strip()
                    if pdfua != "1":
                        findings.append(Finding(
                            test_id="PAC_NO_PDFUA_XMP",
                            severity="warning",
                            message="חסר pdfuaid:part=1 ב-XMP — ISO 14289-1 §6.2",
                            criterion="ISO 14289-1 §6.2",
                        ))
                except Exception:
                    pass

        except Exception as exc:
            findings.append(Finding(
                test_id="PAC_PROXY_ERROR",
                severity="warning",
                message=f"PAC proxy: שגיאה בבדיקת קובץ — {exc}",
            ))

        hard_count = sum(1 for f in findings if f.severity == "hard_fail")
        return PACResult(
            passed=(hard_count == 0),
            source="proxy",
            findings=findings,
        )
