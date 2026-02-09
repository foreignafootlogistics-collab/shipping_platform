import os
import io
import re
import math
import csv
import uuid
import json
from io import StringIO
from datetime import datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, send_file, Response, current_app, session, 
    abort, send_from_directory
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from sqlalchemy import func, or_, and_, asc, desc, cast
from sqlalchemy.types import Date
from sqlalchemy.sql import func

from app.extensions import db
from app.routes.admin_auth_routes import admin_required
from app.models import (
    Prealert, User, ScheduledDelivery, ShipmentLog, Invoice,  
    Package, Payment, shipment_packages, PackageAttachment
)
from app.models import Message as DBMessage

from app.forms import (
    PackageBulkActionForm, UploadPackageForm, PreAlertForm, InvoiceFinalizeForm,
    PaymentForm, ScheduledDeliveryForm
)
from app.utils import email_utils, update_wallet
from app.utils.wallet import process_first_shipment_bonus

from app.calculator_data import calculate_charges, CATEGORIES, USD_TO_JMD


# Base URL for links used in emails (fallback to Render URL)
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://app-faflcourier.onrender.com"
)

# --------------------------------------------------------------------------------------
# Blueprint
# --------------------------------------------------------------------------------------
logistics_bp = Blueprint("logistics", __name__, url_prefix="/admin/logistics")

# --------------------------------------------------------------------------------------
# Constants / Helpers
# --------------------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {"xls", "xlsx", "csv"}

TAB_ALIASES = {
    "shipment": "shipmentLog",
}
ALLOWED_TABS = {"prealert", "view_packages", "shipmentLog", "uploadPackages"}

HEADER_MAP = {
    "USER CODE":        "registration_number",
    "SHIPPER":          "shipper",
    "HOUSE AWB":        "house_awb",
    "HOUSE AWB/CONTROL #": "house_awb",
    "HOUSE AWB / CONTROL #": "house_awb",
    "WEIGHT":           "weight",
    "TRACKING NUMBER":  "tracking_number",
    "TRACKING #":       "tracking_number",   # 👈 new
    "TRACKING#":        "tracking_number",
    "DATE":             "date_received",
    "DATE RECEIVED":    "date_received",
    "RECEIVED DATE":    "date_received",
    "DESCRIPTION":      "description",
    "VALUE":            "value",
    "FULL NAME":        "full_name",
    "EMAIL":            "email",
}
REQUIRED_FIELDS = ["registration_number", "tracking_number", "description", "weight"]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def normalize_tab(raw: str | None) -> str:
    t = (raw or "prealert").strip().lower()
    lower_map = {
        "prealert": "prealert",
        "view_packages": "view_packages",
        "shipmentlog": "shipmentLog",
        "uploadpackages": "uploadPackages",
        "shipment": "shipment",
    }
    t = lower_map.get(t, "prealert")
    t = TAB_ALIASES.get(t, t)
    return t if t in ALLOWED_TABS else "prealert"


def _preview_dir() -> str:
    try:
        base = current_app.instance_path
    except RuntimeError:
        base = os.path.join(os.getcwd(), "instance")
    path = os.path.join(base, "tmp_preview_uploads")
    os.makedirs(path, exist_ok=True)
    return path


def cleanup_preview_dir(max_age_hours: int = 24):
    cutoff = datetime.utcnow().timestamp() - (max_age_hours * 3600)
    d = _preview_dir()
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except Exception:
            pass


