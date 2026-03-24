import os
import json
import uuid
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT,
                pages INTEGER,
                status TEXT DEFAULT 'processing',
                error TEXT,
                created_at TEXT,
                output_path TEXT
            )
        """)

init_db()

# -- Jobs dict for progress tracking --
jobs = {}

def process_pdf(job_id, input_path, output_path, original_name):
    try:
        jobs[job_id] = {'status': 'processing', 'progress': 10}

        # Count pages first
        result = subprocess.run(
            ['python3', '-c',
             f"import pikepdf; pdf=pikepdf.open('{input_path}'); print(len(pdf.pages))"],
            capture_output=True, text=True, timeout=30
        )
        pages = int(result.stdout.strip()) if result.returncode == 0 else 0
        jobs[job_id]['progress'] = 30

        # Extract title from filename
        title = Path(original_name).stem.replace('-', ' ').replace('_', ' ')

        # Run accessibility script
        cmd = [
            'python3', str(SCRIPT_PATH),
            '--input', str(input_path),
            '--output', str(output_path),
            '--lang', 'he-IL',
            '--title', title,
            '--dpi', '200',
            '--stamp'
        ]
        jobs[job_id]['progress'] = 50

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        jobs[job_id]['progress'] = 90

        if proc.returncode != 0:
            raise Exception(proc.stderr or "שגיאה בעיבוד הקובץ")

        # Update DB
        with get_db() as conn:
            conn.execute(
                "UPDATE documents SET status='done', pages=?, output_path=? WHERE id=?",
                (pages, str(output_path), job_id)
            )

        jobs[job_id] = {'status': 'done', 'progress': 100}

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                "UPDATE documents SET status='error', error=? WHERE id=?",
                (str(e), job_id)
            )
        jobs[job_id] = {'status': 'error', 'error': str(e)}
    finally:
        if Path(input_path).exists():
            os.remove(input_path)


# -- Routes --
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'לא נבחר קובץ'}), 400

    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'יש להעלות קובץ PDF בלבד'}), 400

    job_id = str(uuid.uuid4())
    input_path = UPLOAD_DIR / f"{job_id}_input.pdf"
    output_path = OUTPUT_DIR / f"{job_id}_accessible.pdf"

    file.save(input_path)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO documents (id, original_name, status, created_at) VALUES (?,?,?,?)",
            (job_id, file.filename, 'processing', datetime.now().strftime('%d/%m/%Y %H:%M'))
        )

    thread = threading.Thread(
        target=process_pdf,
        args=(job_id, input_path, output_path, file.filename)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'job_id': job_id})

@app.route('/api/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id, {'status': 'processing', 'progress': 5})
    return jsonify(job)

@app.route('/api/download/<job_id>')
def download(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (job_id,)).fetchone()
    if not row or row['status'] != 'done':
        return jsonify({'error': 'קובץ לא נמצא'}), 404

    output_path = Path(row['output_path'])
    if not output_path.exists():
        return jsonify({'error': 'קובץ לא קיים בדיסק'}), 404

    original = Path(row['original_name']).stem
    return send_file(
        output_path,
        as_attachment=True,
        download_name=f"{original}_נגיש.pdf",
        mimetype='application/pdf'
    )

@app.route('/api/history')
def history():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/delete/<job_id>', methods=['DELETE'])
def delete(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT output_path FROM documents WHERE id=?", (job_id,)).fetchone()
        if row and row['output_path']:
            p = Path(row['output_path'])
            if p.exists():
                p.unlink()
        conn.execute("DELETE FROM documents WHERE id=?", (job_id,))
    return jsonify({'ok': True})

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
