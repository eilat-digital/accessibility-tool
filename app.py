import os
import sys
import json
import uuid
import sqlite3
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS

PYTHON = sys.executable

# גבול עמודים — DPI יורד אוטומטית לפי גודל המסמך
MAX_PAGES = 300

def dpi_for_pages(page_count):
    """DPI דינמי: פחות עמודים = איכות גבוהה יותר, יותר עמודים = חוסך זיכרון."""
    if page_count <= 30:
        return 150   # איכות גבוהה — מסמכים קצרים
    elif page_count <= 100:
        return 120   # ברירת מחדל
    else:
        return 100   # 100-300 עמודים — מאזן זיכרון/איכות

# -- Logging Setup --
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB
CORS(app, resources={r"/api/*": {"origins": "*"}})

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
DB_PATH = BASE_DIR / "db" / "history.db"
SCRIPT_PATH = BASE_DIR / "scripts" / "build_accessible_pdf.py"

for d in [UPLOAD_DIR, OUTPUT_DIR, BASE_DIR / "db"]:
    d.mkdir(parents=True, exist_ok=True)

# -- DB --
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Create documents table with enhanced schema
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                file_size INTEGER,
                pages INTEGER,
                status TEXT DEFAULT 'processing',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                output_path TEXT,
                processing_time_seconds REAL,
                accessibility_features TEXT,
                accessibility_score REAL,
                validation_report TEXT
            )
        """)
        
        # Create logs table for audit trail
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                operation TEXT,
                status TEXT,
                message TEXT,
                timestamp TEXT,
                FOREIGN KEY (job_id) REFERENCES documents (id)
            )
        """)
        
        conn.commit()
    logger.info("Database initialized")

init_db()

# -- Jobs dict for progress tracking --
jobs = {}

def log_operation(job_id, operation, status, message=""):
    """Log operation to database audit trail"""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO operation_logs (job_id, operation, status, message, timestamp) VALUES (?,?,?,?,?)",
                (job_id, operation, status, message, datetime.now().isoformat())
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to log operation: {e}")

ALLOWED_EXTENSIONS = {
    '.pdf':  'PDF',
    '.docx': 'Word',
    '.doc':  'Word (ישן)',
    '.pptx': 'PowerPoint',
    '.ppt':  'PowerPoint (ישן)',
}

