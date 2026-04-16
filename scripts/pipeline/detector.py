"""
detector.py — Rule-based structural element detection from positioned text blocks.

Pipeline order (matters — earlier classifiers claim blocks first):
  1. TableDetector   — column-alignment clustering
  2. HeadingDetector — font-size percentile + bold + spacing heuristics
  3. ListDetector    — regex pattern matching (Hebrew + Latin)
  4. Residual        — everything else becomes a paragraph

Output: an ordered list of StructElement objects ready for tag_builder.

Version: 1.1.0 — semantic OCR reconstruction support added.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .models import StructElement, TextBlock

# DocumentType is imported lazily in detect() to avoid circular imports at module load.
# Use: from .classifier import DocumentType

# ---------------------------------------------------------------------------
# Reading-order sort (used internally and exported for main script)
# ---------------------------------------------------------------------------

_LINE_CLUSTER_PTS = 8.0   # blocks within this vertical distance share a line


def sort_reading_order(blocks: List[TextBlock]) -> List[TextBlock]:
    """
    Sort blocks in Hebrew RTL reading order:
      1. page ascending
      2. y ascending (top of page first)
      3. x descending within a line (right → left)
    """
    by_page: Dict[int, List[TextBlock]] = defaultdict(list)
    for b in blocks:
        by_page[b.page_num].append(b)

    result: List[TextBlock] = []
    for page_num in sorted(by_page.keys()):
        page_blocks = sorted(by_page[page_num], key=lambda b: b.y)

        # Cluster into logical lines
        lines: List[List[TextBlock]] = []
        current: List[TextBlock] = [page_blocks[0]] if page_blocks else []
        for block in page_blocks[1:]:
            if abs(block.y - current[0].y) <= _LINE_CLUSTER_PTS:
                current.append(block)
            else:
                lines.append(current)
                current = [block]
        if current:
            lines.append(current)

        for line in lines:
            line.sort(key=lambda b: -b.x)   # right → left
            result.extend(line)

    return result


# ---------------------------------------------------------------------------
# List pattern matching
# ---------------------------------------------------------------------------

_LIST_PATTERNS: List[re.Pattern] = [
    # Numeric:    "1." "1)" "(1)" "1 -" "1/"
    re.compile(r"^[\(\[]?\d+[\.\)\]\-/]\s", re.UNICODE),
    # Hebrew aleph-bet: "א." "א)" "(א)" "א-"
    re.compile(r"^[\(\[]?[אבגדהוזחטיכלמנסעפצקרשת][\.\)\-\]]\s", re.UNICODE),
    # Latin letters:   "a." "A." "a)" "(a)"
    re.compile(r"^[\(\[]?[a-zA-Z][\.\)\]]\s", re.UNICODE),
    # Roman numerals (i–xii only to avoid false positives)
    re.compile(r"^(?:ix|iv|vi{0,3}|xi|x|i{1,3})\.\s", re.I),
    # Bullet characters (expanded)
    re.compile(r"^[-–—•·◦▪▸►→✓✗◆◇■□●○]\s"),
    # Arabic numerals with various separators
    re.compile(r"^\d+[\.\)\]\-\s]+\S", re.UNICODE),
    # Hebrew numbered lists with geresh
    re.compile(r"^[א-ת]׳\.\s", re.UNICODE),
]

# Definition-list item: "term" – definition  (Hebrew legal docs use geresh/quote)
_DEF_ITEM_RE = re.compile(
    r'^["״\u201c\u201d\u05F4][^\u201d"״\u05F4]{1,50}["״\u201d\u05F4]\s*[-\u2013\u2014]',
    re.UNICODE,
)

# Colon-terminated section header: "נוכחים:" / "על סדר היום:" (≤60 chars, ends with colon)
_SECTION_COLON_RE = re.compile(r'^[^\n]{1,60}[:\uFF1A]\s*$', re.UNICODE)

# Legal numbered-clause hierarchy: "1." "1.1." "1.1.1."
_LEGAL_CLAUSE_RE = re.compile(r'^(\d+(?:\.\d+)*)\.\s+\S', re.UNICODE)

# Enhanced key-value patterns for Hebrew and English documents
_KEY_VALUE_RE = re.compile(
    r'^(.{1,50}?)[\s]*[:\uFF1A][\s]*(.{1,200})$',  # Standard colon
    re.UNICODE,
)
_KEY_VALUE_ALT_RE = re.compile(
    r'^(.{1,50}?)[\s]*[-\u2013\u2014][\s]*(.{1,200})$',  # Dash separator
    re.UNICODE,
)
_KEY_VALUE_PAREN_RE = re.compile(
    r'^(.{1,50}?)[\s]*\([^\)]{1,100}\)$',  # Parenthetical values
    re.UNICODE,
)

_SIGNATURE_RE = re.compile(
    r'(signature|signed|signatory|חתימ|חתום|מאשר|אישור|שם\s+החותם|תפקיד|חתימת|מטעם|בשם|נציג)',
    re.I | re.UNICODE,
)

_IMPLIED_LIST_MIN_ITEMS = 3
_IMPLIED_LIST_MAX_WORDS = 8
_IMPLIED_LIST_MAX_CHARS = 70
_IMPLIED_LIST_X_TOLERANCE = 18.0
_IMPLIED_LIST_GAP_TOLERANCE = 8.0


def _legal_clause_level(text: str) -> Optional[int]:
    """
    Return H-level (1-3) for a numbered legal clause, or None.
    "1. title"       → 1 (H1)
    "1.1. title"     → 2 (H2)
    "1.1.1. title"   → 3 (H3)
    """
    m = _LEGAL_CLAUSE_RE.match(text.strip())
    if not m:
        return None
    dots = m.group(1).count('.')   # "1"→0  "1.1"→1  "1.1.1"→2
    return min(dots + 1, 3)


def _is_list_item(text: str) -> bool:
    s = text.strip()
    return any(p.match(s) for p in _LIST_PATTERNS) or bool(_DEF_ITEM_RE.match(s))


def _is_key_value_text(text: str) -> bool:
    """Enhanced key-value detection with multiple patterns."""
    s = text.strip()
    if not s:
        return False

    # Check all key-value patterns
    patterns = [_KEY_VALUE_RE, _KEY_VALUE_ALT_RE, _KEY_VALUE_PAREN_RE]
    for pattern in patterns:
        m = pattern.match(s)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
            if not label or not value:
                continue
            # More lenient validation
            if len(label.split()) <= 8 and len(value.split()) <= 25:
                return True
    return False


def _split_key_value_text(text: str) -> Optional[Tuple[str, str]]:
    """Enhanced key-value splitting with multiple patterns."""
    s = text.strip()

    # Try patterns in order of preference
    patterns = [_KEY_VALUE_RE, _KEY_VALUE_ALT_RE, _KEY_VALUE_PAREN_RE]
    for pattern in patterns:
        m = pattern.match(s)
        if m:
            label, value = m.group(1).strip(), m.group(2).strip()
            if label and value:
                return label, value
    return None


def _is_signature_text(text: str) -> bool:
    """Enhanced signature detection with multiple heuristics."""
    stripped = text.strip()
    if not stripped:
        return False

    # Direct keyword match
    if _SIGNATURE_RE.search(stripped):
        return True

    # Signature line patterns (underscores, etc.)
    if len(stripped) <= 100 and re.search(r'_{4,}|-{4,}', stripped):
        return True

    # Short lines that might be signature placeholders
    words = len(stripped.split())
    if words <= 3 and len(stripped) <= 50:
        # Check for signature-like patterns
        if re.search(r'(חותם|אישור|חתימה)', stripped, re.UNICODE | re.I):
            return True
        # Lines with just names or titles
        if not re.search(r'[0-9\.\,\;\:\(\)]', stripped):
            return True

    # Position-based: very short lines at bottom of page (handled elsewhere)
    return False


def _is_hebrew_dominant(text: str) -> bool:
    """Return True if the text has at least as many Hebrew chars as Latin chars."""
    hebrew = sum(1 for c in text if '\u05D0' <= c <= '\u05EA')
    latin  = sum(1 for c in text if 'a' <= c.lower() <= 'z')
    return hebrew >= max(latin, 1)


def _strip_list_marker(text: str) -> str:
    s = text.strip()
    # Remove "1." / "א." / "A." style markers
    s = re.sub(r"^[\(\[]?[\daא-תa-zA-Z]+[\.\)\]]\s+", "", s)
    # Remove bullet
    s = re.sub(r"^[-–—•·◦▪▸►→✓✗◆◇■□●○]\s+", "", s)
    return s.strip()


# ---------------------------------------------------------------------------
# HeadingDetector
# ---------------------------------------------------------------------------

class HeadingDetector:
    """
    Classifies a TextBlock as H1/H2/H3 (or None) using:
      - Font size relative to document percentiles (p60, p75, p90)
      - Bold flag
      - Vertical gap above the block vs. average gap on that page
      - Short word count (headings are brief)
      - Enhanced Hebrew heading patterns and spacing heuristics
    """

    _MAX_HEADING_WORDS = 25

    def __init__(self, blocks: List[TextBlock]):
        sizes = [b.font_size for b in blocks if b.font_size > 0]
        n = len(sizes)
        if n < 4:
            # Too few blocks to compute reliable percentiles
            med = statistics.median(sizes) if sizes else 12.0
            self.p60 = self.p75 = self.p90 = med
            self.median = med
        else:
            ss = sorted(sizes)
            self.p60 = ss[int(n * 0.60)]
            self.p75 = ss[int(n * 0.75)]
            self.p90 = ss[int(n * 0.90)]
            self.median = statistics.median(ss)

        # Pre-compute per-page average vertical gap
        self._avg_gap: Dict[int, float] = {}
        by_page: Dict[int, List[TextBlock]] = defaultdict(list)
        for b in blocks:
            by_page[b.page_num].append(b)
        for pg, pg_blocks in by_page.items():
            sorted_pg = sorted(pg_blocks, key=lambda b: b.y)
            gaps = []
            for i in range(1, len(sorted_pg)):
                g = sorted_pg[i].y - sorted_pg[i - 1].y_bottom
                if g > 0:
                    gaps.append(g)
            self._avg_gap[pg] = statistics.mean(gaps) if gaps else 0.0

        # Build a per-page sorted list for gap lookup
        self._sorted_by_page: Dict[int, List[TextBlock]] = {
            pg: sorted(blks, key=lambda b: b.y)
            for pg, blks in by_page.items()
        }

        # Enhanced: Track page margins for better header/footer detection
        self._page_margins: Dict[int, Tuple[float, float]] = {}
        for pg, pg_blocks in by_page.items():
            if pg_blocks:
                xs = [b.x for b in pg_blocks]
                ys = [b.y for b in pg_blocks]
                self._page_margins[pg] = (min(xs), max(ys))

    def _gap_above(self, block: TextBlock) -> float:
        pg_sorted = self._sorted_by_page.get(block.page_num, [])
        for i, b in enumerate(pg_sorted):
            if b is block:
                if i == 0:
                    return block.y   # gap from page top
                return block.y - pg_sorted[i - 1].y_bottom
        return 0.0

    def _is_near_page_margin(self, block: TextBlock, margin_type: str = 'top') -> bool:
        """Check if block is near page top/bottom margin."""
        margins = self._page_margins.get(block.page_num)
        if not margins:
            return False
        min_x, max_y = margins
        if margin_type == 'top':
            return block.y < 50  # within 50pts of top
        elif margin_type == 'bottom':
            return block.y > max_y - 50  # within 50pts of bottom
        return False

    def _is_hebrew_heading_pattern(self, text: str) -> bool:
        """Enhanced Hebrew heading pattern detection."""
        # Common Hebrew heading patterns
        hebrew_patterns = [
            r'^פרק\s+\d+',  # "Chapter X"
            r'^סעיף\s+\d+',  # "Section X"
            r'^תקנה\s+\d+',  # "Regulation X"
            r'^נספח\s+[א-ת]',  # "Appendix A"
            r'^טבלת\s+',  # "Table"
            r'^רשימת\s+',  # "List of"
            r'^הגדרות',  # "Definitions"
            r'^מבוא',  # "Introduction"
            r'^סיכום',  # "Summary"
            r'^חתימה',  # "Signature"
        ]
        return any(re.search(pattern, text, re.UNICODE | re.I) for pattern in hebrew_patterns)

    def classify(self, block: TextBlock) -> Optional[str]:
        text = block.text.strip()
        words = len(text.split())

        if words > self._MAX_HEADING_WORDS:
            return None
        if _is_list_item(text):
            return None

        fs   = block.font_size
        bold = block.is_bold
        gap  = self._gap_above(block)
        avg_gap   = self._avg_gap.get(block.page_num, 0.0)
        large_gap = gap > avg_gap * 1.8 and avg_gap > 0

        # Enhanced: Hebrew heading patterns get priority
        if self._is_hebrew_heading_pattern(text):
            if words <= 8 and (bold or large_gap):
                return "H2"
            return "H3"

        # --- Colon-terminated section header (e.g. "נוכחים:", "על סדר היום:") ---
        # Applies regardless of font/bold/gap — common Israeli protocol pattern.
        # Limit to short standalone headers (≤6 words, no mid-sentence punctuation).
        if _SECTION_COLON_RE.match(text) and words <= 6 and '.' not in text and ',' not in text:
            # Detect level by position: large gap → H1, otherwise H2
            if large_gap and gap > avg_gap * 2.5 and avg_gap > 0:
                return "H1"
            return "H2"

        # Enhanced: Check for page position hints
        near_top = self._is_near_page_margin(block, 'top')
        near_bottom = self._is_near_page_margin(block, 'bottom')

        # --- First block on the first page → likely the document title (H1) ---
        min_page = min(self._sorted_by_page.keys()) if self._sorted_by_page else 1
        if block.page_num == min_page and words <= 12:
            pg_blocks = self._sorted_by_page.get(block.page_num, [])
            if pg_blocks and pg_blocks[0] is block:
                return "H1"

        # Enhanced: Near top of page with large gap often indicates major heading
        if near_top and large_gap and gap > avg_gap * 2.0:
            if words <= 10:
                return "H1"
            elif words <= 15:
                return "H2"

        # Uniform-font document (all text same size — common in Israeli official docs).
        # Fall back to bold + gap + word-count heuristics.
        if self.p90 <= self.p60 * 1.05:   # all percentiles within 5%
            if not bold and not large_gap:
                return None
            if words <= 8:
                if large_gap and gap > avg_gap * 3.0:
                    return "H1"
                if bold and large_gap:
                    return "H2"
                if bold:
                    return "H3"
            if words <= 15 and bold and large_gap:
                return "H3"
            return None

        # Variable-font document: percentile-based classification with enhancements
        if fs >= self.p90:
            return "H1"
        if fs >= self.p75:
            return "H2"
        if fs >= self.p60 or (bold and fs > self.median):
            if bold or large_gap:
                return "H3"
        # Enhanced: Additional check for medium-sized bold text near top
        if bold and fs > self.median and near_top and words <= 12:
            return "H3"
        return None


# ---------------------------------------------------------------------------
# TableDetector
# ---------------------------------------------------------------------------

class TableDetector:
    """
    Detects tables by finding text blocks whose X-centres align across
    multiple rows. Enhanced with better clustering and multi-column detection.

    Algorithm
    ---------
    1. Group blocks into logical rows (Y within LINE_THRESH of each other).
    2. For each row, record the sorted X-centres of its blocks → "column sig".
    3. Consecutive rows whose column signatures are compatible (≥70 % of
       column centres within COL_THRESH) form a table candidate.
    4. Enhanced: Allow for slight variations in column count and better alignment.
    5. Candidates with ≥ MIN_ROWS rows and ≥ MIN_COLS columns are kept.
    6. The first row of each table is treated as the header row (TH).
    """

    LINE_THRESH = 6.0    # pts — blocks within this vertical band share a row
    COL_THRESH  = 18.0   # pts — column X-centres must be within this distance (tightened)
    MIN_ROWS    = 2
    MIN_COLS    = 2
    MAX_COLS    = 10     # Prevent false positives with too many columns

    def detect(self, blocks: List[TextBlock]) -> Tuple[
        List[dict], Set[int]
    ]:
        """
        Returns:
          tables      — list of table dicts (see _build_table_dict)
          claimed_ids — set of id(TextBlock) for all blocks absorbed into tables
        """
        tables: List[dict] = []
        claimed: Set[int] = set()

        by_page: Dict[int, List[TextBlock]] = defaultdict(list)
        for b in blocks:
            by_page[b.page_num].append(b)

        for page_num, page_blocks in by_page.items():
            pg_tables, pg_claimed = self._detect_page(page_blocks, page_num)
            tables.extend(pg_tables)
            claimed |= pg_claimed

        return tables, claimed

    # ------------------------------------------------------------------
    def _detect_page(self, blocks: List[TextBlock], page_num: int
                     ) -> Tuple[List[dict], Set[int]]:
        rows = self._group_rows(blocks)
        multi = [(y, row) for y, row in rows if len(row) >= self.MIN_COLS and len(row) <= self.MAX_COLS]
        if len(multi) < self.MIN_ROWS:
            return [], set()

        # Build (y, row, col_centres) triples
        triples = []
        for y, row in multi:
            sorted_row = sorted(row, key=lambda b: -b.x)  # RTL
            centres = [b.center_x for b in sorted_row]
            triples.append((y, sorted_row, centres))

        # Enhanced: Group consecutive compatible rows with better logic
        groups: List[List[tuple]] = []
        current = [triples[0]]
        for triple in triples[1:]:
            if self._centres_compatible(current[-1][2], triple[2]):
                current.append(triple)
            else:
                groups.append(current)
                current = [triple]
        groups.append(current)

        tables: List[dict] = []
        claimed: Set[int] = set()
        for group in groups:
            if len(group) < self.MIN_ROWS:
                continue
            # Enhanced: Validate table structure
            if self._is_valid_table(group):
                td = self._build_table_dict(group, page_num)
                tables.append(td)
                for b in td["all_blocks"]:
                    claimed.add(id(b))

        return tables, claimed

    def _is_valid_table(self, group: List[tuple]) -> bool:
        """Validate that a group of rows forms a coherent table."""
        if len(group) < self.MIN_ROWS:
            return False

        # Check column consistency
        col_counts = [len(row) for _, row, _ in group]
        avg_cols = statistics.mean(col_counts)
        # Allow some variation but not too much
        if max(col_counts) - min(col_counts) > 2:
            return False
        if avg_cols > self.MAX_COLS:
            return False

        # Check that columns are reasonably aligned
        centres_list = [centres for _, _, centres in group]
        if len(centres_list) < 2:
            return True

        # For each column position, check alignment across rows
        max_cols = max(len(c) for c in centres_list)
        for col_idx in range(max_cols):
            col_positions = []
            for centres in centres_list:
                if col_idx < len(centres):
                    col_positions.append(centres[col_idx])

            if len(col_positions) >= len(group) * 0.6:  # Column present in most rows
                if statistics.stdev(col_positions) > self.COL_THRESH:
                    return False  # Too much variation in column position

        return True

    def _group_rows(self, blocks: List[TextBlock]
                    ) -> List[Tuple[float, List[TextBlock]]]:
        if not blocks:
            return []
        sorted_blocks = sorted(blocks, key=lambda b: b.y)
        rows: List[Tuple[float, List[TextBlock]]] = []
        row_y = sorted_blocks[0].y
        row: List[TextBlock] = [sorted_blocks[0]]
        for b in sorted_blocks[1:]:
            if abs(b.y - row_y) <= self.LINE_THRESH:
                row.append(b)
            else:
                rows.append((row_y, row))
                row_y = b.y
                row = [b]
        rows.append((row_y, row))
        return rows

    def _centres_compatible(self, ca: List[float], cb: List[float]) -> bool:
        """Enhanced compatibility check allowing for missing columns."""
        if not ca or not cb:
            return False

        # Allow column count difference of 1
        if abs(len(ca) - len(cb)) > 1:
            return False

        # Try to match columns, allowing for some to be missing
        matched = 0
        total_possible = min(len(ca), len(cb))

        # For each column in the shorter list, find closest match in longer
        shorter, longer = (ca, cb) if len(ca) <= len(cb) else (cb, ca)

        for short_centre in shorter:
            # Find closest centre in longer list
            closest_dist = min(abs(short_centre - long_centre) for long_centre in longer)
            if closest_dist <= self.COL_THRESH:
                matched += 1

        # Require 70% of shorter list to match
        return matched >= len(shorter) * 0.7

    def _build_table_dict(self, group: List[tuple], page_num: int) -> dict:
        rows_of_blocks = [row for _, row, _ in group]
        all_blocks = [b for row in rows_of_blocks for b in row]
        return {
            "rows": rows_of_blocks,   # List[List[TextBlock]]
            "all_blocks": all_blocks,
            "page_num": page_num,
        }


# ---------------------------------------------------------------------------
# StructureDetector — orchestrator
# ---------------------------------------------------------------------------

class BorderTableDetector:
    """
    Detects tables in born-digital PDFs using graphic line borders.

    Algorithm
    ---------
    1. Collect horizontal lines (is_horizontal) and vertical lines (is_vertical)
       from the page's GraphicLine list.
    2. Cluster H-lines into distinct Y-bands (row boundaries).
    3. Cluster V-lines into distinct X-bands (column boundaries).
    4. Where ≥2 H-bands × ≥2 V-bands intersect → table region.
    5. Assign TextBlocks to cells by bounding-box containment.
    6. First row = TH; remaining rows = TD.

    Works reliably on municipal-regulation style tables that have explicit
    borders (like the water-rate table example).
    """

    CLUSTER_THRESH = 4.0    # pt — lines within this gap → same boundary
    MIN_CELL_W     = 15.0   # pt — narrower columns are ignored
    MIN_CELL_H     = 8.0    # pt — shorter rows are ignored
    CELL_MARGIN    = 4.0    # pt — expand cell bbox for text-block matching

    def detect(
        self,
        blocks: List[TextBlock],
        graphic_lines,               # List[GraphicLine]
    ) -> Tuple[List[dict], Set[int]]:
        """
        Returns:
          tables      — list of table dicts (rows of rows of cell dicts)
          claimed_ids — id(TextBlock) for all blocks absorbed into tables
        """
        from collections import defaultdict

        tables: List[dict] = []
        claimed: Set[int]  = set()

        by_page: Dict[int, dict] = defaultdict(lambda: {"blocks": [], "lines": []})
        for b in blocks:
            by_page[b.page_num]["blocks"].append(b)
        for gl in graphic_lines:
            by_page[gl.page_num]["lines"].append(gl)

        for page_num, data in by_page.items():
            pg_tables, pg_claimed = self._detect_page(
                data["blocks"], data["lines"], page_num
            )
            tables.extend(pg_tables)
            claimed |= pg_claimed

        return tables, claimed

    # ------------------------------------------------------------------
    def _detect_page(self, blocks, lines, page_num):
        h_lines = [l for l in lines if l.is_horizontal]
        v_lines = [l for l in lines if l.is_vertical]

        if len(h_lines) < 2 or len(v_lines) < 2:
            return [], set()

        h_ys = self._cluster([l.y0 for l in h_lines])
        v_xs = self._cluster([l.x0 for l in v_lines])

        if len(h_ys) < 2 or len(v_xs) < 2:
            return [], set()

        # Build cell grid
        grid_rows: List[List[dict]] = []
        for i in range(len(h_ys) - 1):
            top = h_ys[i]
            bot = h_ys[i + 1]
            if bot - top < self.MIN_CELL_H:
                continue

            row_cells: List[dict] = []
            # V-lines define columns right→left (RTL Hebrew)
            col_boundaries = sorted(v_xs, reverse=True)   # largest X first
            for j in range(len(col_boundaries) - 1):
                cell_right = col_boundaries[j]
                cell_left  = col_boundaries[j + 1]
                if cell_right - cell_left < self.MIN_CELL_W:
                    continue

                m = self.CELL_MARGIN
                cell_blocks = [
                    b for b in blocks
                    if (b.x        >= cell_left  - m and
                        b.x_right  <= cell_right + m and
                        b.y        >= top         - m and
                        b.y_bottom <= bot         + m)
                ]
                cell_text = " ".join(b.text.strip() for b in cell_blocks
                                     if b.text.strip())
                row_cells.append({
                    "text":   cell_text,
                    "blocks": cell_blocks,
                    "bbox":   (cell_left, top, cell_right, bot),
                })

            if row_cells:
                grid_rows.append(row_cells)

        if len(grid_rows) < 2:
            return [], set()

        # Flatten all claimed blocks
        claimed: Set[int] = set()
        for row in grid_rows:
            for cell in row:
                for b in cell["blocks"]:
                    claimed.add(id(b))

        return [{
            "rows":       [[c["text"] for c in row] for row in grid_rows],
            "rows_raw":   grid_rows,      # kept for StructElement building
            "all_blocks": [b for row in grid_rows
                             for cell in row for b in cell["blocks"]],
            "page_num":   page_num,
            "source":     "border",
        }], claimed

    def _cluster(self, values: List[float]) -> List[float]:
        if not values:
            return []
        sv = sorted(set(values))
        clusters: List[List[float]] = [[sv[0]]]
        for v in sv[1:]:
            if v - clusters[-1][-1] <= self.CLUSTER_THRESH:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [sum(c) / len(c) for c in clusters]


class StructureDetector:
    """
    Drives all detectors over a list of TextBlock objects and returns an
    ordered list of StructElement objects.

    Usage (digital PDF with graphic lines):
        blocks = parser.extract_blocks(path)
        lines  = parser.extract_lines(path)
        elements = StructureDetector().detect(blocks, graphic_lines=lines)

    Usage (no line data):
        elements = StructureDetector().detect(blocks)
    """

    def detect(
        self,
        blocks: List[TextBlock],
        graphic_lines=None,    # Optional[List[GraphicLine]]
        doc_type=None,         # Optional[DocumentType] — from DocumentClassifier
    ) -> List[StructElement]:
        """
        Detect semantic structure elements from text blocks.

        Parameters
        ----------
        blocks        : text blocks from extract_blocks()
        graphic_lines : graphic line objects from extract_lines() (for border tables)
        doc_type      : DocumentType from DocumentClassifier — selects specialized pipeline.
                        None / GENERAL → default pipeline.
        """
        # Lazy import to avoid circular dependency at module load time
        try:
            from .classifier import DocumentType as _DT
        except ImportError:
            _DT = None

        if not blocks:
            return []

        claimed_ids: Set[int] = set()
        raw_tables: List[dict] = []

        # 0. Header / footer artifact detection — remove repeated positional blocks
        if len({b.page_num for b in blocks}) >= 3:
            hf_ids = self._detect_headers_footers(blocks)
            if hf_ids:
                blocks = [b for b in blocks if id(b) not in hf_ids]

        # 1a. Border-based table detection (most reliable — uses graphic lines)
        if graphic_lines:
            bdt = BorderTableDetector()
            border_tables, border_claimed = bdt.detect(blocks, graphic_lines)
            raw_tables.extend(border_tables)
            claimed_ids |= border_claimed

        # 1b. Column-alignment table detection on remaining blocks
        free_after_border = [b for b in blocks if id(b) not in claimed_ids]
        if free_after_border:
            td = TableDetector()
            align_tables, align_claimed = td.detect(free_after_border)
            raw_tables.extend(align_tables)
            claimed_ids |= align_claimed

        # 1c. Repeated key/value rows promote to semantic tables before P fallback.
        free_after_tables = [b for b in blocks if id(b) not in claimed_ids]
        kv_tables, kv_claimed = self._detect_key_value_groups(free_after_tables)
        raw_tables.extend(kv_tables)
        claimed_ids |= kv_claimed

        # 2. Non-table blocks
        free_blocks = [b for b in blocks if id(b) not in claimed_ids]

        # 3. Reading order for free blocks
        ordered = sort_reading_order(free_blocks)

        # 4. Heading + list + paragraph detection — route to specialized pipeline
        hd = HeadingDetector(ordered)

        if _DT and doc_type == _DT.PROTOCOL:
            non_table_elems = self._classify_protocol(ordered, hd)
            non_table_elems = self._group_name_lists(non_table_elems)
        elif _DT and doc_type == _DT.LEGAL:
            non_table_elems = self._classify_legal(ordered, hd)
        elif _DT and doc_type == _DT.WORKPLAN:
            non_table_elems = self._classify_free(ordered, hd)
            # workplan: skip name-list grouping; tables already detected above
        else:
            # GENERAL / NEWSLETTER / FORM / SCANNED / None → default pipeline
            non_table_elems = self._classify_free(ordered, hd)
            non_table_elems = self._group_name_lists(non_table_elems)

        # 5. Convert raw_tables → StructElements
        table_elems = [self._build_table_elem(t) for t in raw_tables]

        # 6. Merge: insert tables at their natural reading-order position
        return self._merge(non_table_elems, table_elems)

    # ------------------------------------------------------------------
    def _detect_key_value_groups(self, blocks: List[TextBlock]) -> Tuple[List[dict], Set[int]]:
        tables: List[dict] = []
        claimed: Set[int] = set()

        by_page: Dict[int, List[TextBlock]] = defaultdict(list)
        for b in blocks:
            by_page[b.page_num].append(b)

        for page_num, page_blocks in by_page.items():
            ordered = sorted(page_blocks, key=lambda b: (b.y, -b.x))
            i = 0
            while i < len(ordered):
                run: List[Tuple[TextBlock, str, str]] = []
                base_x: Optional[float] = None
                j = i
                while j < len(ordered):
                    block = ordered[j]
                    split = _split_key_value_text(block.text)
                    if not split:
                        break
                    if base_x is None:
                        base_x = block.x
                    if abs(block.x - base_x) > 24.0:
                        break
                    run.append((block, split[0], split[1]))
                    j += 1

                if len(run) >= 3:
                    all_blocks = [item[0] for item in run]
                    tables.append({
                        "source": "keyvalue",
                        "rows_raw": run,
                        "all_blocks": all_blocks,
                        "page_num": page_num,
                    })
                    for b in all_blocks:
                        claimed.add(id(b))
                    i = j
                else:
                    i += 1

        return tables, claimed

    def _make_list(self, blocks: List[TextBlock], strip_marker: bool = True) -> StructElement:
        list_elem = StructElement("L", page_num=blocks[0].page_num)
        for lb in blocks:
            item_text = _strip_list_marker(lb.text) if strip_marker else lb.text.strip()
            li = StructElement("LI", page_num=lb.page_num)
            lbody = StructElement(
                "LBody", text=item_text,
                page_num=lb.page_num, source_bbox=lb.bbox,
                attrs={"original_text": lb.text},
            )
            li.add(lbody)
            list_elem.add(li)
        return list_elem

    def _implied_list_run(self, blocks: List[TextBlock], start: int,
                          hd: HeadingDetector) -> List[TextBlock]:
        """Enhanced implied list detection with better heuristics."""
        first = blocks[start]
        if not self._is_implied_list_candidate(first, hd):
            return []

        run = [first]
        gaps: List[float] = []
        last = first
        for block in blocks[start + 1:]:
            if block.page_num != first.page_num:
                break
            if not self._is_implied_list_candidate(block, hd):
                break
            # Enhanced: Check for reasonable horizontal alignment
            if abs(block.x - first.x) > _IMPLIED_LIST_X_TOLERANCE:
                break
            gap = block.y - last.y_bottom
            if gap < -2.0 or gap > max(32.0, last.height * 3.0):  # More lenient upper bound
                break
            if gaps and abs(gap - statistics.median(gaps)) > _IMPLIED_LIST_GAP_TOLERANCE:
                break
            # Enhanced: Check for similar text length patterns
            if abs(len(block.text.strip()) - len(first.text.strip())) > 60:  # More lenient
                break
            gaps.append(gap)
            run.append(block)
            last = block

        # Enhanced: Require minimum items but allow shorter runs in some cases
        if len(run) < _IMPLIED_LIST_MIN_ITEMS:
            return []
        # Additional check: ensure reasonable length consistency
        lengths = [len(b.text.strip()) for b in run]
        if statistics.stdev(lengths) > 40 and len(run) > 3:  # Too much variation
            return []
        return run

    def _is_implied_list_candidate(self, block: TextBlock, hd: HeadingDetector) -> bool:
        """Enhanced candidate detection for implied lists."""
        text = block.text.strip()
        if not text or _is_list_item(text) or _is_key_value_text(text):
            return False
        if _SECTION_COLON_RE.match(text) or _legal_clause_level(text) is not None:
            return False
        # Enhanced: Allow slightly longer texts for implied lists
        if len(text) > _IMPLIED_LIST_MAX_CHARS or len(text.split()) > _IMPLIED_LIST_MAX_WORDS:
            return False
        if hd.classify(block):
            return False
        # Enhanced: Check for reasonable content (not just punctuation)
        if not re.search(r'\w', text):  # Must contain word characters
            return False
        return True

    # ------------------------------------------------------------------
    # Protocol pipeline
    # ------------------------------------------------------------------
    def _classify_protocol(self, blocks: List[TextBlock],
                            hd: HeadingDetector) -> List[StructElement]:
        """
        Protocol-specific classification (פרוטוקול):
          - Colon-terminated headers already handled by HeadingDetector
          - Numbered agenda items: "1. Item" under "על סדר היום" → List/LI
          - Decision blocks: lines starting with "הוחלט" → H3 + P
          - Rest delegated to _classify_free
        """
        elements: List[StructElement] = []
        list_buf: List[TextBlock] = []
        in_agenda = False      # True while inside "על סדר היום" section

        _DECISION_RE = re.compile(r'^הוחלט[:\s]', re.UNICODE)
        _AGENDA_SECTION_RE = re.compile(r'^על\s+סדר\s+היום', re.UNICODE)

        def flush_list():
            if not list_buf:
                return
            elements.append(self._make_list(list_buf, strip_marker=True))
            list_buf.clear()

        i = 0
        while i < len(blocks):
            block = blocks[i]
            text = block.text.strip()
            if not text:
                i += 1
                continue

            implied_run = self._implied_list_run(blocks, i, hd)
            if implied_run:
                flush_list()
                in_agenda = False
                elements.append(self._make_list(implied_run, strip_marker=False))
                i += len(implied_run)
                continue

            # Decision block → H3 heading (marks start of a decision)
            if _DECISION_RE.match(text) and len(text.split()) <= 20:
                flush_list()
                in_agenda = False
                elements.append(StructElement.heading(
                    3, text, page_num=block.page_num, bbox=block.bbox
                ))
                i += 1
                continue

            # Heading detection (includes colon-headers from HeadingDetector)
            level_str = hd.classify(block)
            if level_str:
                flush_list()
                level = int(level_str[1])
                elements.append(StructElement.heading(
                    level, text, page_num=block.page_num, bbox=block.bbox
                ))
                # Track agenda section to force numbered sub-items into lists
                if _AGENDA_SECTION_RE.match(text):
                    in_agenda = True
                else:
                    in_agenda = False
                i += 1
                continue

            # Numbered list items (always list, even inside agenda)
            if _is_list_item(text) or (in_agenda and _LEGAL_CLAUSE_RE.match(text)):
                list_buf.append(block)
                i += 1
                continue

            # Paragraph
            flush_list()
            elements.append(StructElement.paragraph(
                text, page_num=block.page_num, bbox=block.bbox,
            ))
            i += 1

        flush_list()
        return elements

    # ------------------------------------------------------------------
    # Legal pipeline
    # ------------------------------------------------------------------
    def _classify_legal(self, blocks: List[TextBlock],
                         hd: HeadingDetector) -> List[StructElement]:
        """
        Legal / regulatory document pipeline (חוק, תקנות, חוק עזר):
          - Numbered clauses (1. / 1.1. / 1.1.1.) → H1/H2/H3 in strict hierarchy
          - Definition items ("term" – definition) → L/LI
          - Annex/appendix headings ("תוספת", "נספח") → H2
          - Everything else → _classify_free fallback
        """
        elements: List[StructElement] = []
        list_buf: List[TextBlock] = []

        _ANNEX_RE = re.compile(r'^(?:תוספת|נספח|ספח|סד"כ|פרק)\s', re.UNICODE)

        def flush_list():
            if not list_buf:
                return
            lst = StructElement("L", page_num=list_buf[0].page_num)
            for lb in list_buf:
                item_text = _strip_list_marker(lb.text)
                li    = StructElement("LI", page_num=lb.page_num)
                lbody = StructElement(
                    "LBody", text=item_text, page_num=lb.page_num,
                    source_bbox=lb.bbox,
                    attrs={"original_text": lb.text},
                )
                li.add(lbody)
                lst.add(li)
            elements.append(lst)
            list_buf.clear()

        for block in blocks:
            text = block.text.strip()
            if not text:
                continue

            # --- Numbered clause → heading (BEFORE generic list-item check) ---
            clause_lvl = _legal_clause_level(text)
            if clause_lvl is not None:
                flush_list()
                elements.append(StructElement.heading(
                    clause_lvl, text, page_num=block.page_num, bbox=block.bbox
                ))
                continue

            # --- Annex / appendix headings ---
            if _ANNEX_RE.match(text) and len(text.split()) <= 10:
                flush_list()
                elements.append(StructElement.heading(
                    2, text, page_num=block.page_num, bbox=block.bbox
                ))
                continue

            # --- Colon headers (הגדרות:, פרשנות:) via HeadingDetector ---
            level_str = hd.classify(block)
            if level_str:
                flush_list()
                level = int(level_str[1])
                elements.append(StructElement.heading(
                    level, text, page_num=block.page_num, bbox=block.bbox
                ))
                continue

            # --- Definition / regular list item ---
            if _is_list_item(text):
                list_buf.append(block)
                continue

            # --- Paragraph ---
            flush_list()
            elements.append(StructElement.paragraph(
                text, page_num=block.page_num, bbox=block.bbox,
            ))

        flush_list()
        return elements

    # ------------------------------------------------------------------
    # General / fallback pipeline
    # ------------------------------------------------------------------
    def _classify_free(self, blocks: List[TextBlock],
                        hd: HeadingDetector) -> List[StructElement]:
        elements: List[StructElement] = []
        list_buf: List[TextBlock] = []

        def flush_list():
            if not list_buf:
                return
            elements.append(self._make_list(list_buf, strip_marker=True))
            list_buf.clear()

        i = 0
        while i < len(blocks):
            block = blocks[i]
            text = block.text.strip()
            if not text:
                i += 1
                continue

            implied_run = self._implied_list_run(blocks, i, hd)
            if implied_run:
                flush_list()
                elements.append(self._make_list(implied_run, strip_marker=False))
                i += len(implied_run)
                continue

            if _is_signature_text(text):
                flush_list()
                elements.append(StructElement.figure(
                    "Signature", page_num=block.page_num, bbox=block.bbox,
                ))
                i += 1
                continue

            # --- heading? ---
            level_str = hd.classify(block)
            if level_str:
                flush_list()
                level = int(level_str[1])
                elements.append(StructElement.heading(
                    level, text,
                    page_num=block.page_num,
                    bbox=block.bbox,
                ))
                i += 1
                continue

            # --- list item? ---
            if _is_list_item(text):
                list_buf.append(block)
                i += 1
                continue

            # --- paragraph ---
            flush_list()
            elements.append(StructElement.paragraph(
                text, page_num=block.page_num, bbox=block.bbox,
            ))
            i += 1

        flush_list()
        return elements

    # ------------------------------------------------------------------
    def _detect_headers_footers(self, blocks: List[TextBlock]) -> Set[int]:
        """
        Enhanced detection of header/footer artifacts using position and repetition analysis.

        Returns id(block) for blocks whose vertical position (quantised to 10 pt)
        appears near the top or bottom of the page across ≥40% of pages (min 3).
        Also detects repeated text patterns that indicate headers/footers.
        """
        by_page: Dict[int, List[TextBlock]] = defaultdict(list)
        for b in blocks:
            by_page[b.page_num].append(b)

        n_pages = len(by_page)
        min_pages = max(3, int(n_pages * 0.35))  # Slightly more lenient
        GRID = 8.0  # Finer grid for better precision

        # bucket → {page_num → [block, ...]}
        bucket_to_pages: Dict[int, Dict[int, List[TextBlock]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for pg, pg_blocks in by_page.items():
            sorted_pg = sorted(pg_blocks, key=lambda b: b.y)
            # Look at top 4 and bottom 4 blocks per page for headers/footers
            candidates = sorted_pg[:4] + sorted_pg[-4:]
            for blk in candidates:
                bucket = int(blk.y / GRID)
                bucket_to_pages[bucket][pg].append(blk)

        artifact_ids: Set[int] = set()
        for page_map in bucket_to_pages.values():
            if len(page_map) >= min_pages:
                for blk_list in page_map.values():
                    for blk in blk_list:
                        # Additional check: short text or page numbers
                        text = blk.text.strip()
                        if (len(text) <= 50 or
                            re.search(r'\d+', text) or  # Contains numbers (page numbers)
                            re.search(r'(page|עמוד)', text, re.I | re.UNICODE)):  # Page indicators
                            artifact_ids.add(id(blk))

        # Enhanced: Detect repeated text patterns across pages
        text_positions: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        for b in blocks:
            text = b.text.strip()
            if len(text) <= 100:  # Only consider short texts for repetition
                text_positions[text].append((b.page_num, b.y))

        for text, positions in text_positions.items():
            if len(positions) >= min_pages:
                # Check if positions are similar across pages
                ys = [y for _, y in positions]
                if len(ys) > 1 and statistics.stdev(ys) < 20.0:  # Similar vertical position
                    # Mark all instances as artifacts
                    for page_num, y in positions:
                        for b in by_page[page_num]:
                            if abs(b.y - y) < 5.0 and b.text.strip() == text:
                                artifact_ids.add(id(b))

        return artifact_ids

    # ------------------------------------------------------------------
    def _group_name_lists(self, elements: List[StructElement]) -> List[StructElement]:
        """
        Post-process: convert runs of short P elements that immediately follow a
        heading into a List/LI structure.

        Typical Hebrew protocol pattern:
            H2 "נוכחים:"
            P  "ראש העיר משה לוי"
            P  "מנכ"ל העירייה דוד כהן"
            ...  → these should be L / LI / LBody

        Trigger conditions (all must hold):
          - The preceding element is a heading (H1/H2/H3)
          - At least 2 consecutive P elements
          - Each P is short (≤ 10 words)
          - Hebrew-dominant text (more Hebrew chars than Latin)
        """
        _MAX_NAME_WORDS = 10
        _MIN_NAME_ITEMS = 2

        result: List[StructElement] = []
        i = 0
        while i < len(elements):
            elem = elements[i]
            result.append(elem)
            i += 1

            if elem.elem_type not in ("H1", "H2", "H3"):
                continue

            # Peek ahead: collect consecutive short Hebrew P blocks
            name_items: List[StructElement] = []
            j = i
            while j < len(elements):
                nxt = elements[j]
                if nxt.elem_type != "P":
                    break
                text = (nxt.text or "").strip()
                wc   = len(text.split())
                if wc > _MAX_NAME_WORDS or not _is_hebrew_dominant(text):
                    break
                name_items.append(nxt)
                j += 1

            if len(name_items) >= _MIN_NAME_ITEMS:
                lst = StructElement("L", page_num=name_items[0].page_num)
                for item in name_items:
                    li   = StructElement("LI", page_num=item.page_num)
                    lbody = StructElement(
                        "LBody", text=item.text,
                        page_num=item.page_num,
                        source_bbox=item.source_bbox,
                        attrs={"original_text": item.text},
                    )
                    li.add(lbody)
                    lst.add(li)
                result.append(lst)
                i = j  # skip the consumed P elements
            # else: leave P elements as-is (will be appended in next iterations)

        return result

    def _build_table_elem(self, table_data: dict) -> StructElement:
        table  = StructElement("Table", page_num=table_data["page_num"])
        source = table_data.get("source", "align")

        if source == "keyvalue" and "rows_raw" in table_data:
            for block, label, value in table_data["rows_raw"]:
                tr = StructElement("TR", page_num=table_data["page_num"])
                th = StructElement(
                    "TH", text=label, page_num=table_data["page_num"],
                    source_bbox=block.bbox,
                )
                th.attrs["Scope"] = "Row"
                td = StructElement(
                    "TD", text=value, page_num=table_data["page_num"],
                    source_bbox=block.bbox,
                )
                tr.add(th)
                tr.add(td)
                table.add(tr)
        elif source == "border" and "rows_raw" in table_data:
            # BorderTableDetector: rows_raw = List[List[{text, blocks, bbox}]]
            for i, row_cells in enumerate(table_data["rows_raw"]):
                tr        = StructElement("TR", page_num=table_data["page_num"])
                is_header = (i == 0)
                for cell_dict in row_cells:
                    ctype = "TH" if is_header else "TD"
                    cell  = StructElement(
                        ctype,
                        text=cell_dict["text"],
                        page_num=table_data["page_num"],
                        source_bbox=cell_dict.get("bbox"),
                    )
                    if is_header:
                        cell.attrs["Scope"] = "Col"
                    tr.add(cell)
                table.add(tr)
        else:
            # TableDetector (column-alignment): rows = List[List[TextBlock]]
            for i, row_blocks in enumerate(table_data["rows"]):
                tr        = StructElement("TR", page_num=table_data["page_num"])
                is_header = (i == 0)
                for cell_block in row_blocks:   # already sorted RTL
                    ctype = "TH" if is_header else "TD"
                    cell  = StructElement(
                        ctype, text=cell_block.text.strip(),
                        page_num=table_data["page_num"],
                        source_bbox=cell_block.bbox,
                    )
                    if is_header:
                        cell.attrs["Scope"] = "Col"
                    tr.add(cell)
                table.add(tr)

        return table

    def _merge(self, free: List[StructElement],
               tables: List[StructElement]) -> List[StructElement]:
        """
        Interleave table elements into the free-block element list based on
        the table's first row page_num and y-position.
        """
        result = list(free)
        for tbl in sorted(tables, key=lambda t: (t.page_num,
                                                   t.children[0].page_num
                                                   if t.children else 0)):
            tbl_page = tbl.page_num
            # Find first free element that is on the same page and below the table
            # (use page number as proxy when we lack y info on StructElement)
            insert_idx = len(result)
            for i, elem in enumerate(result):
                if elem.page_num > tbl_page:
                    insert_idx = i
                    break
            result.insert(insert_idx, tbl)
        return result


# ---------------------------------------------------------------------------
# AI structure merge
# ---------------------------------------------------------------------------

def merge_ai_structure(
    rule_elements: List[StructElement],
    ai_structures: Dict[int, List[dict]],  # page_num → [{type, text, cells?}]
    lang: str = "he-IL",
) -> List[StructElement]:
    """
    Merge AI-detected structure (from analyze_structure_with_ai) with
    rule-based elements.

    Strategy:
    - For pages where AI returned results AND rule-based found nothing
      (or only generic P): prefer AI results.
    - For pages where rule-based detected headings / tables: keep rule-based
      (it has position info and is more reliable for layout elements).
    - The merge is per-page.
    """
    if not ai_structures:
        return rule_elements

    # Build a per-page index of rule elements
    rule_by_page: Dict[int, List[StructElement]] = defaultdict(list)
    for e in rule_elements:
        rule_by_page[e.page_num].append(e)

    merged: List[StructElement] = []
    all_pages = sorted(
        set(list(rule_by_page.keys()) + list(ai_structures.keys()))
    )

    for pg in all_pages:
        rule_pg = rule_by_page.get(pg, [])
        ai_pg   = ai_structures.get(pg, [])

        rule_has_structure = any(
            e.elem_type in ("H1", "H2", "H3", "Table", "L")
            for e in rule_pg
        )

        if rule_has_structure or not ai_pg:
            merged.extend(rule_pg)
        else:
            # Convert AI dicts to StructElements
            merged.extend(_ai_dicts_to_elems(ai_pg, pg))

    return merged