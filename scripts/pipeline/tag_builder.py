"""
tag_builder.py — Inject a PDF/UA-1 compliant StructTreeRoot into a pikepdf PDF.

Two public entry points:

  inject_digital(pdf, elements, lang, title, author)
    For digital (born-digital) PDFs that already have text in content streams.
    Builds struct elements with ActualText (no MCID wiring).
    Replaces any existing StructTreeRoot.

  inject_scanned(pdf, elements, page_mcid_map, lang, title, author)
    For PDFs rebuilt from rasterised pages (each page = one Figure MCID).
    Wires struct elements to content-stream MCIDs via ParentTree.

Both functions operate in-place on the pikepdf.Pdf object.
Caller is responsible for saving.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pikepdf
from pikepdf import Array, Boolean, Dictionary, Name, String

from .models import StructElement, HEADING_TYPES, TABLE_TYPES, LIST_TYPES

# PDF/UA attribute owners
_O_TABLE = Name("/Table")
_O_LIST  = Name("/List")
_O_LAYOUT= Name("/Layout")


class _Builder:
    """Internal helper: creates pikepdf indirect Dictionary objects."""

    def __init__(self, pdf: pikepdf.Pdf, lang: str):
        self.pdf  = pdf
        self.lang = lang

    # ------------------------------------------------------------------
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
        # Language on every element (PDF/UA §7.3 — inheritable, but explicit is safer)
        d["/Lang"] = String(self.lang)

        if actual_text:
            d["/ActualText"] = String(actual_text)
        if alt:
            d["/Alt"] = String(alt)
        if title:
            d["/T"] = String(title)

        # Wire to content stream via MCID
        if mcid is not None and page_obj is not None:
            d["/K"] = pikepdf.objects.Integer(mcid)
            d["/Pg"] = page_obj

        return self.pdf.make_indirect(d)

    # ------------------------------------------------------------------
    def set_th_attrs(self, elem: pikepdf.Dictionary, scope: str = "Col"):
        """Add /A attribute dict for TH → Scope."""
        attrs = self.pdf.make_indirect(Dictionary())
        attrs["/O"] = _O_TABLE
        attrs["/Scope"] = Name(f"/{scope}")
        elem["/A"] = attrs

    # ------------------------------------------------------------------
    def build_struct_elem(
        self,
        se: StructElement,
        parent_ref,
        *,
        page_obj=None,
        mcid: Optional[int] = None,
    ) -> Optional[pikepdf.Dictionary]:
        """
        Recursively convert a StructElement tree → pikepdf indirect objects.
        Returns the root pikepdf Dictionary (already make_indirect'd).
        """
        stype = se.elem_type
        # Coerce unknown types to P
        if stype not in (HEADING_TYPES | TABLE_TYPES | LIST_TYPES |
                         {"Document", "Sect", "Div", "Part", "Art",
                          "P", "Figure", "Caption", "BlockQuote",
                          "Span", "Link", "Note", "Reference",
                          "TOC", "TOCI", "Index", "Formula"}):
            stype = "P"

        text    = se.text.strip()
        use_mcid = se.mcid if se.mcid is not None else mcid

        pk_elem = self.make_elem(
            stype, parent_ref,
            actual_text=text if text else "",
            alt=text if text and stype == "Figure" else "",
            page_obj=page_obj,
            mcid=use_mcid,
        )

        # TH scope attribute
        if stype == "TH":
            scope = se.attrs.get("Scope", "Col")
            self.set_th_attrs(pk_elem, scope)

        # Children
        if se.children:
            child_refs = []
            for child in se.children:
                cr = self.build_struct_elem(child, pk_elem, page_obj=page_obj)
                if cr is not None:
                    child_refs.append(cr)
            if child_refs:
                pk_elem["/K"] = Array(child_refs)

        return pk_elem


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _set_common_metadata(
    pdf: pikepdf.Pdf,
    lang: str,
    title: str,
    author: str,
):
    """Apply /Lang, /MarkInfo, ViewerPrefs, XMP, docinfo."""
    pdf.Root["/Lang"] = String(lang)
    pdf.Root["/MarkInfo"] = pdf.make_indirect(
        Dictionary(Marked=Boolean(True))
    )
    pdf.Root["/ViewerPreferences"] = pdf.make_indirect(Dictionary(
        Direction=Name("/R2L"),
        DisplayDocTitle=Boolean(True),
    ))
    # Blank RoleMap (we only use standard structure types)
    pdf.Root["/RoleMap"] = pdf.make_indirect(Dictionary())

    # XMP metadata
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

    # DocInfo
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

    # Per-page Tabs
    for page in pdf.pages:
        page.obj["/Tabs"] = Name("/S")


def inject_digital(
    pdf: pikepdf.Pdf,
    elements: List[StructElement],
    *,
    lang: str = "he-IL",
    title: str = "",
    author: str = "",
):
    """
    Build a full StructTreeRoot from *elements* and attach it to *pdf*.

    For digital PDFs: struct elements carry ActualText (no MCID references).
    This is valid per PDF/UA spec §14.8.4.4 when content streams do not
    have BDC markers aligned to the new struct tree.

    PAC may report "content not tagged" warnings for such documents —
    that is acceptable when the original PDF has no tagging infrastructure.
    """
    _set_common_metadata(pdf, lang, title, author)
    b = _Builder(pdf, lang)

    str_root = pdf.make_indirect(Dictionary(
        Type=Name("/StructTreeRoot"),
        Lang=String(lang),
    ))

    doc_elem = b.make_elem("Document", str_root, title=title or "מסמך נגיש")

    child_refs = []
    # Group elements by page into Sect containers
    current_page: Optional[int] = None
    current_sect: Optional[pikepdf.Dictionary] = None
    sect_children: List[pikepdf.Dictionary] = []

    def flush_sect():
        nonlocal current_sect, sect_children
        if current_sect is not None and sect_children:
            current_sect["/K"] = Array(sect_children)
            child_refs.append(current_sect)
        current_sect = None
        sect_children = []

    for elem in elements:
        if elem.page_num != current_page:
            flush_sect()
            current_page = elem.page_num
            pg_label = f"עמוד {elem.page_num}" if elem.page_num else "מסמך"
            current_sect = b.make_elem("Sect", doc_elem, title=pg_label)

        pk = b.build_struct_elem(elem, current_sect)
        if pk is not None:
            sect_children.append(pk)

    flush_sect()

    if child_refs:
        doc_elem["/K"] = Array(child_refs)

    str_root["/K"] = Array([doc_elem])
    # ParentTree is required by spec but empty is valid for no-MCID structure
    str_root["/ParentTree"] = pdf.make_indirect(Dictionary(
        Nums=Array([])
    ))
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(0)
    pdf.Root["/StructTreeRoot"] = str_root


def inject_scanned(
    pdf: pikepdf.Pdf,
    page_elements: Dict[int, List[StructElement]],  # page_num → elements
    *,
    lang: str = "he-IL",
    title: str = "",
    author: str = "",
    fig_mcid: int = 0,   # MCID reserved for the Figure (scanned image) on each page
) -> Dict[int, List[pikepdf.Dictionary]]:
    """
    Build a StructTreeRoot for a rasterised PDF where each page is one Figure
    with MCID *fig_mcid*.

    Returns parent_tree_map: {page_index_0based → [struct_elem_ref, ...]}
    so the caller can wire the ParentTree Nums array.

    This function does NOT patch content streams — do that separately
    (patch_stream in build_accessible_pdf.py handles BDC/EMC injection).
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
        pg_num = pg_idx + 1
        page_obj = pdf.make_indirect(page.obj)
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx)

        media = page_obj.get("/MediaBox")
        pw = float(media[2]) if media else 595.0
        ph = float(media[3]) if media else 842.0

        sect = b.make_elem("Sect", doc_elem, title=f"עמוד {pg_num}")
        children: List[pikepdf.Dictionary] = []

        elems_for_page = page_elements.get(pg_num, [])

        if not elems_for_page:
            # Bare Figure — no semantic structure available
            fig = b.make_elem("Figure", sect,
                              title=f"עמוד {pg_num}",
                              alt=f"עמוד {pg_num}",
                              page_obj=page_obj, mcid=fig_mcid)
            parent_tree_map[pg_idx] = [fig]
            children.append(fig)
        else:
            # Wrap page in one Figure that carries all content
            # (WCAG 1.1.1: Figure with ActualText for screen readers)
            all_text = " ".join(e.text for e in elems_for_page if e.text)
            first_h1 = next(
                (e.text for e in elems_for_page if e.elem_type == "H1"), ""
            )
            fig_alt = first_h1 or (all_text[:200] if all_text else f"עמוד {pg_num}")

            fig = b.make_elem("Figure", sect,
                              title=f"עמוד {pg_num}",
                              alt=fig_alt,
                              actual_text=all_text[:500] if all_text else f"עמוד {pg_num}",
                              page_obj=page_obj, mcid=fig_mcid)
            parent_tree_map[pg_idx] = [fig]
            children.append(fig)

            # Embed semantic sub-structure inside Figure
            # (nested elements without MCID — valid for content accessibility)
            sub_refs: List[pikepdf.Dictionary] = []
            for elem in elems_for_page:
                pk = b.build_struct_elem(elem, fig)
                if pk is not None:
                    sub_refs.append(pk)
            if sub_refs:
                # Figure's /K: integer MCID for content wiring, plus sub-elems
                fig["/K"] = Array(
                    [pikepdf.objects.Integer(fig_mcid)] + sub_refs
                )
            else:
                fig["/K"] = pikepdf.objects.Integer(fig_mcid)

        sect["/K"] = Array(children)
        sect_elems.append(sect)

    doc_elem["/K"] = Array(sect_elems)

    # Build ParentTree Nums array
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


def build_bookmarks(
    pdf: pikepdf.Pdf,
    heading_elements: List[StructElement],
    page_texts: Dict[int, str],
):
    """
    Generate an Outlines (bookmark) tree from H1/H2/H3 elements.
    Falls back to page-based bookmarks when no headings exist.
    """
    pages = list(pdf.pages)

    def _page_dest(page_num: int):
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
                Dest=_page_dest(pg),
                Count=pikepdf.objects.Integer(0),
            )))
    else:
        for i, page in enumerate(pages, 1):
            txt = page_texts.get(i, "")
            label = (txt.split("\n")[0].strip()[:60] if txt else f"עמוד {i}")
            items.append(pdf.make_indirect(Dictionary(
                Title=String(label),
                Dest=_page_dest(i),
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