def convert_to_pdf(input_path: Path, work_dir: Path) -> Path:
    """המרת Word/PowerPoint ל-PDF באמצעות LibreOffice headless."""
    import shutil
    lo = shutil.which('libreoffice') or shutil.which('soffice')
    if not lo:
        raise Exception('LibreOffice אינו מותקן — לא ניתן להמיר את הקובץ')
    result = subprocess.run(
        [lo, '--headless', '--convert-to', 'pdf', '--outdir', str(work_dir), str(input_path)],
        capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        raise Exception(f'שגיאה בהמרה: {result.stderr or result.stdout}')
    pdf_path = work_dir / (input_path.stem + '.pdf')
    if not pdf_path.exists():
        raise Exception('ההמרה הסתיימה אך קובץ PDF לא נמצא')
    logger.info(f"Converted {input_path.suffix} → PDF: {pdf_path}")
    return pdf_path


def validate_pdf_accessibility(pdf_path):
    """בדיקת נגישות אמיתית של PDF לפי IS 5568 / PDF/UA-1 (ציון 0-100)

    משקלות לפי דרישות חוק הנגישות הישראלי:
      35 — שכבת טקסט (OCR) — WCAG 1.4.5, IS 5568 §7.1
      25 — תיוג מבנה PDF/UA (StructTreeRoot) — IS 5568 §7.2
      20 — שפת המסמך מוגדרת (/Lang) — WCAG 3.1.1
      10 — כותרת מוגדרת (/Title) — PDF/UA §7.4
       5 — מזהה PDF/UA-1 ב-XMP (pdfuaid:part) — ISO 14289-1 §6.2
       5 — MarkInfo/Marked = true — PDF/UA §7.3
    """
    try:
        import pikepdf

        score_data = {
            'has_text_content': 0,   # 35 — שכבת טקסט קריאה (OCR / דיגיטלי)
            'has_struct_tree': 0,    # 25 — תיוג מבנה PDF/UA
            'has_lang': 0,           # 20 — שפת המסמך מוגדרת
            'has_title': 0,          # 10 — כותרת מוגדרת
            'has_pdfua_id': 0,       #  5 — מזהה PDF/UA-1 ב-XMP
            'has_markinfo': 0,       #  5 — MarkInfo/Marked=true
        }

        with pikepdf.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)

            # בדיקת טקסט — pdfminer מחלץ טקסט בפועל (לא רק קיום stream)
            try:
                from pdfminer.high_level import extract_text
                sample_pages = list(range(min(3, total_pages)))
                text_sample = extract_text(pdf_path, page_numbers=sample_pages) or ''
                if len(text_sample.strip()) > 20:
                    score_data['has_text_content'] = 35
                elif len(text_sample.strip()) > 5:
                    score_data['has_text_content'] = 15  # טקסט חלקי
            except Exception:
                # fallback: בדוק אם יש אופרטורי טקסט ב-stream
                for page in pdf.pages[:min(3, total_pages)]:
                    try:
                        raw_obj = page.obj.get('/Contents')
                        if raw_obj is None:
                            continue
                        if hasattr(raw_obj, 'read_bytes'):
                            raw = raw_obj.read_bytes()
                        elif isinstance(raw_obj, pikepdf.Array):
                            raw = b''.join(x.read_bytes() for x in raw_obj if hasattr(x, 'read_bytes'))
                        else:
                            raw = b''
                        # אופרטורי טקסט ב-PDF: Tj, TJ, Tf
                        if b'Tj' in raw or b'TJ' in raw:
                            score_data['has_text_content'] = 35
                            break
                    except Exception:
                        pass

            # בדיקת תיוג מבנה PDF/UA
            if '/StructTreeRoot' in pdf.Root:
                score_data['has_struct_tree'] = 25

            # בדיקת שפה — ב-Root (PDF/UA דרישה ראשית)
            root_lang = str(pdf.Root.get('/Lang', '')).strip()
            if root_lang:
                score_data['has_lang'] = 20
            else:
                # fallback: Lang ב-docinfo (לא מספיק ל-PDF/UA אבל חלקי)
                meta_lang = str(pdf.docinfo.get('/Lang', '')).strip()
                if meta_lang:
                    score_data['has_lang'] = 10

            # בדיקת כותרת
            meta = pdf.docinfo
            if str(meta.get('/Title', '')).strip():
                score_data['has_title'] = 10

            # בדיקת מזהה PDF/UA-1 ב-XMP — ISO 14289-1 §6.2
            try:
                with pdf.open_metadata() as xmp:
                    pdfua_part = xmp.get('pdfuaid:part', '')
                    if str(pdfua_part).strip() == '1':
                        score_data['has_pdfua_id'] = 5
            except Exception:
                pass

            # בדיקת MarkInfo/Marked = true — PDF/UA §7.3
            mark_info = pdf.Root.get('/MarkInfo')
            if mark_info is not None:
                marked = mark_info.get('/Marked')
                if marked is not None and bool(marked):
                    score_data['has_markinfo'] = 5

        total_score = min(100, sum(score_data.values()))

        # מיפוי לתקן IS 5568
        if total_score >= 85:
            compliance_status = 'compliant'          # עומד בתקן
        elif total_score >= 60:
            compliance_status = 'needs_review'       # דורש בדיקה
        else:
            compliance_status = 'non_compliant'      # אינו עומד בתקן

        report = {
            'score': total_score,
            'components': score_data,
            'validation_date': datetime.now().isoformat(),
            'standard': 'IS 5568 / PDF/UA-1 / WCAG 2.2',
            'status': compliance_status
        }

        logger.info(f"PDF validated (IS 5568): score={report['score']}, status={report['status']}")
        return report

    except Exception as e:
        logger.error(f"Error validating PDF: {e}")
        return {
            'score': 0,
            'error': str(e),
            'status': 'error'
        }

