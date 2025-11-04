# app/config.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
PROFILE_UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "profile_pics"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

BASE_URL = os.environ.get("BASE_URL", "https://app.faflcourier.com/")
LOGO_URL = os.environ.get("LOGO_URL", f"{BASE_URL.rstrip('/')}/static/logo.png")
DASHBOARD_URL = BASE_URL  # if you reference this in templates

def _normalize_db_url(url: str | None) -> str:
    if not url:
        # local dev fallback only
        return "sqlite:///shipping_platform.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url

SQLALCHEMY_DATABASE_URI = _normalize_db_url(os.environ.get("DATABASE_URL"))
SQLALCHEMY_TRACK_MODIFICATIONS = False

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
WTF_CSRF_TIME_LIMIT = None
