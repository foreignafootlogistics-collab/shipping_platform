import os
import io
import re
import math
import csv
import sqlite3, time
from io import StringIO
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file, Response, make_response
)
from werkzeug.utils import secure_filename
from flask_login import login_required, current_user
import uuid, json
import openpyxl
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from urllib.parse import urlsplit

from app.extensions import db
from app.routes.admin_auth_routes import admin_required
from app.models import Prealert, User, ScheduledDelivery, ShipmentLog, Invoice, Package
from app.forms import PackageBulkActionForm, UploadPackageForm, PreAlertForm, InvoiceFinalizeForm, PaymentForm, ScheduledDeliveryForm
from app.utils import email_utils, update_wallet
from app.config import DB_PATH, UPLOAD_FOLDER
from app.calculator_data import calculate_charges, CATEGORIES, get_freight, USD_TO_JMD
from flask_wtf import FlaskForm
from flask import current_app
from math import ceil
from sqlalchemy import select
from app.utils.unassigned import ensure_unassigned_user


logistics_bp = Blueprint('logistics', __name__, url_prefix='/admin/logistics')

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"xls", "xlsx", "csv"}


# --------------------------
# Helper Functions
# --------------------------



def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _preview_dir() -> str:
    """
    Resolve a writable preview directory.
    Uses app.instance_path when available; falls back to ./instance for CLI/import-time.
    """
    try:
        base = current_app.instance_path  # works inside app/request context
    except RuntimeError:
        # Import-time or CLI: use a local 'instance' folder
        base = os.path.join(os.getcwd(), "instance")

    path = os.path.join(base, "tmp_preview_uploads")
    os.makedirs(path, exist_ok=True)
    return path


def cleanup_preview_dir(max_age_hours: int = 24):
    cutoff = datetime.utcnow().timestamp() - (max_age_hours * 3600)
    try:
        directory = _preview_dir()
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass



# ✅ What columns your uploads actually have (left) → internal keys (right)
HEADER_MAP = {
    "USER CODE":        "registration_number",
    "SHIPPER":          "shipper",
    "HOUSE AWB":        "house_awb",
    "WEIGHT":           "weight",
    "TRACKING NUMBER":  "tracking_number",
    "DATE":             "date_received",
    "DATE RECEIVED":    "date_received",
    "RECEIVED DATE":    "date_received",
    "DESCRIPTION":      "description",
    "VALUE":            "value",          # if present
    "FULL NAME":        "full_name",      # if present
}

REQUIRED_FIELDS = [
    "registration_number",
    "tracking_number",
    "description",
    "weight",    
]

def _normalize_headers(cols):
    """
    Lowercase/strip headers and map known labels (CSV/XLSX) to internal keys.
    """
    norm = []
    for c in cols:
        raw = str(c).strip()
        # try direct map first (exact match)
        mapped = HEADER_MAP.get(raw)
        if mapped:
            norm.append(mapped)
            continue
        # fallback: normalize then try map by normalized title
        low = raw.lower().replace(" ", "_")
        # try reverse lookup by uppercase title
        mapped2 = HEADER_MAP.get(raw.upper())
        norm.append(mapped2 if mapped2 else low)
    return norm

def _read_any_table(file_storage):
    """Read CSV/XLSX to DataFrame without trusting file extension."""
    raw = file_storage.read()
    file_storage.seek(0)
    try:
        # Try Excel first
        df = pd.read_excel(io.BytesIO(raw))  # engine auto-detects (openpyxl)
    except Exception:
        # Fallback to CSV (try utf-8, then latin-1)
        try:
            df = pd.read_csv(io.BytesIO(raw))
        except Exception:
            df = pd.read_csv(io.BytesIO(raw), encoding="latin-1")
    return df

def _validate_rows(df):
    """
    Returns:
      rows: list of dicts (internal keys)
      row_errors: dict {row_index: [errors]}
      display_headers: original column order for preview (keep original names)
    """
    df = df.copy()
    # Keep a copy of original headers for preview display
    display_headers = list(df.columns)

    # Normalize to internal keys
    df.columns = _normalize_headers(df.columns)

    rows = df.to_dict(orient="records")
    row_errors = {}

    for i, r in enumerate(rows):
        errs = []

        # Required fields present and non-empty
        for f in REQUIRED_FIELDS:
            val = r.get(f, None)
            if pd.isna(val) or str(val).strip() == "":
                errs.append(f"Missing {f.replace('_',' ').title()}")

        # Coerce weight
        try:
            if "weight" in r and not pd.isna(r["weight"]):
                r["weight"] = float(r["weight"])
            else:
                r["weight"] = 0.0
        except Exception:
            errs.append("Weight must be a number")

        # Make value optional; default to 50
        try:
            if "value" in r and not pd.isna(r["value"]) and str(r["value"]).strip() != "":
                r["value"] = float(r["value"])
            else:
                r["value"] = 50.0
        except Exception:
            errs.append("Value must be a number")

        # date_received: accept as-is; you re-parse when inserting
        # full_name: optional

        if errs:
            row_errors[i] = errs

    return rows, row_errors, display_headers  # note: return original headers for preview


def _parse_date_any(v) -> str | None:
    """
    Parse many date representations into 'YYYY-MM-DD'.
    Returns None if blank/unparseable (so DB gets NULL).
    """
    if v is None:
        return None

    # Pandas / Python types
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime().date().strftime("%Y-%m-%d")
    if isinstance(v, datetime):
        return v.date().strftime("%Y-%m-%d")

    s = str(v).strip()
    if not s:
        return None

    # Excel serial numbers
    try:
        num = float(s)
        if num > 59:  # avoid tiny numbers that aren't real dates
            excel_epoch = datetime(1899, 12, 30)
            return (excel_epoch + timedelta(days=num)).date().strftime("%Y-%m-%d")
    except Exception:
        pass

    # Normalize ISO-ish strings: remove 'T', 'Z', and trailing timezone offsets
    s_norm = s.replace("T", " ").replace("Z", "")
    s_norm = re.sub(r"\s*([+-]\d{2}:?\d{2})$", "", s_norm)  # drop +hh:mm or +hhmm at end

    # Try pandas parser first (handles many formats)
    try:
        dt = pd.to_datetime(s_norm, errors="raise")
        return dt.to_pydatetime().date().strftime("%Y-%m-%d")
    except Exception:
        pass

    # Common manual formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s_norm, fmt).date().strftime("%Y-%m-%d")
        except Exception:
            continue

    # Last resort ISO
    try:
        return datetime.fromisoformat(s_norm).date().strftime("%Y-%m-%d")
    except Exception:
        return None

def _save_preview_blob(data):
    token = str(uuid.uuid4())
    path = os.path.join(_preview_dir(), f"{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return token

def _load_preview_blob(token):
    path = os.path.join(_preview_dir(), f"{token}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _delete_preview_blob(token):
    path = os.path.join(_preview_dir(), f"{token}.json")
    if os.path.exists(path):
        os.remove(path)

def list_uploaded_files():
    files = []
    try:
        for f in os.listdir(UPLOAD_FOLDER):
            if allowed_file(f):
                full_path = os.path.join(UPLOAD_FOLDER, f)
                mtime = os.path.getmtime(full_path)
                files.append({'name': f, 'mtime': datetime.fromtimestamp(mtime)})
        files.sort(key=lambda x: x['mtime'], reverse=True)
    except Exception as e:
        print(f"Error listing files: {e}")
    return files

def prg_to(tab, **kwargs):
    """
    Post/Redirect/Get helper that avoids redirect loops.
    Usage: return prg_to("uploadPackages")  # from a POST branch
    """
    target = url_for("logistics.logistics_dashboard", tab=tab, **kwargs)

    # Avoid loops: if we're already at the same URL on GET, don't redirect
    if request.method == "GET":
        here = urlsplit(request.url)
        there = urlsplit(request.host_url.rstrip("/") + target)
        if here.path == there.path and request.args.get("tab") == tab:
            return None

    # 303: safe redirection after POST
    return redirect(target, code=303)

def _qmarks(n: int) -> str:
    return ",".join("?" * n)

TAB_ALIASES = {
    "shipment": "shipmentLog",   # accept either, render as shipmentLog
}

ALLOWED_TABS = {"prealert", "view_packages", "shipmentLog", "uploadPackages"}

def normalize_tab(raw):
    t = (raw or "prealert").strip().lower()
    # keep keys in the same case as ALLOWED_TABS; map lower -> proper
    # Convert back to the canonical case used in ALLOWED_TABS
    # (we’ll map lowercase names to the canonical names)
    lower_map = {
        "prealert": "prealert",
        "view_packages": "view_packages",
        "shipmentlog": "shipmentLog",
        "uploadpackages": "uploadPackages",
        "shipment": "shipment",  # will map via TAB_ALIASES next
    }
    t = lower_map.get(t, "prealert")
    t = TAB_ALIASES.get(t, t)
    return t if t in ALLOWED_TABS else "prealert"


def _normalize_weight(w: float) -> float:
    """
    Use the exact same rounding rule as the single-row calculator.
    If your single calc calls get_rate_for_weight() and expects whole pounds,
    keep this equivalent. Adjust if you round to 1/8 lb etc.
    """
    try:
        w = float(w or 0)
    except Exception:
        w = 0.0
    # Typical air freight rule: round up to next whole lb, min 1 lb
    return max(1.0, math.ceil(w))


def get_conn():
    # Manual tx control: use BEGIN ... / COMMIT yourself
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # Pragmas (set early on a fresh connection)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 7000")   # 7s is a comfy default
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn

def _retry_locked(fn, *, retries=6, base_sleep=0.12):
    """
    Retries fn() if SQLite reports a lock/busy. Exponential backoff.
    Keep the critical section in fn() as short as possible.
    """
    for i in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "database is locked" in msg or "database is busy" in msg:
                time.sleep(base_sleep * (2 ** i))
                continue
            raise
    # final attempt to surface any persistent error
    return fn()


def _next_seq_tx(conn, name: str, width: int = 5) -> str:
    cur = conn.cursor()
    cur.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO counters(name, value) VALUES(?, 1)", (name,))
        val = 1
    else:
        row = cur.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
        val = int(row[0])
    return f"{val:0{width}d}"

def _next_invoice_number_tx(conn) -> str:
    """Generate sequential invoice numbers like INV00001"""
    cur = conn.cursor()
    cur.execute("UPDATE counters SET value = value + 1 WHERE name = ?", ('invoice_seq',))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO counters(name, value) VALUES (?, 1)", ('invoice_seq',))
        val = 1
    else:
        val = cur.execute("SELECT value FROM counters WHERE name = ?", ('invoice_seq',)).fetchone()[0]
    return f"INV{int(val):05d}"

def _next_bill_number_tx(conn):
    return _next_seq_tx(conn, 'bill_seq', 5)

def _ensure_aux_tables():
    """Create/upgrade the counters table and seed shipment_seq from existing rows."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        # Create if missing
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
              name  TEXT PRIMARY KEY,
              value INTEGER NOT NULL
            )
        """)

        # --- MIGRATE if the old columns existed (key,value) ---
        cols = [r[1] for r in c.execute("PRAGMA table_info(counters)").fetchall()]
        if ("key" in cols) and ("name" not in cols):
            # Move data from old counters(key,value) → new counters(name,value)
            c.execute("ALTER TABLE counters RENAME TO counters_old")
            c.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            c.execute("INSERT INTO counters(name,value) SELECT key,value FROM counters_old")
            c.execute("DROP TABLE counters_old")

        # seed rows we use
        c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,?)", ("shipment_seq", 0))
        c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,?)", ("invoice_seq", 0))

        # If shipment_seq is 0, initialize from current max suffix in shipment_log
        cur = c.execute("SELECT value FROM counters WHERE name='shipment_seq'").fetchone()
        if cur and (int(cur[0]) == 0):
            mx = c.execute("""
                SELECT COALESCE(MAX(CAST(substr(sl_id, -5) AS INTEGER)), 0)
                FROM shipment_log
            """).fetchone()[0] or 0
            c.execute("UPDATE counters SET value=? WHERE name='shipment_seq'", (int(mx),))

        # Keep sl_id unique
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        conn.commit()
    finally:
        conn.close()

