"""
בדיקת סדר טקסט OCR - מה Tesseract מחזיר בפועל
"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")

# מצא את הקובץ הסרוק האחרון
uploads = "uploads"
outputs = "outputs"

# חפש תמונות PNG בתיקיית temp
import glob, tempfile

# הרץ ישירות על PDF האחרון שנוצר
import sqlite3
con = sqlite3.connect("db/history.db")
rows = con.execute(
    "SELECT original_name, output_path FROM documents ORDER BY created_at DESC LIMIT 5"
).fetchall()
print("מסמכים אחרונים:")
for r in rows:
    print(f"  {r[0]} → {r[1]}")

# קח את הראשון שיש לו output
target_output = None
for r in rows:
    if r[1] and os.path.exists(r[1]):
        target_output = r[1]
        target_name = r[0]
        break

if not target_output:
    print("לא נמצא קובץ output")
    sys.exit(1)

print(f"\nבודק: {target_name}")

# המר עמוד 1 לתמונה
from pdf2image import convert_from_path
poppler_path = os.environ.get("POPPLER_PATH", "C:/poppler/Library/bin")
pages = convert_from_path(target_output, dpi=150, first_page=1, last_page=1,
                          poppler_path=poppler_path)
img = pages[0]

# הרץ Tesseract
import pytesseract
from pytesseract import Output as TessOutput
tess_cmd = os.environ.get("TESSERACT_CMD", "")
if tess_cmd and os.path.isfile(tess_cmd):
    pytesseract.pytesseract.tesseract_cmd = tess_cmd

print("\n--- image_to_string (raw) ---")
raw = pytesseract.image_to_string(img, lang="heb", config="--psm 6")
print(repr(raw[:300]))

print("\n--- image_to_string שורות ---")
for line in raw.strip().split("\n")[:5]:
    if line.strip():
        print(f"  raw: {repr(line)}")
        from bidi.algorithm import get_display
        fixed = get_display(line.strip(), base_dir="R")
        print(f"  after get_display: {repr(fixed)}")
        print(f"  reversed: {repr(line.strip()[::-1])}")
        print()

print("\n--- image_to_data words (first 10) ---")
data = pytesseract.image_to_data(img, lang="heb", config="--psm 6",
                                  output_type=TessOutput.DICT)
n = len(data["text"])
count = 0
for j in range(n):
    if data["level"][j] == 5 and str(data["text"][j]).strip() and int(data["conf"][j]) > 30:
        print(f"  word={repr(data['text'][j])}  x={data['left'][j]}  conf={data['conf'][j]}")
        count += 1
        if count >= 10:
            break