def _save_preview_blob(data: dict) -> str:
    token = str(uuid.uuid4())
    path = os.path.join(_preview_dir(), f"{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return token


def _load_preview_blob(token: str) -> dict | None:
    path = os.path.join(_preview_dir(), f"{token}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_headers(cols):
    norm = []
    for c in cols:
        raw = str(c).strip()
        mapped = HEADER_MAP.get(raw)
        if mapped:
            norm.append(mapped)
            continue
        low = raw.lower().replace(" ", "_")
        mapped2 = HEADER_MAP.get(raw.upper())
        norm.append(mapped2 if mapped2 else low)
    return norm


def _read_any_table(file_storage) -> pd.DataFrame:
    raw = file_storage.read()
    file_storage.seek(0)
    try:
        return pd.read_excel(io.BytesIO(raw))
    except Exception:
        try:
            return pd.read_csv(io.BytesIO(raw))
        except Exception:
            return pd.read_csv(io.BytesIO(raw), encoding="latin-1")


def _validate_rows(df: pd.DataFrame):
    df = df.copy()
    display_headers = list(df.columns)
    df.columns = _normalize_headers(df.columns)
    rows = df.to_dict(orient="records")
    row_errors: dict[int, list[str]] = {}

    for i, r in enumerate(rows):
        errs: list[str] = []
        for f in REQUIRED_FIELDS:
            val = r.get(f, None)
            if pd.isna(val) or str(val).strip() == "":
                errs.append(f"Missing {f.replace('_',' ').title()}")
        try:
            if "weight" in r and not pd.isna(r["weight"]):
                r["weight"] = float(r["weight"])
            else:
                r["weight"] = 0.0
        except Exception:
            errs.append("Weight must be a number")
        try:
            if "value" in r and not pd.isna(r["value"]) and str(r["value"]).strip() != "":
                r["value"] = float(r["value"])
            else:
                r["value"] = 50.0
        except Exception:
            errs.append("Value must be a number")
        if errs:
            row_errors[i] = errs
    return rows, row_errors, display_headers


def _parse_date_any(v):
    if v is None:
        return None

    # If already a pandas Timestamp or datetime, normalize to datetime (no time part if you want)
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()  # or v.to_pydatetime().date()
    if isinstance(v, datetime):
        return v

    s = str(v).strip()
    if not s:
        return None

    # Excel serial date (e.g. 45000)
    try:
        num = float(s)
        if num > 59:
            excel_epoch = datetime(1899, 12, 30)
            return excel_epoch + timedelta(days=num)
    except Exception:
        pass

    # Clean ISO-ish strings
    s_norm = s.replace("T", " ").replace("Z", "")
    s_norm = re.sub(r"\s*([+-]\d{2}:?\d{2})$", "", s_norm)

    # Try pandas
    try:
        dt = pd.to_datetime(s_norm, errors="raise")
        return dt.to_pydatetime()
    except Exception:
        pass

    # Try common formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s_norm, fmt)
        except Exception:
            continue

    # Last-ditch ISO
    try:
        return datetime.fromisoformat(s_norm)
    except Exception:
        return None


def _apply_pkg_filters(q, unassigned_id=None, date_from=None, date_to=None):
    """
    Apply package filters to a SQLAlchemy query.

    Key upgrade:
    - Accepts date_from/date_to as optional args.
    - If not provided, falls back to request.args (old behavior).
    """

    # ✅ Prefer explicitly passed dates (used by dashboard default-today logic)
    if date_from is None:
        date_from = (request.args.get('date_from') or '').strip()
    else:
        date_from = (str(date_from) or '').strip()

    if date_to is None:
        date_to = (request.args.get('date_to') or '').strip()
    else:
        date_to = (str(date_to) or '').strip()

    house      = request.args.get('house',      '', type=str)
    tracking   = request.args.get('tracking',   '', type=str)
    user_code  = request.args.get('user_code',  '', type=str)
    first_name = request.args.get('first_name', '', type=str)
    last_name  = request.args.get('last_name',  '', type=str)
    search     = (request.args.get('search') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    epc_only   = (request.args.get('epc_only') or '').lower() in ('1', 'true', 'on', 'yes')

    # ⬇️ IMPORTANT: read the checkbox value
    unassigned_only = (request.args.get('show_unassigned') or
                       request.args.get('unassigned_only') or '').lower() in ('1', 'true', 'on', 'yes')

    # ✅ Always use the same date expression everywhere
    dt_expr = func.date(func.coalesce(Package.date_received, Package.created_at))

    # 🔧 Cast string params to DATE so Postgres is happy
    if date_from:
        q = q.filter(dt_expr >= cast(date_from, Date))

    if date_to:
        q = q.filter(dt_expr <= cast(date_to, Date))

    if house:
        q = q.filter(Package.house_awb.ilike(f"%{house.strip()}%"))

    if tracking:
        q = q.filter(Package.tracking_number.ilike(f"%{tracking.strip()}%"))

    if user_code:
        q = q.filter(User.registration_number.ilike(f"%{user_code.strip()}%"))

    if first_name:
        q = q.filter(User.full_name.ilike(f"%{first_name.strip()}%"))

    if last_name:
        q = q.filter(User.full_name.ilike(f"%{last_name.strip()}%"))

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            User.full_name.ilike(like),
            User.registration_number.ilike(like),
            Package.tracking_number.ilike(like),
            Package.description.ilike(like),
            Package.house_awb.ilike(like),
        ))

    if status_filter:
        q = q.filter(Package.status == status_filter)

    if epc_only and hasattr(Package, "epc"):
        q = q.filter(func.coalesce(Package.epc, 0) == 1)

    # 🔴 UNASSIGNED filter
    if unassigned_only:
        if unassigned_id:
            q = q.filter(Package.user_id == unassigned_id)
        else:
            q = q.filter(Package.status == "Unassigned")

    return q


def _paginate(q, per_page_default=10):
    allowed = [10, 25, 50, 100, 500, 1000]

    page = request.args.get('page', default=1, type=int)

    # 1) See if a per_page was explicitly requested in URL
    per_page_arg = request.args.get('per_page', type=int)

    if per_page_arg in allowed:
        per_page = per_page_arg
        # Remember this choice in the session
        session['view_packages_per_page'] = per_page
    else:
        # 2) Fall back to last choice from session (if valid)
        per_page = session.get('view_packages_per_page', per_page_default)
        if per_page not in allowed:
            per_page = per_page_default

    items = q.limit(per_page).offset((page-1)*per_page).all()
    total = db.session.query(func.count()).select_from(q.subquery()).scalar()
    total_pages = max((total + per_page - 1) // per_page, 1)
    return page, per_page, total, total_pages, items


def _parse_dt_maybe(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None

def _normalize_weight(w):
    """
    Make sure weight is a clean float and never negative.
    Adjust the rounding/logic if you have special rules.
    """
    try:
        val = float(w or 0)
    except Exception:
        return 0.0

    if val < 0:
        val = 0.0

    # If you want, you can round or enforce a minimum:
    # return max(0.0, round(val, 2))
    return val


def move_package_to_shipment(package: Package, shipment: ShipmentLog | None):
    """
    Ensure a package belongs to at most ONE shipment.
    - If shipment is None: remove from any shipment (unassign)
    - If shipment is set: remove from all others, then add to this shipment
    """
    # Clear all existing shipment associations
    package.shipments.clear()

    # If we’re assigning to a specific shipment, add it back
    if shipment is not None:
        package.shipments.append(shipment)

    # Don’t commit here; calling code will handle db.session.commit()

def _effective_value(p: Package) -> float:
    """
    Single source of truth for package value.
    declared_value ALWAYS wins.
    """
    if hasattr(p, "declared_value") and p.declared_value is not None:
        return float(p.declared_value)
    return float(p.value or 0)

def _system_sender_user():
    """
    Pick a sender for system messages.
    Prefer the currently logged-in admin if available.
    Fallback to an admin user from DB.
    """
    try:
        if current_user and getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:
        pass

    admin = User.query.filter_by(role="admin").order_by(User.id.asc()).first()
    if admin:
        return admin

    return User.query.order_by(User.id.asc()).first()


def _log_in_app_message(recipient_id: int, subject: str, body: str):
    sender = _system_sender_user()
    if not sender:
        return

    m = DBMessage(
        sender_id=sender.id,
        recipient_id=recipient_id,
        subject=(subject or "").strip()[:255],
        body=(body or "").strip(),
        created_at=datetime.utcnow(),
        is_read=False,
    )
    db.session.add(m)
    # do NOT commit here; caller commits


# --------------------------------------------------------------------------------------
# Prealerts
# --------------------------------------------------------------------------------------
@logistics_bp.route("/prealerts")
@logistics_bp.route("/prealerts/<int:user_id>")
@admin_required
def prealerts(user_id=None):
    customer_name = None
    registration_number = None

    # VIEW FOR A SINGLE CUSTOMER
    if user_id:
        customer = db.session.get(User, user_id)
        if not customer:
            flash("Customer not found.", "danger")
            return redirect(url_for("logistics.logistics_dashboard"))

        rows = (
            Prealert.query
            .filter_by(customer_id=user_id)
            .order_by(Prealert.id.desc())
            .all()
        )

        customer_name = customer.full_name
        registration_number = customer.registration_number

        prealerts_data = []
        for p in rows:
            code_number = p.prealert_number or p.id  # fallback for old rows
            prealerts_data.append({
                "code": f"PA-{code_number:05d}",
                "customer_name": customer.full_name,
                "registration_number": customer.registration_number,
                "vendor_name": p.vendor_name,
                "courier_name": p.courier_name,
                "tracking_number": p.tracking_number,
                "purchase_date": p.purchase_date,
                "package_contents": p.package_contents,
                "item_value_usd": p.item_value_usd,
                "invoice_filename": p.invoice_filename,
                "created_at": p.created_at,
            })

    # VIEW FOR ALL CUSTOMERS
    else:
        rows = (
            db.session.query(
                Prealert,
                User.full_name,
                User.registration_number
            )
            .join(User, Prealert.customer_id == User.id)
            .order_by(Prealert.id.desc())
            .all()
        )

        prealerts_data = []
        for prealert, full_name, reg_no in rows:
            code_number = prealert.prealert_number or prealert.id
            prealerts_data.append({
                "code": f"PA-{code_number:05d}",
                "customer_name": full_name,
                "registration_number": reg_no,
                "vendor_name": prealert.vendor_name,
                "courier_name": prealert.courier_name,
                "tracking_number": prealert.tracking_number,
                "purchase_date": prealert.purchase_date,
                "package_contents": prealert.package_contents,
                "item_value_usd": prealert.item_value_usd,
                "invoice_filename": prealert.invoice_filename,
                "created_at": prealert.created_at,
            })
    print("DEBUG prealerts count:", len(prealerts_data))

    # If there are no rows at all, prealerts_data will just be []
    return render_template(
        "admin/logistics/prealerts.html",
        prealerts=prealerts_data,
        customer_name=customer_name,
        registration_number=registration_number,
    )
# --------------------------------------------------------------------------------------
# Dashboard (Tabs: prealert, view_packages, shipmentLog, uploadPackages)
# --------------------------------------------------------------------------------------
@logistics_bp.route('/dashboard', methods=["GET", "POST"], endpoint="logistics_dashboard")
@admin_required(roles=['operations'])
def logistics_dashboard():
    upload_form = UploadPackageForm()
    prealert_form = PreAlertForm()
    bulk_form = PackageBulkActionForm()
    invoice_finalize_form = InvoiceFinalizeForm()

    message = None
    errors = []

    unassigned = User.query.filter(User.registration_number == 'UNASSIGNED').first()
    unassigned_id = unassigned.id if unassigned else None

    # New preview context
    preview_headers = None
    preview_rows    = None
    preview_errors  = None
    summary_counts  = None
    preview_token   = request.args.get("preview_token") or request.form.get("preview_token")

    # Work out which tab is active (use normalizer)
    raw_tab = request.args.get("tab") or request.form.get("tab")
    tab = normalize_tab(raw_tab)   

    # If we're on the upload tab, clean up old preview files
    if tab == "uploadPackages":
        cleanup_preview_dir(24)

    # --------------------------------------------
    # Pre-Alerts tab data for Logistics Dashboard
    # --------------------------------------------
    prealerts_data = []
    if tab == "prealert":
        rows = (
            db.session.query(
                Prealert,
                User.full_name,
                User.registration_number
            )
            .join(User, Prealert.customer_id == User.id)
            .order_by(Prealert.id.desc())
            .all()
        )

        for prealert, full_name, reg_no in rows:
            code_number = prealert.prealert_number or prealert.id  # fallback
            prealerts_data.append({
                "code": f"PA-{code_number:05d}",
                "customer_name": full_name,
                "registration_number": reg_no,
                "vendor_name": prealert.vendor_name,
                "courier_name": prealert.courier_name,
                "tracking_number": prealert.tracking_number,
                "purchase_date": prealert.purchase_date,
                "package_contents": prealert.package_contents,
                "item_value_usd": prealert.item_value_usd,
                "invoice_filename": prealert.invoice_filename,
                "created_at": prealert.created_at,
            })

        print("DEBUG dashboard prealerts count:", len(prealerts_data))
# ----------------------------------------------------------------------------------
    # Upload Tab — Stage: preview
    # ----------------------------------------------------------------------------------
    if request.method == "POST" and tab == "uploadPackages" and request.form.get("stage") == "preview":
        f = request.files.get("file")
        if not f or f.filename.strip() == "":
            flash("Please choose a file.", "danger")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)
        try:
            df = _read_any_table(f)
            cleanup_preview_dir(24)
            rows, row_errors, original_headers = _validate_rows(df)
            original_rows = df.to_dict(orient="records")
            preview_token = _save_preview_blob({
                "rows": rows,
                "row_errors": row_errors,
                "display_headers": original_headers,
                "original_rows": original_rows,
                "created_at": datetime.utcnow().isoformat(),
            })
            valid_count   = len(rows) - len(row_errors)
            invalid_count = len(row_errors)
            flash(f"Preview ready: {valid_count} valid / {invalid_count} invalid out of {len(rows)}.", "info")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages", preview_token=preview_token), code=303)
        except Exception as e:
            flash(f"Failed to read file: {e}", "danger")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

    # ----------------------------------------------------------------------------------
    # Upload Tab — Stage: confirm (ORM inserts)
    # ----------------------------------------------------------------------------------
    if request.method == "POST" and tab == "uploadPackages" and request.form.get("stage") == "confirm":
        preview_token = request.form.get("preview_token")
        data = _load_preview_blob(preview_token)
        if not data:
            flash("Preview session expired. Please upload again.", "warning")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages"), code=303)

        rows       = data.get("rows", [])
        row_errors = data.get("row_errors", {})
        try:
            selected_indices = json.loads(request.form.get("selected_indices", "[]"))
            selected_indices = [int(x) for x in selected_indices]
        except Exception:
            selected_indices = []

        flash(f"DEBUG selected indices: {selected_indices}", "info")


        try:
            batch_size = int(request.form.get("batch_size") or 0)
        except Exception:
            batch_size = 0
        if batch_size and batch_size > 0:
            selected_indices = selected_indices[:batch_size]

        created, skipped = 0, 0
        hard_errors: list[str] = []        

        for i in selected_indices:
            try:
                if i < 0 or i >= len(rows):
                    skipped += 1
                    continue
                errs = (row_errors or {}).get(str(i)) or (row_errors or {}).get(i) or []
                filtered_errs = [e for e in errs if "registration" not in str(e).lower()]
                if filtered_errs:
                    skipped += 1
                    continue
                r = rows[i]

                # Resolve user by code, else email, else UNASSIGNED
                reg = (str(r.get("registration_number") or "").strip())
                user = None
                if reg:
                    user = User.query.filter(User.registration_number == reg).first()
                if not user:
                    email = (str(r.get("email") or "").strip())
                    if email:
                        user = User.query.filter(func.lower(User.email) == func.lower(email)).first()
                assigned_unassigned = False
                if not user and unassigned_id is not None:
                    user = db.session.get(User, unassigned_id)
                    assigned_unassigned = True
                if not user:
                    skipped += 1
                    hard_errors.append(f"Row {i+1}: No matching user and UNASSIGNED user missing.")
                    continue

                shipper         = (str(r.get("shipper", "")).strip() or None)
                house_awb       = (str(r.get("house_awb", "")).strip() or None)
                tracking_number = (str(r.get("tracking_number", "")).strip() or None)
                description     = (str(r.get("description", "")).strip() or None)
                value           = float(r.get("value") or 0)
                weight_actual   = float(r.get("weight") or 0)
                date_raw        = r.get("date_received") or r.get("date")
                date_received   = _parse_date_any(date_raw)
                status_value    = 'Unassigned' if assigned_unassigned else 'Overseas'

                p = Package(
                    user_id=user.id,
                    shipper=shipper if hasattr(Package, 'shipper') else None,
                    merchant=shipper if hasattr(Package, 'merchant') else None,
                    house_awb=house_awb,
                    weight=weight_actual,
                    tracking_number=tracking_number,
                    date_received=date_received,
                    received_date=date_received,
                    description=description,
                    value=value,
                    amount_due=0,
                    status=status_value,
                    created_at=datetime.utcnow(),
                )
                db.session.add(p)
                created += 1

                if (p.status or "").lower() == "overseas":
                    process_first_shipment_bonus(user.id)

            except Exception as e:
                skipped += 1
                hard_errors.append(f"Row {i+1}: {e}")

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f"Database error: {e}", "danger")
            return redirect(url_for("logistics.logistics_dashboard", tab="uploadPackages", preview_token=preview_token), code=303)

        if created:
            flash(f"Imported {created} package(s).", "success")
        if skipped:
            flash(f"Skipped {skipped} row(s).", "warning")
        for err in hard_errors[:5]:
            flash(err, "danger")
        if len(hard_errors) > 5:
            flash(f"...and {len(hard_errors)-5} more errors.", "danger")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"), code=303)

    # ----------------------------------------------------------------------------------
    # Upload Tab — Show preview from token
    # ----------------------------------------------------------------------------------
    if request.method == "GET" and tab == "uploadPackages" and preview_token:
        data = _load_preview_blob(preview_token)
        if data:
            preview_headers = data.get("display_headers", [])
            preview_rows    = data.get("original_rows", [])
            preview_errors  = data.get("row_errors", {})
            total   = len(data.get("rows", []))
            invalid = len(preview_errors or {})
            valid   = total - invalid
            summary_counts = {"total": total, "valid": valid, "invalid": invalid}
        else:
            flash("Preview session expired. Please upload again.", "warning")

    # ----------------------------------------------------------------------------------
    # Shipments (GET context)
    # ----------------------------------------------------------------------------------
    shipments = ShipmentLog.query.order_by(ShipmentLog.created_at.desc()).all()
    shipments_parsed = []
    for s in shipments:
        shipments_parsed.append({
            "id": s.id,
            "sl_id": s.sl_id,
            "created_at": s.created_at,
        })

    selected_shipment_id = request.args.get('shipment_id', type=int)
    if selected_shipment_id:
        selected_shipment = db.session.get(ShipmentLog, selected_shipment_id)
    else:
        selected_shipment = shipments[0] if shipments else None
        selected_shipment_id = selected_shipment.id if selected_shipment else None

    shipment_pkg_rows = []
    if selected_shipment:
        # eager load user to avoid N+1 in template
        rows = (db.session.query(Package, User.full_name, User.registration_number)
                .join(User, Package.user_id == User.id)
                .join(shipment_packages, shipment_packages.c.package_id == Package.id)
                .filter(shipment_packages.c.shipment_id == selected_shipment.id)
                .all())
        for p, full_name, reg in rows:
            shipment_pkg_rows.append({
                "id": p.id,
                "user_id": p.user_id,
                "full_name": full_name,
                "registration_number": reg,
                "tracking_number": p.tracking_number,
                "description": p.description,
                "weight": p.weight,
                "status": p.status,
                "created_at": p.created_at,
                "date_received": p.date_received,
                "house_awb": p.house_awb,
                "value": p.value,
                "declared_value": getattr(p, "declared_value", None),
                "amount_due": p.amount_due,
                "other_charges": getattr(p, "other_charges", 0),
                "epc": getattr(p, "epc", 0),
                "shipper": getattr(p, "merchant", None) or getattr(p, "shipper", None),
                "invoice_id": p.invoice_id,
            })

    # ----------------------------------------------------------------------------------
    # View Packages table (filters + pagination) ORM version
    # ----------------------------------------------------------------------------------
    # Default to today's date range when viewing packages and no filter provided
    # View Packages table (filters + pagination) ORM version
    raw_date_from = (request.args.get('date_from') or '').strip()
    raw_date_to   = (request.args.get('date_to')   or '').strip()

    # TRUE only when the user actually applied a date filter in the URL
    user_date_filter = bool(raw_date_from or raw_date_to)

    date_from = raw_date_from
    date_to   = raw_date_to

    # Default to today's date range when viewing packages and no filter provided
    if tab == 'view_packages' and not date_from and not date_to:
        today = datetime.now().strftime('%Y-%m-%d')
        date_from = today
        date_to   = today


    att_counts_sq = (
        db.session.query(
            PackageAttachment.package_id.label("pkg_id"),
            func.count(PackageAttachment.id).label("att_count")
        )
        .group_by(PackageAttachment.package_id)
        .subquery()
    )

    pkg_q = (
        db.session.query(
            Package,
            User.full_name,
            User.registration_number,
            func.coalesce(att_counts_sq.c.att_count, 0).label("att_count")
        )
        .join(User, Package.user_id == User.id)
        .outerjoin(att_counts_sq, att_counts_sq.c.pkg_id == Package.id)
    )

    pkg_q = _apply_pkg_filters(pkg_q, unassigned_id=unassigned_id)

    pkg_q = pkg_q.order_by(func.date(func.coalesce(Package.date_received, Package.created_at)).desc())

    page, per_page, total_count, total_pages, pkg_rows = _paginate(pkg_q)

    # Collect package ids on this page
    page_pkg_ids = [p.id for (p, full_name, reg, att_count) in pkg_rows]

    # Load attachments in ONE query (avoid N+1)
    attachments_by_pkg = {}
    if page_pkg_ids:
        att_rows = (
            db.session.query(
                PackageAttachment.id,
                PackageAttachment.package_id,
                PackageAttachment.original_name,
                PackageAttachment.file_name,
            )
            .filter(PackageAttachment.package_id.in_(page_pkg_ids))
            .order_by(PackageAttachment.id.desc())
            .all()
        )

        for att_id, pkg_id, original_name, file_name in att_rows:
            attachments_by_pkg.setdefault(pkg_id, []).append({
                "id": att_id,
                "original_name": original_name,
                "file_name": file_name,
            })

    parsed_packages = []
    for p, full_name, reg, att_count in pkg_rows:

        parsed_packages.append({
            "id": p.id,
            "user_id": p.user_id,
            "full_name": full_name,
            "registration_number": reg,
            "tracking_number": p.tracking_number,
            "description": p.description,
            "weight": p.weight,
            "status": p.status,
            "created_at": _parse_dt_maybe(p.created_at),
            "date_received": _parse_dt_maybe(p.date_received),
            "house_awb": p.house_awb,
            "declared_value": getattr(p, "declared_value", None),
            "value": p.value,
            "att_count": int(att_count or 0),
            "attachments": attachments_by_pkg.get(p.id, []),
            "amount_due": p.amount_due,
            "epc": getattr(p, "epc", 0),
            "shipper": getattr(p, "merchant", None) or getattr(p, "shipper", None),
            "invoice_id": p.invoice_id,
        })

    # Agg totals for the current filter
    totals_q = _apply_pkg_filters(
        db.session.query(
            func.count(Package.id),
            func.coalesce(func.sum(Package.weight), 0.0)
        ).join(User, Package.user_id == User.id),
        unassigned_id=unassigned_id,
    )
    cnt, tw = totals_q.first()
    filtered_total_packages = int(cnt or 0)
    filtered_total_weight   = float(tw or 0.0)

    # Daily breakdown when date filters present
    daily_totals = []
    if user_date_filter:
        dtcol = func.date(func.coalesce(Package.date_received, Package.created_at)).label("day")

        dq = _apply_pkg_filters(
            db.session.query(
                dtcol,
                func.count(Package.id),
                func.coalesce(func.sum(Package.weight), 0.0)
            ).join(User, Package.user_id == User.id),
            unassigned_id=unassigned_id,
            date_from=date_from,
            date_to=date_to,
        ).group_by(dtcol).order_by(dtcol.asc())

        for day, cnt, tw in dq.all():
            daily_totals.append({"day": str(day), "count": int(cnt or 0), "total_weight": float(tw or 0.0)})

    # showing range
    offset = (page - 1) * per_page
    showing_from = 0 if total_count == 0 else (offset + 1)
    showing_to   = min(offset + len(parsed_packages), total_count)

    

    categories = list(CATEGORIES.keys())

    # Extract read-back of some filters for template
    epc_only = (request.args.get('epc_only') or '').lower() in ('1','true','on','yes')
    search = (request.args.get('search') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    house = request.args.get('house', '', type=str)
    tracking = request.args.get('tracking', '', type=str)
    user_code = request.args.get('user_code', '', type=str)
    first_name = request.args.get('first_name', '', type=str)
    last_name = request.args.get('last_name', '', type=str)
    unassigned_only = (request.args.get('unassigned_only') or '').lower() in ('1','true','on','yes')

    allowed_page_sizes = [10, 25, 50, 100, 500, 1000]
    prev_page   = page - 1 if page > 1 else None
    next_page   = page + 1 if page < total_pages else None
    first_page  = 1 if page != 1 else None
    last_page   = total_pages if page != total_pages else None

    return render_template(
        "admin/logistics/logistics_dashboard.html",
        upload_form=upload_form,
        prealert_form=prealert_form,
        bulk_form=bulk_form,
        invoice_finalize_form=invoice_finalize_form,

        message=message,
        errors=errors,

        preview_headers=preview_headers,
        preview_token=preview_token,
        preview_rows=preview_rows,
        preview_errors=preview_errors,
        summary_counts=summary_counts,

        shipments=shipments_parsed,
        selected_shipment={
            "id": selected_shipment.id,
            "sl_id": selected_shipment.sl_id,
            "created_at": selected_shipment.created_at,
        } if selected_shipment else None,
        selected_shipment_id=selected_shipment_id,
        shipment_packages=shipment_pkg_rows,
        all_packages=parsed_packages,

        search=search,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        user_date_filter=user_date_filter,
        house=house,
        tracking=tracking,
        user_code=user_code,
        first_name=first_name,
        last_name=last_name,
        unassigned_only=unassigned_only,
        unassigned_id=unassigned_id,
        epc_only=epc_only,

        page=page,
        per_page=per_page,
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
        active_tab=tab,                
        categories=categories,
        CATEGORIES=CATEGORIES,
        USD_TO_JMD=USD_TO_JMD,
        prealerts=prealerts_data,
    )



# --------------------------------------------------------------------------------------
# Bulk Assign to user (code/email) + optional reset Unassigned -> Overseas
# --------------------------------------------------------------------------------------
@logistics_bp.route('/packages/bulk-assign', methods=['POST'])
@admin_required
def bulk_assign_packages():
    user_code  = (request.form.get('user_code') or '').strip()
    reset_flag = request.form.get('reset_status') == '1'
    pkg_ids    = [int(x) for x in request.form.getlist('package_ids') if str(x).isdigit()]

    if not pkg_ids:
        flash("No packages selected.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))
    if not user_code:
        flash("Please enter a customer code or email.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    user = (User.query
            .filter(or_(User.registration_number == user_code,
                        func.lower(User.email) == func.lower(user_code)))
            .first())
    if not user:
        flash(f"No user found for “{user_code}”.", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    pkgs = Package.query.filter(Package.id.in_(pkg_ids)).all()
    updated = 0
    for p in pkgs:
        if reset_flag and (p.status or "").strip().lower() == "unassigned":
            p.status = "Overseas"
        p.user_id = user.id
        updated += 1
    db.session.commit()

    msg = f"Assigned {updated} package(s)"
    if reset_flag:
        msg += " (reset Unassigned → Overseas where applicable)"
    flash(msg + ".", "success")
    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

@logistics_bp.route("/package-attachment/<int:attachment_id>", methods=["GET"])
@admin_required
def view_package_attachment(attachment_id):
    att = PackageAttachment.query.get_or_404(attachment_id)
    if not att.package_id:
        abort(404)

    upload_folder = current_app.config["PACKAGE_ATTACHMENT_FOLDER"]
    file_path = os.path.join(upload_folder, att.file_name)
    if not os.path.exists(file_path):
        abort(404)

    return send_from_directory(
        upload_folder,
        att.file_name,
        as_attachment=False,
        download_name=(att.original_name or att.file_name),
    )


@logistics_bp.route("/package-attachment/<int:attachment_id>/delete", methods=["POST"])
@admin_required
def delete_package_attachment_admin(attachment_id):
    att = PackageAttachment.query.get_or_404(attachment_id)

    upload_folder = current_app.config["PACKAGE_ATTACHMENT_FOLDER"]
    file_path = os.path.join(upload_folder, att.file_name)

    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        current_app.logger.exception("Failed deleting attachment file")

    db.session.delete(att)
    db.session.commit()
    flash("Attachment deleted.", "success")
    return redirect(request.referrer or url_for("logistics.logistics_dashboard", tab="view_packages"))


# --------------------------------------------------------------------------------------
# Preview invalid rows CSV download
# --------------------------------------------------------------------------------------
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

    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(display_headers + ["_Errors"])
    for i in invalid_idxs:
        row = original_rows[i] if i < len(original_rows) else {}
        errors = row_errors.get(str(i)) or row_errors.get(i) or []
        writer.writerow([row.get(h, "") for h in display_headers] + ["; ".join(errors)])

    output = sio.getvalue().encode("utf-8")
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=invalid_rows.csv"})


# --------------------------------------------------------------------------------------
# Export packages (CSV / XLSX) — ORM
# --------------------------------------------------------------------------------------
@logistics_bp.route('/download-packages', methods=['GET'])
@admin_required
def download_packages():
    fmt       = (request.args.get('format') or '').lower()
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    house     = request.args.get('house', '', type=str)
    tracking  = request.args.get('tracking', '', type=str)
    user_code = request.args.get('user_code', '', type=str)
    first     = request.args.get('first_name', '', type=str)
    last      = request.args.get('last_name', '', type=str)
    search    = request.args.get('search', '', type=str)
    status    = request.args.get('status', '', type=str)

    # --- helper: turn "YYYY-MM-DD" into a real date() object ---
    def parse_date(s: str):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    start_date = parse_date(date_from)
    end_date   = parse_date(date_to)

    q = (
        db.session.query(
            Package.id.label("pkg_id"),
            Package.tracking_number,
            Package.house_awb,
            (
                getattr(Package, 'merchant', Package.shipper).label("shipper")
                if hasattr(Package, 'merchant')
                else Package.shipper.label("shipper")
            ),
            Package.weight,
            func.coalesce(Package.date_received, Package.created_at).label("date_any"),
            Package.description,
            User.full_name,
            User.registration_number.label("reg_no"),
            User.trn,
        )
        .join(User, Package.user_id == User.id)
    )

    # ✅ use real date objects in filters so Postgres sees DATE >= DATE
    if start_date:
        q = q.filter(
            func.date(func.coalesce(Package.date_received, Package.created_at)) >= start_date
        )
    if end_date:
        q = q.filter(
            func.date(func.coalesce(Package.date_received, Package.created_at)) <= end_date
        )

    if house:
        q = q.filter(Package.house_awb.ilike(f"%{house.strip()}%"))
    if tracking:
        q = q.filter(Package.tracking_number.ilike(f"%{tracking.strip()}%"))
    if user_code:
        q = q.filter(User.registration_number.ilike(f"%{user_code.strip()}%"))
    if first:
        q = q.filter(User.full_name.ilike(f"%{first.strip()}%"))
    if last:
        q = q.filter(User.full_name.ilike(f"%{last.strip()}%"))
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                User.full_name.ilike(like),
                Package.tracking_number.ilike(like),
                Package.house_awb.ilike(like),
            )
        )
    if status:
        q = q.filter(Package.status == status)

    rows = [
        {
            "pkg_id": r.pkg_id,
            "tracking_number": r.tracking_number,
            "house_awb": r.house_awb,
            "shipper": r.shipper,
            "weight": r.weight,
            "date_any": r.date_any,
            "description": r.description,
            "full_name": r.full_name,
            "reg_no": r.reg_no,
            "trn": r.trn,
        }
        for r in q.order_by(
            func.date(func.coalesce(Package.date_received, Package.created_at)).desc()
        ).all()
    ]

    if fmt == "csv":
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
        w = csv.writer(sio)
        w.writerow(HEADERS)
        for r in rows:
            first_name, last_name = split_name(r["full_name"])
            guid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"pkg|{r['pkg_id']}|{r.get('tracking_number') or ''}|{r.get('house_awb') or ''}"
            ).hex
            d = r.get("date_any")
            if isinstance(d, datetime):
                date_str = d.date().isoformat()
            else:
                date_str = str(d)[:10] if d else ""
            w.writerow([
                guid,
                r.get("reg_no") or "",
                first_name,
                last_name,
                r.get("shipper") or "",
                r.get("house_awb") or "",
                "",
                "",
                "",
                r.get("weight") or "",
                r.get("tracking_number") or "",
                date_str,
                "",
                r.get("description") or "",
                "",
                "",
                r.get("trn") or ""
            ])
        out = sio.getvalue().encode("utf-8")
        return Response(
            out,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=packages_export.csv"},
        )

    # Excel (default)
    buf = io.BytesIO()
    df = pd.DataFrame([
        {
            "User": r.get("full_name"),
            "User Code": r.get("reg_no"),
            "Tracking Number": r.get("tracking_number"),
            "House AWB": r.get("house_awb"),
            "Description": r.get("description"),
            "Weight (lbs)": r.get("weight"),
            "TRN": r.get("trn"),
            "Created/Date": r.get("date_any"),
        }
        for r in rows
    ]) if rows else pd.DataFrame(
        columns=[
            "User","User Code","Tracking Number","House AWB",
            "Description","Weight (lbs)","TRN","Created/Date"
        ]
    )

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Packages")
        ws = writer.sheets["Packages"]
        for col in ws.columns:
            header = col[0].value or ""
            width = min(
                max(len(str(header)), *(len(str(c.value)) for c in col[1:] if c.value is not None)) + 2,
                40
            )
            ws.column_dimensions[col[0].column_letter].width = width

    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="packages.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# --------------------------------------------------------------------------------------
# Email selected packages (group by user)
# --------------------------------------------------------------------------------------
@logistics_bp.route('/logistics/email-selected-packages', methods=['POST'], endpoint='email_selected_packages')
@admin_required
def email_selected_packages():
    # Get the raw ids from the form (strings)
    package_ids = request.form.getlist('package_ids')

    if not package_ids:
        flash("Please select at least one package to email.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    # ✅ Convert to integers, ignore anything that can't be converted
    try:
        package_ids_int = [int(pid) for pid in package_ids if str(pid).strip()]
    except ValueError:
        flash("Invalid package IDs received.", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    if not package_ids_int:
        flash("No valid packages selected.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    # Use the integer list here
    rows = (
        db.session.query(Package, User)
        .join(User, Package.user_id == User.id)
        .filter(Package.id.in_(package_ids_int))
        .all()
    )

    if not rows:
        flash("No matching packages found.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    grouped: dict[int, dict] = {}
    for pkg, user in rows:
        if user.id not in grouped:
            grouped[user.id] = {"user": user, "packages": []}
        grouped[user.id]["packages"].append(pkg)

    sent_count = 0
    failed: list[str] = []
    for bundle in grouped.values():
        user = bundle["user"]
        pkgs = bundle["packages"]
        ok = email_utils.send_overseas_received_email(
            to_email=user.email,
            full_name=(user.full_name or ""),
            reg_number=(user.registration_number or ""),
            packages=pkgs,
        )
        if ok:
            # Log message to in-app messages
            pkg_lines = []
            for p in pkgs:
                pkg_lines.append(f"- {p.tracking_number or ''} | {p.house_awb or ''} | {p.description or ''} | {p.weight or 0} lb")

            subject = "Packages received overseas"
            body = (
                f"Hi {user.full_name or ''},\n\n"
                "Your package(s) have been received overseas and are now being prepared for shipment to Jamaica:\n\n"
                + "\n".join(pkg_lines) +
                "\n\nLog in to your account to track updates.\n"
                "— FAFL Courier"
            )
            _log_in_app_message(user.id, subject, body)

            sent_count += 1
        else:
            failed.append(user.email or "(no email)")


    if sent_count:
        db.session.commit()
        flash(f"Emailed {sent_count} customer(s) about selected package(s).", "success")
    if failed:
        flash("Some emails failed: " + ", ".join(failed), "danger")

    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

# --------------------------------------------------------------------------------------
# Bulk actions on packages (ORM only)
# --------------------------------------------------------------------------------------
@logistics_bp.route("/packages/bulk-action", methods=["POST"])
@admin_required
def packages_bulk_action():
    form = PackageBulkActionForm()
    if not form.validate_on_submit():
        flash("Invalid request (CSRF or form).", "danger")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    action = (request.form.get("action") or request.form.get("bulk_action") or "").strip()

    pkg_ids = [
        int(x)
        for x in (request.form.getlist("package_ids") or request.form.getlist("selected_ids"))
        if str(x).isdigit()
    ]

    if not action:
        flash("No action selected.", "warning")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    if not pkg_ids:
        flash("Please select at least one package.", "warning")
        return redirect(url_for("logistics.logistics_dashboard", tab="view_packages"))

    pkgs = Package.query.filter(Package.id.in_(pkg_ids)).all()

    try:
        if action == "delete":
            affected_invoice_ids = {p.invoice_id for p in pkgs if p.invoice_id}
            for p in pkgs:
                db.session.delete(p)
            db.session.flush()

            if affected_invoice_ids:
                still_used = (
                    db.session.query(Package.invoice_id)
                    .filter(Package.invoice_id.in_(affected_invoice_ids))
                    .group_by(Package.invoice_id)
                    .all()
                )
                still_used_ids = {row[0] for row in still_used}
                to_delete = [iid for iid in affected_invoice_ids if iid not in still_used_ids]
                if to_delete:
                    Invoice.query.filter(Invoice.id.in_(to_delete)).delete(synchronize_session=False)

            db.session.commit()
            flash(f"Deleted {len(pkgs)} package(s).", "success")

        elif action == "mark_received":
            now = datetime.utcnow()
            for p in pkgs:
                p.status = "Received"
                if not getattr(p, "received_date", None):
                    p.received_date = now
                if not getattr(p, "date_received", None):
                    p.date_received = p.received_date
            db.session.commit()
            flash(f"Marked {len(pkgs)} package(s) as Received.", "success")

        elif action == "export_pdf":
            from reportlab.pdfgen import canvas as rlcanvas
            buf = io.BytesIO()
            c = rlcanvas.Canvas(buf, pagesize=letter)
            y = 750
            for row in pkgs:
                c.drawString(
                    50, y,
                    f"Tracking: {row.tracking_number}, Desc: {row.description}, "
                    f"Weight: {row.weight} lbs, Status: {row.status}"
                )
                y -= 20
                if y < 60:
                    c.showPage()
                    y = 750
            c.save()
            buf.seek(0)
            return send_file(buf, as_attachment=True,
                             download_name="packages.pdf",
                             mimetype="application/pdf")

        elif action == "create_shipment":
            return redirect(url_for("logistics.create_shipment"))

        elif action == "assign":
            return redirect(url_for("logistics.assign_packages"))

        elif action in ("invoice_request", "send_invoice_request"):
            from app.utils import email_utils

            grouped = {}
            for p in pkgs:
                if not p.user:
                    continue

                if p.user_id not in grouped:
                    grouped[p.user_id] = {"user": p.user, "packages": []}

                grouped[p.user_id]["packages"].append({
                    "shipper": getattr(p, "shipper", None) or getattr(p, "vendor", None) or "-",
                    "house_awb": p.house_awb or "-",
                    "tracking_number": p.tracking_number or "-",
                    "weight": p.weight or 0,
                    "status": p.status or "At Overseas Warehouse",
                })

            sent = 0
            failed = []

            for data in grouped.values():
                user = data["user"]
                if not user.email:
                    failed.append("(no email)")
                    continue

                ok = email_utils.send_invoice_request_email(
                    to_email=user.email,
                    full_name=user.full_name or "Customer",
                    packages=data["packages"],
                    recipient_user_id=user.id,   # optional (logs to Messages if your send_email supports it)
                )

                if ok:
                    sent += 1
                else:
                    failed.append(user.email)

            if sent:
                flash(f"Sent invoice request email to {sent} customer(s).", "success")
            if failed:
                flash("Some invoice request emails failed: " + ", ".join(failed), "danger")           


        elif action == "mark_epc":
            for p in pkgs:
                if hasattr(Package, "epc"):
                    p.epc = 1
            db.session.commit()
            flash(f"Marked {len(pkgs)} package(s) as EPC.", "success")

        elif action == "clear_epc":
            for p in pkgs:
                if hasattr(Package, "epc"):
                    p.epc = 0
            db.session.commit()
            flash(f"Cleared EPC on {len(pkgs)} package(s).", "success")

        else:
            flash(f"Unknown action: {action}", "danger")

    except Exception as e:
        db.session.rollback()
        flash(f"Error performing bulk action: {e}", "danger")

    # ✅ pull filters from POST (because this is a POST)
    page = request.form.get("page", 1)
    per_page = request.form.get("per_page", 10)

    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10

    return redirect(url_for(
        "logistics.logistics_dashboard",
        tab="view_packages",
        page=page,
        per_page=per_page,
        date_from=request.form.get("date_from") or None,
        date_to=request.form.get("date_to") or None,
        house=request.form.get("house") or None,
        tracking=request.form.get("tracking") or None,
        user_code=request.form.get("user_code") or None,
        first_name=request.form.get("first_name") or None,
        last_name=request.form.get("last_name") or None,
        unassigned_only=request.form.get("unassigned_only") or None,
        epc_only=request.form.get("epc_only") or None,
    ))



# --------------------------------------------------------------------------------------
# Simple view (kept for backwards-compat with existing template)
# --------------------------------------------------------------------------------------
@logistics_bp.route('/view-packages')
@admin_required
def view_packages():
    rows = (db.session.query(Package, User.full_name, User.registration_number)
            .join(User, Package.user_id == User.id)
            .order_by((Package.created_at if hasattr(Package, 'created_at') else Package.id).desc())
            .all())
    all_packages = []
    for p, full_name, reg in rows:
        all_packages.append({
            "id": p.id,
            "user_id": p.user_id,
            "full_name": full_name,
            "registration_number": reg,
            "tracking_number": p.tracking_number,
            "description": p.description,
            "weight": p.weight,
            "status": p.status,
            "created_at": p.created_at,
            "date_received": getattr(p, "date_received", None),
            "house_awb": p.house_awb,
            "value": p.value,
            "amount_due": p.amount_due,
            "epc": getattr(p, "epc", 0),
            "shipper": getattr(p, "merchant", None) or getattr(p, "shipper", None),
            "invoice_id": p.invoice_id,
        })
    return render_template("admin/logistics/view_packages.html",
                           all_packages=all_packages,
                           bulk_form=PackageBulkActionForm())

# --------------------------------------------------------------------------------------
# Shipment Utilities & Routes (ORM-only)
# --------------------------------------------------------------------------------------

def _next_sl_id():
    """
    Global sequence that never resets.
    Format: SL-YYYYMMDD-00001 (date reflects creation day, number always increases)
    """
    today = datetime.utcnow().strftime("%Y%m%d")

    # Look for the latest SL-* regardless of date
    last = (
        db.session.query(ShipmentLog.sl_id)
        .filter(ShipmentLog.sl_id.like("SL-%-%"))
        .order_by(ShipmentLog.sl_id.desc())
        .first()
    )

    last_num = 0
    if last and last[0]:
        try:
            # Extract the trailing numeric portion after the last dash
            last_num = int(last[0].rsplit("-", 1)[-1])
        except ValueError:
            last_num = 0

    return f"SL-{today}-{last_num + 1:05d}"

@logistics_bp.route('/shipmentlog/create', methods=['POST'])
@admin_required
def create_shipment():
    raw_ids = request.form.getlist('package_ids')
    pkg_ids = [int(x) for x in raw_ids if str(x).isdigit()]

    if not pkg_ids:
        flash("No packages selected.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

    try:
        # 1) Create the shipment log record
        sl = ShipmentLog(sl_id=_next_sl_id())
        db.session.add(sl)
        db.session.flush()  # so sl.id is available

        # 2) Load selected packages
        pkgs = Package.query.filter(Package.id.in_(pkg_ids)).all()
        if not pkgs:
            db.session.rollback()
            flash("No matching packages found.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

        # 3) Move each package to THIS shipment (and off any others)
        for p in pkgs:
            move_package_to_shipment(p, sl)

        # Safety: if for some reason nothing ended up on this shipment
        if not sl.packages:
            db.session.rollback()
            flash("No packages could be added to the shipment.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

        db.session.commit()
        flash(f"Shipment {sl.sl_id} created with {len(sl.packages)} package(s).", "success")
        return redirect(url_for('logistics.logistics_dashboard',
                                shipment_id=sl.id,
                                tab="shipmentLog"))
    except Exception as e:
        db.session.rollback()
        flash(f"Error creating shipment: {e}", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

@logistics_bp.route('/shipmentlog/create-empty', methods=['POST', 'GET'])
@admin_required
def create_empty_shipment():
    try:
        sl = ShipmentLog(sl_id=_next_sl_id())
        db.session.add(sl)
        db.session.commit()
        flash(f"Blank shipment {sl.sl_id} created. You can now move packages into it.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=sl.id, tab='shipmentLog'))
    except Exception as e:
        db.session.rollback()
        flash(f"Error creating blank shipment: {e}", "danger")
        return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))

@logistics_bp.route('/shipmentlog/<int:shipment_id>/delete', methods=['POST'])
@admin_required
def delete_shipment(shipment_id):
    sl = db.session.get(ShipmentLog, shipment_id)
    if not sl:
        flash("Shipment not found.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))
    sl_id = sl.sl_id
    try:
        db.session.delete(sl)
        db.session.commit()
        flash(f"Shipment {sl_id} deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting shipment: {e}", "danger")
    return redirect(url_for('logistics.logistics_dashboard', tab='shipmentLog'))

@logistics_bp.route('/shipmentlog/search', methods=['GET'])
@admin_required
def search_shipment_packages():
    name     = (request.args.get('name') or '').strip()
    tracking = (request.args.get('tracking') or '').strip()
    house    = (request.args.get('house') or '').strip()
    reg      = (request.args.get('reg') or '').strip()

    q = (db.session.query(Package.id,
                          Package.tracking_number,
                          Package.description,
                          Package.house_awb,
                          User.full_name,
                          User.registration_number,
                          ShipmentLog.sl_id)
         .join(User, Package.user_id == User.id)
         .join(shipment_packages, shipment_packages.c.package_id == Package.id, isouter=True)
         .join(ShipmentLog, ShipmentLog.id == shipment_packages.c.shipment_id, isouter=True))

    if name:
        q = q.filter(User.full_name.ilike(f"%{name}%"))
    if tracking:
        q = q.filter(Package.tracking_number.ilike(f"%{tracking}%"))
    if house:
        q = q.filter(Package.house_awb.ilike(f"%{house}%"))
    if reg:
        q = q.filter(User.registration_number.ilike(f"%{reg}%"))

    q = q.order_by(ShipmentLog.sl_id.nullslast(), User.full_name.asc()).limit(200)
    rows = [{
        "id": r[0],
        "tracking_number": r[1],
        "description": r[2],
        "house_awb": r[3],
        "full_name": r[4],
        "registration_number": r[5],
        "sl_id": r[6],
    } for r in q.all()]
    return jsonify({"rows": rows})

@logistics_bp.route('/shipmentlog/move', methods=['POST'])
@admin_required
def move_packages_between_shipments():
    from_id = request.form.get('from_shipment_id', type=int)
    to_id   = request.form.get('to_shipment_id', type=int)

    # Support both: list of inputs OR comma-separated string
    raw_ids = request.form.getlist('package_ids')
    if not raw_ids:
        csv_ids = (request.form.get('package_ids') or '')
        raw_ids = [x.strip() for x in csv_ids.split(',') if x.strip()]

    pkg_ids = [int(x) for x in raw_ids if str(x).isdigit()]

    if not to_id or not pkg_ids:
        flash("Select destination shipment and at least one package.", "warning")
        return redirect(url_for('logistics.logistics_dashboard',
                                tab='shipmentLog',
                                shipment_id=from_id or None))

    to_sl = db.session.get(ShipmentLog, to_id)
    if not to_sl:
        flash("Destination shipment not found.", "danger")
        return redirect(url_for('logistics.logistics_dashboard',
                                tab='shipmentLog',
                                shipment_id=from_id or None))

    pkgs = Package.query.filter(Package.id.in_(pkg_ids)).all()
    if not pkgs:
        flash("No matching packages found to move.", "warning")
        return redirect(url_for('logistics.logistics_dashboard',
                                tab='shipmentLog',
                                shipment_id=from_id or None))

    moved = 0
    for p in pkgs:
        # 🔁 use the helper that enforces ONE shipment per package
        move_package_to_shipment(p, to_sl)
        moved += 1

    db.session.commit()

    flash(f"Moved {moved} package(s) to shipment {to_sl.sl_id}.", "success")
    return redirect(url_for('logistics.logistics_dashboard',
                            tab='shipmentLog',
                            shipment_id=to_id))


@logistics_bp.route('/shipmentlog/<int:shipment_id>/bulk-action', methods=['POST']) 
@admin_required
def bulk_shipment_action(shipment_id):
    upload_form = UploadPackageForm()
    prealert_form = PreAlertForm()
    bulk_form = PackageBulkActionForm()
    invoice_finalize_form = InvoiceFinalizeForm()

    package_ids = [int(x) for x in request.form.getlist('package_ids') if str(x).isdigit()]
    action = (request.form.get('action') or '').strip()

    if not action or not package_ids:
        flash("⚠️ Please select both an action and at least one package.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    # 🔹 Server-side Calculate Outstanding (fallback if JS doesn’t handle it)
    if action == "calc_outstanding":
        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        updated = 0

        for p in pkgs:
            form_val = (
                request.form.get(f"value_{p.id}")
                or request.form.get(f"pricing_value_{p.id}")
                or ""
            )
            form_weight = (
                request.form.get(f"weight_{p.id}")
                or request.form.get(f"pricing_weight_{p.id}")
                or ""
            )

            # invoice / value
            try:
                invoice_val = (
                    float(form_val)
                    if form_val not in ("", None)
                    else _effective_value(p)
                )
            except ValueError:
                invoice_val = float(p.value or 0)

            # weight
            try:
                weight = float(form_weight) if form_weight not in ("", None) else float(p.weight or 0)
            except ValueError:
                weight = float(p.weight or 0)

            category = "Other"
            breakdown = calculate_charges(category, invoice_val, weight)

            # Mirror invoice value
            p.value = invoice_val
            if hasattr(p, "declared_value"):
                p.declared_value = invoice_val

            # Amount due / totals
            if hasattr(p, "amount_due"):
                p.amount_due = breakdown["grand_total"]
            if hasattr(p, "grand_total"):
                p.grand_total = breakdown["grand_total"]
            if hasattr(p, "customs_total"):
                p.customs_total = breakdown["customs_total"]

            # Customs breakdown
            if hasattr(p, "duty"):
                p.duty = breakdown["duty"]
            if hasattr(p, "scf"):
                p.scf = breakdown["scf"]
            if hasattr(p, "envl"):
                p.envl = breakdown["envl"]
            if hasattr(p, "caf"):
                p.caf = breakdown["caf"]
            if hasattr(p, "gct"):
                p.gct = breakdown["gct"]
            if hasattr(p, "stamp"):
                p.stamp = breakdown["stamp"]

            # Freight / handling
            if hasattr(p, "freight_fee"):
                p.freight_fee = breakdown["freight"]
            if hasattr(p, "storage_fee"):
                p.storage_fee = breakdown["handling"]
            if hasattr(p, "freight_total"):
                p.freight_total = breakdown["freight_total"]
            if hasattr(p, "other_charges"):
                p.other_charges = breakdown.get("other_charges", 0)

            updated += 1

        db.session.commit()
        flash(f"Calculated outstanding and updated {updated} package(s).", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    # 🧾 Generate invoice – JS should intercept and open the Invoice Preview modal
    elif action == "generate_invoice":
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    elif action == "ready":
        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        now = datetime.utcnow()

        for p in pkgs:
            p.status = "Ready for Pick Up"
            # ✅ ensure date exists for invoice display
            if not getattr(p, "received_date", None):
                p.received_date = now
            if not getattr(p, "date_received", None):
                p.date_received = p.received_date

        db.session.commit()
        flash(f"{len(package_ids)} package(s) marked Ready for Pick Up.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    elif action == "revert_overseas":
        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        now = datetime.utcnow()

        for p in pkgs:
            # ✅ revert status
            p.status = "Overseas"

            # ✅ OPTIONAL: clear the "ready" dates so it doesn't look received in JA
            # (uncomment if that’s how you want it to behave)
            if hasattr(p, "received_date"):
                p.received_date = None
            if hasattr(p, "date_received"):
                p.date_received = None   

        db.session.commit()
        flash(f"{len(package_ids)} package(s) reverted back to Overseas.", "success")
        return redirect(url_for(
            'logistics.logistics_dashboard',
            shipment_id=shipment_id,
            tab="shipmentLog"
        ))

    elif action == "received_local_port":
        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        now = datetime.utcnow()

        for p in pkgs:
            p.status = "Received at Local Port"

            # ✅ only set these if the model has them
            if hasattr(p, "received_date"):
                p.received_date = now
            if hasattr(p, "date_received"):
                p.date_received = now

        db.session.commit()
        flash(f"{len(package_ids)} package(s) marked Received at Local Port.", "success")
        return redirect(url_for(
            'logistics.logistics_dashboard',
            shipment_id=shipment_id,
            tab="shipmentLog"
        ))

    elif action == "notify_ready":
        users = (db.session.query(User)
                 .join(Package, Package.user_id == User.id)
                 .filter(Package.id.in_(package_ids))
                 .distinct(User.id).all())

        from app.utils.email_utils import send_email, compose_ready_pickup_email

        for u in users:
            pkgs = Package.query.filter(
                Package.id.in_(package_ids),
                Package.user_id == u.id
            ).all()

            rows = [{
                "shipper": getattr(p, 'merchant', None) or getattr(p, 'shipper', None),
                "house_awb": p.house_awb,
                "tracking_number": p.tracking_number,
                "weight": p.weight
            } for p in pkgs]

            subject, plain, html = compose_ready_pickup_email(u.full_name, rows)
            send_email(u.email, subject, plain, html)

            _log_in_app_message(
                u.id,
                subject or "Packages ready for pickup",
                plain or "Your packages are ready for pickup. Please log in to view details."
            )

        db.session.commit()
        flash(f"{len(package_ids)} package(s) marked Ready and notifications sent.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    # ✅ SEND INVOICE EMAILS (NEW SYSTEM)
    elif action == "send_invoices":
        # 1) Load selected packages
        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        if not pkgs:
            flash("No matching packages found for the selected items.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

        # 2) Collect unique invoice IDs
        invoice_ids = {p.invoice_id for p in pkgs if p.invoice_id}
        if not invoice_ids:
            flash("The selected packages are not attached to any invoices yet.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

        # 3) Load invoices + users
        rows = (
            db.session.query(Invoice, User)
            .join(User, Invoice.user_id == User.id)
            .filter(Invoice.id.in_(invoice_ids))
            .all()
        )
        if not rows:
            flash("No invoices found for the selected packages.", "warning")
            return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

        from app.utils import email_utils

        TRANSACTIONS_URL = "https://app.faflcourier.com/customer/transactions/all"

        sent = 0
        failed = []

        for inv, user in rows:
            if not user.email:
                failed.append("(no email on file)")
                continue

            amount_due = float(inv.amount_due or inv.grand_total or inv.amount or 0)

            # Build dict expected by send_invoice_email + template
            invoice_dict = {
                "number": inv.invoice_number or f"INV-{inv.id}",
                "date": getattr(inv, "date_issued", None) or getattr(inv, "created_at", None),
                "customer_name": user.full_name or user.email or "Customer",
                "customer_code": getattr(user, "registration_number", "") or "",
                "subtotal": float(getattr(inv, "subtotal", 0) or 0),
                "discount_total": float(getattr(inv, "discount_total", 0) or 0),
                "total_due": amount_due,
                "packages": []
            }

            inv_pkgs = Package.query.filter(Package.invoice_id == inv.id).all()
            for p in inv_pkgs:
                invoice_dict["packages"].append({
                    "house_awb": p.house_awb or "-",
                    "weight": float(p.weight or 0),
                    "value": float(getattr(p, "declared_value", None) or p.value or 0),
                    "description": p.description or "",
                    "freight": float(getattr(p, "freight_fee", None) or 0),
                    "handling": float(getattr(p, "storage_fee", None) or 0),
                    "other_charges": float(getattr(p, "other_charges", None) or 0),
                    "duty": float(getattr(p, "duty", None) or 0),
                    "scf": float(getattr(p, "scf", None) or 0),
                    "envl": float(getattr(p, "envl", None) or 0),
                    "caf": float(getattr(p, "caf", None) or 0),
                    "gct": float(getattr(p, "gct", None) or 0),
                    "discount_due": float(getattr(p, "discount_due", None) or 0),
                })

            # ✅ attach real PDF later; keep None for now so it doesn't crash
            pdf_bytes = None

            ok = email_utils.send_invoice_email(
                to_email=user.email,
                full_name=user.full_name or user.email,
                invoice=invoice_dict,
                pdf_bytes=pdf_bytes,
                recipient_user_id=user.id
            )

            if ok:
                sent += 1
                _log_in_app_message(
                    user.id,
                    f"Invoice Ready: {invoice_dict['number']}",
                    f"Hi {user.full_name or user.email},\n\n"
                    f"Your invoice {invoice_dict['number']} is ready.\n"
                    f"Total Due: JMD {amount_due:,.2f}\n\n"
                    f"View details / pay here:\n{TRANSACTIONS_URL}\n\n"
                    "— FAFL Courier"
                )
            else:
                failed.append(user.email)

        db.session.commit()

        if sent:
            flash(f"Invoice emails sent for {sent} invoice(s).", "success")
        if failed:
            flash("Some invoice emails failed: " + ", ".join(failed), "danger")

        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    elif action == "remove_from_shipment":
        db.session.execute(
            shipment_packages.delete().where(
                shipment_packages.c.shipment_id == shipment_id,
                shipment_packages.c.package_id.in_(package_ids)
            )
        )

        pkgs = Package.query.filter(Package.id.in_(package_ids)).all()
        for p in pkgs:
            p.status = "Overseas"

        db.session.commit()
        flash(f"Removed {len(package_ids)} package(s) from shipment {shipment_id}.", "success")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))

    # Fallback
    else:
        flash("⚠️ Unknown action selected.", "danger")
        return redirect(url_for('logistics.logistics_dashboard', shipment_id=shipment_id, tab="shipmentLog"))


@logistics_bp.route('/shipmentlog/<int:shipment_id>/finance-invoice/preview-json', methods=['POST'])
@admin_required
def shipment_finance_invoice_preview_json(shipment_id):
    payload = request.get_json(silent=True) or {}
    package_ids = payload.get("package_ids") or []

    if not shipment_id or shipment_id <= 0:
        return jsonify({"ok": False, "error": "Invalid shipment id."}), 400

    # ensure shipment exists
    ShipmentLog.query.get_or_404(shipment_id)

    USD_TO_JMD = float(current_app.config.get("USD_TO_JMD", 165) or 165)

    # packages in shipment via relationship join
    q = (Package.query
         .join(Package.shipments)
         .filter(ShipmentLog.id == shipment_id))

    if package_ids:
        q = q.filter(Package.id.in_(package_ids))

    pkgs = q.all()
    if not pkgs:
        return jsonify({"ok": False, "error": "No packages found for this shipment selection."}), 404

    # totals
    total_lbs = sum(float(p.weight or 0) for p in pkgs)
    total_kg = (total_lbs / 2.2) if total_lbs else 0.0

    # freight (USD/kg)
    base_rate_usd_per_kg = 3.0
    freight_usd = total_kg * base_rate_usd_per_kg

    # service bands (per package by lbs)
    bands = [
        ("0-10",       0.0,   10.0,   1.60),
        ("10.01-25",  10.01,  25.0,   2.15),
        ("25.01-50",  25.01,  50.0,   4.65),
        ("50.01-100", 50.01, 100.0,   7.00),
        ("100.01+",  100.01, 10**9,  9.00),
    ]

    band_rows = []
    service_usd_total = 0.0

    for label, lo, hi, rate_usd in bands:
        count = 0
        for p in pkgs:
            w = float(p.weight or 0)
            if w >= lo and w <= hi:
                count += 1

        line_usd = count * rate_usd
        service_usd_total += line_usd

        band_rows.append({
            "band": label,
            "count": count,
            "rate_usd": float(rate_usd),
            "line_usd": round(line_usd, 2),
        })

    # fixed extra service charge (JMD)
    extra_service_jmd = 5000.0

    subtotal_usd = freight_usd + service_usd_total
    subtotal_jmd = subtotal_usd * USD_TO_JMD
    total_jmd = subtotal_jmd + extra_service_jmd

    return jsonify({
        "ok": True,
        "shipment_id": shipment_id,
        "package_count": len(pkgs),

        "total_lbs": round(total_lbs, 2),
        "total_kg": round(total_kg, 2),

        # ✅ keys the modal JS expects
        "usd_to_jmd": USD_TO_JMD,
        "base_rate_usd_per_kg": base_rate_usd_per_kg,
        "freight_usd": round(freight_usd, 2),

        "service_bands": band_rows,
        "service_usd_total": round(service_usd_total, 2),

        "extra_service_jmd": round(extra_service_jmd, 2),

        "subtotal_usd": round(subtotal_usd, 2),
        "subtotal_jmd": round(subtotal_jmd, 2),
        "total_jmd": round(total_jmd, 2),
    })



@logistics_bp.route("/shipmentlog/<int:shipment_id>/finance-invoice/preview", methods=["GET"])
@admin_required
def shipment_finance_invoice_preview_html(shipment_id):
    # 1) Load shipment + its packages
    shipment = ShipmentLog.query.get_or_404(shipment_id)

    # NOTE: adjust this filter if your FK is named differently
    packages = (Package.query
                .filter_by(shipment_id=shipment_id)
                .all())

    # 2) Compute totals
    total_packages = len(packages)
    total_weight_lbs = sum((p.weight or 0) for p in packages)
    total_weight_kg = (total_weight_lbs / 2.2) if total_weight_lbs else 0

    # Base freight: USD $3 per kg
    base_rate_usd_per_kg = 3.0
    base_freight_usd = total_weight_kg * base_rate_usd_per_kg

    # 3) Service charge rules
    service_charge_jmd = 5000.0  # fixed

    # Count packages by weight bracket (lbs)
    def in_range(w, lo, hi=None):
        if w is None:
            return False
        if hi is None:
            return w > lo
        return (w >= lo) and (w <= hi)

    c_0_10     = sum(1 for p in packages if in_range(p.weight, 0, 10))
    c_10_25    = sum(1 for p in packages if (p.weight or 0) > 10 and (p.weight or 0) <= 25)
    c_25_50    = sum(1 for p in packages if (p.weight or 0) > 25 and (p.weight or 0) <= 50)
    c_50_100   = sum(1 for p in packages if (p.weight or 0) > 50 and (p.weight or 0) <= 100)
    c_100_plus = sum(1 for p in packages if (p.weight or 0) > 100)

    # USD charges per package count
    extra_usd = (
        (1.60 * c_0_10) +
        (2.15 * c_10_25) +
        (4.65 * c_25_50) +
        (7.00 * c_50_100) +
        (9.00 * c_100_plus)
    )

    # Convert USD extras to JMD
    usd_to_jmd = float(current_app.config.get("USD_TO_JMD", 165) or 165)
    extra_jmd = extra_usd * usd_to_jmd

    # Totals
    total_usd = base_freight_usd + extra_usd
    total_jmd = (total_usd * usd_to_jmd) + service_charge_jmd

    service_jmd_total = (service_usd_total or 0) * (usd_to_jmd or 0)

    # --- Total package weight in Jamaica ---
    # (if your weights are already in lbs, keep as-is; if they are in kg, flip the formulas)
    total_weight_lbs = sum((p.weight or 0) for p in packages)
    total_weight_kg  = total_weight_lbs * 0.45359237  # lbs -> kg

    return render_template(
        "admin/logistics/_shipment_finance_invoice_preview.html",
        shipment=shipment,
        packages=packages,
        total_packages=total_packages,
        total_weight_lbs=total_weight_lbs,
        total_weight_kg=total_weight_kg,
        base_rate_usd_per_kg=base_rate_usd_per_kg,
        base_freight_usd=base_freight_usd,
        service_charge_jmd=service_charge_jmd,
        c_0_10=c_0_10,
        c_10_25=c_10_25,
        c_25_50=c_25_50,
        c_50_100=c_50_100,
        c_100_plus=c_100_plus,
        extra_usd=extra_usd,
        extra_jmd=extra_jmd,
        total_usd=total_usd,
        total_jmd=total_jmd,
        usd_to_jmd=usd_to_jmd,

        service_usd_total=service_usd_total,
        service_jmd_total=service_jmd_total,
    )


@logistics_bp.route("/shipmentlog/calc-charges", methods=["GET"])
@admin_required
def shipment_calc_charges():
    """
    Lightweight API used by the shipment-log modal to calculate charges
    for a single package (category + invoice USD + weight).
    """
    category  = (request.args.get("category") or "").strip()
    weight    = float(request.args.get("weight") or 0)
    value_usd = float(request.args.get("value_usd") or 0)

    if not category:
        return jsonify({"ok": False, "error": "Category is required."}), 400

    try:
        # NOTE: your calculator signature everywhere else is:
        #   calculate_charges(category, invoice_usd, weight_lbs)
        breakdown = calculate_charges(category, value_usd, weight)

        # we’ll surface the core fields; everything else is still in "data"
        grand_total = float(
            breakdown.get("grand_total")
            or breakdown.get("total_jmd")
            or 0
        )

        return jsonify({
            "ok": True,
            "data": {
                **breakdown,
                "grand_total": grand_total
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ================================
#  BULK INVOICE PREVIEW (JSON)
# ================================
@logistics_bp.route('/shipmentlog/invoices/preview', methods=['POST'])
@admin_required
def bulk_invoice_preview():
    """
    Input JSON: { "package_ids": [1,2,3,...] }
    Group by user and sum Package.amount_due.
    Detained packages are skipped.
    """
    data = request.get_json(silent=True) or {}
    pkg_ids = [int(x) for x in (data.get('package_ids') or []) if str(x).isdigit()]
    if not pkg_ids:
        return jsonify({"rows": [], "detained_skipped": 0})

    rows = (
        db.session.query(
            Package.id,
            Package.user_id,
            Package.amount_due,
            Package.status,
            User.full_name,
            User.registration_number,
        )
        .join(User, Package.user_id == User.id)
        .filter(Package.id.in_(pkg_ids))
        .all()
    )

    grouped = {}
    detained = 0

    for pid, uid, amount_due, status, full_name, reg in rows:
        if (status or "").lower() == "detained":
            detained += 1
            continue

        g = grouped.setdefault(
            uid,
            {
                "user_id": uid,
                "full_name": full_name,
                "registration_number": reg,
                "package_ids": [],
                "package_count": 0,
                "total_due": 0.0,
            },
        )
        g["package_ids"].append(pid)
        g["package_count"] += 1
        g["total_due"] += float(amount_due or 0.0)

    return jsonify({"rows": list(grouped.values()), "detained_skipped": detained})



# ================================
#  HELPER: INVOICE NUMBER
# ================================
def _generate_invoice_number():
    """
    Generates a globally increasing invoice number with today's date, like:
      INV-YYYYMMDD-0001, INV-YYYYMMDD-0002, ...

    IMPORTANT:
    - Date changes every day
    - Sequence NEVER resets
      Example:
        Today last:    INV-20260109-0008
        Tomorrow next: INV-20260110-0009
    """
    today_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"INV-{today_str}-"

    # Ensure any pending invoices are written enough to be query-visible
    db.session.flush()

    # Find the last invoice number overall (not just today)
    last = (
        db.session.query(Invoice.invoice_number)
        .filter(Invoice.invoice_number.like("INV-%-%"))  # safety: matches your format
        .order_by(Invoice.invoice_number.desc())
        .first()
    )

    last_seq = 0
    if last and last[0]:
        try:
            # invoice_number looks like INV-YYYYMMDD-#### -> take ####
            last_seq = int(last[0].split("-")[-1])
        except Exception:
            last_seq = 0

    new_seq = last_seq + 1
    return f"{prefix}{new_seq:04d}"


# ================================
#  BULK FINALIZE (JSON)
# ================================
@logistics_bp.route('/shipmentlog/invoices/finalize-json', methods=['POST'])
@admin_required
def bulk_invoice_finalize_json():
    """
    Input JSON:
      {
        "selections": [
          { "user_id": 123, "package_ids": [1,2,3] },
          ...
        ]
      }

    For each selection we create **one invoice** and attach the packages.
    """
    data = request.get_json(silent=True) or {}
    selections = data.get('selections') or []
    if not selections:
        return jsonify({"created": 0, "invoices": []})

    created = 0
    out = []

    try:
        for sel in selections:
            try:
                uid = int(sel.get("user_id") or 0)
            except Exception:
                continue
            if not uid:
                continue

            pkg_ids = [
                int(x)
                for x in (sel.get("package_ids") or [])
                if str(x).isdigit()
            ]
            if not pkg_ids:
                continue

            # get packages
            pkgs = (
                Package.query
                .filter(Package.id.in_(pkg_ids))
                .all()
            )
            if not pkgs:
                continue

            # skip detained just like preview
            eligible = [p for p in pkgs if (p.status or "").lower() != "detained"]
            if not eligible:
                continue

            total_amount = float(
                sum(float(p.amount_due or 0.0) for p in eligible)
            )

            inv_number = _generate_invoice_number()
            inv = Invoice(
                user_id=uid,
                invoice_number=inv_number,
                status="pending",
                date_submitted=datetime.utcnow(),
                date_issued=datetime.utcnow(),                
                grand_total=total_amount,
                amount_due=total_amount,
                amount=total_amount,
                description=f"Invoice for {len(eligible)} package(s)",
            )
            db.session.add(inv)
            db.session.flush()  # get inv.id

            for p in eligible:
                p.invoice_id = inv.id
                # optionally:
                # p.status = "Invoiced"

            created += 1
            out.append(
                {
                    "invoice_id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "amount": total_amount,
                }
            )

        db.session.commit()
        return jsonify({"created": created, "invoices": out})

    except Exception as e:
        current_app.logger.exception("Error in bulk_invoice_finalize_json")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@logistics_bp.route('/shipmentlog/bulk-calc-outstanding', methods=['POST'])
@admin_required
def bulk_calc_outstanding():
    payload = request.get_json(silent=True) or {}
    rows = payload.get('rows') or []
    if not rows:
        return jsonify({"results": []})

    # map id -> incoming values
    wanted = {int(r["pkg_id"]): r for r in rows if "pkg_id" in r}
    pkgs = (
        db.session.query(Package.id, Package.value, Package.weight)
        .filter(Package.id.in_(list(wanted.keys())))
        .all()
    )

    results = []
    for pid, db_value, db_weight in pkgs:
        r = wanted[pid]
        invoice = r.get("invoice")
        weight  = r.get("weight")

        # ✅ No Package.category in your model → always default
        category = (r.get("category") or "Other")

        # Invoice fallback
        if invoice in (None, '', 0, '0', 0.0, '0.0'):
            invoice = float(db_value or 0) if db_value not in (None, 0) else _effective_value(db.session.get(Package, pid))

        else:
            invoice = float(invoice)

        if weight in (None, '', 0, '0', 0.0, '0.0'):
            weight = float(db_weight or 0)

        weight_eff = _normalize_weight(weight)
        breakdown = calculate_charges(category, float(invoice), float(weight_eff))

        results.append({
            "pkg_id": pid,
            "invoice": float(invoice),
            "grand_total": float(breakdown.get("grand_total", 0)),
            "breakdown": breakdown
        })

    return jsonify({"results": results})


# --------------------------------------------------------------------------------------
# Misc simple APIs
# --------------------------------------------------------------------------------------
@logistics_bp.route('/packages/<int:package_id>/delete', methods=['POST'])
@admin_required
def delete_package(package_id):
    pkg = db.session.get(Package, package_id)
    if not pkg:
        flash("Package not found.", "warning")
        return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))
    try:
        db.session.delete(pkg)
        db.session.commit()
        flash("Package deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting package: {e}", "danger")
    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages'))

@logistics_bp.route('/api/calculate-charges', methods=['POST'])
@admin_required
def api_calculate_charges():
    data = request.get_json() or {}
    invoice = float(data.get('invoice') or data.get('invoice_usd') or 50)
    category = data.get('category')
    weight = float(data.get('weight') or 0)
    result = calculate_charges(category, invoice, weight)
    return jsonify(result)

@logistics_bp.route('/api/package/<int:pkg_id>', methods=['POST'])
@admin_required
def api_update_package(pkg_id):
    data = request.get_json(force=True) or {}

    field_map = {
        'category': 'category',
        'value': 'value',
        'weight': 'weight',
        'amount_due': 'amount_due',
        'duty': 'duty', 'scf': 'scf', 'envl': 'envl', 'caf': 'caf', 'gct': 'gct', 'stamp': 'stamp',
        'customs_total': 'customs_total',
        'freight': 'freight_fee',
        'handling': 'storage_fee',
        'freight_total': 'freight_total',
        'other_charges': 'other_charges',
        'grand_total': 'grand_total',
    }

    p = db.session.get(Package, pkg_id)
    if not p:
        return jsonify({"ok": False, "error": "Package not found"}), 404

    if 'value' in data and data['value'] is not None:
        p.value = data['value']
        if hasattr(p, 'declared_value'):
            p.declared_value = data['value']

    for client_key, col in field_map.items():
        if client_key == 'value':
            continue
        if client_key in data and data[client_key] is not None:
            setattr(p, col, data[client_key])

    db.session.commit()
    return jsonify({"ok": True, "updated": list(data.keys())}), 200

# --------------------------------------------------------------------------------------
# Invoice status
# --------------------------------------------------------------------------------------
@logistics_bp.route('/invoices/<int:invoice_id>/status', methods=['POST'])
@admin_required
def set_invoice_status(invoice_id):
    new_status = (request.form.get('status') or '').lower()
    if new_status not in ('paid','unpaid','cancelled'):
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    inv = db.session.get(Invoice, invoice_id)
    if not inv:
        return jsonify({"ok": False, "error": "Invoice not found"}), 404

    if new_status == 'paid':
        inv.status = 'paid'
        inv.date_paid = datetime.utcnow()
    else:
        inv.status = new_status
        inv.date_paid = None

    db.session.commit()
    return jsonify({"ok": True})

# --------------------------------------------------------------------------------------
# Scheduled Deliveries
# --------------------------------------------------------------------------------------
@logistics_bp.route('/scheduled_deliveries', methods=['GET', 'POST'])
@admin_required(roles=['operations'])
def view_scheduled_deliveries():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    q = ScheduledDelivery.query
    if start_date:
        q = q.filter(ScheduledDelivery.scheduled_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
    if end_date:
        q = q.filter(ScheduledDelivery.scheduled_date <= datetime.strptime(end_date, '%Y-%m-%d').date())

    deliveries = q.order_by(ScheduledDelivery.scheduled_date.desc()).all()
    return render_template('admin/logistics/scheduled_deliveries.html',
                           deliveries=deliveries,
                           start_date=start_date,
                           end_date=end_date)

@logistics_bp.route("/scheduled-delivery/<int:delivery_id>", methods=["GET"])
@admin_required(roles=['operations'])
def view_scheduled_delivery(delivery_id):
    delivery = ScheduledDelivery.query.get_or_404(delivery_id)

    linked_packages = (Package.query
        .filter(Package.scheduled_delivery_id == delivery.id)
        .order_by(Package.created_at.desc())
        .all()
    )

    available_packages = (Package.query
        .filter(Package.scheduled_delivery_id.is_(None))
        .order_by(Package.created_at.desc())
        .limit(200)
        .all()
    )

    return render_template(
        "admin/logistics/scheduled_delivery_view.html",
        delivery=delivery,
        linked_packages=linked_packages,
        available_packages=available_packages
    )

@logistics_bp.route('/scheduled_deliveries/pdf')
@admin_required(roles=['operations'])
def scheduled_deliveries_pdf():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    q = ScheduledDelivery.query
    if start_date:
        q = q.filter(ScheduledDelivery.scheduled_date >= datetime.strptime(start_date, '%Y-%m-%d').date())
    if end_date:
        q = q.filter(ScheduledDelivery.scheduled_date <= datetime.strptime(end_date, '%Y-%m-%d').date())

    deliveries = q.order_by(ScheduledDelivery.scheduled_date.desc()).all()

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 50

    p.setFont("Helvetica-Bold", 14)
    p.drawString(50, y, "Scheduled Deliveries Report")
    y -= 30
    p.setFont("Helvetica", 10)

    for d in deliveries:
        line = f"{d.scheduled_date} {d.scheduled_time} | {d.location} | {d.person_receiving} | Customer: {d.user.full_name}"
        p.drawString(50, y, line[:110])
        y -= 15
        if y < 50:
            p.showPage()
            y = height - 50

    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="scheduled_deliveries.pdf", mimetype='application/pdf')

@logistics_bp.route('/scheduled_deliveries/add', methods=['GET', 'POST'])
@admin_required(roles=['operations'])
def add_scheduled_delivery():
    form = ScheduledDeliveryForm()

    # Needed for the user dropdown
    users = User.query.order_by(User.full_name.asc()).all()

    if form.validate_on_submit():
        new_delivery = ScheduledDelivery(
            user_id=form.user_id.data,
            scheduled_date=form.date.data,
            scheduled_time=form.time.data,
            location=form.location.data,
            direction=form.direction.data,
            mobile_number=form.mobile_number.data,
            person_receiving=form.person_receiving.data,
        )
        db.session.add(new_delivery)
        db.session.commit()

        flash("Scheduled delivery added successfully.", "success")
        return redirect(url_for('logistics.view_scheduled_deliveries'))

    return render_template(
        'admin/logistics/add_scheduled_delivery.html',
        form=form,
        users=users
    )

@logistics_bp.route("/scheduled_deliveries/<int:delivery_id>/set-status/<status>", methods=["POST"])
@admin_required(roles=['operations'])
def scheduled_delivery_set_status(delivery_id, status):
    allowed = {"Scheduled", "Out for Delivery", "Delivered", "Cancelled"}
    if status not in allowed:
        flash("Invalid status", "danger")
        return redirect(url_for("logistics.view_scheduled_deliveries"))

    d = ScheduledDelivery.query.get_or_404(delivery_id)
    d.status = status

    # ✅ Keep Package.status in sync with Delivery status
    linked_pkgs = (Package.query
                   .filter(Package.scheduled_delivery_id == d.id)
                   .all())

    if status == "Out for Delivery":
        # choose your preferred in-transit label
        for p in linked_pkgs:
            # Only move forward (don’t override Delivered/Detained)
            ps = (p.status or "").strip().lower()
            if ps not in ("delivered", "detained"):
                p.status = "Out for Delivery"

    elif status == "Delivered":
        # ✅ This is the key fix: store Delivered (not Delivery)
        for p in linked_pkgs:
            ps = (p.status or "").strip().lower()
            if ps != "detained":
                p.status = "Delivered"

        # optional: clear scheduled_delivery_id once completed
        # for p in linked_pkgs:
        #     p.scheduled_delivery_id = None

    elif status in ("Cancelled", "Scheduled"):
        # Optional: decide what package status should be when delivery is cancelled/reset
        # For now: do nothing to package status.
        pass

    db.session.commit()
    flash(f"Delivery #{d.id} updated to '{status}'", "success")
    return redirect(url_for("logistics.view_scheduled_deliveries"))

@logistics_bp.route("/scheduled-delivery/<int:delivery_id>/attach-package", methods=["POST"])
@admin_required(roles=['operations'])
def attach_package_to_delivery(delivery_id):
    delivery = ScheduledDelivery.query.get_or_404(delivery_id)

    package_id = request.form.get("package_id")
    if not package_id:
        flash("No package selected.", "warning")
        return redirect(url_for("logistics.view_scheduled_delivery", delivery_id=delivery_id))

    pkg = Package.query.get_or_404(int(package_id))

    # OPTIONAL: basic guardrails
    # if pkg.status not in ("Ready", "Arrived", "In Warehouse"):
    #     flash("Only ready/arrived packages can be scheduled.", "warning")
    #     return redirect(url_for("logistics.view_scheduled_delivery", delivery_id=delivery_id))

    pkg.scheduled_delivery_id = delivery.id
    db.session.commit()

    flash(f"Package {pkg.tracking_number or pkg.house_awb or pkg.id} linked to delivery.", "success")
    return redirect(url_for("logistics.view_scheduled_delivery", delivery_id=delivery_id))

@logistics_bp.route("/scheduled-delivery/<int:delivery_id>/remove-package/<int:package_id>", methods=["POST"])
@admin_required(roles=['operations'])
def remove_package_from_delivery(delivery_id, package_id):
    delivery = ScheduledDelivery.query.get_or_404(delivery_id)
    pkg = Package.query.get_or_404(package_id)

    if pkg.scheduled_delivery_id != delivery.id:
        flash("That package is not linked to this delivery.", "warning")
        return redirect(url_for("logistics.view_scheduled_delivery", delivery_id=delivery_id))

    pkg.scheduled_delivery_id = None
    db.session.commit()

    flash("Package removed from scheduled delivery.", "success")
    return redirect(url_for("logistics.view_scheduled_delivery", delivery_id=delivery_id))

@logistics_bp.route("/scheduled_deliveries/<int:delivery_id>")
@admin_required(roles=["operations"])
def scheduled_delivery_detail(delivery_id):
    delivery = ScheduledDelivery.query.get_or_404(delivery_id)

    # (Step 3 we’ll populate these properly)
    linked_packages = delivery.packages.order_by(Package.created_at.desc()).all()

    return render_template(
        "admin/logistics/scheduled_delivery_view.html",
        delivery=delivery,
        linked_packages=linked_packages
    )


@logistics_bp.route("/scheduled-deliveries/<int:delivery_id>", methods=["GET"])
@admin_required(roles=["operations"])
def scheduled_delivery_view(delivery_id):
    d = ScheduledDelivery.query.get_or_404(delivery_id)

    # Packages already linked to this delivery
    assigned_packages = (Package.query
        .filter(Package.scheduled_delivery_id == d.id)
        .order_by(Package.created_at.desc())
        .all()
    )

    # Eligible packages to assign:
    # - belong to same user
    # - NOT already assigned
    # - optional filter by status
    eligible_packages = (Package.query
        .filter(
            Package.user_id == d.user_id,
            Package.scheduled_delivery_id.is_(None),
            Package.status.in_(["Ready", "Ready for Delivery", "At Warehouse", "Delivered Pending"])
        )
        .order_by(Package.created_at.desc())
        .all()
    )

    return render_template(
        "admin/logistics/scheduled_delivery_view.html",
        delivery=d,
        assigned_packages=assigned_packages,
        eligible_packages=eligible_packages
    )



@logistics_bp.route("/scheduled_deliveries/<int:delivery_id>/assign-packages", methods=["POST"])
@admin_required(roles=["operations"])
def scheduled_delivery_assign_packages(delivery_id):
    delivery = ScheduledDelivery.query.get_or_404(delivery_id)

    package_ids = request.form.getlist("package_ids")
    if not package_ids:
        flash("Please select at least one package to assign.", "warning")
        return redirect(url_for("logistics.scheduled_delivery_view", delivery_id=delivery.id))

    # Only allow packages for the same customer + selected IDs
    packages = (Package.query
                .filter(Package.id.in_(package_ids))
                .filter(Package.user_id == delivery.user_id)
                .all())

    if not packages:
        flash("No valid packages selected for this customer.", "danger")
        return redirect(url_for("logistics.scheduled_delivery_view", delivery_id=delivery.id))

    assigned_count = 0
    skipped_count = 0

    for p in packages:
        # If already assigned to another delivery, skip it (safety)
        if p.scheduled_delivery_id and p.scheduled_delivery_id != delivery.id:
            skipped_count += 1
            continue

        p.scheduled_delivery_id = delivery.id
        assigned_count += 1

    db.session.commit()

    if skipped_count:
        flash(f"Assigned {assigned_count} package(s). Skipped {skipped_count} already linked elsewhere.", "info")
    else:
        flash(f"Assigned {assigned_count} package(s) to this delivery.", "success")

    return redirect(url_for("logistics.scheduled_delivery_view", delivery_id=delivery.id))




@logistics_bp.route("/scheduled-deliveries/<int:delivery_id>/unassign/<int:package_id>", methods=["POST"])
@admin_required(roles=["operations"])
def scheduled_delivery_unassign_package(delivery_id, package_id):
    d = ScheduledDelivery.query.get_or_404(delivery_id)

    p = Package.query.get_or_404(package_id)
    if p.scheduled_delivery_id != d.id:
        flash("That package is not linked to this delivery.", "warning")
        return redirect(url_for("logistics.scheduled_delivery_view", delivery_id=delivery_id))

    p.scheduled_delivery_id = None
    db.session.commit()
    flash("Package unassigned from delivery.", "success")
    return redirect(url_for("logistics.scheduled_delivery_view", delivery_id=delivery_id))


@logistics_bp.route('/shipmentlog/create-shipment', methods=['GET'])
@admin_required
def prepare_create_shipment():
    flash("Select packages to include in the new shipment.", "info")
    return redirect(url_for('logistics.logistics_dashboard', tab='view_packages', create_shipment=1))




