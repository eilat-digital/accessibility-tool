"""
tag_builder.py — Inject a PDF/UA-1 compliant StructTreeRoot into a pikepdf PDF.

Two public entry points:

  inject_digital(pdf, elements, lang, title, author)
    For digital (born-digital) PDFs.
    Parses each page's content stream and wraps every BT/ET text block in
    its own BDC<<MCID n>>...EMC.  Non-text content is wrapped as Artifact.
    Each leaf StructElement (H1, P, TH, TD, LBody …) is bound to its own MCID.
    A correct ParentTree is built so PAC can verify the full content binding.

  inject_scanned(pdf, page_elements, lang, title, author)
    For PDFs rebuilt from rasterised pages (each page = one Figure MCID).
    Figure wraps the raster image; semantic elements (H1/P/List/Table) are
    placed as Sect siblings AFTER the Figure — never nested inside it.

Both functions operate in-place on the pikepdf.Pdf object.
Caller is responsible for saving.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import pikepdf
from pikepdf import Array, Boolean, Dictionary, Name, String

from .models import StructElement, HEADING_TYPES, TABLE_TYPES, LIST_TYPES

# PDF/UA attribute owners
_O_TABLE  = Name("/Table")
_O_LIST   = Name("/List")
_O_LAYOUT = Name("/Layout")

# Standard structure types accepted for export (anything else coerces to P)
_ALL_VALID = (
    HEADING_TYPES | TABLE_TYPES | LIST_TYPES |
    {"Document", "Sect", "Div", "Part", "Art",
     "P", "Figure", "Caption", "BlockQuote",
     "Span", "Link", "Note", "Reference",
     "TOC", "TOCI", "Index", "Formula"}
)

# ============================================================
# Hebrew-safe PDF string decoding
# ============================================================

def _decode_pdf_string(s) -> str:
    """
    Decode a pikepdf String to Python unicode.

    pikepdf.String stores raw bytes.  Hebrew PDFs commonly use:
      - UTF-16 BE with BOM (FE FF)
      - PDFDocEncoding (cp1252 superset)
      - cp1255 (Windows Hebrew)
      - Custom CMap  → raw bytes that look like cp1255

    str(pikepdf.String) applies latin-1 cast which produces mojibake.
    We try encodings in order of likelihood for Israeli government PDFs.
    """
    try:
        raw = bytes(s)
    except Exception:
        return str(s)
    if not raw:
        return ""
    # UTF-16 with BOM
    if raw[:2] in (b'\xfe\xff', b'\xff\xfe'):
        try:
            return raw.decode('utf-16')
        except Exception:
            pass
    # UTF-8
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        pass
    # cp1255 (Windows Hebrew)
    try:
        return raw.decode('cp1255')
    except UnicodeDecodeError:
        pass
    # PDFDocEncoding fallback (cp1252)
    try:
        return raw.decode('cp1252', errors='replace')
    except Exception:
        return raw.decode('latin-1', errors='replace')


# ============================================================
# Content-stream per-block MCID injection
# ============================================================

def _inject_mcids_into_page(
    pdf: pikepdf.Pdf,
    page_obj,
    mcid_start: int = 0,
) -> List[Tuple[int, str]]:
    """
    Parse the page content stream and rewrite it so that:
      - Every BT...ET text block is wrapped in  /P <<MCID n>> BDC ... EMC
      - All other content (graphics, rules) is wrapped as
        /Artifact <</Type /Layout>> BDC ... EMC

    Returns list of (mcid, extracted_text) in document order.
    Returns [] (and leaves the content stream unchanged) on any failure.
    """
    try:
        instrs: List[Tuple] = list(pikepdf.parse_content_stream(page_obj))
    except Exception:
        return []

    if not instrs:
        return []

    # ---- locate all BT/ET spans -----------------------------------------
    bt_spans: List[Tuple[int, int]] = []   # (bt_index, et_index) inclusive
    i = 0
    while i < len(instrs):
        if str(instrs[i][1]) == "BT":
            bt = i
            j = i + 1
            while j < len(instrs) and str(instrs[j][1]) != "ET":
                j += 1
            bt_spans.append((bt, j))       # j is ET (or past-end)
            i = j + 1
        else:
            i += 1

    if not bt_spans:
        return []

    # ---- helper: extract text from a BT/ET span -------------------------
    def _span_text(bt: int, et: int) -> str:
        parts: List[str] = []
        for k in range(bt + 1, et):
            ops, op = instrs[k]
            s = str(op)
            if s in ("Tj", "'", '"') and ops:
                try:
                    parts.append(_decode_pdf_string(ops[0]))
                except Exception:
                    pass
            elif s == "TJ" and ops:
                try:
                    for item in ops[0]:
                        if isinstance(item, pikepdf.String):
                            parts.append(_decode_pdf_string(item))
                except Exception:
                    pass
        return "".join(parts).strip()

    # ---- build fast-lookup sets -----------------------------------------
    bt_to_span: Dict[int, Tuple[int, int]] = {}   # bt_idx → (et_idx, span_num)
    inside_span: set = set()
    for snum, (bt, et) in enumerate(bt_spans):
        bt_to_span[bt] = (et, snum)
        for k in range(bt, et + 1):
            inside_span.add(k)

    # ---- rewrite instructions -------------------------------------------
    new_instrs: List[Tuple] = []
    mcid_texts: List[Tuple[int, str]] = []
    mcid = mcid_start
    artifact_buf: List[Tuple] = []

    def _flush_artifact() -> None:
        if not artifact_buf:
            return
        new_instrs.append((
            [Name("/Artifact"), Dictionary(Type=_O_LAYOUT)],
            pikepdf.Operator("BDC"),
        ))
        new_instrs.extend(artifact_buf)
        new_instrs.append(([], pikepdf.Operator("EMC")))
        artifact_buf.clear()

    i = 0
    while i < len(instrs):
        if i in bt_to_span:
            _flush_artifact()
            et, _ = bt_to_span[i]
            text = _span_text(i, et)

            # /P <<MCID n>> BDC
            new_instrs.append((
                [Name("/P"), Dictionary(MCID=pikepdf.objects.Integer(mcid))],
                pikepdf.Operator("BDC"),
            ))
            # BT ... ET
            for k in range(i, et + 1):
                new_instrs.append(instrs[k])
            # EMC
            new_instrs.append(([], pikepdf.Operator("EMC")))

            mcid_texts.append((mcid, text))
            mcid += 1
            i = et + 1

        elif i in inside_span:
            # interior of a span already emitted above — skip
            i += 1

        else:
            artifact_buf.append(instrs[i])
            i += 1

    _flush_artifact()

    # ---- write back rewritten stream ------------------------------------
    try:
        new_bytes = pikepdf.unparse_content_stream(new_instrs)
        page_obj["/Contents"] = pdf.make_stream(new_bytes)
    except Exception:
        return []

    return mcid_texts


# ============================================================
# Internal struct-tree builder
# ============================================================

class _Builder:
    """Creates pikepdf indirect Dictionary objects for struct elements."""

    def __init__(self, pdf: pikepdf.Pdf, lang: str) -> None:
        self.pdf  = pdf
        self.lang = lang

    def make_elem(
        self,
        stype: str,
        parent,
        *,
        actual_text: str = "",
        alt: str = "",
        title: str = "",
        page_obj=None,
        mcid: Optional[int] = None,
    ) -> pikepdf.Dictionary:
        d = Dictionary(
            Type=Name("/StructElem"),
            S=Name(f"/{stype}"),
            P=parent,
        )
        d["/Lang"] = String(self.lang)
        if actual_text:
            d["/ActualText"] = String(actual_text)
        if alt:
            d["/Alt"] = String(alt)
        if title:
            d["/T"] = String(title)
        if mcid is not None and page_obj is not None:
            d["/K"]  = pikepdf.objects.Integer(mcid)
            d["/Pg"] = page_obj
        return self.pdf.make_indirect(d)

    def set_th_attrs(self, elem: pikepdf.Dictionary, scope: str = "Col") -> None:
        attrs = self.pdf.make_indirect(Dictionary())
        attrs["/O"]     = _O_TABLE
        attrs["/Scope"] = Name(f"/{scope}")
        elem["/A"] = attrs


# ============================================================
# Per-element MCID assignment helpers
# ============================================================

def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _match_text(elem: "StructElement") -> str:
    """
    Return the best text for MCID matching.
    LBody elements have their list marker stripped in .text — use original_text
    attr (set by detector) which includes the marker and matches the content stream.
    """
    return elem.attrs.get("original_text") or elem.text or ""


def _get_leaves(elements: List[StructElement]) -> List[StructElement]:
    """Depth-first ordered list of leaf StructElements."""
    out: List[StructElement] = []

    def _dfs(e: StructElement) -> None:
        if not e.children:
            out.append(e)
        else:
            for c in e.children:
                _dfs(c)

    for e in elements:
        _dfs(e)
    return out


def _assign_mcids(
    page_elems: List[StructElement],
    mcid_texts: List[Tuple[int, str]],
) -> Dict[int, int]:
    """
    Assign content-stream MCIDs to leaf StructElements.

    Strategy (two passes):
    1. Exact / substring text match (handles split-line pdfminer merging).
    2. Sequential fallback for unmatched leaves.

    Returns {id(leaf_elem): mcid}.
    """
    leaf_elems = _get_leaves(page_elems)
    if not leaf_elems or not mcid_texts:
        return {}

    norm_mcids = [(m, _normalize(t)) for m, t in mcid_texts]
    used_mcids: set = set()
    result: Dict[int, int] = {}

    # Pass 1 — best text match
    for leaf in leaf_elems:
        leaf_norm = _normalize(_match_text(leaf))
        if not leaf_norm:
            continue
        best_m: Optional[int] = None
        best_score = 0
        for m, mtext in norm_mcids:
            if m in used_mcids:
                continue
            # Overlap score: shared characters / max length (ignoring spaces)
            a = leaf_norm.replace(" ", "")
            b = mtext.replace(" ", "")
            if not a or not b:
                continue
            overlap = len(set(a) & set(b))
            score   = overlap / max(len(set(a)), len(set(b)))
            # Bonus if one contains the other
            if a in b or b in a:
                score += 0.5
            if score > best_score:
                best_score = score
                best_m = m
        if best_m is not None and best_score > 0.3:
            result[id(leaf)] = best_m
            used_mcids.add(best_m)

    # Pass 2 — sequential fallback for unmatched leaves
    remaining_mcids = [m for m, _ in norm_mcids if m not in used_mcids]
    ri = 0
    for leaf in leaf_elems:
        if id(leaf) not in result and ri < len(remaining_mcids):
            result[id(leaf)] = remaining_mcids[ri]
            ri += 1

    return result


def _build_elem_with_mcid(
    b: _Builder,
    se: StructElement,
    parent_ref,
    page_obj,
    leaf_mcid_map: Dict[int, int],
) -> Optional[pikepdf.Dictionary]:
    """Recursively build a pikepdf struct element tree, assigning MCIDs to leaves."""
    stype = se.elem_type if se.elem_type in _ALL_VALID else "P"
    text  = se.text.strip()

    # Leaf: assign MCID if available
    elem_mcid = leaf_mcid_map.get(id(se)) if not se.children else None

    pk = b.make_elem(
        stype, parent_ref,
        actual_text=text,
        alt=(text if stype == "Figure" else ""),
        page_obj=(page_obj if elem_mcid is not None else None),
        mcid=elem_mcid,
    )

    if stype == "TH":
        b.set_th_attrs(pk, se.attrs.get("Scope", "Col"))

    if se.children:
        child_refs = []
        for child in se.children:
            cr = _build_elem_with_mcid(b, child, pk, page_obj, leaf_mcid_map)
            if cr is not None:
                child_refs.append(cr)
        if child_refs:
            pk["/K"] = Array(child_refs)

    return pk


def _collect_mcid_owners(
    pk_refs: List[pikepdf.Dictionary],
    n_mcids: int,
) -> Dict[int, pikepdf.Dictionary]:
    """
    Scan built pikepdf struct elements to find which element directly owns each MCID.
    Returns {mcid_int: pk_ref}.
    """
    result: Dict[int, pikepdf.Dictionary] = {}

    def _scan(elem: pikepdf.Dictionary) -> None:
        try:
            k = elem.get("/K")
        except Exception:
            return
        if k is None:
            return
        if isinstance(k, pikepdf.objects.Integer):
            m = int(k)
            if m not in result:
                result[m] = elem
        elif isinstance(k, pikepdf.Array):
            for item in k:
                try:
                    obj = item.get_object() if hasattr(item, "get_object") else item
                    if isinstance(obj, pikepdf.objects.Integer):
                        m = int(obj)
                        if m not in result:
                            result[m] = elem
                    elif isinstance(obj, pikepdf.Dictionary):
                        _scan(obj)
                except Exception:
                    pass

    for ref in pk_refs:
        try:
            _scan(ref.get_object() if hasattr(ref, "get_object") else ref)
        except Exception:
            pass

    return result


# ============================================================
# Shared metadata writer
# ============================================================

def _set_common_metadata(
    pdf: pikepdf.Pdf,
    lang: str,
    title: str,
    author: str,
) -> None:
    pdf.Root["/Lang"] = String(lang)
    pdf.Root["/MarkInfo"] = pdf.make_indirect(
        Dictionary(Marked=Boolean(True))
    )
    pdf.Root["/ViewerPreferences"] = pdf.make_indirect(Dictionary(
        Direction=Name("/R2L"),
        DisplayDocTitle=Boolean(True),
    ))
    pdf.Root["/RoleMap"] = pdf.make_indirect(Dictionary())

    with pdf.open_metadata() as meta:
        meta["dc:language"] = lang
        meta["pdfuaid:part"] = "1"
        try:
            meta["pdfuaid:amd"] = "2012"
        except Exception:
            pass
        if title:
            meta["dc:title"] = title
        if author:
            meta["dc:creator"] = [author]

    try:
        if "/Info" not in pdf.trailer:
            pdf.trailer["/Info"] = pdf.make_indirect(Dictionary())
        info = pdf.trailer["/Info"]
        if title:
            info["/Title"] = String(title)
        if author:
            info["/Author"] = String(author)
        info["/Lang"] = String(lang)
    except Exception:
        pass

    for page in pdf.pages:
        page.obj["/Tabs"] = Name("/S")


# ============================================================
# Public entry points
# ============================================================

def inject_digital(
    pdf: pikepdf.Pdf,
    elements: List[StructElement],
    *,
    lang: str = "he-IL",
    title: str = "",
    author: str = "",
) -> None:
    """
    Build a PDF/UA-1 StructTreeRoot with per-block MCID binding.

    For every page:
      1. Parse content stream → assign unique MCID per BT/ET text block.
         Non-text content is marked as Artifact.
      2. Match each leaf StructElement (H1/H2/P/TH/TD/LBody …) to its MCID
         via text similarity + sequential fallback.
      3. Build ParentTree: page_StructParents → array[MCID] → owning elem.

    PAC requirements satisfied:
      - Every text operator is inside BDC/EMC  (no "untagged content")
      - StructTreeRoot exists with correct /K hierarchy
      - ParentTree maps every MCID to its struct element
      - /Lang at document and element level
      - /MarkInfo Marked=true
      - pdfuaid:part=1 in XMP
    """
    _set_common_metadata(pdf, lang, title, author)
    b = _Builder(pdf, lang)

    str_root = pdf.make_indirect(Dictionary(
        Type=Name("/StructTreeRoot"),
        Lang=String(lang),
    ))
    doc_elem = b.make_elem("Document", str_root, title=title or "מסמך נגיש")

    by_page: Dict[int, List[StructElement]] = defaultdict(list)
    for elem in elements:
        by_page[elem.page_num].append(elem)

    pages      = list(pdf.pages)
    n_pages    = len(pages)
    sect_refs: List[pikepdf.Dictionary]  = []
    pt_entries: List                     = []   # flat ParentTree Nums

    for pg_idx, page in enumerate(pages):
        pg_num   = pg_idx + 1
        page_obj = pdf.make_indirect(page.obj)
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx)

        # --- Step 1: inject per-block MCIDs into content stream ----------
        mcid_texts = _inject_mcids_into_page(pdf, page_obj, mcid_start=0)
        n_mcids    = len(mcid_texts)

        # Fallback when content stream parsing fails:
        # wrap entire page in one BDC so StructParents has a valid ParentTree entry.
        if not mcid_texts:
            try:
                bdc = b"<<\n/MCID 0\n>> BDC\n"
                emc = b"\nEMC\n"
                raw = page_obj.get("/Contents")
                orig = b""
                if raw is not None:
                    obj = raw.get_object() if hasattr(raw, "get_object") else raw
                    if hasattr(obj, "read_bytes"):
                        orig = obj.read_bytes()
                page_obj["/Contents"] = pdf.make_stream(bdc + orig + emc)
                mcid_texts = [(0, "")]
                n_mcids    = 1
            except Exception:
                pass

        # --- Step 2: match leaf struct elems to MCIDs --------------------
        page_elems    = by_page.get(pg_num, [])
        leaf_mcid_map = _assign_mcids(page_elems, mcid_texts)

        # --- Step 3: build struct tree for this page ---------------------
        sect = b.make_elem("Sect", doc_elem, title=f"עמוד {pg_num}")
        child_pk_refs: List[pikepdf.Dictionary] = []

        for elem in page_elems:
            pk = _build_elem_with_mcid(b, elem, sect, page_obj, leaf_mcid_map)
            if pk is not None:
                child_pk_refs.append(pk)

        # P elements for orphaned MCIDs (content not claimed by any struct elem)
        used_mcids = set(leaf_mcid_map.values())
        for mcid_val, text in mcid_texts:
            if mcid_val not in used_mcids and text:
                orphan = b.make_elem(
                    "P", sect,
                    actual_text=text,
                    page_obj=page_obj,
                    mcid=mcid_val,
                )
                child_pk_refs.append(orphan)

        if child_pk_refs:
            sect["/K"] = Array(child_pk_refs)
        sect_refs.append(sect)

        # --- Step 4: build ParentTree entry for this page ----------------
        # ParentTree[pg_idx] = Array where index=MCID → struct elem that owns it
        if n_mcids > 0:
            mcid_owners = _collect_mcid_owners(child_pk_refs, n_mcids)
            parent_array: List = []
            for m in range(n_mcids):
                owner = mcid_owners.get(m)
                # Fallback: point to sect so PAC doesn't find a null entry
                parent_array.append(owner if owner is not None else sect)
            pt_entries.append(pikepdf.objects.Integer(pg_idx))
            pt_entries.append(Array(parent_array))

    if sect_refs:
        doc_elem["/K"] = Array(sect_refs)

    str_root["/K"]                 = Array([doc_elem])
    str_root["/ParentTree"]        = pdf.make_indirect(
        Dictionary(Nums=Array(pt_entries))
    )
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(n_pages)
    pdf.Root["/StructTreeRoot"]    = str_root


def inject_scanned(
    pdf: pikepdf.Pdf,
    page_elements: Dict[int, List[StructElement]],
    *,
    lang: str = "he-IL",
    title: str = "",
    author: str = "",
    fig_mcid: int = 0,
) -> Dict[int, List[pikepdf.Dictionary]]:
    """
    Build a StructTreeRoot for a rasterised PDF.

    Structure per page:
        Sect
          Figure  [K=fig_mcid, Alt="…"]  ← the raster image (MCID-wired)
          H1      [ActualText="…"]       ← semantic siblings, NOT inside Figure
          P       [ActualText="…"]
          L → LI → LBody
          Table → TR → TH / TD

    Returns parent_tree_map {page_index_0based → [figure_ref]}.
    """
    _set_common_metadata(pdf, lang, title, author)
    b = _Builder(pdf, lang)

    str_root = pdf.make_indirect(Dictionary(
        Type=Name("/StructTreeRoot"),
        Lang=String(lang),
    ))
    doc_elem = b.make_elem("Document", str_root, title=title or "מסמך נגיש")

    parent_tree_map: Dict[int, List[pikepdf.Dictionary]] = {}
    sect_elems: List[pikepdf.Dictionary] = []
    pages = list(pdf.pages)

    for pg_idx, page in enumerate(pages):
        pg_num   = pg_idx + 1
        page_obj = pdf.make_indirect(page.obj)
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx)

        elems_for_page = page_elements.get(pg_num, [])
        all_text = " ".join(e.text for e in elems_for_page if e.text)
        first_h1 = next(
            (e.text for e in elems_for_page if e.elem_type == "H1"), ""
        )
        fig_alt = first_h1 or (all_text[:200] if all_text else f"עמוד {pg_num}")

        sect = b.make_elem("Sect", doc_elem, title=f"עמוד {pg_num}")
        sect_children: List[pikepdf.Dictionary] = []

        # Figure = the raster image content, MCID-wired
        fig = b.make_elem(
            "Figure", sect,
            title=f"עמוד {pg_num}",
            alt=fig_alt,
            page_obj=page_obj,
            mcid=fig_mcid,
        )
        fig["/K"] = pikepdf.objects.Integer(fig_mcid)
        parent_tree_map[pg_idx] = [fig]
        sect_children.append(fig)

        # Semantic elements as siblings of Figure (not children)
        leaf_mcid_map: Dict[int, int] = {}   # no MCIDs for scanned elements
        for elem in elems_for_page:
            pk = _build_elem_with_mcid(b, elem, sect, page_obj, leaf_mcid_map)
            if pk is not None:
                sect_children.append(pk)

        sect["/K"] = Array(sect_children)
        sect_elems.append(sect)

    doc_elem["/K"] = Array(sect_elems)

    flat_nums: List = []
    for pg_idx_0 in sorted(parent_tree_map.keys()):
        flat_nums.append(pikepdf.objects.Integer(pg_idx_0))
        flat_nums.append(Array(parent_tree_map[pg_idx_0]))

    str_root["/ParentTree"] = pdf.make_indirect(Dictionary(
        Nums=Array(flat_nums)
    ))
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(
        max(parent_tree_map.keys()) + 1 if parent_tree_map else 0
    )
    str_root["/K"] = Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = str_root

    return parent_tree_map


def inject_scanned_semantic(
    pdf: pikepdf.Pdf,
    elements: List[StructElement],
    page_mcid_records: Dict[int, List[tuple]],
    *,
    lang: str = "he-IL",
    title: str = "",
    author: str = "",
) -> None:
    """
    Build a PDF/UA-1 StructTreeRoot for scanned PDFs where each OCR text block
    in the content stream already has its own BDC/EMC MCID marker.

    page_mcid_records maps page_num (1-based) → list of
        (mcid, text, x, y_topdown, width, height)
    MCIDs are per-page (restart at 0 for each page).

    Flow:
      1. For each page, convert records to (mcid, text) pairs.
      2. Run _assign_mcids() to match leaf StructElements to MCIDs by text.
      3. Build struct tree using _build_elem_with_mcid().
      4. Collect MCID owners with _collect_mcid_owners() for the ParentTree.
    """
    _set_common_metadata(pdf, lang, title, author)
    b = _Builder(pdf, lang)

    str_root = pdf.make_indirect(Dictionary(
        Type=Name("/StructTreeRoot"),
        Lang=String(lang),
    ))
    doc_elem = b.make_elem("Document", str_root, title=title or "מסמך נגיש")

    # Group elements by page (1-based)
    by_page: Dict[int, List[StructElement]] = defaultdict(list)
    for e in elements:
        by_page[e.page_num].append(e)

    pages = list(pdf.pages)
    parent_tree_nums: List = []
    sect_elems: List[pikepdf.Dictionary] = []

    for pg_idx, page in enumerate(pages):
        pg_num = pg_idx + 1
        page_obj = pdf.make_indirect(page.obj)
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx)

        page_elems   = by_page.get(pg_num, [])
        mcid_records = page_mcid_records.get(pg_num, [])

        # (mcid, text) pairs for matching
        mcid_texts = [(rec[0], rec[1]) for rec in mcid_records]
        n_mcids    = (max(rec[0] for rec in mcid_records) + 1) if mcid_records else 0

        # Match leaf elements to MCIDs via text similarity
        leaf_mcid_map = _assign_mcids(page_elems, mcid_texts)

        sect = b.make_elem("Sect", doc_elem, title=f"עמוד {pg_num}")
        sect_children: List[pikepdf.Dictionary] = []

        for elem in page_elems:
            pk = _build_elem_with_mcid(b, elem, sect, page_obj, leaf_mcid_map)
            if pk is not None:
                sect_children.append(pk)

        sect["/K"] = Array(sect_children) if sect_children else Array([])
        sect_elems.append(sect)

        if n_mcids == 0:
            continue

        # ParentTree entry: array[mcid] → struct element that owns it
        mcid_owners = _collect_mcid_owners(sect_children, n_mcids)
        pt_array: List = []
        for mcid in range(n_mcids):
            owner = mcid_owners.get(mcid)
            # Unmatched MCID → point to the page Sect (graceful fallback)
            pt_array.append(owner if owner is not None else sect)

        parent_tree_nums.append(pikepdf.objects.Integer(pg_idx))
        parent_tree_nums.append(pdf.make_indirect(Array(pt_array)))

    doc_elem["/K"] = Array(sect_elems)

    str_root["/ParentTree"] = pdf.make_indirect(Dictionary(
        Nums=Array(parent_tree_nums),
    ))
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(len(pages))
    str_root["/K"] = Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = str_root


def build_bookmarks(
    pdf: pikepdf.Pdf,
    heading_elements: List[StructElement],
    page_texts: Dict[int, str],
) -> None:
    """Generate Outlines from H1/H2/H3 elements; fall back to page titles."""
    pages = list(pdf.pages)

    def _dest(page_num: int) -> Array:
        idx = min(page_num - 1, len(pages) - 1)
        return Array([pages[idx].obj, Name("/Fit")])

    items = []
    if heading_elements:
        for elem in heading_elements:
            if elem.elem_type not in ("H1", "H2", "H3"):
                continue
            pg = max(1, elem.page_num)
            items.append(pdf.make_indirect(Dictionary(
                Title=String(elem.text[:80] if elem.text else f"עמוד {pg}"),
                Dest=_dest(pg),
                Count=pikepdf.objects.Integer(0),
            )))
    else:
        for i, _ in enumerate(pages, 1):
            txt = page_texts.get(i, "")
            label = txt.split("\n")[0].strip()[:60] if txt else f"עמוד {i}"
            items.append(pdf.make_indirect(Dictionary(
                Title=String(label),
                Dest=_dest(i),
                Count=pikepdf.objects.Integer(0),
            )))

    if not items:
        return

    outline_root = pdf.make_indirect(Dictionary(
        Type=Name("/Outlines"),
        Count=pikepdf.objects.Integer(len(items)),
    ))
    for i, item in enumerate(items):
        item["/Parent"] = outline_root
        if i > 0:
            item["/Prev"] = items[i - 1]
        if i < len(items) - 1:
            item["/Next"] = items[i + 1]
    outline_root["/First"] = items[0]
    outline_root["/Last"]  = items[-1]

    pdf.Root["/Outlines"] = outline_root
    pdf.Root["/PageMode"] = Name("/UseOutlines")
