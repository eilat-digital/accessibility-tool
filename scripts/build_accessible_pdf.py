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


STAMP_PNG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "accessibility_stamp.png")

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


def _load_stamp_png(png_path, size=150):
    """Load pre-rendered stamp PNG. Returns None on failure."""
    try:
        from PIL import Image as PILImage
        import io
        img = PILImage.open(png_path).convert("RGBA")
        img.thumbnail((size, size), PILImage.LANCZOS)
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    except Exception as e:
        print(f"   PNG stamp: {e}")
        return None


def _make_image_xobject(pdf, png_bytes):
    """Create pikepdf Image XObject with transparency from PNG bytes."""
    import io, zlib
    from PIL import Image as PILImage
    import pikepdf

    img = PILImage.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size

    rgb_data  = zlib.compress(img.convert("RGB").tobytes())
    alpha_data = zlib.compress(img.split()[3].tobytes())

    smask = pdf.make_indirect(pikepdf.Stream(pdf, alpha_data))
    smask["/Type"]             = pikepdf.Name("/XObject")
    smask["/Subtype"]          = pikepdf.Name("/Image")
    smask["/Width"]            = pikepdf.objects.Integer(w)
    smask["/Height"]           = pikepdf.objects.Integer(h)
    smask["/ColorSpace"]       = pikepdf.Name("/DeviceGray")
    smask["/BitsPerComponent"] = pikepdf.objects.Integer(8)
    smask["/Filter"]           = pikepdf.Name("/FlateDecode")

    xobj = pdf.make_indirect(pikepdf.Stream(pdf, rgb_data))
    xobj["/Type"]             = pikepdf.Name("/XObject")
    xobj["/Subtype"]          = pikepdf.Name("/Image")
    xobj["/Width"]            = pikepdf.objects.Integer(w)
    xobj["/Height"]           = pikepdf.objects.Integer(h)
    xobj["/ColorSpace"]       = pikepdf.Name("/DeviceRGB")
    xobj["/BitsPerComponent"] = pikepdf.objects.Integer(8)
    xobj["/Filter"]           = pikepdf.Name("/FlateDecode")
    xobj["/SMask"]            = smask

    return xobj, w, h


