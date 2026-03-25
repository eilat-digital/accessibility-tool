#!/usr/bin/env python3
"""
build_accessible_pdf.py — v3
"""

import argparse
import json
import os
import sys
import tempfile


def ensure_deps():
    missing = []
    for pkg, imp in [("reportlab", "reportlab"), ("pikepdf", "pikepdf"),
                     ("pdf2image", "pdf2image"), ("Pillow", "PIL")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"חסרות תלויות: {', '.join(missing)}")
        sys.exit(1)


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
]


def find_embedded_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("AccessFont", fp))
                print(f"   פונט: {os.path.basename(fp)}")
                return "AccessFont"
            except Exception:
                continue
    return None


def extract_pages(input_pdf, pages_dir, dpi=200, batch_size=20):
    from pdf2image import convert_from_path, pdfinfo_from_path
    print(f"מחלץ עמודים ({dpi} DPI)...")
    try:
        total_pages = pdfinfo_from_path(input_pdf)["Pages"]
    except Exception:
        total_pages = None

    paths = []
    if total_pages:
        for start in range(1, total_pages + 1, batch_size):
            end = min(start + batch_size - 1, total_pages)
            batch = convert_from_path(
                input_pdf, dpi=dpi,
                first_page=start, last_page=end,
                thread_count=1
            )
            for i, img in enumerate(batch, start):
                p = os.path.join(pages_dir, f"page_{i:04d}.jpg")
                img.save(p, "JPEG", quality=85)
                paths.append(p)
            del batch  # free memory before next batch
    else:
        # fallback for PDFs where page count can't be determined
        batch = convert_from_path(input_pdf, dpi=dpi, thread_count=1)
        for i, img in enumerate(batch, 1):
            p = os.path.join(pages_dir, f"page_{i:04d}.jpg")
            img.save(p, "JPEG", quality=85)
            paths.append(p)

    print(f"{len(paths)} עמודים")
    return paths


def run_ocr(page_paths, lang_code="he-IL"):
    try:
        import pytesseract
        from PIL import Image
        pytesseract.get_tesseract_version()  # verify binary exists
    except ImportError:
        print("  OCR: pytesseract לא מותקן — ללא שכבת טקסט (WCAG 1.4.5 יכשל)")
        return {}
    except Exception:
        print("  OCR: Tesseract לא נמצא — ללא שכבת טקסט (WCAG 1.4.5 יכשל)")
        return {}

    lang_map = {"he-IL": "heb+eng", "he": "heb+eng", "ar": "ara+heb", "en-US": "eng", "en": "eng"}
    tess = lang_map.get(lang_code, "heb+eng")
    texts = {}
    print(f"  OCR: מריץ Tesseract ({tess}) על {len(page_paths)} עמודים...")
    for i, path in enumerate(page_paths, 1):
        try:
            img = Image.open(path)
            texts[i] = pytesseract.image_to_string(img, lang=tess, config="--psm 6").strip()
        except Exception as e:
            print(f"  OCR עמוד {i}: {e}")
            texts[i] = ""
    extracted = sum(1 for t in texts.values() if t)
    print(f"  OCR: חולץ טקסט מ-{extracted}/{len(page_paths)} עמודים")
    return texts