def _ensure_package_columns():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cols = [
        ("stamp", "REAL", "0"),
        ("customs_total", "REAL", "0"),
        ("freight_fee", "REAL", "0"),
        ("storage_fee", "REAL", "0"),
        ("freight_total", "REAL", "0"),
        ("other_charges", "REAL", "0"),
        ("grand_total", "REAL", "0")
    ]
    for name, typ, default in cols:
        try:
            c.execute(f"ALTER TABLE packages ADD COLUMN {name} {typ} DEFAULT {default}")
        except Exception:
            pass  # already exists
    conn.commit()
    conn.close()


def _create_bill(conn, user_id: int, pkg_ids: list[int], total_amount: float) -> tuple[int, str]:
    bill_no = _next_bill_number_tx(conn)
    now_iso = datetime.utcnow().isoformat()
    # Use the first package just to satisfy NOT NULL constraint
    first_pkg_id = int(pkg_ids[0])

    # Optional description; adjust as you like
    description = f"Auto bill for {len(pkg_ids)} package(s)"

    cur = conn.execute("""
        INSERT INTO bills (user_id, package_id, description, amount, status, due_date, bill_number, total_amount, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'unpaid', NULL, ?, ?, ?, ?)
    """, (user_id, first_pkg_id, description, float(total_amount), bill_no, float(total_amount), now_iso, now_iso))
    return cur.lastrowid, bill_no



# --- shipment log counters bootstrap (global, never resets per day) ---
_COUNTORS_READY = False  # module-level guard

def _ensure_counters_table():
    """Create/upgrade the counters table and seed shipment_seq from existing sl_id rows."""
    global _COUNTORS_READY
    if _COUNTORS_READY:
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        # Create if missing (new schema uses 'name' not 'key')
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
              name  TEXT PRIMARY KEY,
              value INTEGER NOT NULL
            )
        """)

        # If an old counters table existed with (key,value), migrate it.
        cols = [r[1] for r in c.execute("PRAGMA table_info(counters)").fetchall()]
        if ("key" in cols) and ("name" not in cols):
            c.execute("ALTER TABLE counters RENAME TO counters_old")
            c.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            c.execute("INSERT INTO counters(name,value) SELECT key,value FROM counters_old")
            c.execute("DROP TABLE counters_old")

        # Seed required rows
        c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,?)", ("shipment_seq", 0))
        c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,?)", ("invoice_seq", 0))

        # Initialize shipment_seq from current max suffix in shipment_log if still 0
        cur = c.execute("SELECT value FROM counters WHERE name='shipment_seq'").fetchone()
        if cur and int(cur[0]) == 0:
            mx = c.execute("""
                SELECT COALESCE(MAX(CAST(substr(sl_id, -5) AS INTEGER)), 0)
                FROM shipment_log
            """).fetchone()[0] or 0
            c.execute("UPDATE counters SET value=? WHERE name='shipment_seq'", (int(mx),))

        # Indices / constraints that help
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package
            ON shipment_packages(package_id)
        """)

        conn.commit()
        _COUNTORS_READY = True
    finally:
        conn.close()


def _next_global_sl_id(c):
    """
    Increment the global shipment_seq and return SL-YYYYMMDD-xxxxx.
    Caller must pass a cursor inside an active transaction.
    """
    c.execute("UPDATE counters SET value = value + 1 WHERE name='shipment_seq'")
    c.execute("SELECT value FROM counters WHERE name='shipment_seq'")
    seq = int(c.fetchone()[0])
    date_str = datetime.utcnow().strftime("%Y%m%d")
    return f"SL-{date_str}-{seq:05d}"

# --- bootstrap once at import ---
_BOOTSTRAPPED = False
def _bootstrap_once():
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _ensure_counters_table()
    _ensure_package_columns()
    _BOOTSTRAPPED = True

_bootstrap_once()


# --------------------------
# Pre-Alerts
# --------------------------
@logistics_bp.route("/prealerts")
@logistics_bp.route("/prealerts/<int:user_id>")
@admin_required
def prealerts(user_id=None):
    if user_id:
        customer = User.query.get_or_404(user_id)
        prealerts_query = PreAlert.query.filter_by(user_id=user_id).order_by(PreAlert.id.desc()).all()
        customer_name = customer.full_name
        registration_number = customer.registration_number
        prealerts_data = [
            dict(
                id=f"PA-{p.prealert_number}",
                customer_id=p.user_id,
                vendor_name=p.vendor_name,
                courier_name=p.courier_name,
                tracking_number=p.tracking_number,
                purchase_date=p.purchase_date,
                package_contents=p.package_contents,
                item_value_usd=p.item_value_usd,
                invoice_filename=p.invoice_filename,
                created_at=p.created_at,
                customer_name=customer_name,
                registration_number=registration_number
            ) for p in prealerts_query
        ]
    else:
        prealerts_query = (
            db.session.query(
                PreAlert,
                User.full_name.label("customer_name"),
                User.registration_number.label("registration_number")
            )
            .join(User, PreAlert.user_id == User.id)
            .order_by(PreAlert.id.desc())
            .all()
        )
        prealerts_data = [
            dict(
                id=f"PA-{p.prealert.prealert_number}",
                customer_id=p.prealert.user_id,
                vendor_name=p.prealert.vendor_name,
                courier_name=p.prealert.courier_name,
                tracking_number=p.prealert.tracking_number,
                purchase_date=p.prealert.purchase_date,
                package_contents=p.prealert.package_contents,
                item_value_usd=p.prealert.item_value_usd,
                invoice_filename=p.prealert.invoice_filename,
                created_at=p.prealert.created_at,
                customer_name=p.customer_name,
                registration_number=p.registration_number
            ) for p in prealerts_query
        ]

    return render_template("admin/logistics/prealerts.html",
                           prealerts=prealerts_data,
                           customer_name=(customer_name if user_id else None),
                           registration_number=(registration_number if user_id else None))


