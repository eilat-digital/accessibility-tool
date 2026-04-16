"""
classifier.py — Heuristic document-type classification for the accessibility pipeline.

Classifies a PDF into one of several canonical types using text-block signals.
The document type is used downstream to:
  1. Select the specialized detection pipeline (StructureDetector.detect)
  2. Apply type-specific validation rules (type_specific_warnings)

Scoring: each type accumulates points from keyword hits and structural signals.
Highest score wins. Score < 2 → GENERAL fallback.

Detection order (rough priority by signal strength):
  SCANNED    — too few text blocks relative to page count
  PROTOCOL   — meeting minutes (פרוטוקול, נוכחים, החלטה, על סדר היום)
  LEGAL      — regulation / bylaw (תקנות, חוק, סעיף, אגרה + numbered clauses)
  WORKPLAN   — budget / work plan / report (תקציב, יעד, ביצוע + table density)
  FORM       — municipal form (חתימה, טופס, מספר זהות)
  NEWSLETTER — newsletter / brochure (high font-size variance across page)
  GENERAL    — fallback for anything else
"""
from __future__ import annotations

import re
from collections import defaultdict
from enum import Enum
from typing import Dict, List, Optional

from .models import TextBlock


# ---------------------------------------------------------------------------
# Document type enum
# ---------------------------------------------------------------------------

class DocumentType(str, Enum):
    PROTOCOL   = "protocol"    # פרוטוקול — meeting minutes
    LEGAL      = "legal"       # חוק/תקנות — law, regulation, municipal bylaw
    WORKPLAN   = "workplan"    # תוכנית עבודה/תקציב/דו"ח — work plan, budget, report
    NEWSLETTER = "newsletter"  # ניוזלטר/עלון — newsletter, brochure
    FORM       = "form"        # טופס — municipal form
    SCANNED    = "scanned"     # scanned / image-only (handled by OCR path)
    GENERAL    = "general"     # fallback


# Friendly display names (Hebrew) for log output
DOC_TYPE_LABELS: Dict[str, str] = {
    DocumentType.PROTOCOL:   "פרוטוקול",
    DocumentType.LEGAL:      "חוק / תקנות",
    DocumentType.WORKPLAN:   "תוכנית עבודה / דו\"ח / תקציב",
    DocumentType.NEWSLETTER: "ניוזלטר / עלון",
    DocumentType.FORM:       "טופס",
    DocumentType.SCANNED:    "סרוק / ארכיון",
    DocumentType.GENERAL:    "כללי",
}


# ---------------------------------------------------------------------------
# Keyword sets (Hebrew)
# ---------------------------------------------------------------------------

_PROTOCOL_KW = frozenset({
    "פרוטוקול", "נוכחים", "חסרים", "משתתפים",
    "החלטה", "הצבעה", "בעד", "נגד", "נמנע",
    "ועדה", "ישיבה", "יו\"ר",
})

_LEGAL_KW = frozenset({
    "תקנות", "חוק", "סעיף", "פרק", "אגרה",
    "תוספת", "הגדרות", "ביטול", "תחילה",
    "תקנה", "צו", "קנס", "פרסום", "חייב",
    "רשות", "עירייה",
})

_WORKPLAN_KW = frozenset({
    "תקציב", "תוכנית", "דו\"ח", "רבעון",
    "יעד", "מדד", "ביצוע", "תחזית",
    "אחראי", "משימה", "פרויקט",
})

_FORM_KW = frozenset({
    "שם", "תאריך", "זהות", "חתימה", "כתובת",
    "טלפון", "בקשה", "טופס", "פקס",
})

