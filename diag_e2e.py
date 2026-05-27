"""
בדיקת קצה-לקצה: מעבד PDF סרוק ובודק שה-ActualText נכון
"""
import os, sys, tempfile, shutil
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("POPPLER_PATH", "C:/poppler/Library/bin")
os.environ.setdefault("TESSERACT_CMD", "C:/Program Files/Tesseract-OCR/tesseract.exe")

# מצא את המסמך המקורי
import sqlite3
con = sqlite3.connect("db/history.db")
row = con.execute(
    "SELECT original_name, output_path FROM documents WHERE original_name LIKE '%קול קורא%' ORDER BY created_at ASC LIMIT 1"
).fetchone()
print(f"מסמך: {row[0] if row else 'לא נמצא'}")

# נעבד את הפלט הקיים — הוא מכיל תמונות עמוד מהמקור
src = row[1] if row else None
if not src or not os.path.exists(src):
    print("אין קובץ מקור")
    sys.exit(1)

# יצור תמונות עמוד
from pdf2image import convert_from_path
poppler_path = os.environ.get("POPPLER_PATH")
pages = convert_from_path(src, dpi=150, first_page=1, last_page=min(3, 99),
                          poppler_path=poppler_path)

tmpdir = tempfile.mkdtemp()
page_paths = []
for i, p in enumerate(pages, 1):
    path = os.path.join(tmpdir, f"page_{i:03d}.png")
    p.save(path)
    page_paths.append(path)
print(f"עמודים: {len(page_paths)}")

# עיבוד מלא
out_pdf = os.path.join(tmpdir, "test_out.pdf")
sys.path.insert(0, "scripts")

from build_accessible_pdf import process_scanned_pdf
process_scanned_pdf(
    page_paths, out_pdf,
    lang="he-IL", title="קול קורא בדיקה", stamp=False
)

# בדוק ActualText
import pikepdf
print("\n=== ActualText ב-output החדש ===")
with pikepdf.open(out_pdf) as pdf:
    sr = pdf.Root.get('/StructTreeRoot')
    count = 0
    def walk(obj, depth=0):
        global count
        if depth > 8 or count > 10:
            return
        try:
            if isinstance(obj, pikepdf.Array):
                for item in list(obj)[:15]:
                    walk(item, depth+1)
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                at = obj.get('/ActualText')
                if at:
                    raw = bytes(at)
                    text = raw.decode('utf-16') if raw[:2] in (b'\xfe\xff', b'\xff\xfe') else str(at)
                    if any('֐' <= c <= '׿' for c in text) and len(text.strip()) > 2:
                        print(f"  {repr(text.strip()[:70])}")
                        count += 1
                for k in ['/K', '/C']:
                    child = obj.get(k)
                    if child: walk(child, depth+1)
        except: pass
    if sr:
        walk(sr)
    else:
        print("אין StructTreeRoot")

shutil.rmtree(tmpdir, ignore_errors=True)
print("\nבדיקה הסתיימה")