def process_pdf(job_id, input_path, output_path, original_name, file_size):
    """Process PDF and make it accessible"""
    start_time = datetime.now()
    
    try:
        logger.info(f"Starting processing for job {job_id}: {original_name} (size: {file_size} bytes)")
        jobs[job_id] = {'status': 'processing', 'progress': 10}
        log_operation(job_id, 'start', 'in_progress')

        # Count pages first
        logger.info(f"Analyzing PDF pages for job {job_id}")
        result = subprocess.run(
            [PYTHON, '-c',
             f"import pikepdf; pdf=pikepdf.open('{input_path}'); print(len(pdf.pages))"],
            capture_output=True, text=True, timeout=60
        )
        pages = int(result.stdout.strip()) if result.returncode == 0 else 0
        jobs[job_id]['progress'] = 30
        log_operation(job_id, 'count_pages', 'success', f'Pages: {pages}')
        logger.info(f"File has {pages} pages")

        # Extract title from filename
        title = Path(original_name).stem.replace('-', ' ').replace('_', ' ')

        # DPI דינמי לפי מספר עמודים — שומר על זיכרון Railway (512 MB)
        dpi = str(dpi_for_pages(pages))
        logger.info(f"Using DPI: {dpi} for job {job_id} ({pages} pages)")

        # Run accessibility script
        logger.info(f"Running accessibility script for job {job_id}")
        cmd = [
            PYTHON, str(SCRIPT_PATH),
            '--input', str(input_path),
            '--output', str(output_path),
            '--lang', 'he-IL',
            '--title', title,
            '--author', 'עיריית אילת',
            '--dpi', dpi,
            '--stamp',
            '--ocr',   # IS 5568: scanned PDFs must have a text layer for screen readers
        ]
        jobs[job_id]['progress'] = 50

        # Timeout דינמי: 5 דקות בסיס + 4 שניות לעמוד (OCR איטי)
        # מינימום 5 דק', מקסימום 60 דק' (מסמך 300 עמודים ≈ 20 דק')
        timeout_seconds = max(300, min(3600, 300 + pages * 4))
        logger.info(f"Processing timeout set to {timeout_seconds}s for {pages} pages")
        
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        jobs[job_id]['progress'] = 90

        if proc.returncode != 0:
            error_msg = (proc.stderr or proc.stdout or "שגיאה בעיבוד הקובץ").strip()
            logger.error(f"Script error for job {job_id}: stdout={proc.stdout!r} stderr={proc.stderr!r}")
            raise Exception(error_msg)

        # Calculate processing time
        processing_time = (datetime.now() - start_time).total_seconds()
        
        # Define accessibility features applied
        accessibility_features = [
            "OCR - זיהוי טקסט",
            "תיוג PDF/UA",
            "מטא-נתונים",
            "סימן מים",
            "שפה: עברית"
        ]
        
        # Validate the generated PDF
        logger.info(f"Validating PDF for job {job_id}")
        validation_report = validate_pdf_accessibility(str(output_path))

        # Update DB with comprehensive information
        logger.info(f"Updating database for job {job_id}")
        with get_db() as conn:
            conn.execute(
                """UPDATE documents SET 
                   status='done', 
                   pages=?, 
                   output_path=?, 
                   processing_time_seconds=?,
                   accessibility_features=?,
                   accessibility_score=?,
                   validation_report=?,
                   updated_at=?
                   WHERE id=?""",
                (pages, str(output_path), processing_time, json.dumps(accessibility_features), 
                 validation_report['score'], json.dumps(validation_report), datetime.now().isoformat(), job_id)
            )
            conn.commit()

        jobs[job_id] = {'status': 'done', 'progress': 100, 'score': validation_report['score']}
        log_operation(job_id, 'complete', 'success', f'Time: {processing_time:.1f}s, Score: {validation_report["score"]}')
        logger.info(f"Successfully completed job {job_id} with accessibility score {validation_report['score']}")

    except Exception as e:
        logger.error(f"Error processing job {job_id}: {str(e)}")
        with get_db() as conn:
            conn.execute(
                "UPDATE documents SET status='error', error=?, updated_at=? WHERE id=?",
                (str(e), datetime.now().isoformat(), job_id)
            )
            conn.commit()
        jobs[job_id] = {'status': 'error', 'error': str(e)}
        log_operation(job_id, 'error', 'failed', str(e))
        
    finally:
        if Path(input_path).exists():
            os.remove(input_path)