# Numbered-clause pattern: "1." / "1.1." / "1.1.1."
_CLAUSE_RE = re.compile(r'^\d+(?:\.\d+)*\.\s+\S', re.UNICODE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _word_tokens(blocks: List[TextBlock]) -> set:
    """Flat set of unique stripped words across all blocks."""
    tokens: set = set()
    for b in blocks:
        for w in b.text.split():
            tokens.add(w.strip('.,;:()"\'[]').lower())
    return tokens


def _first_page_text(blocks: List[TextBlock], n: int = 800) -> str:
    """Concatenated text of the first page blocks (up to n chars), lower-cased."""
    if not blocks:
        return ""
    min_pg = min(b.page_num for b in blocks)
    return " ".join(b.text for b in blocks if b.page_num == min_pg)[:n].lower()


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class DocumentClassifier:
    """
    Classify a document from its extracted text blocks into a DocumentType.

    Usage:
        doc_type = DocumentClassifier().classify(blocks)

    The classifier is intentionally stateless — create one per document.
    """

    def classify(
        self,
        blocks: List[TextBlock],
        metadata: Optional[Dict] = None,
    ) -> DocumentType:
        """
        Return the most likely DocumentType for these blocks.

        Parameters
        ----------
        blocks   : text blocks extracted from the PDF (pdfminer output)
        metadata : optional dict with keys "title", "subject", "keywords"
                   (from PDF metadata — used as additional signal)
        """
        if not blocks:
            return DocumentType.SCANNED

        pages = max((b.page_num for b in blocks), default=1)
        # Very few text blocks per page → scanned / image-only PDF
        if len(blocks) < max(4, pages * 1.5):
            return DocumentType.SCANNED

        tokens = _word_tokens(blocks)
        first  = _first_page_text(blocks)

        # Optional: boost from PDF metadata title/subject
        meta_text = ""
        if metadata:
            meta_text = " ".join(
                str(metadata.get(k, "")) for k in ("title", "subject", "keywords")
            ).lower()

        scores: Dict[DocumentType, float] = {
            DocumentType.PROTOCOL:   self._score_protocol(tokens, first, meta_text),
            DocumentType.LEGAL:      self._score_legal(tokens, first, blocks, meta_text),
            DocumentType.WORKPLAN:   self._score_workplan(tokens, first, blocks),
            DocumentType.FORM:       self._score_form(tokens, first),
            DocumentType.NEWSLETTER: self._score_newsletter(blocks),
        }

        best, best_score = max(scores.items(), key=lambda x: x[1])
        if best_score < 2:
            return DocumentType.GENERAL
        return best

    # ------------------------------------------------------------------
    def _score_protocol(self, tokens: set, first: str, meta: str) -> float:
        score = float(len(tokens & _PROTOCOL_KW))
        if "פרוטוקול" in first or "פרוטוקול" in meta:
            score += 4
        if "נוכחים" in first:
            score += 2
        if "על סדר היום" in first:
            score += 2
        if "הצבעה" in first or "החלטה" in first:
            score += 1
        return score

    def _score_legal(self, tokens: set, first: str,
                      blocks: List[TextBlock], meta: str) -> float:
        score = float(len(tokens & _LEGAL_KW))
        if "תקנות" in first or "חוק" in first:
            score += 3
        if "תקנות" in meta or "חוק" in meta:
            score += 2
        if "אגרה" in first or "תוספת" in first:
            score += 2
        # Numbered clause structure is a very strong legal signal
        clause_count = sum(1 for b in blocks if _CLAUSE_RE.match(b.text.strip()))
        if clause_count >= 5:
            score += 4
        elif clause_count >= 2:
            score += 2
        elif clause_count >= 1:
            score += 1
        return score

    def _score_workplan(self, tokens: set, first: str,
                         blocks: List[TextBlock]) -> float:
        score = float(len(tokens & _WORKPLAN_KW))
        # High column spread across multiple pages → report/budget table
        by_page: Dict[int, List[TextBlock]] = defaultdict(list)
        for b in blocks:
            by_page[b.page_num].append(b)
        table_pages = sum(
            1 for blks in by_page.values()
            if len({round(b.x / 60) for b in blks}) >= 3
        )
        if table_pages >= 3:
            score += 3
        elif table_pages >= 1:
            score += 1
        return score

    def _score_form(self, tokens: set, first: str) -> float:
        score = float(len(tokens & _FORM_KW))
        if "טופס" in first:
            score += 3
        if "חתימה" in first:
            score += 2
        return score

    def _score_newsletter(self, blocks: List[TextBlock]) -> float:
        """High font-size variance across the page → newsletter / brochure."""
        sizes = [b.font_size for b in blocks if b.font_size > 0]
        if len(sizes) < 8:
            return 0.0
        ss = sorted(sizes)
        p10 = ss[int(len(ss) * 0.10)]
        p90 = ss[int(len(ss) * 0.90)]
        if p10 > 0 and p90 / p10 > 2.5:
            return 4.0
        return 0.0


# ---------------------------------------------------------------------------
# Type-specific validation warnings
# ---------------------------------------------------------------------------

def type_specific_warnings(elements, doc_type: DocumentType) -> List[str]:
    """
    Return a list of validation warning strings specific to the detected
    document type. These supplement the generic StructValidator output.

    Parameters
    ----------
    elements : List[StructElement] — the detected structure
    doc_type : the classified document type
    """
    from .models import StructElement

    warnings: List[str] = []
    types = {e.elem_type for e in elements}

    def _has(*et):
        return bool(types & set(et))

    if doc_type == DocumentType.PROTOCOL:
        if not _has("H1", "H2", "H3"):
            warnings.append(
                "פרוטוקול: לא זוהו כותרות (H1/H2/H3) — "
                "בדוק ש-'פרוטוקול', 'נוכחים', 'על סדר היום' זוהו ככותרות"
            )
        if not _has("L"):
            warnings.append(
                "פרוטוקול: לא זוהו רשימות (L/LI) — "
                "רשימת משתתפים וסדר היום חייבים להיות מסומנים כרשימה"
            )

    elif doc_type == DocumentType.LEGAL:
        if not _has("H1", "H2", "H3"):
            warnings.append(
                "חוק/תקנות: לא זוהתה היררכיית סעיפים — "
                "סעיפים ממוספרים (1. / 1.1. / 1.1.1.) חייבים להיות H1/H2/H3"
            )
        if not _has("L"):
            warnings.append(
                "חוק/תקנות: לא זוהו רשימות — "
                "הגדרות ורשימות חוקיות חייבות להיות מסומנות כ-L/LI"
            )

    elif doc_type == DocumentType.WORKPLAN:
        if not _has("Table"):
            warnings.append(
                "תוכנית עבודה/תקציב: לא זוהו טבלאות — "
                "טבלאות נתונים חייבות להיות מסומנות כ-Table/TR/TH/TD"
            )

    elif doc_type == DocumentType.NEWSLETTER:
        if not _has("H1", "H2", "H3"):
            warnings.append(
                "ניוזלטר/עלון: לא זוהו כותרות — "
                "כותרות מאמרים חייבות להיות מסומנות כ-H1/H2"
            )

    elif doc_type == DocumentType.FORM:
        warnings.append(
            "טופס: שדות טופס (Form Fields) דורשים תיוג AcroForm — "
            "Pipeline הנוכחי לא מטפל בטפסים אינטראקטיביים"
        )

    return warnings
