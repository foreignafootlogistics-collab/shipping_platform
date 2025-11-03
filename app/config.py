# app/config.py
import os
import sqlite3

# -------------------------------
# Base directory
# -------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# -------------------------------
# Branding & URLs
# -------------------------------
LOGO_URL = "https://www.foreignafoot.com/app/static/logo.png"  # ✅ update to your actual logo path
DASHBOARD_URL = "https://www.foreignafoot.com"  # optional, used in welcome/login emails

# -------------------------------
# Database paths
# -------------------------------
DB_PATH = os.path.join(BASE_DIR, "shipping_platform.db")
SQLALCHEMY_DATABASE_URI = "sqlite:///" + DB_PATH
SQLALCHEMY_TRACK_MODIFICATIONS = False

# -------------------------------
# Upload folders
# -------------------------------
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
PROFILE_UPLOAD_FOLDER = os.path.join(BASE_DIR, "app", "static", "profile_pics")

# Ensure folders exist (safe in dev; in prod do this at deploy time)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROFILE_UPLOAD_FOLDER, exist_ok=True)

# -------------------------------
# Security
# -------------------------------
# NOTE: replace with an env var in production
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "4abd9feb74d226e1be13f69bbd9e62ee4d131637d9f9c6382b5e9bacf18c3ea8",
)
WTF_CSRF_TIME_LIMIT = None  # CSRF tokens never expire in development

# -------------------------------
# Helper: SQLite connection (legacy)
# -------------------------------
def get_db_connection():
    """Returns a raw SQLite connection to shipping_platform.db (legacy/utility)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
