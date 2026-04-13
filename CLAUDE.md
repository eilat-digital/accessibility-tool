# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Flask-based web service that processes documents (PDF, Office, email) to make them accessible according to PDF/UA-1 and IS 5568 standards. Built for Eilat Municipality (עיריית אילת) with full Hebrew/RTL support. All code comments and UI are in Hebrew.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server (localhost:5000)
python app.py

# Run production server (waitress, port 5001)
python run_server.py

# Process a single PDF directly (bypassing the web layer)
python scripts/build_accessible_pdf.py --input path/to/input.pdf --output path/to/output.pdf --lang he-IL --title "Document Title" --dpi 150 --stamp --ocr

# Diagnostic: validate structure/content hierarchy of an existing PDF
python scripts/build_accessible_pdf.py --check-structure path/to/file.pdf

# Health check
curl http://localhost:5000/api/health
```

**External dependencies required (not in pip):**
- Tesseract OCR with Hebrew + English language packs (`tesseract-ocr`, `tesseract-ocr-heb`)
- Poppler utilities (for `pdf2image` / `pdftoppm`)
- LibreOffice (headless, for converting non-PDF formats)

**Windows service deployment:** Run `install_service.bat` as Administrator to register as a Windows service using NSSM (`tools/nssm/`). Service name: `PDFAccessibility`. To uninstall: `uninstall_service.bat`.

## Environment Variables (`.env` file or system env)

| Variable | Default | Purpose |
|---|---|---|
| `ACCESS_PASSWORD` | `eilat2026` | Login password (change in production) |
| `SECRET_KEY` | random | Flask session secret |
| `ANTHROPIC_API_KEY` | *(unset)* | Enables Claude Haiku AI features (optional) |
| `POPPLER_PATH` | *(PATH)* | Relative or absolute path to Poppler `bin/` dir |
| `TESSERACT_CMD` | *(PATH)* | Full path to `tesseract.exe` on Windows |
| `HOST` | `0.0.0.0` | Waitress bind address |
| `PORT` | `5001` | Waitress port |
| `THREADS` | `4` | Waitress thread count |

## Architecture

The processing pipeline is split across two files:

- **[app.py](app.py)** — Flask server: session auth, 13 REST endpoints, SQLite DB, in-process job tracking dict (`jobs`), and background-threaded orchestration. Calls `build_accessible_pdf.py` via `subprocess`. Non-PDF uploads are converted to PDF first (LibreOffice or email parser) before the script is invoked.
- **[scripts/build_accessible_pdf.py](scripts/build_accessible_pdf.py)** — Standalone CLI script: OCR (pytesseract + pdf2image), AI page description and structure analysis (Claude Haiku, optional), PDF/UA tagging via pikepdf, accessibility stamp overlay. Prints progress to stdout; `app.py` only reads the exit code.

**Job lifecycle:** Upload → UUID assigned → background thread converts if needed → spawns subprocess → status polled via `/api/status/{job_id}` → download via `/api/download/{job_id}`.

**Timeout:** Dynamic — `max(300, min(3600, 300 + pages × 4))` seconds.

**Supported input formats:** `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.xlsx`, `.xls` (all via LibreOffice), `.eml` (RFC 2822 parser → LibreOffice), `.msg` (extract-msg library → LibreOffice).

**Storage:**
- `uploads/` — incoming files (deleted after processing)
- `outputs/` — processed accessible PDFs
- `db/history.db` — SQLite: `documents` table (metadata + scores) and `operation_logs` table (audit trail)
- `logs/app.log`, `logs/server.log` — application logs

**Authentication:** Session-based login, 12-hour duration. Password compared as SHA-256 hash. API routes under `/api/` return 401 JSON when unauthenticated; browser routes redirect to `/login`.

**Department access control:** Enforced via `X-Department` HTTP header. Allowed values: `אתרים`, `דיגיטל`, `מנהל`, `שרות`, `אדמיניסטרציה`.

**DPI heuristic (page-count-based):**
- ≤30 pages → 150 DPI (high quality)
- 31–100 pages → 120 DPI (default)
- 101–300 pages → 100 DPI (memory-saving)

**Max upload size:** 200 MB. **Max pages:** 300.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET/POST | `/login` | Login page |
| GET | `/logout` | Invalidate session |
| GET | `/` | Main UI (`templates/index.html`) |
| POST | `/api/upload` | Upload file(s); returns `job_id` list |
| GET | `/api/status/<job_id>` | Poll job status and progress (0–100) |
| GET | `/api/document/<job_id>` | Full document metadata + validation report |
| GET | `/api/download/<job_id>` | Download processed PDF |
| GET | `/api/history` | Paginated processing history |
| DELETE | `/api/delete/<job_id>` | Delete job + output file |
| GET | `/api/validate/<job_id>` | Re-run accessibility validation only |
| GET | `/api/stats` | Aggregate statistics |
| GET | `/api/health` | Health check (no auth required) |
| GET | `/api/docs` | API documentation JSON |

## AI Features (optional)

When `ANTHROPIC_API_KEY` is set, `build_accessible_pdf.py` calls Claude Haiku (`claude-haiku-4-5-20251001`) for two tasks:
1. **Page descriptions** (`describe_pages_with_ai`) — WCAG 1.1.1 alt text for each page image.
2. **Structure analysis** (`analyze_structure_with_ai`) — WCAG 1.3.1 heading/paragraph/list/table tagging per page.

Without the key these functions return `{}` and the script continues without AI tagging.

## Accessibility Scoring (IS 5568 / PDF/UA-1)

Scoring weights in `validate_pdf_accessibility()`:
- 35 pts — text layer (OCR / digital text)
- 25 pts — `StructTreeRoot` structure tags
- 20 pts — `/Lang` defined at Root level
- 10 pts — `/Title` in metadata
- 5 pts — `pdfuaid:part = 1` in XMP
- 5 pts — `MarkInfo/Marked = true`

Status thresholds: ≥85 → `compliant`, 60–84 → `needs_review`, <60 → `non_compliant`.

## Related: WordPress Site

`C:\xampp\htdocs\eilatmuni` is a separate WordPress installation (MySQL `eilatmuni_db`, `http://localhost/eilatmuni`). Independent from this Flask service. The custom plugin `accessible-poetry` (`wp-content/plugins/accessible-poetry/`) adds an accessibility toolbar to WordPress pages.
