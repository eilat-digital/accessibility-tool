# 📄 מערכת הנגשת מסמכים - API Documentation

## תיאור כללי
מערכת web המאפשרת להנגיש מסמכי PDF בהתאם לתקנים בינלאומיים (PDF/UA, IS 5568).

## בסיס ה-URL
```
http://localhost:5000/api
```

---

## Endpoints

### 1. POST `/api/upload`
**תיאור:** העלאת קובץ PDF להנגשה

**Request:**
- Content-Type: `multipart/form-data`
- Body:
  - `file` (binary): קובץ PDF (עד 200MB)

**Response:**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Status Codes:**
- `200`: הועלה בהצלחה
- `400`: קובץ לא תקין (לא PDF או שדה חסר)

**דוגמה:**
```bash
curl -X POST \
  -F "file=@document.pdf" \
  http://localhost:5000/api/upload
```

---

### 2. GET `/api/status/{job_id}`
**תיאור:** בדיקת מצב עיבוד וקבלת אחוז ההתקדמות

**Parameters:**
- `job_id` (path): UUID של עבודת העיבוד

**Response:**
```json
{
  "status": "processing",
  "progress": 50
}
```

**או בעת השלמה:**
```json
{
  "status": "done",
  "progress": 100
}
```

**או בעת שגיאה:**
```json
{
  "status": "error",
  "error": "תיאור השגיאה"
}
```

**Status Values:**
- `processing`: בעיבוד
- `done`: הושלם בהצלחה
- `error`: תקלה

---

### 3. GET `/api/document/{job_id}`
**תיאור:** קבלת מטא-נתונים מפורטי של מסמך מנוגש

**Parameters:**
- `job_id` (path): UUID של המסמך

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "original_name": "report.pdf",
  "file_size": 2048576,
  "pages": 12,
  "status": "done",
  "created_at": "2026-03-25T14:30:00",
  "updated_at": "2026-03-25T14:35:00",
  "processing_time_seconds": 300,
  "accessibility_features": [
    "OCR - זיהוי טקסט",
    "תיוג PDF/UA",
    "מטא-נתונים",
    "סימן מים",
    "שפה: עברית"
  ]
}
```

**Status Codes:**
- `200`: הצליח
- `404`: מסמך לא נמצא

---

### 4. GET `/api/download/{job_id}`
**תיאור:** הורדת קובץ PDF מונגש

**Parameters:**
- `job_id` (path): UUID של המסמך

**Response:**
- Binary PDF file

**Headers:**
- `Content-Type: application/pdf`
- `Content-Disposition: attachment`

**Status Codes:**
- `200`: הצליח
- `404`: קובץ לא נמצא או עדיין בעיבוד

---

### 5. GET `/api/history`
**תיאור:** קבלת היסטוריית כל המסמכים שעובדו

**Response:**
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "original_name": "document.pdf",
    "file_size": 1024000,
    "pages": 5,
    "status": "done",
    "processing_time_seconds": 45,
    "created_at": "2026-03-25T14:30:00",
    "updated_at": "2026-03-25T14:30:45"
  },
  ...
]
```

**Query Parameters:**
- אין (מחزיר 100 מסמכים אחרונים)

---

### 6. GET `/api/stats`
**תיאור:** קבלת נתונים סטטיסטיים מצטברים על כל המסמכים שעובדו

**Response:**
```json
{
  "total_documents": 150,
  "successful": 145,
  "failed": 5,
  "success_rate": 96.7,
  "total_pages": 2345,
  "total_size_mb": 45.23,
  "avg_processing_time_seconds": 120.5,
  "documents_today": 12
}
```

**שדות:**
- `total_documents`: סך הכל מסמכים
- `successful`: מסמכים שהונגשו בהצלחה
- `failed`: מסמכים שנכשלו
- `success_rate`: אחוז הצלחה
- `total_pages`: סך הכל עמודים שעובדו
- `total_size_mb`: גודל כולל בMB
- `avg_processing_time_seconds`: זמן עיבוד ממוצע
- `documents_today`: מסמכים מהיום