def apply_stamp_to_pdf(pdf_path):
    """Overlay accessibility badge (SVG or fallback disc) on every page. In-place."""
    import pikepdf, shutil, os
    from pikepdf import Stream as PdfStream, Array as PdfArray, Dictionary

    mm = 2.8346
    STAMP_PTS = 16 * mm   # 16 mm — small but visible
    MARGIN    =  5 * mm

    # Load pre-rendered PNG stamp
    png_bytes = _load_stamp_png(STAMP_PNG_PATH) if os.path.exists(STAMP_PNG_PATH) else None

    tmp = pdf_path + ".stamp_tmp"
    try:
        with pikepdf.open(pdf_path) as pdf:
            xobj = None
            if png_bytes:
                xobj, _w, _h = _make_image_xobject(pdf, png_bytes)

            for page in pdf.pages:
                mb = page.obj.get("/MediaBox")
                pw = float(mb[2]) if mb else 595.0
                ph = float(mb[3]) if mb else 842.0

                x = pw - MARGIN - STAMP_PTS
                y = MARGIN
                bbox = f"[{x:.3f} {y:.3f} {x+STAMP_PTS:.3f} {y+STAMP_PTS:.3f}]"

                if xobj is not None:
                    # Add XObject to page resources
                    if "/Resources" not in page.obj:
                        page.obj["/Resources"] = pdf.make_indirect(Dictionary())
                    res = page.obj["/Resources"]
                    if "/XObject" not in res:
                        res["/XObject"] = pdf.make_indirect(Dictionary())
                    res["/XObject"]["/AccessStamp"] = xobj

                    stream_data = (
                        f"/Artifact <</Type /Layout /Attached [/Bottom /Right] /BBox {bbox}>> BDC\n"
                        f"q\n"
                        f"{STAMP_PTS:.3f} 0 0 {STAMP_PTS:.3f} {x:.3f} {y:.3f} cm\n"
                        f"/AccessStamp Do\n"
                        f"Q\nEMC\n"
                    ).encode()
                else:
                    # Fallback: simple teal disc (matches SVG color #0097b2)
                    R  = STAMP_PTS / 2
                    cx = x + R
                    cy = y + R
                    k  = R * 0.5523
                    lw = R * 0.18
                    stream_data = "\n".join([
                        f"/Artifact <</Type /Layout /Attached [/Bottom /Right] /BBox {bbox}>> BDC",
                        "q",
                        "0.0 0.592 0.698 rg",
                        (f"{cx:.3f} {cy+R:.3f} m "
                         f"{cx+k:.3f} {cy+R:.3f} {cx+R:.3f} {cy+k:.3f} {cx+R:.3f} {cy:.3f} c "
                         f"{cx+R:.3f} {cy-k:.3f} {cx+k:.3f} {cy-R:.3f} {cx:.3f} {cy-R:.3f} c "
                         f"{cx-k:.3f} {cy-R:.3f} {cx-R:.3f} {cy-k:.3f} {cx-R:.3f} {cy:.3f} c "
                         f"{cx-R:.3f} {cy+k:.3f} {cx-k:.3f} {cy+R:.3f} {cx:.3f} {cy+R:.3f} c h f"),
                        "1 1 1 RG", f"{lw:.3f} w 1 J 1 j",
                        (f"{cx-R*0.38:.3f} {cy+R*0.05:.3f} m "
                         f"{cx-R*0.10:.3f} {cy-R*0.32:.3f} l "
                         f"{cx+R*0.42:.3f} {cy+R*0.38:.3f} l S"),
                        "Q\nEMC",
                    ]).encode()

                s = pdf.make_indirect(PdfStream(pdf, stream_data))
                existing = page.obj.get("/Contents")
                if existing is None:
                    page.obj["/Contents"] = s
                elif isinstance(existing, pikepdf.Array):
                    existing.append(s)
                else:
                    page.obj["/Contents"] = PdfArray([existing, s])

            pdf.save(tmp)
        shutil.move(tmp, pdf_path)
        label = "SVG" if png_bytes else "ברירת מחדל"
        print(f"   חותמת נגישות הוספה ({label})")
    except Exception as e:
        print(f"   stamp warning: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def analyze_structure_with_ai(page_paths, page_texts, lang_code="he-IL"):
    """Use Claude Haiku to analyze document structure per page (WCAG 1.3.1).
    Returns {page_num: [{type, text} | {type:'tr', cells:[{type,text}]}]}.
    Types: h1 h2 h3 p li caption tr(with cells: th/td)."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    try:
        import anthropic, base64, io, json, re
        from PIL import Image as PILImage
    except ImportError:
        return {}

    client = anthropic.Anthropic()
    structures = {}
    print(f"  AI: מנתח מבנה {len(page_paths)} עמודים (WCAG 1.3.1)...")

    for i, path in enumerate(page_paths, 1):
        try:
            img = PILImage.open(path)
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            data = base64.standard_b64encode(buf.getvalue()).decode()
            ocr_text = page_texts.get(i, "")[:3000]

            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                messages=[{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text", "text": (
                        f"Analyze the document structure of this page.\n"
                        f"OCR text: {ocr_text}\n\n"
                        "Return ONLY a JSON array with elements in reading order.\n"
                        "Simple elements: {\"type\":\"h1\"|\"h2\"|\"h3\"|\"p\"|\"li\"|\"caption\", \"text\":\"...\"}\n"
                        "Table row: {\"type\":\"tr\", \"cells\":[{\"type\":\"th\"|\"td\", \"text\":\"...\"}]}\n"
                        "Use h1 for main title, h2 for section headings, h3 for sub-headings, "
                        "p for paragraphs, li for list items.\n"
                        "Return ONLY the JSON array."
                    )}
                ]}]
            )
            text = resp.content[0].text.strip()
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                elements = json.loads(match.group())
                structures[i] = elements
                print(f"  AI מבנה עמוד {i}: {len(elements)} אלמנטים ✓")
        except Exception as e:
            print(f"  AI מבנה עמוד {i}: {e}")

    return structures


def build_page_elements(struct_list, sect, pdf, make_elem):
    """Convert AI structure list to pikepdf structure elements under sect.
    Returns list of pikepdf elements (children of sect)."""
    import pikepdf
    from pikepdf import Dictionary, Array, Name, String

    children = []
    list_buf = []   # buffer for consecutive li items → wrap in L

    def flush_list():
        if not list_buf:
            return
        l_elem = make_elem("L", sect)
        li_elems = []
        for li_text in list_buf:
            li = make_elem("LI", l_elem)
            lbody = make_elem("LBody", li, actual_text=li_text)
            li["/K"] = Array([lbody])
            li_elems.append(li)
        l_elem["/K"] = Array(li_elems)
        children.append(l_elem)
        list_buf.clear()

    # Group consecutive tr elements into a single Table
    def flush_table(tr_buf):
        if not tr_buf:
            return
        tbl = make_elem("Table", sect)
        tbl["/K"] = Array(tr_buf)
        for tr in tr_buf:
            tr["/P"] = tbl
        children.append(tbl)

    tr_buf = []

    for item in struct_list:
        t = str(item.get("type", "p")).lower()
        text = str(item.get("text", "")).strip()

        if t == "li":
            if tr_buf:
                flush_table(tr_buf); tr_buf = []
            list_buf.append(text)
            continue

        # Flush pending list/table before non-li/non-tr elements
        if t != "tr" and list_buf:
            flush_list()
        if t != "tr" and tr_buf:
            flush_table(tr_buf); tr_buf = []

        if t in ("h1", "h2", "h3"):
            flush_list()
            pdf_type_map = {"h1": "H1", "h2": "H2", "h3": "H3"}
            children.append(make_elem(pdf_type_map[t], sect, actual_text=text))
        elif t == "tr":
            cells = item.get("cells", [])
            if not cells:
                continue
            tr = make_elem("TR", sect)  # parent will be fixed when flushing
            cell_elems = []
            for cell in cells:
                ct = str(cell.get("type", "td")).lower()
                ct_pdf = "TH" if ct == "th" else "TD"
                c_text = str(cell.get("text", "")).strip()
                ce = make_elem(ct_pdf, tr, actual_text=c_text)
                if ct == "th":
                    ce["/Scope"] = Name("/Col")
                cell_elems.append(ce)
            tr["/K"] = Array(cell_elems)
            tr_buf.append(tr)
        elif t == "caption":
            children.append(make_elem("Caption", sect, actual_text=text))
        else:
            # p or unknown → P
            children.append(make_elem("P", sect, actual_text=text))

    # Flush remaining
    flush_list()
    flush_table(tr_buf)

    return children


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


def patch_stream(raw, fig_mcid, txt_mcid, page_w, page_h):
    """Wrap the entire page content (image + invisible OCR) in a single Figure MCID.

    PDF/UA best practice for scanned documents: the whole page is one Figure.
    - Figure carries Alt (AI description) and ActualText (OCR text).
    - No split needed; no orphaned MCIDs; no P elements without content reference.
    """
    return (b"/Figure <</MCID " + str(fig_mcid).encode() + b">> BDC\n" +
            raw.strip(b" \n") + b"\nEMC\n")


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


def add_metadata_only(input_pdf, output_pdf, lang="he-IL", title="מסמך נגיש", author=""):
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
        if author:
            meta["dc:creator"] = [author]
        # PDF/UA-1 identifier — ISO 14289-1 §6.2 (required for IS 5568 compliance)
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
        if author:
            pdf.trailer["/Info"]["/Author"] = String(author)
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
                   author="", page_texts=None, page_titles=None, tables_info=None,
                   pdf_type="scanned", ai_descriptions=None, page_structures=None):
    import pikepdf
    from pikepdf import Dictionary, Array, Name, String, Stream

    if page_texts is None: page_texts = {}
    if page_titles is None: page_titles = {}
    if tables_info is None: tables_info = {}
    if ai_descriptions is None: ai_descriptions = {}
    if page_structures is None: page_structures = {}

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
        if author:
            meta["dc:creator"] = [author]
        # PDF/UA-1 identifier — ISO 14289-1 §6.2 (required for IS 5568 compliance)
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
        if author:
            pdf.trailer["/Info"]["/Author"] = String(author)
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
    # MCIDs per page (reset per page — PDF spec §14.7.4.4):
    #   MCID 0 → Figure (scan image)
    #   MCID 1 → P     (OCR text layer)
    FIG_MCID = 0
    TXT_MCID = 1

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
        page_obj = pdf.make_indirect(page.obj)  # ensure indirect ref for /Pg in struct elements
        page_obj["/Tabs"] = Name("/S")
        page_obj["/StructParents"] = pikepdf.objects.Integer(pg_idx_0)

        media = page_obj.get("/MediaBox")
        pw = float(media[2]) if media else 595.0
        ph = float(media[3]) if media else 842.0

        page_text = page_texts.get(pg_idx, "")
        page_title = (page_titles.get(str(pg_idx)) or page_titles.get(pg_idx) or
                      (page_text.split("\n")[0].strip() if page_text else f"\u05e2\u05de\u05d5\u05d3 {pg_idx}"))

        ai_desc = ai_descriptions.get(pg_idx, "")
        # Sect is a container — must NOT have /Alt (PDF/UA: nested alt text forbidden)
        sect = make_elem("Sect", doc_elem, title_text=f"\u05e2\u05de\u05d5\u05d3 {pg_idx}")
        sect_elems.append(sect)
        children = []

        body_text = page_text.strip() if page_text else f"תוכן עמוד {pg_idx}"
        if pdf_type == 'digital':
            # For digital PDFs: do NOT add MCID — the original content stream
            # may already contain BDC markers with MCID 0, causing "MCID already
            # present" errors in PAC. Use ActualText/Alt only (no content ref).
            struct_list = page_structures.get(pg_idx, [])
            if struct_list:
                text_children = build_page_elements(struct_list, sect, pdf, make_elem)
            else:
                text_children = [make_elem("P", sect, actual_text=body_text, alt_text=body_text)]
            children.extend(text_children)
        else:
            # WCAG 1.1.1 + PDF/UA: entire page = one Figure (MCID 0)
            # Alt  = AI visual description (for screen readers without text extraction)
            # ActualText = OCR text (for text extraction / copy-paste)
            # All page content (image + invisible OCR) wrapped in one Figure MCID.
            # This avoids P elements with no MCID that PAC rejects.
            fig_alt = (ai_desc if ai_desc
                       else (body_text[:300] if body_text else f"תמונת עמוד {pg_idx}"))
            fig = make_elem("Figure", sect,
                            title_text=f"עמוד {pg_idx}",
                            alt_text=fig_alt,
                            actual_text=body_text if body_text else f"עמוד {pg_idx}",
                            page_obj=page_obj, mcid=FIG_MCID)
            parent_tree_map[pg_idx_0] = [fig]
            children.append(fig)

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
                    th = make_elem("TH", tr_h, actual_text=hdr)
                    th["/Scope"] = Name("/Col")
                    th_list.append(th)
                tr_h["/K"] = Array(th_list)
                tbl_children.append(tr_h)
            tbl["/K"] = Array(tbl_children)
            children.append(tbl)

        sect["/K"] = Array(children)
        page_patch_info.append((FIG_MCID, TXT_MCID, pw, ph))

    doc_elem["/K"] = Array(sect_elems)

    flat = []
    for pg_idx_0 in sorted(parent_tree_map.keys()):
        flat.append(pikepdf.objects.Integer(pg_idx_0))
        entry = parent_tree_map[pg_idx_0]
        # PDF spec §14.7.4.4: value MUST always be an array — MCID i → index i.
        # Storing a bare ref (not wrapped in Array) makes PAC unable to resolve
        # the content-to-structure link → 10 failures per page.
        flat.append(Array(entry))
    str_root["/ParentTree"] = pdf.make_indirect(Dictionary(Nums=Array(flat)))
    str_root["/ParentTreeNextKey"] = pikepdf.objects.Integer(len(parent_tree_map))
    pdf.Root["/StructTreeRoot"] = str_root

    for pg_idx, page in enumerate(pages):
        if pdf_type == 'digital':
            continue  # Never touch digital PDF content streams — avoids MCID conflicts
        fig_m, txt_m, pw, ph = page_patch_info[pg_idx]
        try:
            raw_obj = page.obj.get("/Contents")
            if raw_obj is None: continue
            def get_bytes(obj):
                if hasattr(obj, "read_bytes"): return obj.read_bytes()
                if isinstance(obj, pikepdf.Array): return b"".join(get_bytes(x) for x in obj)
                return b""
            orig = get_bytes(raw_obj)
            new_data = patch_stream(orig, fig_m, txt_m, pw, ph)
            page.obj["/Contents"] = pdf.make_indirect(Stream(pdf, new_data))
        except Exception as e:
            print(f"   content stream עמוד {pg_idx + 1}: {e}")

    add_bookmarks(pdf, pages, page_titles, page_texts)
    pdf.save(output_pdf)
    print(f"\u2705 PDF \u05e0\u05d2\u05d9\u05e9: {output_pdf}")


def check_structure(pdf_path):
    """
    בדיקת היררכיית תוכן של PDF מעובד.
    מדפיס: עץ מבנה, ParentTree, MCIDs בכל עמוד, ושגיאות.
    שימוש: python build_accessible_pdf.py --check-structure path/to/output.pdf
    """
    import re, sys
    # Windows terminal: force UTF-8 so Hebrew prints correctly
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        import pikepdf
        from pikepdf import Dictionary, Array, Name
    except ImportError:
        print("❌ pikepdf לא מותקן")
        return

    print(f"\n{'='*60}")
    print(f"[STRUCT] {pdf_path}")
    print('='*60)

    errors = []
    warnings = []

    with pikepdf.open(pdf_path) as pdf:
        root = pdf.Root

        # ── 1. בדיקות מטא-דאטה בסיסיות ──────────────────────────
        print("\n[META]")
        lang = root.get("/Lang", "")
        print(f"  /Lang       = {lang!r}")
        if not lang:
            errors.append("/Lang חסר ב-Root")

        mark_info = root.get("/MarkInfo", {})
        marked = mark_info.get("/Marked", False) if mark_info else False
        print(f"  /MarkInfo   = Marked={bool(marked)}")
        if not marked:
            errors.append("/MarkInfo/Marked אינו True")

        # ── 2. עץ המבנה ───────────────────────────────────────────
        str_root = root.get("/StructTreeRoot")
        if not str_root:
            errors.append("אין StructTreeRoot — המסמך אינו tagged")
            _report(errors, warnings)
            return

        print("\n[TREE] עץ מבנה (StructTreeRoot):")

        def elem_label(e):
            try:
                s  = str(e.get("/S", "?")).lstrip("/")
                t  = str(e.get("/T", "")).strip('"\'')
                at = str(e.get("/ActualText", ""))[:40].strip('"\'')
                al = str(e.get("/Alt", ""))[:40].strip('"\'')
                pg = e.get("/Pg")
                k  = e.get("/K")
                mcid = f" MCID={int(k)}" if isinstance(k, pikepdf.objects.Integer) else ""
                page_num = ""
                if pg:
                    for i, page in enumerate(pdf.pages):
                        if page.obj.objgen == pg.objgen:
                            page_num = f" עמוד={i+1}"
                            break
                has_alt  = " ✔Alt"  if al  else ""
                has_at   = " ✔ActualText" if at else ""
                label = f"<{s}>{mcid}{page_num}{has_alt}{has_at}"
                if t: label += f" [{t}]"
                return label, s
            except Exception as ex:
                return f"<?>  ({ex})", "?"

        def walk(obj, depth=0):
            indent = "  " * depth
            try:
                obj = pdf.get_object(obj.objgen) if hasattr(obj, 'objgen') else obj
            except Exception:
                pass

            if isinstance(obj, pikepdf.Dictionary):
                obj_type = str(obj.get("/Type", "")).lstrip("/")
                if obj_type in ("StructTreeRoot", "StructElem"):
                    label, stype = elem_label(obj)
                    print(f"{indent}{label}")

                    # בדיקות תקינות לכל element
                    s_name = stype
                    grouping = s_name in ("Document", "Sect", "Div", "Art", "Part",
                                          "BlockQuote", "Caption", "TOC", "TOCI",
                                          "Index", "NonStruct", "Private",
                                          "Table", "L", "LI", "TR")
                    k = obj.get("/K")
                    alt = obj.get("/Alt")
                    actual_text = obj.get("/ActualText")

                    if alt and not isinstance(k, pikepdf.objects.Integer) and not isinstance(k, pikepdf.Array):
                        # /Alt על grouping ללא MCID — "nested alt text" ב-Acrobat
                        if isinstance(k, pikepdf.Dictionary) or k is None:
                            if grouping:
                                warnings.append(f"⚠ {s_name} יש /Alt על grouping element (עלול לגרום 'Nested alternate text')")

                    if not grouping and alt is None and actual_text is None and k is not None:
                        if isinstance(k, pikepdf.objects.Integer):
                            warnings.append(f"⚠ {s_name} MCID={int(k)} — אין /Alt ולא /ActualText")

                    # מעבר על ילדים
                    if isinstance(k, pikepdf.Array):
                        for child in k:
                            walk(child, depth + 1)
                    elif isinstance(k, pikepdf.Dictionary):
                        walk(k, depth + 1)
                    elif isinstance(k, pikepdf.objects.Integer):
                        pass  # leaf: MCID ref — already shown in label
                elif obj_type == "MCR":
                    mcid = obj.get("/MCID", "?")
                    print(f"{indent}  [MCR MCID={mcid}]")
                elif obj_type == "OBJR":
                    print(f"{indent}  [OBJR]")

            elif isinstance(obj, pikepdf.Array):
                for item in obj:
                    walk(item, depth)

        doc_k = str_root.get("/K")
        if doc_k is not None:
            walk(doc_k if not isinstance(doc_k, pikepdf.Array) else str_root, depth=0)
        else:
            walk(str_root, depth=0)

        # ── 3. ParentTree ─────────────────────────────────────────
        print("\n[PARENT-TREE]")
        pt = str_root.get("/ParentTree")
        if not pt:
            errors.append("אין ParentTree ב-StructTreeRoot")
        else:
            nums = pt.get("/Nums", [])
            i = 0
            while i + 1 < len(nums):
                page_idx = int(nums[i])
                entry    = nums[i + 1]
                is_array = isinstance(entry, pikepdf.Array)
                length   = len(entry) if is_array else "direct-ref (שגוי!)"
                status   = "✔" if is_array else "❌"
                print(f"  {status} עמוד {page_idx}: {'Array['+str(length)+']' if is_array else str(length)}")
                if not is_array:
                    errors.append(f"ParentTree עמוד {page_idx} — ערך ישיר ולא Array (PDF spec §14.7.4.4)")
                i += 2

        # ── 4. MCIDs בזרמי תוכן ──────────────────────────────────
        print("\n[CONTENT] MCIDs בזרמי תוכן (BDC markers):")
        bdc_re = re.compile(rb'/(\w+)\s+<<[^>]*?/MCID\s+(\d+)')

        for pg_num, page in enumerate(pdf.pages, 1):
            try:
                raw_obj = page.obj.get("/Contents")
                if raw_obj is None:
                    print(f"  עמוד {pg_num}: אין /Contents")
                    continue

                def get_bytes(o):
                    if hasattr(o, "read_bytes"): return o.read_bytes()
                    if isinstance(o, pikepdf.Array): return b"".join(get_bytes(x) for x in o)
                    return b""

                raw = get_bytes(raw_obj)
                found = bdc_re.findall(raw)
                struct_parents = page.obj.get("/StructParents")
                sp_str = f" StructParents={int(struct_parents)}" if struct_parents is not None else " ⚠ אין StructParents"
                if found:
                    tags = ", ".join(f"{t.decode()}/MCID={m.decode()}" for t, m in found)
                    print(f"  עמוד {pg_num}{sp_str}: {tags}")
                else:
                    print(f"  עמוד {pg_num}{sp_str}: ⚠ אין BDC markers — תוכן לא מתויג")
                    warnings.append(f"עמוד {pg_num}: אין BDC markers בזרם התוכן")
            except Exception as ex:
                print(f"  עמוד {pg_num}: שגיאה — {ex}")

    _report(errors, warnings)


def _report(errors, warnings):
    print(f"\n{'='*60}")
    if warnings:
        print("[WARN]")
        for w in warnings:
            print(f"   {w}")
    if errors:
        print("[ERRORS]")
        for e in errors:
            print(f"   {e}")
        print(f"\nסיכום: {len(errors)} שגיאות, {len(warnings)} אזהרות")
    else:
        print(f"[OK] ההיררכיה תקינה ({len(warnings)} אזהרות)")
    print('='*60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",        default=None)
    parser.add_argument("--output",       default=None)
    parser.add_argument("--check-structure", metavar="PDF",
                        help="בדוק היררכיית תוכן של PDF מעובד ויצא")
    parser.add_argument("--lang",         default="he-IL")
    parser.add_argument("--title",        default="\u05de\u05e1\u05de\u05da \u05e0\u05d2\u05d9\u05e9")
    parser.add_argument("--author",       default="")
    parser.add_argument("--dpi",          type=int, default=200)
    parser.add_argument("--stamp",        action="store_true")
    parser.add_argument("--ocr",          action="store_true")
    parser.add_argument("--force-ocr",    action="store_true")
    parser.add_argument("--text-json",    default=None)
    parser.add_argument("--page-titles",  default=None)
    parser.add_argument("--tables-json",  default=None)
    args = parser.parse_args()

    # מצב בדיקת היררכיה — לא צריך --input/--output
    if args.check_structure:
        check_structure(args.check_structure)
        return

    if not args.input or not args.output:
        parser.error("--input ו-–output נדרשים (או השתמש ב-–check-structure)")

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
            add_metadata_only(base_pdf, args.output, lang=args.lang, title=args.title,
                              author=args.author)
        else:
            # WCAG 1.3.1: analyze document structure with AI for rich semantic tagging
            page_structures = {}
            if os.environ.get("ANTHROPIC_API_KEY") and page_paths:
                page_structures = analyze_structure_with_ai(
                    page_paths, page_texts, lang_code=args.lang)

            add_pdfua_tags(base_pdf, args.output,
                           lang=args.lang, title=args.title, author=args.author,
                           page_texts=page_texts,
                           page_titles=page_titles,
                           tables_info=tables_info,
                           pdf_type=pdf_type,
                           ai_descriptions=ai_descriptions,
                           page_structures=page_structures)

        if args.stamp:
            apply_stamp_to_pdf(args.output)


if __name__ == "__main__":
    main()