# -- Routes --
@app.route('/')
def index():
    """Serve the main application interface"""
    logger.info("Serving index page")
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload():
    """Upload a PDF file for accessibility processing
    
    Returns:
        - 200: {job_id: str} - Job ID for tracking
        - 400: {error: str} - Validation error
    """
    if 'file' not in request.files:
        msg = 'לא נבחר קובץ'
        logger.warning(f"Upload attempt without file: {request.remote_addr}")
        return jsonify({'error': msg}), 400

    file = request.files['file']
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ', '.join(ALLOWED_EXTENSIONS.keys())
        msg = f'סוג קובץ לא נתמך. ניתן להעלות: {allowed}'
        logger.warning(f"Upload attempt with unsupported type: {file.filename}")
        return jsonify({'error': msg}), 400

    # Get file size
    file.seek(0, 2)  # Seek to end
    file_size = file.tell()
    file.seek(0)  # Seek back to start
    
    # Validate file size (max 200MB)
    max_size = 200 * 1024 * 1024
    if file_size > max_size:
        msg = f'קובץ גדול מדי. גודל מרבי: 200MB, הקובץ שלך: {file_size / (1024*1024):.1f}MB'
        logger.warning(f"Upload attempt with oversized file: {file.filename} ({file_size} bytes)")
        return jsonify({'error': msg}), 400
    
    # Warn if file is large (will take longer)
    if file_size > 50 * 1024 * 1024:
        logger.info(f"Large file upload: {file.filename} ({file_size/(1024*1024):.1f}MB) - will use optimized settings")

    job_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{job_id}_input.pdf"
    output_path = OUTPUT_DIR / f"{job_id}_accessible.pdf"

    file.save(input_path)

    # המרה ל-PDF אם נדרש (Word / PowerPoint)
    if ext != '.pdf':
        try:
            logger.info(f"Converting {ext} to PDF for job {job_id}")
            converted = convert_to_pdf(input_path, UPLOAD_DIR)
            input_path.unlink(missing_ok=True)
            input_path = converted
        except Exception as conv_err:
            input_path.unlink(missing_ok=True)
            logger.error(f"Conversion failed for job {job_id}: {conv_err}")
            return jsonify({'error': str(conv_err)}), 500

    # בדיקת מספר עמודים לפני עיבוד
    try:
        import pikepdf as _pk
        with _pk.open(input_path) as _pdf:
            page_count = len(_pdf.pages)
        if page_count > MAX_PAGES:
            input_path.unlink(missing_ok=True)
            msg = f'הקובץ מכיל {page_count} עמודים. המקסימום המותר הוא {MAX_PAGES} עמודים.'
            logger.warning(f"Upload rejected — too many pages: {page_count} (max {MAX_PAGES})")
            return jsonify({'error': msg}), 400
        logger.info(f"Page count OK: {page_count}/{MAX_PAGES}")
    except Exception as e:
        logger.warning(f"Could not count pages: {e}")

    logger.info(f"File uploaded: job_id={job_id}, filename={file.filename}, size={file_size} bytes")

    with get_db() as conn:
        conn.execute(
            """INSERT INTO documents (id, original_name, file_size, status, created_at) 
               VALUES (?,?,?,?,?)""",
            (job_id, file.filename, file_size, 'processing', datetime.now().isoformat())
        )
        conn.commit()

    # Start background processing
    thread = threading.Thread(
        target=process_pdf,
        args=(job_id, input_path, output_path, file.filename, file_size)
    )
    thread.daemon = True
    thread.start()

    logger.info(f"Processing started for job {job_id}")
    return jsonify({'job_id': job_id})

