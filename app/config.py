# app/config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# =======================
# Environment detection
# =======================
IS_RENDER = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))

# =======================
# Folder Setup (local)
# =======================
UPLOAD_FOLDER = BASE_DIR / "uploads"
PROFILE_UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "profile_pics"

# IMPORTANT:
# Do NOT mkdir Render disk paths here (build time can be read-only).
# Only mkdir safe local repo paths:
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# =======================
# Invoice / Package Docs Uploads
# =======================
# Render disk mount path must match what you set in Render (you said /var/data)
RENDER_DISK_PATH = Path(os.environ.get("RENDER_DISK_PATH", "/var/data"))

if IS_RENDER:
    # Use persistent disk at runtime
    INVOICE_UPLOAD_FOLDER = str(RENDER_DISK_PATH / "invoices")
else:
    # Local/dev fallback (served via /static if you want)
    INVOICE_UPLOAD_FOLDER = str(BASE_DIR / "app" / "static" / "invoices")

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