---

### 7. DELETE `/api/delete/{job_id}`
**תיאור:** מחיקת מסמך והקובץ המנוגש שלו

**Parameters:**
- `job_id` (path): UUID של המסמך

**Response:**
```json
{
  "ok": true
}
```

**Status Codes:**
- `200`: הצליח
- `404`: מסמך לא נמצא

---

### 8. GET `/api/health`
**תיאור:** בדיקת בריאות המערכת (health check)

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2026-03-25T14:35:00",
  "version": "2.0",
  "database": "connected"
}
```

**Status Codes:**
- `200`: כל הסיסטם תקין

---

### 9. GET `/api/docs`
**תיאור:** תיעוד API בדפדפן (HTML)

**Response:** קובץ HTML עם תיעוד מלא

**Status Codes:**
- `200`: הצליח

---

## תכונות הנגשה המיושמות

כל קובץ PDF מונגש לוקח בעצמו את התכונות הבאות:

1. **OCR (Optical Character Recognition)** - זיהוי טקסט מתמונות בקובץ
2. **PDF/UA Tagging** - תיוג מבנה המסמך לקומפטיביליות עם קוראי מסך
3. **Metadata** - הוספת מטא-נתונים (כותרת, מחבר, שפה)
4. **Watermark** - סימן מים המציין הנגשה
5. **RTL Support** - תמיכה בטקסט מימין לשמאל (עברית)

---

## קודי שגיאה נפוצים

| Code | Message | פתרון |
|------|---------|--------|
| 400 | יש להעלות קובץ PDF בלבד | וודא שהקובץ הוא PDF |
| 404 | קובץ לא נמצא | בדוק את ה-job_id |
| 500 | שגיאה בעיבוד הקובץ | נסה שוב או צור קשר לתמיכה |

---

## דוגמאות שימוש

### JavaScript/Fetch

```javascript
// העלאה
const fd = new FormData();
fd.append('file', fileElement.files[0]);
const res = await fetch('/api/upload', { method: 'POST', body: fd });
const data = await res.json();
const jobId = data.job_id;

// בדיקת סטטוס
const status = await fetch(`/api/status/${jobId}`).then(r => r.json());
console.log(status.progress); // 0-100

// הורדה
window.location = `/api/download/${jobId}`;

// קבלת סטטיסטיקות
const stats = await fetch('/api/stats').then(r => r.json());
console.log(stats.success_rate); // אחוז הצלחה
```

### Python/Requests

```python
import requests

# העלאה
files = {'file': open('document.pdf', 'rb')}
res = requests.post('http://localhost:5000/api/upload', files=files)
job_id = res.json()['job_id']

# בדיקת סטטוס
status = requests.get(f'http://localhost:5000/api/status/{job_id}').json()
print(status['progress'])

# הורדה
r = requests.get(f'http://localhost:5000/api/download/{job_id}')
with open('output.pdf', 'wb') as f:
    f.write(r.content)
```

### cURL

```bash
# העלאה
curl -X POST -F "file=@document.pdf" http://localhost:5000/api/upload

# בדיקת סטטוס
curl http://localhost:5000/api/status/550e8400-e29b-41d4-a716-446655440000

# קבלת סטטיסטיקות
curl http://localhost:5000/api/stats

# הורדה
curl -O http://localhost:5000/api/download/550e8400-e29b-41d4-a716-446655440000
```

---

## טיעונים חשובים

- **Maximum file size**: 200MB
- **Supported format**: PDF בלבד
- **Processing timeout**: 300 שניות (5 דקות)
- **Language**: עברית (he-IL)
- **PDF Standard**: PDF/UA-1

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.0 | 2026-03-25 | הוספת logging, stats, features tracking, improved UI |
| 1.0 | 2026-03-20 | גרסה ראשונית |

