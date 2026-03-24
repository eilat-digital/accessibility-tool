#!/usr/bin/env python3
"""
build_accessible_pdf.py — PDF Accessibility Tool
עיריית אילת — PDF/UA + IS 5568 + WCAG 2.1 AA

תומך בשני סוגי קלט אוטומטית:
  1. PDF ממחשב (Word / מערכת) — מחלץ טקסט קיים
  2. PDF סרוק (צילום נייר)   — מריץ OCR עברי אוטומטי
"""
import argparse, json, os, sys, tempfile
from pathlib import Path

try:
    from pdf2image import convert_from_path
    from PIL import Image
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    import pikepdf
    from pikepdf import Dictionary, Array, Name, String, Boolean
except ImportError as e:
    print(f"ERROR: חסרה חבילה — {e}", file=sys.stderr)
    sys.exit(1)

try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


# ── זיהוי סוג PDF ──────────────────────────────────────────────────────────────
def detect_pdf_type(input_path: Path):
    """מחזיר ('digital', {page:text}) או ('scanned', {})"""
    try:
        pdf = pikepdf.open(str(input_path))
        pages_text = {}
        total_chars = 0
        for i, page in enumerate(pdf.pages):
            try:
                from pdfminer.high_level import extract_text
                txt = extract_text(str(input_path), page_numbers=[i]) or ''
                if len(txt.strip()) > 20:
                    pages_text[str(i+1)] = txt.strip()
                    total_chars += len(txt.strip())
            except Exception:
                pass
        pdf.close()
        if total_chars > 50:
            print(f"  → זוהה: PDF ממחשב ({total_chars} תווים)", flush=True)
            return 'digital', pages_text
        print(f"  → זוהה: PDF סרוק", flush=True)
        return 'scanned', {}
    except Exception:
        return 'scanned', {}


# ── OCR ────────────────────────────────────────────────────────────────────────
def run_ocr(image_paths: list, lang_code: str = 'he-IL') -> dict:
    if not OCR_AVAILABLE:
        print("  ⚠ pytesseract לא מותקן", flush=True)
        return {}
    tess_lang = {'he-IL':'heb','he':'heb','en-US':'eng','en':'eng'}.get(lang_code, 'heb')
    try:
        available = pytesseract.get_languages()
        if tess_lang not in available:
            print(f"  ⚠ שפת OCR '{tess_lang}' לא מותקנת — עובר ל-eng", flush=True)
            tess_lang = 'eng'
    except Exception:
        pass

    results = {}
    for i, (img_path, _) in enumerate(image_paths):
        try:
            img  = Image.open(img_path).convert('L')
            text = pytesseract.image_to_string(img, lang=tess_lang, config='--psm 3')
            results[str(i+1)] = text.strip() if text.strip() else f'עמוד {i+1}'
            print(f"  → OCR עמוד {i+1}: {len(results[str(i+1)])} תווים", flush=True)
        except Exception as e:
            results[str(i+1)] = f'עמוד {i+1}'
    return results


# ── חותמת עגולה שקופה ────────────────────────────────────────────────────────────
def _draw_stamp(c, pt_w, pt_h):
    """
    חותמת עגולה כתומה שקופה — פינה ימנית עליונה, עמוד אחרון בלבד.
    עיגול + ✓ + "נגיש"
    """
    from reportlab.lib.units import mm
    import math

    ORANGE = (0.878, 0.361, 0.125)  # #E05C20
    ALPHA  = 0.75
    R      = 14 * mm          # רדיוס העיגול
    MARGIN = 6 * mm
    cx     = pt_w - MARGIN - R
    cy     = pt_h - MARGIN - R

    c.saveState()

    # עיגול שקוף
    c.setStrokeColorRGB(*ORANGE, alpha=ALPHA)
    c.setLineWidth(1.5)
    c.circle(cx, cy, R, stroke=1, fill=0)

    # ✓  — מצויר כנתיב
    c.setLineCap(1)   # round
    c.setLineJoin(1)  # round
    c.setLineWidth(2)
    p = c.beginPath()
    # נקודות ✓: שמאל-תחת → מרכז → ימין-עליון
    p.moveTo(cx - R*0.38, cy + R*0.05)
    p.lineTo(cx - R*0.08, cy - R*0.32)
    p.lineTo(cx + R*0.42, cy + R*0.38)
    c.drawPath(p, stroke=1, fill=0)

    # "נגיש" מתחת ל-✓
    c.setFillColorRGB(*ORANGE, alpha=ALPHA)
    c.setFont('Helvetica-Bold', 6.5)
    c.drawCentredString(cx, cy - R*0.72, '\u05e0\u05d2\u05d9\u05e9')

    c.restoreState()