# --------------------------
# Dashboard
# --------------------------
@logistics_bp.route('/dashboard', methods=["GET", "POST"], endpoint="logistics_dashboard")
@admin_required
def logistics_dashboard():
    upload_form = UploadPackageForm()
    prealert_form = PreAlertForm()
    bulk_form = PackageBulkActionForm()
    invoice_finalize_form = InvoiceFinalizeForm()

    message = None
    errors = []

    # Legacy preview vars (kept for backward-compat; we now use the new preview_* set)
    preview_headers = []
    preview_data = []
    uploaded_files = []
    inserted_count = 0

    # --- Active tab (normalized) ---
    active_tab = normalize_tab(request.args.get("tab") or request.form.get("tab"))

    # Housekeep only when on the Upload tab
    if active_tab == "uploadPackages":
        cleanup_preview_dir(24)

    # ---------- DELETE PACKAGE (POST -> redirect keeps tab=view_packages) ----------
    if request.method == "POST" and "delete_package_id" in request.form:
        package_id = request.form["delete_package_id"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM packages WHERE id=?", (package_id,))
        conn.commit()
        conn.close()
        flash("Package deleted successfully.", "success")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"), code=303)

    # ======================================================================
    #                          UPLOAD TAB (NEW FLOW)
    # ======================================================================
    preview_token   = request.args.get("preview_token") or request.form.get("preview_token")
    preview_headers = None       # ORIGINAL headers for UI
    preview_rows    = None       # ORIGINAL rows (dicts) for UI
    preview_errors  = None       # {row_index: [errors]}
    summary_counts  = None       # {'total': N, 'valid': V, 'invalid': I}

    # 1) PREVIEW REQUEST: read file, validate ALL rows, store blob, redirect with token
    if request.method == "POST" and active_tab == "uploadPackages" and request.form.get("stage") == "preview":
        f = request.files.get("file")
        if not f or f.filename.strip() == "":
            flash("Please choose a file.", "danger")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

        try:
            df = _read_any_table(f)

            # purge old preview files before saving a new blob
            cleanup_preview_dir(24)

            # Validate to INTERNAL keys (normalized) for import,
            # but keep ORIGINAL headers/rows for the preview table.
            rows, row_errors, normalized_headers = _validate_rows(df)
            original_rows    = df.to_dict(orient="records")
            original_headers = list(df.columns)

            preview_token = _save_preview_blob({
                "rows": rows,                         # normalized/internal keys for confirm/import
                "row_errors": row_errors,             # {row_index: [errors]}
                "display_headers": original_headers,  # ORIGINAL column labels for UI
                "original_rows": original_rows,       # ORIGINAL row dicts for UI
                "created_at": datetime.utcnow().isoformat(),
            })

            valid_count   = len(rows) - len(row_errors)
            invalid_count = len(row_errors)
            flash(f"Preview ready: {valid_count} valid / {invalid_count} invalid out of {len(rows)}.", "info")

            # PRG with token so refresh persists
            return redirect(
                url_for("logistics.logistics_dashboard", tab="uploadPackages", preview_token=preview_token),
                code=303
            )

        except Exception as e:
            flash(f"Failed to read file: {e}", "danger")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

    # 2) CONFIRM UPLOAD: import selected indices (optionally in batches), skip invalid rows
    if request.method == "POST" and active_tab == "uploadPackages" and request.form.get("stage") == "confirm":
        preview_token = request.form.get("preview_token")
        data = _load_preview_blob(preview_token)
        if not data:
            flash("Preview session expired. Please upload again.", "warning")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

        rows       = data.get("rows", [])
        row_errors = data.get("row_errors", {})

        # Selected indices: JSON array of integers
        try:
            selected_indices = json.loads(request.form.get("selected_indices", "[]"))
            selected_indices = [int(x) for x in selected_indices]
        except Exception:
            selected_indices = []

        # Optional batch size cap
        try:
            batch_size = int(request.form.get("batch_size") or 0)
        except Exception:
            batch_size = 0
        if batch_size and batch_size > 0:
            selected_indices = selected_indices[:batch_size]

        created, skipped = 0, 0
        hard_errors = []

        # DB insert (SQLite direct)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # --- Cache UNASSIGNED user id once
        UNASSIGNED_ID = None
        try:
            row_un = c.execute(
                "SELECT id FROM users WHERE registration_number='UNASSIGNED' LIMIT 1"
            ).fetchone()
            if row_un:
                UNASSIGNED_ID = int(row_un[0])
            else:
                # try to create/ensure it (uses SQLAlchemy helper)
                try:
                    from app.utils.unassigned import ensure_unassigned_user
                    UNASSIGNED_ID = int(ensure_unassigned_user())
                except Exception:
                    UNASSIGNED_ID = None
        except Exception:
            UNASSIGNED_ID = None

        for i in selected_indices:
            try:
                if i < 0 or i >= len(rows):
                    skipped += 1
                    continue

                # ----- Relaxed validation: allow missing registration number.
                errs = (row_errors or {}).get(str(i)) or (row_errors or {}).get(i) or []
                filtered_errs = [e for e in errs if "registration" not in str(e).lower()]
                if filtered_errs:
                    skipped += 1
                    continue

                r = rows[i]  # normalized keys via _normalize_headers

                # ---- Resolve user: registration_number -> email -> UNASSIGNED
                reg = (str(r.get("registration_number") or "").strip())
                user_id = None

                if reg:
                    u = c.execute(
                        "SELECT id FROM users WHERE registration_number=? LIMIT 1", (reg,)
                    ).fetchone()
                    if u:
                        user_id = int(u[0])

                if user_id is None:
                    email = (str(r.get("email") or "").strip())
                    if email:
                        u2 = c.execute(
                            "SELECT id FROM users WHERE LOWER(email)=LOWER(?) LIMIT 1", (email,)
                        ).fetchone()
                        if u2:
                            user_id = int(u2[0])

                assigned_unassigned = False
                if user_id is None and UNASSIGNED_ID is not None:
                    user_id = UNASSIGNED_ID
                    assigned_unassigned = True

                if user_id is None:
                    skipped += 1
                    hard_errors.append(f"Row {i+1}: No matching user and UNASSIGNED user missing.")
                    continue

                # ---- Field mapping
                shipper         = (str(r.get("shipper", "")).strip() or None)
                house_awb       = (str(r.get("house_awb", "")).strip() or None)
                tracking_number = (str(r.get("tracking_number", "")).strip() or None)
                description     = (str(r.get("description", "")).strip() or None)
                value           = float(r.get("value") or 0)
                weight_actual   = float(r.get("weight") or 0)

                date_raw = r.get("date_received") or r.get("date")
                date_received = _parse_date_any(date_raw)  # global helper

                status_value = 'Unassigned' if assigned_unassigned else 'Overseas'

                c.execute("""
                    INSERT INTO packages
                    (user_id, shipper, house_awb, weight, tracking_number, date_received, description, value, amount_due, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id,
                    shipper,
                    house_awb,
                    weight_actual,
                    tracking_number,
                    date_received,
                    description,
                    value,
                    0,
                    status_value,
                    datetime.utcnow(),
                ))
                created += 1

            except Exception as e:
                skipped += 1
                hard_errors.append(f"Row {i+1}: {e}")

        conn.commit()
        conn.close()

        if created:
            flash(f"Imported {created} package(s).", "success")
        if skipped:
            flash(f"Skipped {skipped} row(s).", "warning")
        for err in hard_errors[:5]:
            flash(err, "danger")
        if len(hard_errors) > 5:
            flash(f"...and {len(hard_errors)-5} more errors.", "danger")

        # Keep preview active for more batches (do NOT delete the token)
        return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages", preview_token=preview_token), code=303)


    # 3) If we have a preview_token on GET, load it so the page shows the full preview
    if request.method == "GET" and active_tab == "uploadPackages" and preview_token:
        data = _load_preview_blob(preview_token)
        if data:
            # show ORIGINAL headers/rows in the UI
            preview_headers = data.get("display_headers", [])
            preview_rows    = data.get("original_rows", [])
            preview_errors  = data.get("row_errors", {})

            total   = len(data.get("rows", []))      # count on normalized set
            invalid = len(preview_errors or {})
            valid   = total - invalid
            summary_counts = {"total": total, "valid": valid, "invalid": invalid}
        else:
            flash("Preview session expired. Please upload again.", "warning")



    # ======================================================================
    #                         OTHER TABS / GET RENDER
    # ======================================================================
    # ---------- PREALERT FORM (POST -> PRG) ----------
    if request.method == "POST" and prealert_form.validate_on_submit():
        flash("Pre-alert submitted successfully.", "success")
        return redirect(url_for("logistics.logistics_dashboard", tab="prealert"), code=303)

    # ------- Everything below is GET-only rendering -------
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Shipments
    c.execute("SELECT * FROM shipment_log ORDER BY created_at DESC")
    shipments = c.fetchall()

    def parse_datetime(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    shipments_parsed = []
    for s in shipments:
        d = dict(s)
        d["created_at"] = parse_datetime(d.get("created_at"))
        shipments_parsed.append(d)

    selected_shipment_id = request.args.get('shipment_id', type=int)
    if selected_shipment_id:
        c.execute("SELECT * FROM shipment_log WHERE id=?", (selected_shipment_id,))
        selected_shipment = c.fetchone()
        if selected_shipment:
            selected_shipment = dict(selected_shipment)
            selected_shipment["created_at"] = parse_datetime(selected_shipment.get("created_at"))
    else:
        selected_shipment = shipments_parsed[0] if shipments_parsed else None
        selected_shipment_id = selected_shipment['id'] if selected_shipment else None

    shipment_packages = []
    if selected_shipment:
        c.execute("""
            SELECT p.*, u.full_name, u.registration_number, p.invoice_id
            FROM packages p
            JOIN users u ON u.id = p.user_id
            JOIN shipment_packages sp ON sp.package_id = p.id
            WHERE sp.shipment_id=?
        """, (selected_shipment_id,))
        for row in c.fetchall():
            pkg = dict(row)
            pkg["date_received"] = parse_datetime(pkg.get("date_received"))
            pkg["created_at"] = parse_datetime(pkg.get("created_at"))
            shipment_packages.append(pkg)

    # ---------- View Packages: filters + pagination (SQLite) ----------

    cursor = conn.cursor()
    c = cursor

    unassigned_id = None
    try:
        c.execute("SELECT id FROM users WHERE registration_number='UNASSIGNED' LIMIT 1")
        row = c.fetchone()
        if row:
            unassigned_id = int(row[0])
    except Exception:
        unassigned_id = None

    date_from  = (request.args.get('date_from') or '').strip()
    date_to    = (request.args.get('date_to')   or '').strip()
    house      = request.args.get('house',      '', type=str)
    tracking   = request.args.get('tracking',   '', type=str)
    user_code  = request.args.get('user_code',  '', type=str)
    first_name = request.args.get('first_name', '', type=str)
    last_name  = request.args.get('last_name',  '', type=str)

    epc_only = (request.args.get('epc_only') or '').lower() in ('1','true','on','yes')

    # (Keep old ones if the template still uses them)
    search        = (request.args.get('search') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    unassigned_only = (request.args.get('unassigned_only') or '').lower() in ('1','true','on','yes')


    allowed_page_sizes = [10, 25, 50, 100, 500, 1000]
    page     = request.args.get('page', default=1, type=int)
    per_page = request.args.get('per_page', type=int) or 10
    if per_page not in allowed_page_sizes:
        per_page = 10
    offset = (page - 1) * per_page

    if (request.args.get('tab') == 'view_packages') and not date_from and not date_to:
        today = datetime.now().strftime('%Y-%m-%d')
        date_from = today
        date_to   = today

    base_select = """
        SELECT
            p.id, p.user_id, p.tracking_number, p.description, p.weight, p.status,
            p.created_at, p.date_received, p.house_awb, p.value, p.amount_due,
            COALESCE(p.epc, 0) AS epc, 
            p.shipper, p.invoice_id,            
            u.full_name, u.registration_number
        FROM packages p
        JOIN users u ON p.user_id = u.id
        WHERE 1=1
    """
    base_count = """
        SELECT COUNT(*)
        FROM packages p
        JOIN users u ON p.user_id = u.id
        WHERE 1=1
    """

    where_sql    = []
    params       = []
    count_params = []
    date_expr = "date(COALESCE(p.date_received, p.created_at))"

    if date_from and date_to:
        where_sql.append(f" AND {date_expr} BETWEEN ? AND ?")
        params.extend([date_from, date_to])
        count_params.extend([date_from, date_to])
    elif date_from:
        where_sql.append(f" AND {date_expr} >= ?")
        params.append(date_from)
        count_params.append(date_from)
    elif date_to:
        where_sql.append(f" AND {date_expr} <= ?")
        params.append(date_to)
        count_params.append(date_to)

    if house:
        like = f"%{house.strip()}%"
        where_sql.append(" AND p.house_awb LIKE ?")
        params.append(like)
        count_params.append(like)

    if tracking:
        like = f"%{tracking.strip()}%"
        where_sql.append(" AND p.tracking_number LIKE ?")
        params.append(like)
        count_params.append(like)

    if user_code:
        like = f"%{user_code.strip()}%"
        where_sql.append(" AND u.registration_number LIKE ?")
        params.append(like)
        count_params.append(like)

    if first_name:
        like = f"%{first_name.strip()}%"
        where_sql.append(" AND u.full_name LIKE ?")
        params.append(like)
        count_params.append(like)

    if last_name:
        like = f"%{last_name.strip()}%"
        where_sql.append(" AND u.full_name LIKE ?")
        params.append(like)
        count_params.append(like)

    if search:
        like = f"%{search}%"
        where_sql.append(" AND (u.full_name LIKE ? OR p.tracking_number LIKE ? OR p.house_awb LIKE ?)")
        params.extend([like, like, like])
        count_params.extend([like, like, like])

    if status_filter:
        where_sql.append(" AND p.status = ?")
        params.append(status_filter)
        count_params.append(status_filter)

    # Unassigned-only filter — show packages tied to UNASSIGNED user or with status 'Unassigned'
    if unassigned_only:
        cond = " AND (LOWER(p.status)='unassigned'"
        if unassigned_id is not None:
            cond += " OR p.user_id = ?"
            where_sql.append(cond + ")")
            params.append(unassigned_id)
            count_params.append(unassigned_id)
        else:
            where_sql.append(cond + ")")

    # Show only UNASSIGNED packages when the checkbox is on
    show_unassigned = request.args.get('show_unassigned')
    if show_unassigned == '1' and unassigned_id:
        where_sql.append(" AND p.user_id = ?")
        params.append(unassigned_id)
        count_params.append(unassigned_id)

    # ADD THIS ↓↓↓
    if epc_only:
        where_sql.append(" AND COALESCE(p.epc, 0) = 1")


    where_clause = "".join(where_sql)

    date_expr = "date(COALESCE(p.date_received, p.created_at))"

    order_limit  = f" ORDER BY {date_expr} DESC LIMIT ? OFFSET ?"
    select_sql   = base_select + where_clause + order_limit
    count_sql    = base_count  + where_clause

    # Execute (paged rows)
    c.execute(select_sql, params + [per_page, offset])
    rows = c.fetchall()

    # Count for pagination
    c.execute(count_sql, count_params)
    total_count = int(c.fetchone()[0] or 0)

    # 🔢 Overall totals for the same filter (count + weight)
    totals_sql = """
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(
                CASE
                    WHEN p.weight IS NULL OR TRIM(p.weight) = '' THEN 0
                    ELSE CAST(p.weight AS REAL)
                END
            ), 0) AS total_weight
        FROM packages p
        JOIN users u ON p.user_id = u.id
        WHERE 1=1
    """ + where_clause
    c.execute(totals_sql, count_params)
    _tot = c.fetchone() or (0, 0)
    filtered_total_packages = int(_tot[0] or 0)
    filtered_total_weight   = float(_tot[1] or 0.0)

    # 📅 Per-day breakdown (only when a date filter is set)
    daily_totals = []
    if date_from or date_to:
        daily_sql = f"""
            SELECT
                {date_expr} AS day,
                COUNT(*) AS cnt,
                COALESCE(SUM(
                    CASE
                        WHEN p.weight IS NULL OR TRIM(p.weight) = '' THEN 0
                        ELSE CAST(p.weight AS REAL)
                    END
                ), 0) AS total_weight
            FROM packages p
            JOIN users u ON p.user_id = u.id
            WHERE 1=1
        """ + where_clause + f"""
            GROUP BY {date_expr}
            ORDER BY {date_expr} ASC
        """
        c.execute(daily_sql, count_params)
        for d, cnt, tw in c.fetchall():
            daily_totals.append({
                "day": d,
                "count": int(cnt or 0),
                "total_weight": float(tw or 0.0),
            })

    # Pagination helpers
    total_pages = max((total_count + per_page - 1) // per_page, 1)
    prev_page   = page - 1 if page > 1 else None
    next_page   = page + 1 if page < total_pages else None
    first_page  = 1 if page != 1 else None
    last_page   = total_pages if page != total_pages else None

    # Done with DB work for this section
    conn.close()

    # Parse rows and dates
    parsed_packages = []
    for pkg in rows:
        d = dict(pkg)
        d["created_at"]    = parse_datetime(d.get("created_at"))
        d["date_received"] = parse_datetime(d.get("date_received"))
        parsed_packages.append(d)

    # "showing X–Y of N"
    showing_from = 0 if total_count == 0 else (offset + 1)
    showing_to   = min(offset + len(parsed_packages), total_count)


    categories = list(CATEGORIES.keys())

    return render_template(
        "admin/logistics/logistics_dashboard.html",
        upload_form=upload_form,
        prealert_form=prealert_form,
        bulk_form=bulk_form,
        invoice_finalize_form=invoice_finalize_form,

        message=message,
        errors=errors,

        # Legacy preview vars (kept for compatibility; now unused in new flow)
        
        preview_data=preview_data,

        # New preview/batch-import context
        preview_headers=locals().get("preview_headers"),
        preview_token=preview_token,
        preview_rows=preview_rows,
        preview_errors=preview_errors,
        summary_counts=summary_counts,

        uploaded_files=uploaded_files,
        shipments=shipments_parsed,
        selected_shipment=selected_shipment,
        selected_shipment_id=selected_shipment_id,
        shipment_packages=shipment_packages,
        all_packages=parsed_packages,

        search=search,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        house=house,
        tracking=tracking,
        user_code=user_code,
        first_name=first_name,
        last_name=last_name,
        unassigned_only=unassigned_only,
        unassigned_id=unassigned_id,
        epc_only=epc_only,

        page=page,
        allowed_page_sizes=allowed_page_sizes,
        total_pages=total_pages,
        prev_page=prev_page,
        next_page=next_page,
        first_page=first_page,
        last_page=last_page,
        total_count=total_count,
        showing_from=showing_from,
        showing_to=showing_to,

        total_packages=filtered_total_packages,
        total_weight=filtered_total_weight,
        daily_totals=daily_totals,

        now=datetime.now,

        active_tab=active_tab,
        tab=active_tab,

        categories=categories,
        CATEGORIES=CATEGORIES,
        USD_TO_JMD=USD_TO_JMD
    )



@logistics_bp.route('/packages/bulk-assign', methods=['POST'])
@admin_required
def bulk_assign_packages():
    from flask import request, redirect, url_for, flash
    import sqlite3
    from app.config import DB_PATH

    user_code  = (request.form.get('user_code') or '').strip()
    reset_flag = request.form.get('reset_status') == '1'
    pkg_ids    = request.form.getlist('package_ids')

    if not pkg_ids:
        flash("No packages selected.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    if not user_code:
        flash("Please enter a customer code or email.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        # Resolve the customer by registration_number OR email
        c.execute("""
            SELECT id FROM users
            WHERE registration_number = ?
               OR lower(email) = lower(?)
            LIMIT 1
        """, (user_code, user_code))
        row = c.fetchone()
        if not row:
            conn.close()
            flash(f"No user found for “{user_code}”.", "danger")
            return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

        new_user_id = int(row[0])

        updated = 0
        skipped = 0

        for pid in pkg_ids:
            try:
                pid_i = int(pid)
            except Exception:
                skipped += 1
                continue

            # Fetch current status and existence
            cur = c.execute("SELECT status FROM packages WHERE id = ? LIMIT 1", (pid_i,))
            pkg = cur.fetchone()
            if not pkg:
                skipped += 1
                continue

            current_status = (pkg[0] or '').strip()

            # If asked, and currently Unassigned, also reset status to Overseas
            if reset_flag and current_status.lower() == 'unassigned':
                c.execute("""
                    UPDATE packages
                    SET user_id = ?, status = 'Overseas'
                    WHERE id = ?
                """, (new_user_id, pid_i))
            else:
                c.execute("""
                    UPDATE packages
                    SET user_id = ?
                    WHERE id = ?
                """, (new_user_id, pid_i))

            updated += 1

        conn.commit()
        conn.close()

        msg = f"Assigned {updated} package(s)"
        if reset_flag:
            msg += " (reset Unassigned → Overseas where applicable)"
        if skipped:
            msg += f". Skipped {skipped}."
        flash(msg + ".", "success")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash(f"Bulk assign failed: {e}", "danger")

    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

@logistics_bp.route('/preview/<token>/invalid.csv', methods=['GET'])
@admin_required
def download_preview_invalid(token):
    data = _load_preview_blob(token)
    if not data:
        flash("Preview session expired.", "warning")
        return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

    display_headers = data.get("display_headers", [])
    original_rows   = data.get("original_rows", [])
    row_errors      = data.get("row_errors", {}) or {}

    invalid_idxs = sorted([int(k) for k in row_errors.keys() if str(k).isdigit()])

    # Build CSV
    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(display_headers + ["_Errors"])

    for i in invalid_idxs:
        row = original_rows[i] if i < len(original_rows) else {}
        errors = row_errors.get(str(i)) or row_errors.get(i) or []
        writer.writerow([row.get(h, "") for h in display_headers] + ["; ".join(errors)])

    output = sio.getvalue().encode("utf-8")
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=invalid_rows.csv"}
    )


@logistics_bp.route('/invoice/<int:invoice_id>/payment', methods=['POST'])
@admin_required
def add_payment(invoice_id):
    form = PaymentForm()
    if not form.validate_on_submit():
        flash("⚠️ Invalid payment submission. Please try again.", "danger")
        return redirect(request.referrer or url_for("logistics.logistics_dashboard"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # pull what we need
    c.execute("SELECT user_id, invoice_number, amount FROM invoices WHERE id=?", (invoice_id,))
    inv = c.fetchone()
    if not inv:
        conn.close()
        flash("Invoice not found.", "danger")
        return redirect(request.referrer or url_for("logistics.logistics_dashboard"))

    user_id = inv["user_id"]
    invoice_number = inv["invoice_number"]
    current_amount = float(inv["amount"] or 0)
    payment_amt = float(form.amount.data or 0)

    # record payment
    c.execute("""
        INSERT INTO payments (user_id, bill_number, payment_date, payment_type, amount, authorized_by, invoice_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        invoice_number,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        form.payment_type.data,
        payment_amt,
        "FAFL ADMIN",
        f"invoices/{invoice_number.replace(' ', '_')}.pdf"
    ))

    # update invoice balance
    new_balance = max(current_amount - payment_amt, 0)
    c.execute("UPDATE invoices SET amount=? WHERE id=?", (new_balance, invoice_id))

    conn.commit()
    conn.close()

    flash(f"✅ Payment of {payment_amt:,.2f} JMD recorded for {invoice_number}.", "success")
    return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