@app.route('/api/status/<job_id>')
def status(job_id):
    """Get processing status and progress for a job
    
    Args:
        job_id: UUID of the processing job
    
    Returns:
        {status: str, progress: int, error?: str}
    """
    job = jobs.get(job_id, {'status': 'processing', 'progress': 5})
    return jsonify(job)

@app.route('/api/document/<job_id>')
def get_document(job_id):
    """Get detailed information about a processed document
    
    Args:
        job_id: UUID of the document
    
    Returns:
        - 200: Document metadata including accessibility features
        - 404: Document not found
    """
    logger.info(f"Fetching document details for job {job_id}")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (job_id,)).fetchone()
    
    if not row:
        logger.warning(f"Document not found: {job_id}")
        return jsonify({'error': 'מסמך לא נמצא'}), 404
    
    doc = dict(row)
    if doc.get('accessibility_features'):
        doc['accessibility_features'] = json.loads(doc['accessibility_features'])
    
    return jsonify(doc)

@app.route('/api/download/<job_id>')
def download(job_id):
    """Download the processed accessible PDF file
    
    Args:
        job_id: UUID of the document
    
    Returns:
        - 200: PDF file
        - 404: Document not found or not ready
    """
    logger.info(f"Download requested for job {job_id}")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (job_id,)).fetchone()
    
    if not row or row['status'] != 'done':
        logger.warning(f"Download failed - invalid status: {job_id}")
        return jsonify({'error': 'קובץ לא נמצא'}), 404

    output_path = Path(row['output_path'])
    if not output_path.exists():
        logger.error(f"Output file not found on disk: {output_path}")
        return jsonify({'error': 'קובץ לא קיים בדיסק'}), 404

    original = Path(row['original_name']).stem
    logger.info(f"Downloaded: {original} (job {job_id})")
    
    return send_file(
        output_path,
        as_attachment=True,
        download_name=f"{original}_הונגש.pdf",
        mimetype='application/pdf'
    )

@app.route('/api/history')
def history():
    """Get list of all documents with processing history
    
    Returns:
        [{document metadata}] - Last 100 documents
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    
    docs = []
    for r in rows:
        doc = dict(r)
        if doc.get('accessibility_features'):
            doc['accessibility_features'] = json.loads(doc['accessibility_features'])
        docs.append(doc)
    
    logger.info(f"History requested - returning {len(docs)} documents")
    return jsonify(docs)

@app.route('/api/delete/<job_id>', methods=['DELETE'])
def delete(job_id):
    """Delete a document and its processed output
    
    Args:
        job_id: UUID of the document
    
    Returns:
        {ok: bool}
    """
    logger.info(f"Delete requested for job {job_id}")
    with get_db() as conn:
        row = conn.execute("SELECT output_path FROM documents WHERE id=?", (job_id,)).fetchone()
        if row and row['output_path']:
            p = Path(row['output_path'])
            if p.exists():
                p.unlink()
                logger.info(f"Deleted output file: {p}")
        conn.execute("DELETE FROM documents WHERE id=?", (job_id,))
        conn.commit()
    
    logger.info(f"Document deleted: {job_id}")
    return jsonify({'ok': True})

@app.route('/api/validate/<job_id>')
def validate(job_id):
    """Get accessibility validation report for a processed document
    
    Args:
        job_id: UUID of the document
    
    Returns:
        {score: 0-100, status: compliant|needs_review|error, components: {...}}
    """
    logger.info(f"Validation requested for job {job_id}")
    with get_db() as conn:
        row = conn.execute(
            "SELECT accessibility_score, validation_report FROM documents WHERE id=?", 
            (job_id,)
        ).fetchone()
    
    if not row:
        logger.warning(f"Document not found for validation: {job_id}")
        return jsonify({'error': 'מסמך לא נמצא'}), 404
    
    if row['validation_report']:
        report = json.loads(row['validation_report'])
    else:
        report = {'score': 0, 'status': 'pending', 'message': 'עדיין בעיבוד'}
    
    return jsonify(report)

@app.route('/api/stats')
def get_stats():
    """Get aggregate statistics about processed documents
    
    Returns:
        {total: int, successful: int, success_rate: float, total_pages: int, total_size: int, today_count: int}
    """
    with get_db() as conn:
        stats = conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as successful,
                SUM(pages) as total_pages,
                SUM(file_size) as total_size,
                AVG(processing_time_seconds) as avg_time
            FROM documents
        """).fetchone()
        
        today = datetime.now().date().isoformat()
        today_count = conn.execute(
            "SELECT COUNT(*) as count FROM documents WHERE date(created_at) = ?",
            (today,)
        ).fetchone()['count']
    
    total = stats['total'] or 0
    successful = stats['successful'] or 0
    success_rate = (successful / total * 100) if total > 0 else 0
    
    result = {
        'total_documents': total,
        'successful': successful,
        'failed': total - successful,
        'success_rate': round(success_rate, 1),
        'total_pages': stats['total_pages'] or 0,
        'total_size_mb': round((stats['total_size'] or 0) / (1024 * 1024), 2),
        'avg_processing_time_seconds': round(stats['avg_time'] or 0, 1),
        'documents_today': today_count
    }
    
    logger.info(f"Stats retrieved: {result}")
    return jsonify(result)