# ── בניית PDF נגיש ─────────────────────────────────────────────────────────────
def build_accessible_pdf(
    input_path, output_path,
    lang='he-IL', title='', dpi=200, stamp=True, force_ocr=False,
    text_data=None, page_titles=None, tables_data=None,
):
    input_path  = Path(input_path)
    output_path = Path(output_path)
    if not title:
        title = input_path.stem.replace('-',' ').replace('_',' ')

    # זיהוי אוטומטי
    pdf_type, existing_text = detect_pdf_type(input_path)

    # המרה לתמונות
    print(f"  → ממיר עמודים ({dpi} DPI)...", flush=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        images_pil = convert_from_path(str(input_path), dpi=dpi,
                                       output_folder=tmpdir, fmt='png')
        image_paths = []
        for i, img in enumerate(images_pil):
            p = Path(tmpdir) / f"page_{i+1:04d}.png"
            img.save(str(p), 'PNG')
            image_paths.append((str(p), img.size))
        num_pages = len(image_paths)
        print(f"  → {num_pages} עמודים", flush=True)

        # בחירת טקסט
        if text_data:
            page_texts = text_data
            print("  → טקסט ידני", flush=True)
        elif pdf_type == 'digital' and not force_ocr:
            page_texts = existing_text
            print("  → טקסט מחולץ מה-PDF", flush=True)
        else:
            print(f"  → מריץ OCR ({lang})...", flush=True)
            page_texts = run_ocr(image_paths, lang)

        # בניית PDF
        print("  → בונה PDF...", flush=True)
        rl_stage = str(output_path) + '.stage.pdf'
        c = canvas.Canvas(rl_stage)
        page_sizes = []

        for i, (img_path, (px_w, px_h)) in enumerate(image_paths):
            pt_w = px_w * 72.0 / dpi
            pt_h = px_h * 72.0 / dpi
            page_sizes.append((pt_w, pt_h))
            c.setPageSize((pt_w, pt_h))
            c.drawImage(ImageReader(img_path), 0, 0, pt_w, pt_h)

            # חותמת עגולה — עמוד אחרון בלבד
            if stamp and i == num_pages - 1:
                _draw_stamp(c, pt_w, pt_h)

            txt = page_texts.get(str(i+1), '')
            if txt:
                c.saveState()
                c.setFillColorRGB(0, 0, 0, alpha=0)
                c.setFont('Helvetica', 10)
                y = pt_h - 30
                for line in txt.split('\n'):
                    if y < 20: break
                    try: c.drawString(30, y, line)
                    except: pass
                    y -= 14
                c.restoreState()

            c.showPage()
        c.save()

        # תיוג PDF/UA
        print("  → מוסיף תיוג PDF/UA...", flush=True)
        pdf = pikepdf.open(rl_stage)

        with pdf.open_metadata() as meta:
            meta['dc:title']     = title
            meta['dc:language']  = lang
            meta['pdf:Producer'] = 'Eilat Municipality Accessibility Tool'
            meta['pdfuaid:part'] = '1'

        pdf.Root['/Lang']    = String(lang)
        pdf.Root['/MarkInfo'] = Dictionary(Marked=Boolean(True))
        pdf.Root['/ViewerPreferences'] = Dictionary(
            Direction=Name('/R2L'), DisplayDocTitle=Boolean(True))
        pdf.docinfo['/Title']    = title
        pdf.docinfo['/Producer'] = 'Eilat Municipality Accessibility Tool'

        doc_kids   = Array()
        mcid_ctr   = [0]
        ptree_map  = {}

        for pi, page in enumerate(pdf.pages):
            pn    = pi + 1
            ptitle = (page_titles or {}).get(str(pn), f'\u05e2\u05de\u05d5\u05d3 {pn}')
            ptext  = page_texts.get(str(pn), '')

            h1_m = mcid_ctr[0]; mcid_ctr[0] += 1
            h1 = Dictionary(Type=Name('/StructElem'), S=Name('/H1'),
                            Lang=String(lang), ActualText=String(ptitle), K=h1_m)

            p_m = mcid_ctr[0]; mcid_ctr[0] += 1
            p = Dictionary(Type=Name('/StructElem'), S=Name('/P'),
                           Lang=String(lang),
                           ActualText=String(ptext[:500] if ptext else f'\u05e2\u05de\u05d5\u05d3 {pn}'),
                           K=p_m)

            kids = Array([pdf.make_indirect(h1), pdf.make_indirect(p)])

            for tbl in (tables_data or {}).get(str(pn), []):
                th_kids = Array()
                for hdr in tbl.get('headers', []):
                    th_m = mcid_ctr[0]; mcid_ctr[0] += 1
                    th = Dictionary(Type=Name('/StructElem'), S=Name('/TH'),
                                    Scope=Name('/Col'), Lang=String(lang),
                                    ActualText=String(hdr), K=th_m)
                    th_kids.append(pdf.make_indirect(th))
                tbl_e = Dictionary(Type=Name('/StructElem'), S=Name('/Table'),
                                   Lang=String(lang),
                                   Summary=String(tbl.get('summary','')), K=th_kids)
                kids.append(pdf.make_indirect(tbl_e))

            sect = Dictionary(Type=Name('/StructElem'), S=Name('/Sect'),
                              Lang=String(lang), K=kids)
            sr = pdf.make_indirect(sect)
            doc_kids.append(sr)
            page.obj['/Tabs'] = Name('/S')
            page.obj['/StructParents'] = pi
            ptree_map[pi] = Array([sr])

        ptree_arr = Array()
        for i in range(num_pages):
            ptree_arr.append(ptree_map.get(i, Array()))

        struct_doc = Dictionary(Type=Name('/StructElem'), S=Name('/Document'),
                                Lang=String(lang), K=doc_kids)
        struct_root = Dictionary(
            Type=Name('/StructTreeRoot'),
            K=pdf.make_indirect(struct_doc),
            ParentTree=pdf.make_indirect(Dictionary(Nums=ptree_arr)),
        )
        pdf.Root['/StructTreeRoot'] = pdf.make_indirect(struct_root)

        if num_pages >= 9:
            _add_bookmarks(pdf, page_titles, num_pages)

        pdf.save(str(output_path), fix_metadata_version=True)
        pdf.close()
        os.unlink(rl_stage)

    print(f"  \u2713 הושלם: {output_path}", flush=True)
    return num_pages


def _add_bookmarks(pdf, page_titles, num_pages):
    try:
        items = Array()
        for i, page in enumerate(pdf.pages):
            label = (page_titles or {}).get(str(i+1), f'\u05e2\u05de\u05d5\u05d3 {i+1}')
            items.append(pdf.make_indirect(
                Dictionary(Title=String(label), Dest=Array([page.obj, Name('/Fit')]))))
        pdf.Root['/Outlines'] = pdf.make_indirect(
            Dictionary(Type=Name('/Outlines'), Count=num_pages,
                       First=items[0], Last=items[-1]))
        pdf.Root['/PageMode'] = Name('/UseOutlines')
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input',       required=True)
    ap.add_argument('--output',      required=True)
    ap.add_argument('--lang',        default='he-IL')
    ap.add_argument('--title',       default='')
    ap.add_argument('--dpi',         type=int, default=200)
    ap.add_argument('--stamp',       action='store_true')
    ap.add_argument('--force-ocr',   action='store_true')
    ap.add_argument('--text-json',   default=None)
    ap.add_argument('--page-titles', default=None)
    ap.add_argument('--tables-json', default=None)
    args = ap.parse_args()

    pages = build_accessible_pdf(
        input_path  = args.input,
        output_path = args.output,
        lang        = args.lang,
        title       = args.title,
        dpi         = args.dpi,
        stamp       = args.stamp,
        force_ocr   = getattr(args, 'force_ocr', False),
        text_data   = json.loads(Path(args.text_json).read_text('utf-8'))   if args.text_json   else None,
        page_titles = json.loads(Path(args.page_titles).read_text('utf-8')) if args.page_titles else None,
        tables_data = json.loads(Path(args.tables_json).read_text('utf-8')) if args.tables_json else None,
    )
    print(f'עמודים: {pages}')

if __name__ == '__main__':
    main()
