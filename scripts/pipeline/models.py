"""
models.py — Data models for the PDF accessibility pipeline.

TextBlock   : one positioned text fragment extracted from the PDF
StructElement: one node in the semantic tag tree (H1, P, Table, etc.)
ValidationResult: scoring output from the validator
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class TextBlock:
    """
    One contiguous text region extracted from a PDF page.

    Coordinates are top-down from the page top (y=0 at top, increases down).
    This matches reading order intuition: smaller y → higher on page.
    """
    text: str
    x: float            # left edge (points)
    y: float            # distance from page top (points)
    width: float
    height: float
    font_size: float
    is_bold: bool
    page_num: int       # 1-based
    font_name: str = ""

    @property
    def x_right(self) -> float:
        return self.x + self.width

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def y_bottom(self) -> float:
        return self.y + self.height

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.x_right, self.y_bottom)


# ---------------------------------------------------------------------------
# Semantic structure tree
# ---------------------------------------------------------------------------

# Valid PDF/UA-1 structure types we use
HEADING_TYPES  = {"H1", "H2", "H3", "H4", "H5", "H6"}
BLOCK_TYPES    = {"P", "BlockQuote", "Caption", "Note"}
LIST_TYPES     = {"L", "LI", "LBody", "LLabel"}
TABLE_TYPES    = {"Table", "TR", "TH", "TD", "THead", "TBody", "TFoot"}
INLINE_TYPES   = {"Span", "Link", "Reference"}
GROUP_TYPES    = {"Document", "Part", "Sect", "Div", "Art", "Figure", "Formula",
                  "TOC", "TOCI", "Index"}
ALL_STRUCT     = HEADING_TYPES | BLOCK_TYPES | LIST_TYPES | TABLE_TYPES | INLINE_TYPES | GROUP_TYPES


@dataclass
class StructElement:
    """
    One node in the PDF/UA tag tree.

    elem_type  : PDF structure type (H1, P, Table, TR, TH, TD, L, LI, LBody, Figure …)
    text       : display text (used for ActualText / Alt in pikepdf)
    children   : child StructElements (empty for leaf nodes)
    attrs      : additional PDF attributes, e.g. {"Scope": "Col"} for TH
    page_num   : 1-based page number (0 = unknown)
    source_bbox: (x0, y0, x1, y1) top-down coords of the originating TextBlock(s)
    mcid       : MCID integer (set by tag_builder when wiring to content stream)
    """
    elem_type: str
    text: str = ""
    children: List[StructElement] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    page_num: int = 0
    source_bbox: Optional[Tuple[float, float, float, float]] = None
    mcid: Optional[int] = None

    def add(self, child: StructElement) -> StructElement:
        self.children.append(child)
        return child

    # convenience constructors
    @staticmethod
    def heading(level: int, text: str, page_num: int = 0,
                bbox=None) -> StructElement:
        return StructElement(f"H{level}", text=text, page_num=page_num,
                             source_bbox=bbox)

    @staticmethod
    def paragraph(text: str, page_num: int = 0, bbox=None) -> StructElement:
        return StructElement("P", text=text, page_num=page_num, source_bbox=bbox)

    @staticmethod
    def figure(alt: str, page_num: int = 0, bbox=None) -> StructElement:
        return StructElement("Figure", text=alt, page_num=page_num, source_bbox=bbox)


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    score: int                             # 0–100
    status: str                            # compliant | needs_review | non_compliant
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    components: dict = field(default_factory=dict)  # criterion → points earned

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "status": self.status,
            "errors": self.errors,
            "warnings": self.warnings,
            "components": self.components,
        }
