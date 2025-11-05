# app/config.py

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# folders
UPLOAD_FOLDER = BASE_DIR / "uploads"
PROFILE_UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "profile_pics"

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# urls
BASE_URL = os.environ.get("BASE_URL", "https://app.faflcourier.com/")
LOGO_URL = os.environ.get("LOGO_URL", f"{BASE_URL.rstrip('/')}/static/logo.png")
DASHBOARD_URL = BASE_URL


# ======= DB CONFIG (production → render postgres) =======
# DATABASE_URL will be set inside Render environment
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # Render postgres URL fix for SQLAlchemy (psycopg3)
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
    if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

SQLALCHEMY_DATABASE_URI = DATABASE_URL or "sqlite:///shipping_platform.db"

SQLALCHEMY_TRACK_MODIFICATIONS = False


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
WTF_CSRF_TIME_LIMIT = None

# ---- Back-compat shim for legacy routes that still import get_db_connection ----
def get_db_connection():
    """
    TEMPORARY: returns a raw DBAPI connection from SQLAlchemy's engine.
    This lets old code that expected sqlite3.connect(...) keep working long enough
    for us to migrate those routes to SQLAlchemy.
    """
    try:
        from app.extensions import db  # your SQLAlchemy instance
        return db.engine.raw_connection()
    except Exception:
        # Last-resort fallback to local SQLite if engine isn't ready (dev only)
        import sqlite3
        try:
            return sqlite3.connect((BASE_DIR / "shipping_platform.db").resolve().as_posix())
        except Exception as e:
            raise RuntimeError(f"Unable to provide a database connection: {e}")
