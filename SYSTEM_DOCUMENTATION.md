# מערכת הנגשת מסמכים — עיריית אילת
## תיעוד טכני ותקינה

**גרסה:** 3.0  
**תאריך:** מאי 2026  
**מפתח:** Claude Code / Anthropic  

---

## 1. סקירה כללית

מערכת web לעיבוד מסמכים (PDF, Word, PowerPoint, Excel, דואר אלקטרוני) והפיכתם לנגישים עבור אנשים עם מוגבלויות. המערכת פועלת על שרת Windows פנימי בעיריית אילת ומשרתת את מחלקות העירייה.

**נתונים עד מאי 2026:**
- 40+ מסמכים עובדו
- ציון ממוצע: 96.1%
- פעיל מאפריל 2026

---

## 2. תקנים שהמערכת עומדת בהם

### PDF/UA-1 (ISO 14289-1)
התקן הבינלאומי הרשמי לנגישות קובצי PDF. מוודא שמסמכי PDF נגישים לטכנולוגיות עזר (screen readers, תוכנות הגדלה, קוראי braille).

**אימות:** veraPDF 1.30.1 — 106 חוקים, 0 כשלונות.

### IS 5568 — תקן ישראלי
התקן הישראלי לנגישות מסמכים דיגיטליים, המבוסס על WCAG 2.1 AA. מחייב על-פי **תקנות שוויון זכויות לאנשים עם מוגבלות (התאמות נגישות לשירות), התשע"ג-2013**.

### WCAG 2.1 AA
הנחיות הנגישות של W3C לתוכן רשת. המערכת מיישמת:
- **1.1.1** — חלופת טקסט לתמונות (alt text)
- **1.3.1** — מידע ומבנה (כותרות, רשימות, טבלאות)
- **1.3.2** — סדר קריאה משמעותי (RTL עברית)
- **1.4.5** — טקסט כתמונה (OCR)
- **2.4.2** — כותרת עמוד (Title)

---

## 3. שש בדיקות הנגישות האוטומטיות

כל מסמך מקבל ציון 0–100 המחושב מ-6 רכיבים:

| # | רכיב | ניקוד | בדיקה |
|---|---|---|---|
| 1 | **שכבת טקסט קריאה** | 35 נק' | OCR / טקסט דיגיטלי + /ActualText ב-StructTree |
| 2 | **תיוג מבנה PDF/UA** | 25 נק' | קיום StructTreeRoot עם H1/H2/P/Figure/Table |
| 3 | **שפת המסמך** | 20 נק' | /Lang = he-IL ב-Root |
| 4 | **כותרת מסמך** | 10 נק' | /Title ב-metadata |
| 5 | **מזהה PDF/UA-1** | 5 נק' | pdfuaid:part=1 ב-XMP |
| 6 | **MarkInfo** | 5 נק' | MarkInfo/Marked=true |

**סף ציונים:**
- 🟢 85–100 = תקני מלא (compliant)
- 🟡 60–84 = דורש בדיקה (needs review)
- 🔴 מתחת 60 = לא תקין (non-compliant)

---

## 4. צינור העיבוד (Pipeline)

### מסמך סרוק (תמונה)
```
קלט PDF סרוק
    ↓
המרת עמודים לתמונות PNG (pdf2image / Poppler)
    ↓
OCR — Tesseract 5 (עברית + אנגלית)
confidence מינימלי: 30% למילה
    ↓
[אם ANTHROPIC_API_KEY מוגדר]
Claude Haiku — ניתוח תמונת העמוד ישירות
החזרת כותרות / פסקאות / טבלאות מדויקים
    ↓
סינון OCR — הסרת שורות גרועות:
  • >55% מילים חד-תוויות
  • פחות מ-4 אותיות עבריות בשורה
  • >25% טוקנים ללא אות/ספרה
    ↓
בניית StructTree — BDC/EMC עם MCIDs
( ) Tj + /ActualText (PDF/UA תקני)
    ↓
XMP metadata: pdfuaid:part=1, Lang, Title
    ↓
veraPDF validation — 106 חוקים
    ↓
פלט PDF/UA-1 נגיש
```

