# app/config.py

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# =======================
# Environment detection
# =======================
IS_RENDER = bool(
    os.environ.get("RENDER") or
    os.environ.get("RENDER_EXTERNAL_URL")
)

# =======================
# Folder Setup
# =======================
UPLOAD_FOLDER = BASE_DIR / "uploads"
PROFILE_UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "profile_pics"

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# =======================
# Invoice / Package Docs Uploads
# =======================
RENDER_DISK_PATH = Path(os.environ.get("RENDER_DISK_PATH", "/var/data"))

def _pick_invoice_folder() -> Path:
    """
    Prefer Render disk path if it exists AND is writable.
    Otherwise fallback to a local folder inside the repo that is writable at runtime.
    """
    if IS_RENDER:
        try:
            RENDER_DISK_PATH.mkdir(parents=True, exist_ok=True)  # will fail if read-only
            test_file = RENDER_DISK_PATH / ".write_test"
            test_file.write_text("ok")
            test_file.unlink(missing_ok=True)
            return RENDER_DISK_PATH / "invoices"
        except Exception:
            # No disk mounted or not writable -> fallback
            return BASE_DIR / "uploads" / "invoices"

    return BASE_DIR / "app" / "static" / "invoices"

INVOICE_UPLOAD_FOLDER = _pick_invoice_folder()
INVOICE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
INVOICE_UPLOAD_FOLDER = str(INVOICE_UPLOAD_FOLDER)

# =======================
# URLs
# =======================
BASE_URL = os.environ.get("BASE_URL", "https://app.faflcourier.com/")
LOGO_URL = os.environ.get("LOGO_URL", f"{BASE_URL.rstrip('/')}/static/logo.png")
DASHBOARD_URL = BASE_URL

# =======================
# DATABASE CONFIG
# =======================
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

    SQLALCHEMY_DATABASE_URI = DATABASE_URL

elif IS_RENDER:
    raise RuntimeError("DATABASE_URL is not set on Render.")

else:
    SQLITE_PATH = BASE_DIR / "instance" / "shipping_platform.db"
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{SQLITE_PATH}"

SQLALCHEMY_TRACK_MODIFICATIONS = False

# =======================
# Security
# =======================
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
WTF_CSRF_TIME_LIMIT = None

def get_db_connection():
    from app.extensions import db
    return db.engine.raw_connection()