def _make_stamp_stream(pw, ph):
    """Small, clean accessibility badge — bottom-right corner.
    6mm radius disc: solid blue fill + white checkmark stroke.
    Wrapped in Artifact so PAC does not flag it as untagged content."""
    mm = 2.8346
    R  = 6.0 * mm   # 6 mm radius — small and unobtrusive
    M  = 4.0 * mm   # margin from edge
    cx = pw - M - R
    cy = M + R
    bbox = f"[{cx-R:.3f} {cy-R:.3f} {cx+R:.3f} {cy+R:.3f}]"

    def circ(x, y, r):
        k = r * 0.5523
        return (
            f"{x:.3f} {y+r:.3f} m "
            f"{x+k:.3f} {y+r:.3f} {x+r:.3f} {y+k:.3f} {x+r:.3f} {y:.3f} c "
            f"{x+r:.3f} {y-k:.3f} {x+k:.3f} {y-r:.3f} {x:.3f} {y-r:.3f} c "
            f"{x-k:.3f} {y-r:.3f} {x-r:.3f} {y-k:.3f} {x-r:.3f} {y:.3f} c "
            f"{x-r:.3f} {y+k:.3f} {x-k:.3f} {y+r:.3f} {x:.3f} {y+r:.3f} c h"
        )

    lw = R * 0.18   # checkmark stroke width

    ops = "\n".join([
        f"/Artifact <</Type /Layout /Attached [/Bottom /Right] /BBox {bbox}>> BDC",
        "q",
        # solid dark-blue disc
        "0.102 0.306 0.541 rg",
        circ(cx, cy, R), "f",
        # white checkmark (✓): left-bottom → dip → upper-right
        "1 1 1 RG",
        f"{lw:.3f} w", "1 J 1 j",
        (f"{cx - R*0.38:.3f} {cy + R*0.05:.3f} m "
         f"{cx - R*0.10:.3f} {cy - R*0.32:.3f} l "
         f"{cx + R*0.42:.3f} {cy + R*0.38:.3f} l"),
        "S",
        "Q",
        "EMC",
    ])
    return ops.encode()


