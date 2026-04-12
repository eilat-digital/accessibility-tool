# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based web service that processes PDF files to make them accessible according to PDF/UA-1 and IS 5568 standards. Built for Eilat Municipality (עיריית אילת) with full Hebrew/RTL support. Documentation and comments are in Hebrew.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server (localhost:5000)
python app.py

# Process a single PDF directly (bypassing the web layer)
python scripts/build_accessible_pdf.py --input path/to/input.pdf --output path/to/output.pdf --lang he-IL --title "Document Title" --dpi 200

# Health check
curl http://localhost:5000/api/health
```

**External dependencies required (not in pip):**
- Tesseract OCR with Hebrew + English language packs (`tesseract-ocr`, `tesseract-ocr-heb`)
- Poppler utilities (for `pdf2image` / `pdftoppm`)

## Architecture

The processing pipeline is split across two files:

- **[app.py](app.py)** — Flask server with 9 REST API endpoints, SQLite DB management, and background-threaded job orchestration. Calls `build_accessible_pdf.py` via `subprocess`.
- **[scripts/build_accessible_pdf.py](scripts/build_accessible_pdf.py)** — Standalone CLI script that performs the actual PDF work: OCR (pytesseract + pdf2image), metadata injection, PDF/UA tagging via pikepdf. Returns a JSON result on stdout that `app.py` parses.

**Job lifecycle:** Upload → UUID assigned → background thread spawns subprocess → status polled via `/api/status/{job_id}` → download via `/api/download/{job_id}`.

**Storage:**
- `uploads/` — incoming PDFs (temporary)
- `outputs/` — processed PDFs
- `db/history.db` — SQLite: `documents` table (metadata + scores) and `operation_logs` table (audit trail)
- `logs/app.log` — application log

**Department access control:** Enforced via `X-Department` HTTP header. Allowed values: `אתרים`, `דיגיטל`, `מנהל`, `שרות`, `אדמיניסטרציה`.

**DPI heuristic:** Files ≤50MB use 200 DPI for OCR; files >50MB use 150 DPI to reduce memory pressure.

## Related: WordPress Site

`C:\xampp\htdocs\eilatmuni` is a separate WordPress installation (Eilat Municipality site, MySQL `eilatmuni_db`, accessed at `http://localhost/eilatmuni`). It is independent from this Flask service but serves the same organization. The custom plugin `accessible-poetry` (`wp-content/plugins/accessible-poetry/`) adds an accessibility toolbar to WordPress.