# --------------------------

@logistics_bp.route('/download-packages', methods=['GET'])
@admin_required
def download_packages():
    """
    Export filtered packages. If ?format=csv, emit a CSV with the exact
    headings the warehouse wants (including TRN). Otherwise, keep the
    existing Excel export.
    """
    fmt      = (request.args.get('format') or '').lower()  # csv | xlsx (default)
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    house     = request.args.get('house', '', type=str)
    tracking  = request.args.get('tracking', '', type=str)
    user_code = request.args.get('user_code', '', type=str)
    first     = request.args.get('first_name', '', type=str)
    last      = request.args.get('last_name', '', type=str)
    search    = request.args.get('search', '', type=str)
    status    = request.args.get('status', '', type=str)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    base = """
        FROM packages p
        JOIN users u ON p.user_id = u.id
        WHERE 1=1
    """
    where_sql, params = [], []

    # Use date_received if present, else created_at (matches dashboard)
    date_expr = "date(COALESCE(p.date_received, p.created_at))"
    if date_from and date_to:
        where_sql.append(f"AND {date_expr} BETWEEN ? AND ?")
        params += [date_from, date_to]
    elif date_from:
        where_sql.append(f"AND {date_expr} >= ?")
        params.append(date_from)
    elif date_to:
        where_sql.append(f"AND {date_expr} <= ?")
        params.append(date_to)

    if house:
        where_sql.append("AND p.house_awb LIKE ?")
        params.append(f"%{house.strip()}%")

    if tracking:
        where_sql.append("AND p.tracking_number LIKE ?")
        params.append(f"%{tracking.strip()}%")

    if user_code:
        where_sql.append("AND u.registration_number LIKE ?")
        params.append(f"%{user_code.strip()}%")

    if first:
        where_sql.append("AND u.full_name LIKE ?")
        params.append(f"%{first.strip()}%")
    if last:
        where_sql.append("AND u.full_name LIKE ?")
        params.append(f"%{last.strip()}%")

    if search:
        like = f"%{search}%"
        where_sql.append("AND (u.full_name LIKE ? OR p.tracking_number LIKE ? OR p.house_awb LIKE ?)")
        params += [like, like, like]

    if status:
        where_sql.append("AND p.status = ?")
        params.append(status)

    # Pull everything we need (include TRN)
    select_sql = f"""
        SELECT
            p.id                       AS pkg_id,
            p.tracking_number          AS tracking_number,
            p.house_awb                AS house_awb,
            p.shipper                  AS shipper,
            p.weight                   AS weight,
            COALESCE(p.date_received, p.created_at) AS date_any,
            p.description              AS description,
            u.full_name                AS full_name,
            u.registration_number      AS reg_no,
            u.trn                      AS trn
    """ + base + " " + " ".join(where_sql) + f" ORDER BY {date_expr} DESC"

    c.execute(select_sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    # -------------- CSV with exact headings --------------
    if fmt == "csv":
        
        # exact headings required by the warehouse
        HEADERS = [
            "GUID","USER CODE","FIRST NAME","LAST NAME","SHIPPER","HOUSE AWB",
            "MANIFEST CODE","COLLECTION CODE","COLLECTION ID","WEIGHT",
            "TRACKING Number","DATE","BRANCH","DESCRIPTION","HS CODE","UNKNOWN","TRN"
        ]

        def split_name(full):
            parts = (full or "").strip().split()
            if not parts:
                return "", ""
            if len(parts) == 1:
                return parts[0], ""
            return parts[0], " ".join(parts[1:])

        sio = io.StringIO(newline="")
        writer = csv.writer(sio)
        writer.writerow(HEADERS)

        for r in rows:
            first, last = split_name(r.get("full_name"))
            # GUID: use p.guid if you add one later; for now derive a stable uuid5 from IDs
            # (keeps it deterministic per package)
            guid = uuid.uuid5(uuid.NAMESPACE_URL,
                              f"pkg|{r.get('pkg_id')}|{r.get('tracking_number') or ''}|{r.get('house_awb') or ''}").hex

            date_str = ""
            d = r.get("date_any")
            if d:
                # best effort format YYYY-MM-DD
                try:
                    from datetime import datetime
                    # handle both datetime and string
                    if isinstance(d, str):
                        try:
                            date_str = datetime.fromisoformat(d).date().isoformat()
                        except Exception:
                            date_str = d[:10]
                    else:
                        date_str = d[:10] if isinstance(d, str) else d.date().isoformat()
                except Exception:
                    date_str = str(d)[:10]

            writer.writerow([
                guid,
                r.get("reg_no") or "",                 # USER CODE
                first,                                  # FIRST NAME
                last,                                   # LAST NAME
                r.get("shipper") or "",                 # SHIPPER
                r.get("house_awb") or "",               # HOUSE AWB
                "",                                     # MANIFEST CODE (no column yet)
                "",                                     # COLLECTION CODE
                "",                                     # COLLECTION ID
                r.get("weight") or "",                  # WEIGHT
                r.get("tracking_number") or "",         # TRACKING Number (case preserved)
                date_str,                               # DATE
                "",                                     # BRANCH (no column yet)
                r.get("description") or "",             # DESCRIPTION
                "",                                     # HS CODE (no column yet)
                "",                                     # UNKNOWN (placeholder column)
                r.get("trn") or ""                      # TRN (customer TRN)
            ])

        out = sio.getvalue().encode("utf-8")
        return Response(
            out,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=packages_export.csv"}
        )

    # -------------- default: keep your Excel export --------------
    # (unchanged from your current behavior, minor tidy)
    output = io.BytesIO()
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=[
            "User","User Code","Tracking Number","House AWB",
            "Description","Weight (lbs)","Value (USD)",
            "Amount Due","Status","Created At"
        ])
    else:
        # Present a friendlier default tab if they don't choose CSV
        df = pd.DataFrame([{
            "User": r.get("full_name"),
            "User Code": r.get("reg_no"),
            "Tracking Number": r.get("tracking_number"),
            "House AWB": r.get("house_awb"),
            "Description": r.get("description"),
            "Weight (lbs)": r.get("weight"),
            "TRN": r.get("trn"),
            "Created/Date": r.get("date_any"),
        } for r in rows])

        try:
            df["Created/Date"] = pd.to_datetime(df["Created/Date"])
        except Exception:
            pass

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Packages")
        ws = writer.sheets["Packages"]
        for col in ws.columns:
            header = col[0].value or ""
            width = min(max(len(str(header)), *(len(str(c.value)) for c in col[1:] if c.value is not None)) + 2, 40)
            ws.column_dimensions[col[0].column_letter].width = width

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="packages.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@logistics_bp.route('/logistics/email-selected-packages', methods=['POST'], endpoint='email_selected_packages')
@admin_required
def email_selected_packages():
    """
    Sends ONE email per customer for the selected packages.
    Email includes: House AWB, Rounded-up Weight (lbs), Tracking #, Description.
    Subject format: 'Foreign A Foot Logistics Limited received a new package overseas for FAFL #<reg_number>'
    """
    # 1) Ensure we have selected ids
    package_ids = request.form.getlist('package_ids')
    if not package_ids:
        flash("Please select at least one package to email.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    # 2) Fetch packages joined with their owners
    #    (Use ORM join to keep it SQLAlchemy-friendly)
    rows = (
        db.session.query(Package, User)
        .join(User, Package.user_id == User.id)
        .filter(Package.id.in_(package_ids))
        .all()
    )

    if not rows:
        flash("No matching packages found.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    # 3) Group selected packages by customer
    grouped = {}  # {user_id: {"user": User, "packages": [Package,...]}}
    for pkg, user in rows:
        if user.id not in grouped:
            grouped[user.id] = {"user": user, "packages": []}
        grouped[user.id]["packages"].append(pkg)

    # 4) Send ONE email per customer bundle
    sent_count = 0
    failed = []

    for bundle in grouped.values():
        user = bundle["user"]
        pkgs = bundle["packages"]

        ok = email_utils.send_overseas_received_email(
            to_email=user.email,
            full_name=(user.full_name or ""),
            reg_number=(user.registration_number or ""),
            packages=pkgs,  # list of Package rows
        )
        if ok:
            sent_count += 1
        else:
            failed.append(user.email or "(no email)")

    # 5) Feedback to UI
    if sent_count:
        flash(f"Emailed {sent_count} customer(s) about selected package(s).", "success")
    if failed:
        flash("Some emails failed: " + ", ".join(failed), "danger")

    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

@logistics_bp.route('/send-invoice-request', methods=['POST'])
@admin_required
def send_invoice_request():
    # Get selected package IDs
    package_ids = request.form.getlist('package_ids')

    # TODO: Add your logic to send invoice requests
    flash(f"Invoice request sent for {len(package_ids)} package(s).", "success")

    return redirect(url_for('logistics.logistics_dashboard'))


# --------------------------
# --------------------------
# Bulk Actions (Update Status / Delete / Export)

@logistics_bp.route("/packages/bulk-action", methods=["POST"])
@admin_required
def packages_bulk_action():
    form = PackageBulkActionForm()

    if not form.validate_on_submit():
        flash("Invalid request (CSRF or form).", "danger")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    action = (request.form.get("action") or request.form.get("bulk_action") or "").strip()
    # Accept either name from the UI: "package_ids" or "selected_ids"
    package_ids = request.form.getlist("package_ids") or request.form.getlist("selected_ids")

    if not action:
        flash("No action selected.", "warning")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    if not package_ids:
        flash("Please select at least one package.", "warning")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    try:
        # --- Ensure EPC column exists (SQLite) ---
        c.execute("PRAGMA table_info(packages)")
        cols = {row["name"] for row in c.fetchall()}
        if "epc" not in cols:
            c.execute("ALTER TABLE packages ADD COLUMN epc INTEGER NOT NULL DEFAULT 0")
            conn.commit()

        if action == "delete":
            q_marks = ",".join("?" * len(package_ids))
            c.execute(f"SELECT DISTINCT invoice_id FROM packages WHERE id IN ({q_marks}) AND invoice_id IS NOT NULL", package_ids)
            affected_invoice_ids = [row["invoice_id"] for row in c.fetchall()]
            c.execute(f"DELETE FROM shipment_packages WHERE package_id IN ({q_marks})", package_ids)
            c.execute(f"DELETE FROM packages WHERE id IN ({q_marks})", package_ids)

            if affected_invoice_ids:
                q2 = ",".join("?" * len(affected_invoice_ids))
                c.execute(f"""
                    SELECT invoice_id, COUNT(*) AS cnt
                    FROM packages
                    WHERE invoice_id IN ({q2})
                    GROUP BY invoice_id
                """, affected_invoice_ids)
                still_used = {row["invoice_id"] for row in c.fetchall() if row["cnt"] > 0}
                orphan_invoices = [iid for iid in affected_invoice_ids if iid not in still_used]
                if orphan_invoices:
                    q3 = ",".join("?" * len(orphan_invoices))
                    c.execute(f"DELETE FROM invoices WHERE id IN ({q3})", orphan_invoices)

            conn.commit()
            flash(f"Deleted {len(package_ids)} package(s).", "success")

        elif action == "mark_received":
            q_marks = ",".join("?" * len(package_ids))
            c.execute(f"UPDATE packages SET status='Received' WHERE id IN ({q_marks})", package_ids)
            conn.commit()
            flash(f"Marked {len(package_ids)} package(s) as Received.", "success")

        elif action == "export_pdf":
            pdf_buffer = io.BytesIO()
            p = canvas.Canvas(pdf_buffer, pagesize=letter)
            y = 750
            q_marks = ",".join("?" * len(package_ids))
            c.execute(f"SELECT tracking_number, description, weight, status FROM packages WHERE id IN ({q_marks})", package_ids)
            for pkg in c.fetchall():
                p.drawString(50, y, f"Tracking: {pkg['tracking_number']}, Desc: {pkg['description']}, Weight: {pkg['weight']} lbs, Status: {pkg['status']}")
                y -= 20
                if y < 60:
                    p.showPage()
                    y = 750
            p.save()
            pdf_buffer.seek(0)
            conn.close()
            return send_file(pdf_buffer, as_attachment=True, download_name="packages.pdf", mimetype="application/pdf")

        elif action == "create_shipment":
            conn.close()
            return redirect(url_for('logistics.create_shipment'))

        elif action == "assign":
            conn.close()
            return redirect(url_for('logistics.assign_packages'))

        elif action == "invoice":
            conn.close()
            return redirect(url_for('logistics.send_invoice_request'))

        # ---------- NEW: EPC bulk actions ----------
        elif action == "mark_epc":
            q_marks = ",".join("?" * len(package_ids))
            c.execute(f"UPDATE packages SET epc = 1 WHERE id IN ({q_marks})", package_ids)
            conn.commit()
            flash(f"Marked {len(package_ids)} package(s) as EPC.", "success")

        elif action == "clear_epc":
            q_marks = ",".join("?" * len(package_ids))
            c.execute(f"UPDATE packages SET epc = 0 WHERE id IN ({q_marks})", package_ids)
            conn.commit()
            flash(f"Cleared EPC on {len(package_ids)} package(s).", "success")

        else:
            flash("Unknown action.", "danger")

    except Exception as e:
        conn.rollback()
        flash(f"Error performing bulk action: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("logistics.logistics_dashboard", tab="view_packages",
                            page=request.args.get("page", 1),
                            per_page=request.args.get("per_page", 10),
                            date_from=request.args.get("date_from"),
                            date_to=request.args.get("date_to"),
                            house=request.args.get("house"),
                            tracking=request.args.get("tracking"),
                            user_code=request.args.get("user_code"),
                            first_name=request.args.get("first_name"),
                            last_name=request.args.get("last_name")))

# --------------------------
# View Single Package
# --------------------------
@logistics_bp.route('/view-packages')
@admin_required
def view_packages():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT p.*, u.full_name, u.registration_number
        FROM packages p
        JOIN users u ON p.user_id = u.id
        ORDER BY p.created_at DESC
    """)
    all_packages = c.fetchall()
    conn.close()

    return render_template("admin/logistics/view_packages.html",
                           all_packages=all_packages,
                           bulk_form=PackageBulkActionForm())
@logistics_bp.route('/shipmentlog/invoices/preview', methods=['POST'])
@admin_required
def bulk_invoice_preview():
    data = request.get_json(silent=True) or {}
    pkg_ids = [int(x) for x in (data.get('package_ids') or [])]
    if not pkg_ids:
        return jsonify({"rows": [], "detained_skipped": 0})

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    q = f"""
      SELECT p.id, p.user_id, p.amount_due, p.status,
             u.full_name, u.registration_number
        FROM packages p
        JOIN users u ON u.id = p.user_id
       WHERE p.id IN ({",".join(["?"]*len(pkg_ids))})
    """
    c.execute(q, pkg_ids)
    rows = c.fetchall()
    conn.close()

    grouped = {}
    detained = 0
    for r in rows:
        r = dict(r)
        if (r.get("status") or "").lower() == "detained":
            detained += 1
            continue
        uid = r["user_id"]
        g = grouped.setdefault(uid, {
            "user_id": uid, "full_name": r["full_name"],
            "registration_number": r["registration_number"],
            "package_ids": [], "package_count": 0, "total_due": 0.0
        })
        g["package_ids"].append(r["id"])
        g["package_count"] += 1
        g["total_due"] += float(r.get("amount_due") or 0.0)

    return jsonify({"rows": list(grouped.values()), "detained_skipped": detained})


@logistics_bp.route('/shipmentlog/invoices/finalize-json', methods=['POST'])
@admin_required
def bulk_invoice_finalize_json():
    data = request.get_json(silent=True) or {}
    selections = data.get('selections') or []
    if not selections:
        return jsonify({"created": 0, "invoices": []})

    def work_once():
        conn = get_conn()
        try:
            # single writer txn
            conn.execute("BEGIN IMMEDIATE;")

            out = []
            created = 0

            for sel in selections:
                uid = int(sel["user_id"])
                pkg_ids = [int(x) for x in sel.get("package_ids") or []]
                if not pkg_ids:
                    continue

                placeholders = ",".join("?" for _ in pkg_ids)
                tot = conn.execute(
                    f"SELECT COALESCE(SUM(amount_due),0) AS tot FROM packages WHERE id IN ({placeholders})",
                    pkg_ids
                ).fetchone()["tot"] or 0.0
                total_amount = float(tot)

                # bill first (so invoices.bill_id NOT NULL is satisfied)
                bill_no = _next_bill_number_tx(conn)
                now_iso = datetime.utcnow().isoformat()
                first_pkg_id = pkg_ids[0]

                cur = conn.execute("""
                    INSERT INTO bills (user_id, package_id, description, amount, status, due_date, bill_number, total_amount, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'unpaid', NULL, ?, ?, ?, ?)
                """, (uid, first_pkg_id, f"Auto bill for {len(pkg_ids)} package(s)",
                      total_amount, bill_no, total_amount, now_iso, now_iso))
                bill_id = cur.lastrowid

                # invoice next
                inv_no = _next_invoice_number_tx(conn)
                cur = conn.execute("""
                    INSERT INTO invoices (user_id, bill_id, invoice_number, description, amount, status, date_submitted)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """, (uid, bill_id, inv_no, f"Auto invoice for {len(pkg_ids)} package(s)",
                      total_amount, now_iso))
                invoice_id = cur.lastrowid

                # attach packages
                conn.execute(
                    f"UPDATE packages SET invoice_id=? WHERE id IN ({placeholders})",
                    [invoice_id] + pkg_ids
                )

                created += 1
                out.append({
                    "bill_id": bill_id, "bill_number": bill_no,
                    "invoice_id": invoice_id, "invoice_number": inv_no,
                    "amount": total_amount
                })

            conn.commit()
            return jsonify({"created": created, "invoices": out})

        except Exception as e:
            conn.rollback()
            return jsonify({"error": str(e)}), 500
        finally:
            conn.close()

    # retry if the DB is momentarily locked
    return _retry_locked(work_once)


@logistics_bp.route('/shipmentlog/bulk-calc-outstanding', methods=['POST'])
@admin_required
def bulk_calc_outstanding():
    """
    Body: { rows: [{pkg_id, category, invoice, weight}, ...] }
    Returns: { results: [{pkg_id, invoice, grand_total, breakdown}, ...] }
    """
    payload = request.get_json(silent=True) or {}
    rows = payload.get('rows') or []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    results = []

    for r in rows:
        pkg_id   = int(r.get('pkg_id'))
        # read client-provided fields
        category = (r.get('category') or '').strip()
        invoice  = r.get('invoice')
        weight   = r.get('weight')

        # fallback to DB values when client sent None/0/empty
        if invoice in (None, '', 0, 0.0, '0', '0.0'):
            c.execute("SELECT value FROM packages WHERE id=?", (pkg_id,))
            row = c.fetchone()
            invoice = float((row['value'] if row else 0) or 0)

        if weight in (None, '', 0, 0.0, '0', '0.0'):
            c.execute("SELECT weight FROM packages WHERE id=?", (pkg_id,))
            row = c.fetchone()
            weight = float((row['weight'] if row else 0) or 0)

        if not category:
            c.execute("SELECT category FROM packages WHERE id=?", (pkg_id,))
            row = c.fetchone()
            category = (row['category'] if row and row['category'] else 'Other')

        # IMPORTANT: normalize weight exactly like the single-row path
        weight_eff = _normalize_weight(weight)

        # Use the SAME calculator as the single API
        breakdown = calculate_charges(category, float(invoice), float(weight_eff))

        results.append({
            "pkg_id": pkg_id,
            "invoice": float(invoice),
            "grand_total": float(breakdown.get("grand_total", 0)),
            "breakdown": breakdown
        })

    conn.close()
    return jsonify({"results": results})

@logistics_bp.route('/shipmentlog/bulk-save', methods=['POST'])
@admin_required
def bulk_save_packages():
    """
    Body: { rows: [{
        pkg_id, value, amount_due,
        duty, scf, envl, caf, gct, stamp,
        freight, handling, category, weight,
        customs_total, freight_total, other_charges, grand_total
    }, ...] }

    Notes:
      - 'freight' maps to 'freight_fee'
      - 'handling' maps to 'storage_fee'
      - when 'value' is provided, we also set declared_value = value
      - COALESCE(?, col) means: if payload field is None/missing, leave the column unchanged
    """
    payload = request.get_json(silent=True) or {}
    rows = payload.get('rows') or []

    if not rows:
        return jsonify({"updated": 0}), 200

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    sql = """
      UPDATE packages
      SET
        -- value + mirror to declared_value
        value          = COALESCE(?, value),
        declared_value = COALESCE(?, declared_value),

        amount_due     = COALESCE(?, amount_due),

        duty           = COALESCE(?, duty),
        scf            = COALESCE(?, scf),
        envl           = COALESCE(?, envl),
        caf            = COALESCE(?, caf),
        gct            = COALESCE(?, gct),
        stamp          = COALESCE(?, stamp),

        customs_total  = COALESCE(?, customs_total),

        freight_fee    = COALESCE(?, freight_fee),   -- from 'freight'
        storage_fee    = COALESCE(?, storage_fee),   -- from 'handling'
        freight_total  = COALESCE(?, freight_total),
        other_charges  = COALESCE(?, other_charges),

        category       = COALESCE(?, category),
        weight         = COALESCE(?, weight),

        grand_total    = COALESCE(?, grand_total)
      WHERE id = ?
    """

    params = []
    for r in rows:
        # pull once, keep None if not present (so COALESCE leaves column as-is)
        value         = r.get('value')
        amount_due    = r.get('amount_due')
        duty          = r.get('duty')
        scf           = r.get('scf')
        envl          = r.get('envl')
        caf           = r.get('caf')
        gct           = r.get('gct')
        stamp         = r.get('stamp')
        customs_total = r.get('customs_total')
        freight       = r.get('freight')            # -> freight_fee
        handling      = r.get('handling')           # -> storage_fee
        freight_total = r.get('freight_total')
        other_charges = r.get('other_charges')
        category      = r.get('category') or None
        weight        = r.get('weight')
        grand_total   = r.get('grand_total')

        pkg_id        = int(r['pkg_id'])

        params.append((
            value,          # value
            value,          # declared_value mirror
            amount_due,

            duty, scf, envl, caf, gct, stamp,
            customs_total,

            freight,        # freight_fee
            handling,       # storage_fee
            freight_total,
            other_charges,

            category,
            weight,

            grand_total,
            pkg_id
        ))

    c.executemany(sql, params)
    conn.commit()
    conn.close()

    return jsonify({"updated": len(params)}), 200
@logistics_bp.route('/packages/<int:package_id>/delete', methods=['POST'])
@admin_required
def delete_package(package_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("DELETE FROM packages WHERE id=?", (package_id,))
        conn.commit()
        flash("Package deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting package: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for('logistics.logistics_dashboard'))

@logistics_bp.route('/api/save-pricing', methods=['POST'], endpoint='api_save_pricing')
@admin_required
def api_save_pricing():
    data = request.get_json()
    pkg_id = data.get('pkg_id')
    # save all other fields to DB here
    return jsonify({"status": "success"})


@logistics_bp.route('/api/package/<int:pkg_id>', methods=['POST'])
@admin_required
def api_update_package(pkg_id):
    data = request.get_json(force=True) or {}

    # client->db column map
    field_map = {
        'category': 'category',
        'value': 'value',               # special: also mirrors declared_value
        'weight': 'weight',
        'amount_due': 'amount_due',

        'duty': 'duty',
        'scf': 'scf',
        'envl': 'envl',
        'caf': 'caf',
        'gct': 'gct',
        'stamp': 'stamp',
        'customs_total': 'customs_total',

        'freight': 'freight_fee',       # mapped
        'handling': 'storage_fee',      # mapped
        'freight_total': 'freight_total',
        'other_charges': 'other_charges',

        'grand_total': 'grand_total'
    }

    sets, params = [], []

    # mirror declared_value whenever value is present
    if 'value' in data and data['value'] is not None:
        sets.append("value = ?")
        params.append(data['value'])
        sets.append("declared_value = ?")
        params.append(data['value'])

    # handle the rest
    for client_key, db_col in field_map.items():
        if client_key == 'value':  # already handled above
            continue
        if client_key in data and data[client_key] is not None:
            sets.append(f"{db_col} = ?")
            params.append(data[client_key])

    if not sets:
        return jsonify({"ok": True, "message": "Nothing to update"}), 200

    params.append(pkg_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE packages SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "updated": list(data.keys())}), 200


# --------------------------
# --------------------------
# --------------------------
# Shipment Logs (sqlite3 version)
# --------------------------

@logistics_bp.route('/shipmentlog/create', methods=['POST'])
@admin_required
def create_shipment():
    # package_ids may come duplicated; sanitize to unique integers
    raw_ids = request.form.getlist('package_ids')
    package_ids = []
    for x in raw_ids:
        try:
            package_ids.append(int(x))
        except Exception:
            continue
    package_ids = list(dict.fromkeys(package_ids))  # preserve order, dedupe

    if not package_ids:
        flash("No packages selected.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    # Ensure counters/indices exist (safe to call repeatedly)
    _ensure_counters_table()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        # Keep unique constraints
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package
            ON shipment_packages(package_id)
        """)

        # transactional section for counter + inserts
        conn.isolation_level = None
        c.execute("BEGIN IMMEDIATE")  # lock to avoid race

        # global, non-resetting sequence
        sl_id = _next_global_sl_id(c)

        # Insert the new shipment shell
        created_at = datetime.utcnow().isoformat()
        c.execute(
            "INSERT INTO shipment_log (sl_id, created_at) VALUES (?, ?)",
            (sl_id, created_at)
        )
        shipment_id = c.lastrowid

        # Link packages, skipping ones already assigned elsewhere
        conflicts, inserted = [], 0
        for pkg_id in package_ids:
            row = c.execute("SELECT shipment_id FROM shipment_packages WHERE package_id=?", (pkg_id,)).fetchone()
            if row:
                conflicts.append(pkg_id)
                continue
            c.execute(
                "INSERT INTO shipment_packages (shipment_id, package_id) VALUES (?, ?)",
                (shipment_id, pkg_id)
            )
            inserted += 1

        if inserted == 0:
            # nothing added → remove empty shipment (sequence was already advanced; that’s OK)
            c.execute("DELETE FROM shipment_log WHERE id=?", (shipment_id,))
            c.execute("COMMIT")
            conn.close()
            if conflicts:
                flash("All selected packages are already in other shipments. No new shipment was created.", "warning")
            else:
                flash("No valid packages to add.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

        c.execute("COMMIT")

    except Exception as e:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        flash(f"Error creating shipment: {e}", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    conn.close()

    if conflicts:
        flash(f"{len(conflicts)} package(s) were already in another shipment and were skipped.", "warning")
    flash(f"Shipment {sl_id} created with {inserted} package(s).", "success")
    return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

@logistics_bp.route('/shipmentlog/create-empty', methods=['POST'])
@admin_required
def create_empty_shipment():
    """
    Create a blank shipment_log row and redirect to it on the Shipment Log tab.
    """
    _ensure_counters_table()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package ON shipment_packages(package_id)")

        conn.isolation_level = None
        c.execute("BEGIN IMMEDIATE")

        sl_id = _next_global_sl_id(c)
        created_at = datetime.utcnow().isoformat()

        c.execute("INSERT INTO shipment_log (sl_id, created_at) VALUES (?, ?)", (sl_id, created_at))
        shipment_id = c.lastrowid

        c.execute("COMMIT")
    except Exception as e:
        try: c.execute("ROLLBACK")
        except Exception: pass
        conn.close()
        flash(f"Error creating blank shipment: {e}", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))

    conn.close()
    flash(f"Blank shipment {sl_id} created. You can now move packages into it.", "success")
    return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab='shipmentLog'))


