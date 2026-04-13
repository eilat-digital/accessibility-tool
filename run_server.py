"""
run_server.py — הפעלת שרת ייצור (waitress) לפריסה מקומית על Windows
שימוש: python run_server.py
"""
import os
import sys
import logging
from pathlib import Path

# ── טעינת .env ──────────────────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    with open(_env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())

# ── הגדרות ──────────────────────────────────────────────────────────────────
HOST    = os.environ.get("HOST", "0.0.0.0")
PORT    = int(os.environ.get("PORT", "5001"))
THREADS = int(os.environ.get("THREADS", "4"))

# ── לוג להפעלה ──────────────────────────────────────────────────────────────
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "server.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── בדיקת תלויות ────────────────────────────────────────────────────────────
def check_deps():
    missing = []
    for pkg in ("waitress", "flask", "pikepdf", "reportlab"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        log.error(f"חסרות תלויות: {', '.join(missing)}")
        log.error("הרץ: pip install -r requirements.txt && pip install waitress")
        sys.exit(1)

check_deps()

# ── הפעלת השרת ──────────────────────────────────────────────────────────────
from waitress import serve
from app import app

log.info("=" * 55)
log.info("  מערכת הנגשת מסמכים — עיריית אילת")
log.info(f"  כתובת: http://{HOST}:{PORT}")
log.info(f"  threads: {THREADS}")
log.info("=" * 55)

serve(app, host=HOST, port=PORT, threads=THREADS,
      channel_timeout=600,        # 10 דקות — לקבצים גדולים
      max_request_body_size=209715200)  # 200MB
