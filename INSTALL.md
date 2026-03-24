# מערכת הנגשת מסמכים — מדריך התקנה והפעלה
## עיריית אילת — מחלקה דיגיטלית

---

## דרישות מערכת

- **שרת לינוקס** (Ubuntu 20.04+ / Debian 11+) — או Windows עם WSL
- **Python 3.10+**
- **חיבור לאינטרנט** (להתקנה בלבד, לאחר מכן עובד offline)
- **דיסק**: לפחות 2GB פנויים

---

## שלב 1 — התקנת Python ו-pip

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
python3 --version   # אמור להציג 3.10 ומעלה
```

---

## שלב 2 — התקנת Poppler (לפיצול PDF לתמונות)

```bash
sudo apt install -y poppler-utils
```

**לאימות:**
```bash
pdftoppm -v   # אמור להציג גרסה
```

---

## שלב 3 — העתקת קבצי הפרויקט לשרת

העתק את תיקיית `accessibility-tool` לשרת, למשל:
```
/opt/accessibility-tool/
```

מבנה התיקייה הנדרש:
```
accessibility-tool/
├── app.py
├── requirements.txt
├── templates/
│   └── index.html
├── scripts/
│   └── build_accessible_pdf.py   ← הסקריפט הקיים
├── uploads/          ← נוצר אוטומטית
├── outputs/          ← נוצר אוטומטית
└── db/               ← נוצר אוטומטית
```

---

## שלב 4 — סביבה וירטואלית והתקנת תלויות

```bash
cd /opt/accessibility-tool

# יצירת סביבה וירטואלית
python3 -m venv venv

# הפעלת הסביבה
source venv/bin/activate

# התקנת חבילות Python
pip install -r requirements.txt
```

---

## שלב 5 — הפעלת השרת (בדיקה ראשונית)

```bash
cd /opt/accessibility-tool
source venv/bin/activate
python3 app.py
```

פתח דפדפן: `http://localhost:5000`

אם הכל תקין — תראה את ממשק ההנגשה.

לסגירה: `Ctrl+C`

---

## שלב 6 — הפעלה כשירות קבוע (Systemd)

כדי שהמערכת תעלה אוטומטית עם השרת:

```bash
sudo nano /etc/systemd/system/accessibility.service
```

הדבק את הטקסט הבא:
```ini
[Unit]
Description=Eilat Municipality PDF Accessibility Tool
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/accessibility-tool
ExecStart=/opt/accessibility-tool/venv/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable accessibility
sudo systemctl start accessibility
sudo systemctl status accessibility
```

---

## שלב 7 — גישה מהרשת המקומית

ברירת המחדל: פורט **5000**.

אם רוצים גישה בפורט 80 (ללא לציין פורט בכתובת), אפשר:

**אפשרות א׳ — פורט ישיר (פשוט יותר):**
```bash
sudo ufw allow 5000
```
גישה: `http://IP_השרת:5000`

**אפשרות ב׳ — Nginx כ-reverse proxy (מומלץ לפרודקשן):**
```bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/accessibility
```
```nginx
server {
    listen 80;
    server_name _;
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/accessibility /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## פקודות שימושיות לתחזוקה

```bash
# צפייה בלוגים
sudo journalctl -u accessibility -f

# הפעלה מחדש
sudo systemctl restart accessibility

# עצירה
sudo systemctl stop accessibility

# ניקוי קבצי outputs ישנים (מעל 30 יום)
find /opt/accessibility-tool/outputs -mtime +30 -delete
```

---

## פתרון בעיות נפוצות

| בעיה | פתרון |
|------|--------|
| `ModuleNotFoundError: flask` | הרץ `source venv/bin/activate` לפני ה-python |
| `pdftoppm: command not found` | `sudo apt install poppler-utils` |
| שגיאת הרשאות בתיקיית outputs | `sudo chown -R www-data:www-data /opt/accessibility-tool` |
| הדפדפן לא מגיע לשרת | בדוק `sudo ufw allow 5000` |

---

## גיבוי

קובץ מסד הנתונים עם ההיסטוריה:
```
/opt/accessibility-tool/db/history.db
```
מומלץ לגבות אותו אחת לשבוע.

---

_גרסה 1.0 — עיריית אילת, מחלקה דיגיטלית_