@app.route('/api/health')
def health_check():
    """Health check endpoint for monitoring
    
    Returns:
        {status: 'ok', timestamp: str, version: str}
    """
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'version': '2.0',
        'database': 'connected',
        'limits': {
            'max_pages': MAX_PAGES,
            'dpi_tiers': {'1-30': 150, '31-100': 120, '101-300': 100},
            'max_file_size_mb': 200
        }
    })

@app.route('/api/docs')
def api_docs():
    """API Documentation endpoint
    
    Returns HTML with API endpoint documentation
    """
    docs_html = """
    <!DOCTYPE html>
    <html lang="he" dir="rtl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>API Documentation</title>
        <style>
            body { font-family: 'Heebo', sans-serif; max-width: 900px; margin: 2rem auto; padding: 1rem; }
            h1 { color: #1A4E8A; }
            .endpoint { background: #f5f5f5; padding: 1rem; margin: 1rem 0; border-radius: 8px; border-left: 4px solid #1A4E8A; }
            .method { font-weight: bold; color: #2563B0; }
            .path { font-family: monospace; }
            code { background: #eee; padding: 2px 6px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <h1>📄 מערכת הנגשת מסמכים - API Documentation</h1>
        
        <div class="endpoint">
            <div class="method">POST</div>
            <div class="path">/api/upload</div>
            <p>העלאת קובץ PDF להנגשה</p>
            <p><strong>Returns:</strong> <code>{job_id: string}</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/status/{job_id}</div>
            <p>בדיקת מצב עיבוד</p>
            <p><strong>Returns:</strong> <code>{status, progress, error?}</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/document/{job_id}</div>
            <p>קבלת מטא-נתונים של מסמך</p>
            <p><strong>Returns:</strong> <code>{id, status, pages, accessibility_features[], ...}</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/download/{job_id}</div>
            <p>הורדת קובץ PDF מונגש</p>
            <p><strong>Returns:</strong> Binary PDF file</p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/history</div>
            <p>קבלת רשימת כל המסמכים</p>
            <p><strong>Returns:</strong> <code>[{document metadata}, ...]</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/stats</div>
            <p>נתונים סטטיסטיים מצטברים</p>
            <p><strong>Returns:</strong> <code>{total_documents, successful, success_rate, total_pages, total_size_mb, ...}</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">DELETE</div>
            <div class="path">/api/delete/{job_id}</div>
            <p>מחיקת מסמך</p>
            <p><strong>Returns:</strong> <code>{ok: true}</code></p>
        </div>
        
        <div class="endpoint">
            <div class="method">GET</div>
            <div class="path">/api/health</div>
            <p>בדיקת בריאות המערכת</p>
            <p><strong>Returns:</strong> <code>{status, timestamp, version}</code></p>
        </div>
    </body>
    </html>
    """
    return docs_html, 200, {'Content-Type': 'text/html; charset=utf-8'}

if __name__ == '__main__':
    logger.info("Starting accessibility tool server")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