def apply_stamp_to_pdf(pdf_path):
    """Overlay accessibility badge on every page of any PDF. In-place."""
    import pikepdf, shutil, os
    from pikepdf import Stream as PdfStream, Array as PdfArray

    tmp = pdf_path + ".stamp_tmp"
    try:
        with pikepdf.open(pdf_path) as pdf:
            for page in pdf.pages:
                mb = page.obj.get("/MediaBox")
                pw = float(mb[2]) if mb else 595.0
                ph = float(mb[3]) if mb else 842.0
                s = pdf.make_indirect(PdfStream(pdf, _make_stamp_stream(pw, ph)))
                existing = page.obj.get("/Contents")
                if existing is None:
                    page.obj["/Contents"] = s
                elif isinstance(existing, pikepdf.Array):
                    existing.append(s)
                else:
                    page.obj["/Contents"] = PdfArray([existing, s])
            pdf.save(tmp)
        shutil.move(tmp, pdf_path)
        print("   חותמת נגישות הוספה")
    except Exception as e:
        print(f"   stamp warning: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def describe_pages_with_ai(page_paths, lang_code="he-IL"):
    """Use Claude Vision (Haiku) to describe each page for WCAG 1.1.1 alt text.
    Only runs when ANTHROPIC_API_KEY env var is set. Returns {page_num: description}."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    try:
        import anthropic
        import base64
        import io
        from PIL import Image as PILImage
    except ImportError:
        print("  AI: חסרות תלויות (anthropic/Pillow)")
        return {}

    lang_map = {"he-IL": "בעברית", "he": "בעברית", "ar": "بالعربية",
                "en-US": "in English", "en": "in English"}
    lang_word = lang_map.get(lang_code, "בעברית")

    client = anthropic.Anthropic()
    descriptions = {}
    print(f"  AI: מתאר {len(page_paths)} עמודים {lang_word} (WCAG 1.1.1)...")

    for i, path in enumerate(page_paths, 1):
        try:
            img = PILImage.open(path)
            img.thumbnail((800, 800))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=75)
            data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text",
                     "text": (f"תאר {lang_word} את תוכן הדף הזה בקצרה (2-3 משפטים), "
                              "כולל תמונות וגרפיקה, לצורך נגישות לאנשים עם לקות ראייה.")}
                ]}]
            )
            descriptions[i] = resp.content[0].text.strip()
            print(f"  AI עמוד {i}: ✓")
        except Exception as e:
            print(f"  AI עמוד {i}: {e}")

    return descriptions


def detect_pdf_type(input_path):
    try:
        import pikepdf
        from pdfminer.high_level import extract_text
        total = 0
        pages_text = {}
        pdf = pikepdf.open(input_path)
        for i in range(len(pdf.pages)):
            try:
                txt = extract_text(input_path, page_numbers=[i]) or ''
                if len(txt.strip()) > 10:
                    pages_text[i+1] = txt.strip()
                    total += len(txt.strip())
            except Exception:
                pass
        pdf.close()
        if total > 50:
            print(f"  זוהה: PDF ממחשב ({total} תווים)")
            return 'digital', pages_text
        print("  זוהה: PDF סרוק")
        return 'scanned', {}
    except Exception:
        return 'scanned', {}


def build_image_pdf(page_paths, page_texts, output_path, stamp=False):
    from reportlab.pdfgen import canvas
    from PIL import Image as PILImage

    print(f"בונה PDF ({len(page_paths)} עמודים)...")
    font_name = find_embedded_font() or "Helvetica"

    c = canvas.Canvas(output_path)
    for i, img_path in enumerate(page_paths, 1):
        img = PILImage.open(img_path)
        iw, ih = img.size
        dpi_x = img.info.get("dpi", (200, 200))[0] or 200
        pw = iw * 72.0 / dpi_x
        ph = ih * 72.0 / dpi_x
        c.setPageSize((pw, ph))
        c.drawImage(img_path, 0, 0, width=pw, height=ph)

        text_content = page_texts.get(i, "")
        if text_content:
            try:
                txt = c.beginText(8, ph - 16)
                txt.setTextRenderMode(3)
                txt.setFont(font_name, 9)
                for line in text_content.split("\n")[:80]:
                    txt.textLine(line[:200])
                c.drawText(txt)
            except Exception:
                pass

        c.showPage()
    c.save()
    print(f"PDF בסיסי: {output_path}")


def patch_stream(raw, p_mcid, page_w, page_h):
    bbox = f"[0 0 {page_w:.1f} {page_h:.1f}]".encode()
    bt_pos = -1
    for needle in (b"\nBT\n", b"\nBT "):
        pos = raw.find(needle)
        if pos >= 0 and (bt_pos < 0 or pos < bt_pos):
            bt_pos = pos + 1
    if bt_pos < 0:
        return (b"/Artifact <</Type /Layout /BBox " + bbox + b">> BDC\n" +
                raw + b"\n" + b"EMC\n")
    last_Q = raw.rfind(b"\nQ\n", 0, bt_pos)
    search_from = last_Q if last_Q >= 0 else 0
    unclosed_q = raw.find(b"\nq\n", search_from, bt_pos)
    split_pos = (unclosed_q + 1) if unclosed_q >= 0 else bt_pos
    image_part = raw[:split_pos].rstrip(b" \n")
    text_part = raw[split_pos:]
    return (b"/Artifact <</Type /Layout /BBox " + bbox + b">> BDC\n" +
            image_part + b"\n" + b"EMC\n" +
            b"/P <</MCID " + str(p_mcid).encode() + b">> BDC\n" +
            text_part.strip(b" \n") + b"\n" + b"EMC\n")


def add_bookmarks(pdf, pages, page_titles, page_texts):
    import pikepdf
    from pikepdf import Dictionary, Array, Name, String
    n = len(pages)
    items = []
    for pg_idx, page in enumerate(pages, 1):
        title = (page_titles.get(str(pg_idx)) or page_titles.get(pg_idx) or
                 (page_texts.get(pg_idx, "").split("\n")[0].strip() if page_texts.get(pg_idx) else "") or
                 f"\u05e2\u05de\u05d5\u05d3 {pg_idx}")
        item = pdf.make_indirect(Dictionary(
            Title=String(title),
            Dest=Array([page.obj, Name("/Fit")]),
            Count=pikepdf.objects.Integer(0),
        ))
        items.append(item)
    outline_root = pdf.make_indirect(Dictionary(
        Type=Name("/Outlines"),
        Count=pikepdf.objects.Integer(n),
    ))
    for i, item in enumerate(items):
        item["/Parent"] = outline_root
        if i > 0: item["/Prev"] = items[i-1]
        if i < n-1: item["/Next"] = items[i+1]
    outline_root["/First"] = items[0]
    outline_root["/Last"] = items[-1]
    pdf.Root["/Outlines"] = outline_root
    pdf.Root["/PageMode"] = Name("/UseOutlines")


_WINANSI_TOUNICODE = b"""\
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def
/CMapName /Adobe-WinAnsi-UCS def
/CMapType 2 def
1 begincodespacerange <20> <FF> endcodespacerange
95 beginbfchar
<20> <0020> <21> <0021> <22> <0022> <23> <0023> <24> <0024>
<25> <0025> <26> <0026> <27> <0027> <28> <0028> <29> <0029>
<2A> <002A> <2B> <002B> <2C> <002C> <2D> <002D> <2E> <002E>
<2F> <002F> <30> <0030> <31> <0031> <32> <0032> <33> <0033>
<34> <0034> <35> <0035> <36> <0036> <37> <0037> <38> <0038>
<39> <0039> <3A> <003A> <3B> <003B> <3C> <003C> <3D> <003D>
<3E> <003E> <3F> <003F> <40> <0040> <41> <0041> <42> <0042>
<43> <0043> <44> <0044> <45> <0045> <46> <0046> <47> <0047>
<48> <0048> <49> <0049> <4A> <004A> <4B> <004B> <4C> <004C>
<4D> <004D> <4E> <004E> <4F> <004F> <50> <0050> <51> <0051>
<52> <0052> <53> <0053> <54> <0054> <55> <0055> <56> <0056>
<57> <0057> <58> <0058> <59> <0059> <5A> <005A> <5B> <005B>
<5C> <005C> <5D> <005D> <5E> <005E> <5F> <005F> <60> <0060>
<61> <0061> <62> <0062> <63> <0063> <64> <0064> <65> <0065>
<66> <0066> <67> <0067> <68> <0068> <69> <0069> <6A> <006A>
<6B> <006B> <6C> <006C> <6D> <006D> <6E> <006E> <6F> <006F>
<70> <0070> <71> <0071> <72> <0072> <73> <0073> <74> <0074>
<75> <0075> <76> <0076> <77> <0077> <78> <0078> <79> <0079>
<7A> <007A> <7B> <007B> <7C> <007C> <7D> <007D> <7E> <007E>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def fix_standard_font_encoding(pdf):
    import pikepdf
    from pikepdf import Stream
    fixed = set()
    for page in pdf.pages:
        resources = page.obj.get("/Resources", {})
        font_dict = resources.get("/Font", {})
        for fname, fref in font_dict.items():
            try:
                f = pdf.get_object(fref.objgen)
                if "/ToUnicode" not in f:
                    key = str(fref.objgen)
                    if key not in fixed:
                        f["/ToUnicode"] = pdf.make_indirect(Stream(pdf, _WINANSI_TOUNICODE))
                        fixed.add(key)
            except Exception:
                pass
    if fixed:
        print(f"   ToUnicode הוזרק ל-{len(fixed)} פונטים")


def add_metadata_only(input_pdf, output_pdf, lang="he-IL", title="מסמך נגיש"):
    """For digital PDFs: only add PDF/UA metadata — never touch StructTreeRoot or content streams."""
    import pikepdf
    from pikepdf import Dictionary, Name, String

    pdf = pikepdf.open(input_pdf)

    fix_standard_font_encoding(pdf)

    pdf.Root["/Lang"] = String(lang)
    pdf.Root["/ViewerPreferences"] = pdf.make_indirect(Dictionary(
        Direction=Name("/R2L"),
        DisplayDocTitle=pikepdf.objects.Boolean(True),
    ))

    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        meta["dc:language"] = lang
        try:
            meta["pdfuaid:part"] = "1"
        except Exception:
            pass
        try:
            meta["pdfuaid:amd"] = "2012"
        except Exception:
            pass

    try:
        if "/Info" not in pdf.trailer:
            pdf.trailer["/Info"] = pdf.make_indirect(Dictionary())
        pdf.trailer["/Info"]["/Title"] = String(title)
    except Exception:
        pass

    if "/MarkInfo" not in pdf.Root:
        pdf.Root["/MarkInfo"] = pdf.make_indirect(
            Dictionary(Marked=pikepdf.objects.Boolean(True))
        )

    for page in pdf.pages:
        page.obj["/Tabs"] = Name("/S")

    pdf.save(output_pdf)
    print(f"✅ PDF נגיש (metadata): {output_pdf}")


def add_pdfua_tags(input_pdf, output_pdf, lang="he-IL", title="\u05de\u05e1\u05de\u05da \u05e0\u05d2\u05d9\u05e9",
                   page_texts=None, page_titles=None, tables_info=None, pdf_type="scanned",
                   ai_descriptions=None):
    import pikepdf
    from pikepdf import Dictionary, Array, Name, String, Stream

    if page_texts is None: page_texts = {}
    if page_titles is None: page_titles = {}
    if tables_info is None: tables_info = {}
    if ai_descriptions is None: ai_descriptions = {}

    print("\u05de\u05d5\u05e1\u05d9\u05e3 \u05ea\u05d9\u05d5\u05d2 PDF/UA...")
    pdf = pikepdf.open(input_pdf)
    pages = list(pdf.pages)

    fix_standard_font_encoding(pdf)

    pdf.Root["/Lang"] = String(lang)
    pdf.Root["/ViewerPreferences"] = pdf.make_indirect(Dictionary(
        Direction=Name("/R2L"),
        DisplayDocTitle=pikepdf.objects.Boolean(True),
    ))

    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        meta["dc:language"] = lang
        # PDF/UA-1 identifier — required for WCAG 2.2 / PDF/UA compliance
        try:
            meta["pdfuaid:part"] = "1"
        except Exception:
            pass
        try:
            meta["pdfuaid:amd"] = "2012"
        except Exception:
            pass

    try:
        if "/Info" not in pdf.trailer:
            pdf.trailer["/Info"] = pdf.make_indirect(Dictionary())
        pdf.trailer["/Info"]["/Title"] = String(title)
    except Exception:
        pass

    pdf.Root["/MarkInfo"] = pdf.make_indirect(
        Dictionary(Marked=pikepdf.objects.Boolean(True))
    )

    # RoleMap: only needed for non-standard custom types.
    # H, H1-H6, Sect, Figure, P, Table, TR, TH, TD are all PDF standard types —
    # including them in RoleMap confuses PAC and causes 1.3 failures.
    pdf.Root["/RoleMap"] = pdf.make_indirect(Dictionary())

    parent_tree_map = {}
    P_MCID = 0

    def make_elem(stype, parent, title_text="", actual_text="", alt_text="", page_obj=None, mcid=None):
        d = Dictionary(Type=Name("/StructElem"), S=Name(f"/{stype}"), P=parent)
        if title_text: d["/T"] = String(title_text)
        if actual_text: d["/ActualText"] = String(actual_text)
        if alt_text: d["/Alt"] = String(alt_text)
        if mcid is not None and page_obj is not None:
            d["/K"] = pikepdf.objects.Integer(mcid)
            d["/Pg"] = page_obj
        return pdf.make_indirect(d)

    str_root = pdf.make_indirect(Dictionary(Type=Name("/StructTreeRoot"), Lang=String(lang)))
    doc_elem = make_elem("Document", str_root, title_text=title)
    str_root["/K"] = Array([doc_elem])
    sect_elems = []
    page_patch_info = []

    for pg_idx, page in enumerate(pages, 1):
        pg_idx_0 = pg_idx - 1
        page_obj = page.obj
        page_obj["/Tabs"] = Name("/S")
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx_0)

        media = page_obj.get("/MediaBox")
        pw = float(media[2]) if media else 595.0
        ph = float(media[3]) if media else 842.0

        page_text = page_texts.get(pg_idx, "")
        page_title = (page_titles.get(str(pg_idx)) or page_titles.get(pg_idx) or
                      (page_text.split("\n")[0].strip() if page_text else f"\u05e2\u05de\u05d5\u05d3 {pg_idx}"))

        ai_desc = ai_descriptions.get(pg_idx, "")
        sect = make_elem("Sect", doc_elem,
                         title_text=f"\u05e2\u05de\u05d5\u05d3 {pg_idx}",
                         alt_text=ai_desc if ai_desc else "")
        sect_elems.append(sect)
        children = []

        body_text = page_text.strip() if page_text else f"תוכן עמוד {pg_idx}"
        if pdf_type == 'digital':
            # For digital PDFs: do NOT add MCID — the original content stream
            # may already contain BDC markers with MCID 0, causing "MCID already
            # present" errors in PAC. Use ActualText/Alt only (no content ref).
            p = make_elem("P", sect, actual_text=body_text, alt_text=body_text)
        else:
            p = make_elem("P", sect,
                          actual_text=body_text,
                          alt_text=body_text,
                          page_obj=page_obj, mcid=P_MCID)
            parent_tree_map[pg_idx_0] = [p]
        children.append(p)

        for tbl_def in tables_info.get(str(pg_idx), tables_info.get(pg_idx, [])):
            summary = tbl_def.get("summary") or f"\u05d8\u05d1\u05dc\u05d4 \u05d1\u05e2\u05de\u05d5\u05d3 {pg_idx}"
            headers = tbl_def.get("headers", [])
            tbl = make_elem("Table", sect)
            tbl["/Summary"] = String(summary)
            tbl_children = []
            if headers:
                tr_h = make_elem("TR", tbl)
                th_list = []
                for hdr in headers:
                    th = make_elem("TH", tr_h, actual_text=hdr, alt_text=hdr)
                    th["/Scope"] = Name("/Col")
                    th_list.append(th)
                tr_h["/K"] = Array(th_list)
                tbl_children.append(tr_h)
            tbl["/K"] = Array(tbl_children)
            children.append(tbl)

        sect["/K"] = Array(children)
        page_patch_info.append((P_MCID, pw, ph))

    doc_elem["/K"] = Array(sect_elems)

    flat = []
    for pg_idx_0 in sorted(parent_tree_map.keys()):
        flat.append(pikepdf.objects.Integer(pg_idx_0))
        flat.append(Array(parent_tree_map[pg_idx_0]))
    str_root["/ParentTree"] = pdf.make_indirect(Dictionary(Nums=Array(flat)))
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(len(parent_tree_map))
    pdf.Root["/StructTreeRoot"] = str_root

    for pg_idx, page in enumerate(pages):
        if pdf_type == 'digital':
            continue  # Never touch digital PDF content streams — avoids MCID conflicts
        m_p, pw, ph = page_patch_info[pg_idx]
        try:
            raw_obj = page.obj.get("/Contents")
            if raw_obj is None: continue
            def get_bytes(obj):
                if hasattr(obj, "read_bytes"): return obj.read_bytes()
                if isinstance(obj, pikepdf.Array): return b"".join(get_bytes(x) for x in obj)
                return b""
            orig = get_bytes(raw_obj)
            new_data = patch_stream(orig, m_p, pw, ph)
            page.obj["/Contents"] = pdf.make_indirect(Stream(pdf, new_data))
        except Exception as e:
            print(f"   content stream עמוד {pg_idx + 1}: {e}")

    add_bookmarks(pdf, pages, page_titles, page_texts)
    pdf.save(output_pdf)
    print(f"\u2705 PDF \u05e0\u05d2\u05d9\u05e9: {output_pdf}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",        required=True)
    parser.add_argument("--output",       required=True)
    parser.add_argument("--lang",         default="he-IL")
    parser.add_argument("--title",        default="\u05de\u05e1\u05de\u05da \u05e0\u05d2\u05d9\u05e9")
    parser.add_argument("--dpi",          type=int, default=200)
    parser.add_argument("--stamp",        action="store_true")
    parser.add_argument("--ocr",          action="store_true")
    parser.add_argument("--force-ocr",    action="store_true")
    parser.add_argument("--text-json",    default=None)
    parser.add_argument("--page-titles",  default=None)
    parser.add_argument("--tables-json",  default=None)
    args = parser.parse_args()

    ensure_deps()

    page_texts = {}
    if args.text_json and os.path.exists(args.text_json):
        with open(args.text_json, encoding="utf-8") as f:
            page_texts = {int(k): v for k, v in json.load(f).items()}

    page_titles = {}
    if args.page_titles and os.path.exists(args.page_titles):
        with open(args.page_titles, encoding="utf-8") as f:
            page_titles = json.load(f)

    tables_info = {}
    if args.tables_json and os.path.exists(args.tables_json):
        with open(args.tables_json, encoding="utf-8") as f:
            tables_info = json.load(f)

    with tempfile.TemporaryDirectory() as tmpdir:
        pages_dir = os.path.join(tmpdir, "pages")
        os.makedirs(pages_dir)
        base_pdf = os.path.join(tmpdir, "base.pdf")

        # זיהוי אוטומטי: ממחשב vs סרוק
        pdf_type, existing_texts = detect_pdf_type(args.input)
        if not page_texts:
            page_texts = existing_texts if pdf_type == 'digital' else {}

        ai_descriptions = {}
        if pdf_type == 'digital' and not getattr(args, 'force_ocr', False):
            # WCAG 1.4.5: preserve original text — do NOT rasterize digital PDFs.
            # Converting to images would turn selectable text into image-of-text,
            # which fails WCAG 2.2 criterion 1.4.5 and breaks screen readers.
            import shutil
            shutil.copy2(args.input, base_pdf)
            print("  PDF ממחשב: שומר טקסט מקורי (WCAG 1.4.5)")
            # WCAG 1.1.1: describe page visuals with AI when API key is available
            if os.environ.get("ANTHROPIC_API_KEY"):
                ai_pages_dir = os.path.join(tmpdir, "ai_pages")
                os.makedirs(ai_pages_dir)
                ai_paths = extract_pages(args.input, ai_pages_dir, dpi=72)
                ai_descriptions = describe_pages_with_ai(ai_paths, lang_code=args.lang)
        else:
            # Scanned PDF: rasterize + optional OCR
            if not page_texts and args.ocr:
                page_paths_tmp = extract_pages(args.input, pages_dir, dpi=args.dpi)
                page_texts = run_ocr(page_paths_tmp, lang_code=args.lang)
            page_paths = extract_pages(args.input, pages_dir, dpi=args.dpi)
            build_image_pdf(page_paths, page_texts, base_pdf, stamp=args.stamp)

        if pdf_type == 'digital':
            # Digital PDFs already have a StructTreeRoot with MCIDs in content streams.
            # Replacing it breaks the MCID→struct mapping → 2984+ Content failures in PAC.
            # Only add PDF/UA metadata (XMP, Lang, ViewerPreferences) — leave structure intact.
            add_metadata_only(base_pdf, args.output, lang=args.lang, title=args.title)
        else:
            add_pdfua_tags(base_pdf, args.output,
                           lang=args.lang, title=args.title,
                           page_texts=page_texts,
                           page_titles=page_titles,
                           tables_info=tables_info,
                           pdf_type=pdf_type,
                           ai_descriptions=ai_descriptions)

        if args.stamp:
            apply_stamp_to_pdf(args.output)


if __name__ == "__main__":
    main()
