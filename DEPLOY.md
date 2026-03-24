# מדריך העלאה ל-GitHub + Render
## עיריית אילת — הקמת מערכת הנגשה

---

## שלב 1 — GitHub: יצירת חשבון ו-Repository

1. היכנסי ל: **https://github.com**
2. לחצי **Sign up** → הזיני אימייל, סיסמה, שם משתמש
3. אמתי את האימייל

4. לאחר ההרשמה — לחצי **New repository** (כפתור ירוק)
5. מלאי:
   - Repository name: `accessibility-tool`
   - ✅ Public (כדי ש-Render יוכל לקרוא)
   - לחצי **Create repository**

6. בדף שנפתח — לחצי **uploading an existing file**
7. גרורי את **כל הקבצים מתוך ה-zip** (לאחר פתיחתו):
   ```
   accessibility-tool/
   ├── app.py
   ├── requirements.txt
   ├── render.yaml
   ├── Procfile
   ├── INSTALL.md
   ├── templates/
   │   └── index.html
   └── scripts/
       └── build_accessible_pdf.py
   ```
8. לחצי **Commit changes**

---

## שלב 2 — Render: חיבור והפעלה

1. היכנסי ל: **https://render.com**
2. לחצי **Get Started for Free**
3. בחרי **Sign in with GitHub** — מחבר אוטומטית

4. לחצי **New +** → **Web Service**
5. בחרי את ה-repository: `accessibility-tool`
6. לחצי **Connect**

7. Render יזהה אוטומטית את ה-`render.yaml` ויגדיר הכל.
   **אין צורך לשנות כלום** — פשוט גללי למטה ולחצי:
   **Create Web Service**

8. המתיני ~5 דקות — Render:
   - מתקין Python
   - מתקין poppler-utils
   - מתקין את כל החבילות מ-requirements.txt
   - מעלה את האתר

9. בסיום תראי:
   ```
   ✅ Your service is live at:
   https://accessibility-tool-xxxx.onrender.com
   ```

---

## שלב 3 — בדיקה ראשונה

1. פתחי את הכתובת בדפדפן
2. גרורי קובץ PDF כלשהו
3. המתיני ~30 שניות (בפעם הראשונה השרת מתעורר)
4. וודאי שמקבלים קובץ נגיש עם חותמת ✓

---

## הערות חשובות

**גרסה חינמית של Render:**
- השרת "נרדם" אחרי 15 דקות ללא שימוש
- מתעורר תוך ~30 שניות בפעם הבאה
- לשימוש יומיומי במחלקה — מספיק לגמרי

**שדרוג ל-$7/חודש (אם צריך):**
- השרת לא נרדם
- מהירות טובה יותר
- דיסק קבוע לאורך זמן

---

_גרסה 1.0 — עיריית אילת, מחלקה דיגיטלית_
