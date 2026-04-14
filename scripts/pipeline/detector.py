"""
detector.py — Rule-based structural element detection from positioned text blocks.

Pipeline order (matters — earlier classifiers claim blocks first):
  1. TableDetector   — column-alignment clustering
  2. HeadingDetector — font-size percentile + bold + spacing heuristics
  3. ListDetector    — regex pattern matching (Hebrew + Latin)
  4. Residual        — everything else becomes a paragraph

Output: an ordered list of StructElement objects ready for tag_builder.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .models import StructElement, TextBlock

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
    # Numeric:    "1." "1)" "(1)" "1 -"
    re.compile(r"^[\(\[]?\d+[\.\)\]]\s", re.UNICODE),
    # Hebrew aleph-bet: "א." "א)" "(א)"
    re.compile(r"^[\(\[]?[אבגדהוזחטיכלמנסעפצקרשת][\.\)]\s", re.UNICODE),
    # Latin letters:   "a." "A."
    re.compile(r"^[\(\[]?[a-zA-Z][\.\)]\s", re.UNICODE),
    # Bullet characters
    re.compile(r"^[-–—•·◦▪▸►→✓✗]\s"),
    # Roman numerals (i–xii only to avoid false positives)
    re.compile(r"^(?:ix|iv|vi{0,3}|xi|x|i{1,3})\.\s", re.I),
]

# Definition-list item: "term" – definition  (Hebrew legal docs use geresh/quote)
_DEF_ITEM_RE = re.compile(
    r'^["״\u201c\u201d\u05F4][^\u201d"״\u05F4]{1,50}["״\u201d\u05F4]\s*[-\u2013\u2014]',
    re.UNICODE,
)

# Colon-terminated section header: "נוכחים:" / "על סדר היום:" (≤60 chars, ends with colon)
_SECTION_COLON_RE = re.compile(r'^[^\n]{1,60}[:\uFF1A]\s*$', re.UNICODE)


def _is_list_item(text: str) -> bool:
    s = text.strip()
    return any(p.match(s) for p in _LIST_PATTERNS) or bool(_DEF_ITEM_RE.match(s))


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
    s = re.sub(r"^[-–—•·◦▪▸►→✓✗]\s+", "", s)
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

    def _gap_above(self, block: TextBlock) -> float:
        pg_sorted = self._sorted_by_page.get(block.page_num, [])
        for i, b in enumerate(pg_sorted):
            if b is block:
                if i == 0:
                    return block.y   # gap from page top
                return block.y - pg_sorted[i - 1].y_bottom
        return 0.0

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

        # --- Colon-terminated section header (e.g. "נוכחים:", "על סדר היום:") ---
        # Applies regardless of font/bold/gap — common Israeli protocol pattern.
        # Limit to short standalone headers (≤6 words, no mid-sentence punctuation).
        if _SECTION_COLON_RE.match(text) and words <= 6 and '.' not in text and ',' not in text:
            # Detect level by position: large gap → H1, otherwise H2
            if large_gap and gap > avg_gap * 2.5 and avg_gap > 0:
                return "H1"
            return "H2"

        # --- First block on the first page → likely the document title (H1) ---
        min_page = min(self._sorted_by_page.keys()) if self._sorted_by_page else 1
        if block.page_num == min_page and words <= 12:
            pg_blocks = self._sorted_by_page.get(block.page_num, [])
            if pg_blocks and pg_blocks[0] is block:
                return "H1"

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

        # Variable-font document: percentile-based classification
        if fs >= self.p90:
            return "H1"
        if fs >= self.p75:
            return "H2"
        if fs >= self.p60 or (bold and fs > self.median):
            if bold or large_gap:
                return "H3"
        return None


# ---------------------------------------------------------------------------
# TableDetector
# ---------------------------------------------------------------------------

class TableDetector:
    """
    Detects tables by finding text blocks whose X-centres align across
    multiple rows.

    Algorithm
    ---------
    1. Group blocks into logical rows (Y within LINE_THRESH of each other).
    2. For each row, record the sorted X-centres of its blocks → "column sig".
    3. Consecutive rows whose column signatures are compatible (≥70 % of
       column centres within COL_THRESH) form a table candidate.
    4. Candidates with ≥ MIN_ROWS rows and ≥ MIN_COLS columns are kept.
    5. The first row of each table is treated as the header row (TH).
    """

    LINE_THRESH = 6.0    # pts — blocks within this vertical band share a row
    COL_THRESH  = 22.0   # pts — column X-centres must be within this distance
    MIN_ROWS    = 2
    MIN_COLS    = 2

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
        multi = [(y, row) for y, row in rows if len(row) >= self.MIN_COLS]
        if len(multi) < self.MIN_ROWS:
            return [], set()

        # Build (y, row, col_centres) triples
        triples = []
        for y, row in multi:
            sorted_row = sorted(row, key=lambda b: -b.x)  # RTL
            centres = [b.center_x for b in sorted_row]
            triples.append((y, sorted_row, centres))

        # Group consecutive compatible rows
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
            td = self._build_table_dict(group, page_num)
            tables.append(td)
            for b in td["all_blocks"]:
                claimed.add(id(b))

        return tables, claimed

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
        if abs(len(ca) - len(cb)) > 1:
            return False
        matched = sum(
            1 for a, b in zip(ca, cb)
            if abs(a - b) <= self.COL_THRESH
        )
        return matched >= min(len(ca), len(cb)) * 0.7

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
    ) -> List[StructElement]:
        if not blocks:
            return []

        claimed_ids: Set[int] = set()
        raw_tables: List[dict] = []

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

        # 2. Non-table blocks
        free_blocks = [b for b in blocks if id(b) not in claimed_ids]

        # 3. Reading order for free blocks
        ordered = sort_reading_order(free_blocks)

        # 4. Heading + list + paragraph detection
        hd = HeadingDetector(ordered)
        non_table_elems = self._classify_free(ordered, hd)

        # 4b. Post-process: group consecutive short P after a heading → List
        non_table_elems = self._group_name_lists(non_table_elems)

        # 5. Convert raw_tables → StructElements
        table_elems = [self._build_table_elem(t) for t in raw_tables]

        # 6. Merge: insert tables at their natural reading-order position
        return self._merge(non_table_elems, table_elems)

    # ------------------------------------------------------------------
    def _classify_free(self, blocks: List[TextBlock],
                        hd: HeadingDetector) -> List[StructElement]:
        elements: List[StructElement] = []
        list_buf: List[TextBlock] = []

        def flush_list():
            if not list_buf:
                return
            list_elem = StructElement("L", page_num=list_buf[0].page_num)
            for lb in list_buf:
                item_text = _strip_list_marker(lb.text)
                li = StructElement("LI", page_num=lb.page_num)
                lbody = StructElement(
                    "LBody", text=item_text,
                    page_num=lb.page_num, source_bbox=lb.bbox,
                    # original_text preserves list marker for MCID content-stream matching
                    attrs={"original_text": lb.text},
                )
                li.add(lbody)
                list_elem.add(li)
            elements.append(list_elem)
            list_buf.clear()

        for block in blocks:
            text = block.text.strip()
            if not text:
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
                continue

            # --- list item? ---
            if _is_list_item(text):
                list_buf.append(block)
                continue


            # --- paragraph ---
            flush_list()
            elements.append(StructElement.paragraph(
                text, page_num=block.page_num, bbox=block.bbox,
            ))

        flush_list()
        return elements

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

        if source == "border" and "rows_raw" in table_data:
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


def _ai_dicts_to_elems(items: List[dict], page_num: int) -> List[StructElement]:
    """Convert AI structure dicts to StructElement list."""
    elements: List[StructElement] = []
    list_buf: List[str] = []

    _type_map = {
        "h1": "H1", "h2": "H2", "h3": "H3",
        "p": "P", "caption": "Caption",
    }

    def flush_list():
        if not list_buf:
            return
        l = StructElement("L", page_num=page_num)
        for txt in list_buf:
            li = StructElement("LI", page_num=page_num)
            li.add(StructElement("LBody", text=txt, page_num=page_num))
            l.add(li)
        elements.append(l)
        list_buf.clear()

    for item in items:
        t = str(item.get("type", "p")).lower()
        text = str(item.get("text", "")).strip()

        if t == "li":
            list_buf.append(text)
            continue

        flush_list()

        if t in _type_map:
            elements.append(StructElement(_type_map[t], text=text, page_num=page_num))
        elif t == "tr":
            # standalone tr — build a minimal Table wrapper
            cells = item.get("cells", [])
            tbl = StructElement("Table", page_num=page_num)
            tr  = StructElement("TR", page_num=page_num)
            for c in cells:
                ct   = "TH" if str(c.get("type", "td")).lower() == "th" else "TD"
                cell = StructElement(ct, text=str(c.get("text", "")), page_num=page_num)
                if ct == "TH":
                    cell.attrs["Scope"] = "Col"
                tr.add(cell)
            tbl.add(tr)
            elements.append(tbl)
        else:
            elements.append(StructElement("P", text=text, page_num=page_num))

    flush_list()
    return elements