### מסמך דיגיטלי (טקסט)
```
קלט PDF / Word / PowerPoint / Excel
    ↓
[Word/PPT/XLS] המרה ל-PDF דרך LibreOffice
    ↓
pdfminer — חילוץ בלוקי טקסט עם מיקום
    ↓
DocumentClassifier — זיהוי סוג מסמך:
פרוטוקול / חוק / תוכנית עבודה / עלון / טופס / כללי
    ↓
StructureDetector — זיהוי כותרות / רשימות / טבלאות
HeadingDetector — H1/H2/H3 לפי גודל גופן ומיקום
TableDetector — זיהוי טבלאות לפי מרווחים
    ↓
[אם ANTHROPIC_API_KEY] merge_ai_structure()
    ↓
inject_digital() — כתיבת StructTree + ParentTree
    ↓
FileValidator — בדיקת PDF שנוצר
```

---

## 5. תמיכה ב-RTL עברית

- סדר קריאה: מימין לשמאל, מלמעלה למטה
- מיון אלמנטים: `(עמוד, y-מלמעלה, -x-מימין)`
- bidi algorithm: python-bidi לתיקון כיוון
- /ActualText: UTF-16 BE + BOM לקריאה נכונה ב-screen readers
- Lang: he-IL על כל המסמך

---

## 6. AI — Claude Haiku (אופציונלי)

כש-`ANTHROPIC_API_KEY` מוגדר, המערכת שולחת תמונת כל עמוד ל-Claude Haiku לשני שימושים:

**a. תיאור תמונות (WCAG 1.1.1):**
כל עמוד סרוק מקבל alt text מדויק בעברית.

**b. ניתוח מבנה (WCAG 1.3.1):**
Claude מזהה כותרות, פסקאות, שורות טבלה — מדויק יותר מ-Tesseract לבדו.

**עלות:** ~$0.003 לעמוד (~$0.07 ל-24 עמודים).

---

## 7. פורמטים נתמכים

| פורמט | עיבוד |
|---|---|
| PDF סרוק | OCR + tagging |
| PDF דיגיטלי | tagging ישיר |
| .docx / .doc | LibreOffice → PDF → tagging |
| .pptx / .ppt | LibreOffice → PDF → tagging |
| .xlsx / .xls | LibreOffice → PDF → tagging |
| .eml (דואר) | parser → LibreOffice → PDF |
| .msg (Outlook) | extract-msg → LibreOffice → PDF |

**גודל מקסימלי:** 200 MB | **עמודים מקסימלי:** 300

---

## 8. אבטחה

- אימות סיסמה + session (12 שעות)
- בקרת גישה לפי מחלקה (X-Department header)
- קבצי upload נמחקים לאחר עיבוד
- כל פעולה נרשמת ב-audit log (SQLite)

---

## 9. חתימת נוסח מונגש

כל מסמך מנוגש מכיל הערה ברורה:

> **"נוסח נגיש למטרת נגישות בלבד.**  
> **הנוסח המחייב הוא המקור הסרוק והחתום המצורף."**

---

## 10. תשתית טכנית

| רכיב | פרטים |
|---|---|
| שרת web | Python 3.11 + Flask + Waitress |
| OCR | Tesseract 5 (heb+eng) |
| עיבוד PDF | pikepdf 9.x + ReportLab |
| חילוץ טקסט | pdfminer.six |
| המרת Office | LibreOffice headless |
| תמונות | pdf2image + Pillow |
| AI | Anthropic Claude Haiku |
| DB | SQLite |
| פורט | 5001 |

---

## 11. אימות חיצוני

**veraPDF** (גרסה 1.30.1) — הכלי הרשמי של PDF Association לבדיקת PDF/UA-1:

```
צו הארנונה 2022 חתום — isCompliant="true"
  passedRules=106  failedRules=0
  passedChecks=3675  failedChecks=0

20190806085623072 — isCompliant="true"
  passedRules=106  failedRules=0
  passedChecks=2047  failedChecks=0
```

**בדיקה נוספת מומלצת:** PAC (PDF Accessibility Checker) — pdfua.foundation/pac
