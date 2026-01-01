# app/config.py

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# =======================
# Folder Setup
# =======================
UPLOAD_FOLDER = BASE_DIR / "uploads"
PROFILE_UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "profile_pics"

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


# =======================
# Invoice / Package Docs Uploads (Persistent on Render)
# =======================

# Render persistent disk mount (you set mount path to /var/data)
RENDER_DISK_PATH = Path(os.environ.get("RENDER_DISK_PATH", "/var/data"))

# Store invoice / attachment files here
INVOICE_UPLOAD_FOLDER = (
    (RENDER_DISK_PATH / "invoices") if IS_RENDER
    else (BASE_DIR / "app" / "static" / "invoices")
)

# Make sure the folder exists
Path(INVOICE_UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)

# Optional: keep a string version too (handy with os.path.join)
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

# DATABASE CONFIG
IS_RENDER = bool(
    os.environ.get("RENDER") or
    os.environ.get("RENDER_EXTERNAL_URL")
)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Use Postgres anywhere if DATABASE_URL is set
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

    SQLALCHEMY_DATABASE_URI = DATABASE_URL

elif IS_RENDER:
    # Safety: on Render, DATABASE_URL must exist
    raise RuntimeError("DATABASE_URL is not set on Render.")

else:
    # Local fallback → SQLite
    SQLITE_PATH = BASE_DIR / "instance" / "shipping_platform.db"
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{SQLITE_PATH}"

SQLALCHEMY_TRACK_MODIFICATIONS = False

# =======================
# Security
# =======================
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
WTF_CSRF_TIME_LIMIT = None

# =======================
# TEMP RAW CONNECTION SUPPORT
# =======================
def get_db_connection():
    """
    TEMPORARY: returns a raw DBAPI connection from SQLAlchemy's engine.
    Allows legacy SQLite-style code to keep working temporarily.
    """
    from app.extensions import db
    return db.engine.raw_connection()
