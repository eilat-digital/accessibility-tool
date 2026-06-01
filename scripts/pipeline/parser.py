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

_HEB_CHAR    = re.compile(r'[א-ת]')
_FINAL_FORMS = set('ךםןףץ')  # ך ם ן ף ץ
# מילים עבריות נפוצות בסדר ויזואלי (הפוך) — כל אחת כאן = אות ויזואלית
_VISUAL_WORDS = {
    'לש','לע','לכ','יכ','מע',
    'מא','מג','קר','נכ','אוה',
    'מה','שי','תא','לב','למ',
    'יתלא','תליא',
    'הפוקת','לועיפ',
    'רואית',
}


def _page_is_visual_hebrew(all_text: str) -> bool:
    """Detect if an entire page has visual-order Hebrew.
    Uses two signals:
    1. Final-form letter not at word end (impossible in logical order)
    2. Common reversed words present"""
    tokens = all_text.split()
    if not tokens:
        return False
    visual = 0
    total_heb = 0
    for tok in tokens:
        heb = [c for c in tok if _HEB_CHAR.match(c)]
        if not heb:
            continue
        total_heb += 1
        # Signal 1: final form not at end
        for c in tok[:-1]:
            if c in _FINAL_FORMS:
                visual += 1
                break
        # Signal 2: recognised visual-order word
        if tok in _VISUAL_WORDS:
            visual += 1
    if total_heb == 0:
        return False
    return visual / total_heb >= 0.15


def _fix_line_visual(text: str) -> str:
    """Reverse chars and word order for one line of visual-order Hebrew."""
    tokens = text.split()
    fixed = [tok[::-1] if _HEB_CHAR.search(tok) else tok for tok in tokens]
    return ' '.join(reversed(fixed))


def _fix_visual_hebrew(text: str) -> str:
    """Apply fix to a single line (called only when page is known visual-order)."""
    if not _HEB_CHAR.search(text):
        return text
    return _fix_line_visual(text)



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

            # ── שלב 1: חלץ כל הטקסט מהעמוד כדי לזהות סדר ויזואלי ──────────
            raw_page_lines: List = []
            for element in page_layout:
                if not isinstance(element, LTTextBox):
                    continue
                for line in element:
                    if isinstance(line, LTTextLine):
                        raw_page_lines.append(line)

            page_raw_text = ' '.join(
                _clean_cid(ln.get_text().strip())
                for ln in raw_page_lines
            )
            is_visual = _page_is_visual_hebrew(page_raw_text)

            # ── שלב 2: בנה בלוקים עם תיקון במידת הצורך ────────────────────
            for line in raw_page_lines:
                block = _line_to_block(line, page_height, page_num)
                if block:
                    if is_visual and _HEB_CHAR.search(block.text):
                        block = block._replace(text=_fix_line_visual(block.text))
                    blocks.append(block)

    except Exception:
        pass

    return sort_layout_blocks(blocks)


def _has_tounicode_gaps(pdf_path: str) -> bool:
    """
    Return True if the PDF contains fonts that lack ToUnicode maps.
    Such fonts cause Acrobat's "Character encoding" check to fail even when
    pdfminer can extract some readable text (partial ToUnicode coverage).
    We detect this by checking whether any font resource lacks /ToUnicode.
    """
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            for page in pdf.pages:
                resources = page.obj.get("/Resources")
                if not resources:
                    continue
                fonts = resources.get("/Font")
                if not fonts:
                    continue
                for _fname, font_ref in fonts.items():
                    try:
                        font = font_ref.get_object() if hasattr(font_ref, "get_object") else font_ref
                        if not isinstance(font, pikepdf.Dictionary):
                            continue
                        subtype = str(font.get("/Subtype", ""))
                        # Only composite (Type0) and Type1/TrueType fonts matter
                        if subtype not in ("/Type1", "/TrueType", "/Type0", "/CIDFontType2", "/CIDFontType0"):
                            continue
                        if "/ToUnicode" not in font:
                            # Type0 fonts: check DescendantFonts
                            if subtype == "/Type0":
                                desc = font.get("/DescendantFonts")
                                if desc:
                                    for df in desc:
                                        df_obj = df.get_object() if hasattr(df, "get_object") else df
                                        if isinstance(df_obj, pikepdf.Dictionary) and "/ToUnicode" not in df_obj:
                                            return True
                            else:
                                return True
                    except Exception:
                        continue
    except Exception:
        pass
    return False


def has_text(pdf_path: str, min_chars: int = 50) -> bool:
    """
    Quick check: does this PDF contain selectable Unicode text?
    Returns False for PDFs where most text is CID-encoded (no ToUnicode map)
    or where fonts lack ToUnicode coverage, so they fall through to the OCR path.
    Acrobat's "Character encoding" check fails on fonts without ToUnicode maps.
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
        if len(cleaned) < min_chars:
            return False
        # If any font lacks a ToUnicode map, route to OCR so Acrobat's
        # "Character encoding" check passes on the output PDF.
        if _has_tounicode_gaps(pdf_path):
            return False
        return True
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
