"""
parser.py — Extract positioned text blocks and graphic lines from a PDF.

Text blocks: pdfminer.six → TextBlock (text, x/y top-down, font_size, is_bold, page_num)
Graphic lines: pdfminer LTLine/LTRect → GraphicLine (used by BorderTableDetector)

Falls back gracefully when pdfminer is not installed: returns [].
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .models import TextBlock

# ---------------------------------------------------------------------------
# pdfminer import (optional)
# ---------------------------------------------------------------------------
try:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import (
        LTPage, LTTextBox, LTTextLine, LTChar,
        LTFigure, LTLayoutContainer, LTLine, LTRect, LTCurve,
    )
    _PDFMINER_OK = True
except ImportError:
    _PDFMINER_OK = False

_BOLD_KEYWORDS = re.compile(r"bold|heavy|black|demi|semibold", re.I)
_CID_PATTERN = re.compile(r"\(cid:\d+\)")
# pdfminer emits U+00FE U+00FF when it reads a UTF-16 BE BOM without a ToUnicode map
_UTF16_BOM_CHARS = "\xfe\xff"


def _cid_ratio(text: str) -> float:
    """Return fraction of text that is CID placeholders or UTF-16 BOM garbage."""
    if not text:
        return 0.0
    cid_chars = sum(len(m.group()) for m in _CID_PATTERN.finditer(text))
    bom_chars = text.count("\xfe") + text.count("\xff")
    return (cid_chars + bom_chars) / max(len(text), 1)


def _clean_cid(text: str) -> str:
    """Remove CID placeholders and UTF-16 BOM markers from text."""
    text = _CID_PATTERN.sub("", text)
    text = text.replace("\xfe", "").replace("\xff", "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# GraphicLine — a horizontal or vertical rule extracted from the PDF
# ---------------------------------------------------------------------------

@dataclass
class GraphicLine:
    """One straight line segment (from LTLine or LTRect border) in a PDF page."""
    x0: float
    y0: float   # top-down (0 = page top)
    x1: float
    y1: float   # top-down
    page_num: int

    @property
    def is_horizontal(self) -> bool:
        return abs(self.y1 - self.y0) <= 3.0 and (self.x1 - self.x0) >= 10.0

    @property
    def is_vertical(self) -> bool:
        return abs(self.x1 - self.x0) <= 3.0 and (self.y1 - self.y0) >= 10.0

    @property
    def length(self) -> float:
        import math
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)


def _is_bold_font(font_name: str) -> bool:
    return bool(_BOLD_KEYWORDS.search(font_name))


_CID_SKIP_RATIO = 0.4  # skip block if >40% is CID garbage


def _line_to_block(line: LTTextLine, page_height: float, page_num: int) -> Optional[TextBlock]:
    raw = line.get_text().strip()
    if not raw:
        return None
    if _cid_ratio(raw) > _CID_SKIP_RATIO:
        return None
    text = _clean_cid(raw)
    if not text:
        return None

    font_sizes: List[float] = []
    bold_chars = 0
    total_chars = 0
    dominant_font = ""

    for char in line:
        if not isinstance(char, LTChar):
            continue
        font_sizes.append(char.size)
        total_chars += 1
        if not dominant_font:
            dominant_font = char.fontname
        if _is_bold_font(char.fontname):
            bold_chars += 1

    if not font_sizes:
        return None

    x0, y0, x1, y1 = line.bbox
    return TextBlock(
        text=text,
        x=x0,
        y=page_height - y1,
        width=x1 - x0,
        height=y1 - y0,
        font_size=sum(font_sizes) / len(font_sizes),
        is_bold=total_chars > 0 and (bold_chars / total_chars) >= 0.45,
        page_num=page_num,
        font_name=dominant_font,
    )


def sort_layout_blocks(blocks: List[TextBlock]) -> List[TextBlock]:
    return sorted(blocks, key=lambda b: (b.page_num, b.y, -b.x))


def extract_blocks(pdf_path: str) -> List[TextBlock]:
    """
    Return a list of TextBlock objects extracted from *pdf_path*.

    Blocks are sorted by page, then top-down, then right-to-left
    (consistent with Hebrew RTL reading order within a line).

    Returns [] if pdfminer is unavailable or the file has no text.
    """
    if not _PDFMINER_OK:
        return []

    blocks: List[TextBlock] = []

    try:
        for page_num, page_layout in enumerate(extract_pages(pdf_path), 1):
            page_height: float = page_layout.height

            for element in page_layout:
                if not isinstance(element, LTTextBox):
                    continue

                for line in element:
                    if not isinstance(line, LTTextLine):
                        continue
                    block = _line_to_block(line, page_height, page_num)
                    if block:
                        blocks.append(block)
    except Exception:
        # Corrupted PDF or missing pdfminer feature — return whatever we got
        pass

    return sort_layout_blocks(blocks)


def has_text(pdf_path: str, min_chars: int = 50) -> bool:
    """
    Quick check: does this PDF contain selectable Unicode text?
    Returns False for PDFs where most text is CID-encoded (no ToUnicode map),
    so they fall through to the OCR path.
    """
    if not _PDFMINER_OK:
        return False
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(pdf_path) or ""
        text = text.strip()
        if len(text) < min_chars:
            return False
        # If most of the extracted text is CID/BOM garbage, treat as scanned
        if _cid_ratio(text) > 0.3:
            return False
        cleaned = _clean_cid(text)
        return len(cleaned) >= min_chars
    except Exception:
        return False


def extract_lines(pdf_path: str) -> List[GraphicLine]:
    """
    Extract horizontal and vertical graphic lines from a PDF.

    Sources: LTLine (explicit lines) and LTRect (rectangles — each border
    becomes four GraphicLine objects). These are used by BorderTableDetector
    to find table cell boundaries in born-digital PDFs.

    Returns [] if pdfminer unavailable or PDF has no graphic content.
    """
    if not _PDFMINER_OK:
        return []

    lines: List[GraphicLine] = []

    def _add(x0, y0_pdf, x1, y1_pdf, page_h, page_num):
        """Convert PDF bottom-left coords to top-down and append."""
        # Normalise so x0 ≤ x1 and y0 ≤ y1 (top-down)
        lx0 = min(x0, x1)
        lx1 = max(x0, x1)
        ly0 = min(page_h - y1_pdf, page_h - y0_pdf)
        ly1 = max(page_h - y1_pdf, page_h - y0_pdf)
        gl = GraphicLine(x0=lx0, y0=ly0, x1=lx1, y1=ly1, page_num=page_num)
        if gl.is_horizontal or gl.is_vertical:
            lines.append(gl)

    def _walk(element, page_h, page_num):
        """Recursively visit layout elements for lines and rects."""
        if isinstance(element, LTLine):
            x0, y0, x1, y1 = element.bbox
            _add(x0, y0, x1, y1, page_h, page_num)
        elif isinstance(element, LTRect):
            x0, y0, x1, y1 = element.bbox
            # Four border edges of the rectangle
            _add(x0, y0, x1, y0, page_h, page_num)   # bottom
            _add(x0, y1, x1, y1, page_h, page_num)   # top
            _add(x0, y0, x0, y1, page_h, page_num)   # left
            _add(x1, y0, x1, y1, page_h, page_num)   # right
        elif isinstance(element, LTLayoutContainer):
            for child in element:
                _walk(child, page_h, page_num)

    try:
        for page_num, page_layout in enumerate(extract_pages(pdf_path), 1):
            ph = page_layout.height
            for element in page_layout:
                _walk(element, ph, page_num)
    except Exception:
        pass

    return lines
