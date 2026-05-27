"""
מעקב מלא אחרי הטקסט בתהליך ה-OCR → StructTree
"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("POPPLER_PATH", "C:/poppler/Library/bin")
os.environ.setdefault("TESSERACT_CMD", "C:/Program Files/Tesseract-OCR/tesseract.exe")

# --- שלב 1: OCR ---
from scripts.build_accessible_pdf import run_ocr_with_positions, _merge_ocr_lines_into_paragraphs
from pdf2image import convert_from_path
import tempfile, pikepdf

poppler_path = os.environ.get("POPPLER_PATH")
pdf_path = "outputs/88990875-bee0-46ec-812f-c9641f267213_accessible.pdf"
if not os.path.exists(pdf_path):
    import sqlite3
    con = sqlite3.connect("db/history.db")
    row = con.execute("SELECT output_path FROM documents WHERE original_name LIKE '%קול קורא%' ORDER BY created_at DESC LIMIT 1").fetchone()
    if row: pdf_path = row[0]

# המר מהמסמך המקורי — נחפש uploads
import glob
orig_pdfs = glob.glob("uploads/*.pdf") + glob.glob("C:/Users/admin/Downloads/*.pdf")
print(f"PDF לעיבוד: {pdf_path}")

# בוא נעבוד עם הקובץ שכבר עבד — נבצע OCR על ה-output שלו לצורך הדיאגנוסטיקה
pages = convert_from_path(pdf_path, dpi=150, first_page=1, last_page=1,
                          poppler_path=poppler_path)

with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
    pages[0].save(f.name)
    tmp_png = f.name

print("\n=== שלב 1: run_ocr_with_positions ===")
page_texts, page_blocks, quality = run_ocr_with_positions([tmp_png], lang_code="he-IL")
print(f"page_texts[1]: {repr(page_texts.get(1, '')[:200])}")
print(f"page_blocks[1] (first 3 blocks):")
for blk in (page_blocks.get(1, []))[:3]:
    print(f"  blk.text={repr(blk.text)}")

print("\n=== שלב 2: merge ===")
merged = _merge_ocr_lines_into_paragraphs(page_blocks)
print("merged blocks (first 3):")
for blk in (merged.get(1, []))[:3]:
    print(f"  blk.text={repr(blk.text)}")

print("\n=== שלב 3: StructureDetector ===")
sys.path.insert(0, "scripts")
from pipeline import StructureDetector, DocumentClassifier
all_blocks = [b for blks in merged.values() for b in blks]
doc_type = DocumentClassifier().classify(all_blocks)
elements = StructureDetector().detect(all_blocks, doc_type=doc_type)
print(f"elements (first 5):")
for e in elements[:5]:
    print(f"  {e.elem_type}: {repr(e.text[:60])}")

print("\n=== שלב 4: ActualText ב-StructTree (קובץ קיים) ===")
with pikepdf.open(pdf_path) as pdf:
    sr = pdf.Root.get('/StructTreeRoot')
    count = 0
    def walk(obj, depth=0):
        global count
        if depth > 8 or count > 8:
            return
        try:
            if isinstance(obj, pikepdf.Array):
                for item in list(obj)[:10]:
                    walk(item, depth+1)
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                at = obj.get('/ActualText')
                if at:
                    raw = bytes(at)
                    text = raw.decode('utf-16') if raw[:2] in (b'\xfe\xff', b'\xff\xfe') else str(at)
                    if any('֐' <= c <= '׿' for c in text):
                        print(f"  ActualText: {repr(text[:60])}")
                        count += 1
                for k in ['/K', '/C']:
                    child = obj.get(k)
                    if child: walk(child, depth+1)
        except: pass
    if sr: walk(sr)

os.unlink(tmp_png)
print("\nסיום דיאגנוסטיקה")