@logistics_bp.route('/shipmentlog/<int:shipment_id>/delete', methods=['POST'])
@admin_required
def delete_shipment(shipment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Read sl_id for message
    c.execute("SELECT sl_id FROM shipment_log WHERE id=?", (shipment_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        flash("Shipment not found.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))

    sl_id = row[0]

    # Remove links, then the shipment container
    c.execute("DELETE FROM shipment_packages WHERE shipment_id=?", (shipment_id,))
    c.execute("DELETE FROM shipment_log WHERE id=?", (shipment_id,))

    conn.commit()
    conn.close()

    flash(f"Shipment {sl_id} deleted.", "success")
    return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))

@logistics_bp.route('/shipmentlog/search', methods=['GET'])
@admin_required
def search_shipment_packages():
    name     = (request.args.get('name') or '').strip()
    tracking = (request.args.get('tracking') or '').strip()
    house    = (request.args.get('house') or '').strip()
    reg      = (request.args.get('reg') or '').strip()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sql = """
      SELECT
        p.id, p.tracking_number, p.description, p.house_awb,
        u.full_name, u.registration_number,
        sl.sl_id
      FROM packages p
      JOIN users u ON u.id = p.user_id
      LEFT JOIN shipment_packages sp ON sp.package_id = p.id
      LEFT JOIN shipment_log sl ON sl.id = sp.shipment_id
      WHERE 1=1
    """
    params = []

    if name:
        sql += " AND u.full_name LIKE ?"
        params.append(f"%{name}%")
    if tracking:
        sql += " AND p.tracking_number LIKE ?"
        params.append(f"%{tracking}%")
    if house:
        sql += " AND p.house_awb LIKE ?"
        params.append(f"%{house}%")
    if reg:
        sql += " AND u.registration_number LIKE ?"
        params.append(f"%{reg}%")

    sql += " ORDER BY COALESCE(sl.sl_id, 'ZZZ'), u.full_name LIMIT 200"

    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    return jsonify({"rows": rows})

@logistics_bp.route('/shipmentlog/move', methods=['POST'])
@admin_required
def move_packages_between_shipments():
    from_id = request.form.get('from_shipment_id', type=int)
    to_id   = request.form.get('to_shipment_id', type=int)
    ids_raw = request.form.get('package_ids', '')  # comma-separated
    pkg_ids = [int(x) for x in ids_raw.split(',') if x.strip().isdigit()]

    if not to_id or not pkg_ids:
        flash("Select destination shipment and at least one package.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog', shipment_id=from_id or None))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Ensure unique index exists
    c.execute("""
      CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package
      ON shipment_packages(package_id)
    """)

    moved, inserted, updated = 0, 0, 0
    for pid in pkg_ids:
      # Try update first
      c.execute("UPDATE shipment_packages SET shipment_id=? WHERE package_id=?", (to_id, pid))
      if c.rowcount:
          updated += 1
          moved += 1
          continue
      # No existing row: insert
      try:
          c.execute("INSERT INTO shipment_packages (shipment_id, package_id) VALUES (?, ?)", (to_id, pid))
          inserted += 1
          moved += 1
      except sqlite3.IntegrityError:
          # should not happen because of update-first; but just in case
          pass

    conn.commit()
    conn.close()

    flash(f"Moved {moved} package(s) to shipment.", "success")
    return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog', shipment_id=to_id))


@logistics_bp.route('/api/calculate-charges', methods=['POST'])
@admin_required
def api_calculate_charges():
    data = request.get_json() or {}
    # accept either "invoice" (JS) or "invoice_usd"
    invoice = float(data.get('invoice') or data.get('invoice_usd') or 50)
    category = data.get('category')
    weight = float(data.get('weight') or 0)
    
    result = calculate_charges(category, invoice, weight)
    return jsonify(result)

def parse_date(value):
    """Convert DB date strings to datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None



@logistics_bp.route('/shipmentlog/<int:shipment_id>/bulk-action', methods=['POST'])
@admin_required
def bulk_shipment_action(shipment_id):
    upload_form = UploadPackageForm()
    prealert_form = PreAlertForm()
    bulk_form = PackageBulkActionForm()
    invoice_finalize_form = InvoiceFinalizeForm()

    package_ids = request.form.getlist('package_ids')
    action = request.form.get('action')

    if not action or not package_ids:
        flash("⚠️ Please select both an action and at least one package.", "warning")
        return redirect(url_for('logistics.logistics_dashboard',
                                shipment_id=shipment_id, tab="shipmentLog"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if action == "generate_invoice":
        conn.close()
        flash("Use the new invoice preview modal to generate invoices.", "info")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    elif action == "ready":
        c.executemany(
            "UPDATE packages SET status=? WHERE id=?",
            [("Ready for Pick Up", int(pid)) for pid in package_ids]
        )
        conn.commit()
        conn.close()
        flash(f"{len(package_ids)} package(s) marked Ready for Pick Up.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    # ----------------------------------------------------------
    # NEW: Bulk email — Notify ready for pick-up (group by user)
    # ----------------------------------------------------------
    elif action == "notify_ready":
        from app.utils.email_utils import send_email, compose_ready_pickup_email
        from math import ceil

        # group by user
        for uid in set([row['user_id'] for row in conn.execute("SELECT user_id FROM packages WHERE id IN ({})".format(",".join(package_ids)))]):
            c.execute("SELECT full_name, email, registration_number FROM users WHERE id=?", (uid,))
            u = c.fetchone()

            # get only this users selected packages
            c.execute(
                "SELECT shipper, house_awb, tracking_number, weight FROM packages WHERE id IN ({}) AND user_id=?".format(",".join(package_ids)),
                (uid,)
            )
            pkgs = [dict(x) for x in c.fetchall()]

            subject, plain, html = compose_ready_pickup_email(u['full_name'], pkgs)
            send_email(u['email'], subject, plain, html)

        conn.close()
        flash(f"{len(package_ids)} package(s) marked Ready and notifications sent.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    elif action == "send_invoices":
        # For each selected package's user, compute total_due and send a link
        placeholders = ",".join("?" for _ in package_ids)
        # Get distinct users from selected packages
        users_sql = f"""
          SELECT DISTINCT u.id AS user_id, u.full_name, u.email
          FROM packages p
          JOIN users u ON u.id = p.user_id
          WHERE p.id IN ({placeholders})
        """
        users = c.execute(users_sql, [int(x) for x in package_ids]).fetchall()

        # For each user, get sum of their open invoices (you can scope to current shipment if you store that linkage)
        sent = failed = 0
        from app.utils.email_utils import send_shipment_invoice_link_email

        for urow in users:
            email = urow["email"]
            full_name = urow["full_name"]
            uid = urow["user_id"]
            if not email:
                failed += 1
                continue

            # Sum outstanding amount_due
            tot_sql = """
              SELECT IFNULL(SUM(amount_due),0) AS total_due
              FROM invoices
              WHERE user_id=? AND amount_due>0 AND LOWER(status) IN ('pending','issued')
            """
            total_due = float(c.execute(tot_sql, (uid,)).fetchone()["total_due"] or 0)

            # Build a link to your invoice/receivables page (adjust route)
            invoice_link = url_for('finance.unpaid_invoices', _external=True, q=full_name)

            try:
                ok = send_shipment_invoice_link_email(email, full_name, total_due, invoice_link)
                sent += 1 if ok else 0
                failed += 0 if ok else 1
            except Exception as e:
                print(f"[EMAIL ERROR send_invoices] {e}")
                failed += 1

        conn.close()
        flash(f"Invoice emails queued for {sent} customer(s). {failed} failed/skipped.", "info")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))


        
    # ✅ REMOVE FROM THIS SHIPMENT (does NOT delete the package)
    elif action == "remove_from_shipment":
        # 1) Drop the junction links
        c.executemany(
            "DELETE FROM shipment_packages WHERE shipment_id=? AND package_id=?",
            [(shipment_id, int(pid)) for pid in package_ids]
        )

        # 2) (Optional) Revert package status back to a pre-shipment state
        #    Tweak the status string to match your app’s canonical values.
        c.executemany(
            "UPDATE packages SET status=? WHERE id=?",
            [("Overseas", int(pid)) for pid in package_ids]
        )

        conn.commit()
        removed = len(package_ids)
        conn.close()
        flash(f"Removed {removed} package(s) from shipment {shipment_id}.", "success")
        return redirect(url_for('logistics.logistics_dashboard',
                                shipment_id=shipment_id, tab="shipmentLog"))


    # ✅ UNKNOWN ACTION
    else:
        conn.close()
        flash("⚠️ Unknown action selected.", "danger")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

@logistics_bp.route('/invoices/<int:invoice_id>/status', methods=['POST'])
@admin_required
def set_invoice_status(invoice_id):
    new_status = (request.form.get('status') or '').lower()  # paid | unpaid | cancelled
    if new_status not in ('paid','unpaid','cancelled'):
        return jsonify({"ok": False, "error": "Invalid status"}), 400
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    if new_status == 'paid':
        c.execute("UPDATE invoices SET status=?, date_paid=? WHERE id=?", (new_status, datetime.utcnow().isoformat(), invoice_id))
    else:
        c.execute("UPDATE invoices SET status=?, date_paid=NULL WHERE id=?", (new_status, invoice_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})



# --------------------------
# Scheduled Deliveries
# --------------------------
@logistics_bp.route('/scheduled_deliveries', methods=['GET', 'POST'])
@admin_required
def view_scheduled_deliveries():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = ScheduledDelivery.query
    if start_date:
        query = query.filter(ScheduledDelivery.date >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        query = query.filter(ScheduledDelivery.date <= datetime.strptime(end_date, '%Y-%m-%d'))

    deliveries = query.order_by(ScheduledDelivery.date.desc()).all()
    return render_template('admin/logistics/scheduled_deliveries.html',
                           deliveries=deliveries,
                           start_date=start_date,
                           end_date=end_date)
@logistics_bp.route('/shipmentlog/create-shipment', methods=['GET'])
@admin_required
def prepare_create_shipment():
    """
    Redirect to the View Packages tab with a flag to allow package selection.
    """
    flash("Select packages to include in the new shipment.", "info")
    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages', create_shipment=1))


@logistics_bp.route('/scheduled_deliveries/pdf')
@admin_required
def scheduled_deliveries_pdf():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    query = ScheduledDelivery.query
    if start_date:
        query = query.filter(ScheduledDelivery.date >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        query = query.filter(ScheduledDelivery.date <= datetime.strptime(end_date, '%Y-%m-%d'))

    deliveries = query.order_by(ScheduledDelivery.date.desc()).all()

    # Generate PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 50

    p.setFont("Helvetica-Bold", 14)
    p.drawString(50, y, "Scheduled Deliveries Report")
    y -= 30
    p.setFont("Helvetica", 10)

    for d in deliveries:
        line = f"{d.date} {d.time} | {d.location} | {d.person_receiving} | {d.status} | Customer: {d.user.full_name}"
        p.drawString(50, y, line[:110])
        y -= 15
        if y < 50:
            p.showPage()
            y = height - 50

    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="scheduled_deliveries.pdf", mimetype='application/pdf')

# --------------------------
# Add Scheduled Delivery
# --------------------------
@logistics_bp.route('/scheduled_deliveries/add', methods=['GET', 'POST'])
@admin_required
def add_scheduled_delivery():
    form = ScheduledDeliveryForm()
    if form.validate_on_submit():
        # Create new scheduled delivery
        new_delivery = ScheduledDelivery(
            user_id=form.user_id.data,
            date=form.date.data,
            time=form.time.data,
            location=form.location.data,
            person_receiving=form.person_receiving.data,
            status=form.status.data
        )
        db.session.add(new_delivery)
        db.session.commit()
        flash("Scheduled delivery added successfully.", "success")
        return redirect(url_for('logistics.view_scheduled_deliveries'))
    
    return render_template('admin/logistics/add_scheduled_delivery.html', form=form)
