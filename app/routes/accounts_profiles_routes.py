# app/routes/accounts_profiles_routes.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, send_file, current_app, jsonify, make_response
)
import os
import uuid
import json
import pandas as pd
from datetime import datetime, timedelta, timezone, date
import bcrypt
import re
import io
import csv
import xlsxwriter
import math

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from werkzeug.utils import secure_filename
from sqlalchemy import func, or_, and_, select, case
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlparse, urljoin
from weasyprint import HTML
from flask_login import current_user, login_required

from app.forms import UploadUsersForm, ConfirmUploadForm
from app.extensions import db
from app.routes.admin_auth_routes import admin_required
from app.calculator_data import CATEGORIES
from app.utils.time import to_jamaica
from app.utils.messages import make_thread_key
from app.utils.subscription_utils import get_subscription_summary
from app.utils import next_registration_number

# Models (these exist in your file)
from app.models import (
    User, Invoice, Payment, Package, Prealert,
    Message as DBMessage,  # ✅ alias it like customer_routes.py
    Settings, PackageAttachment, ScheduledDelivery, 
    Claim, ClaimAuditLog, WalletTransaction, AuditLog
)
from app.models import generate_claim_case_id
from app.models import SubscriptionPlan, Subscription, SubscriptionUsage, SubscriptionMember

accounts_bp = Blueprint('accounts_profiles', __name__)

@accounts_bp.route("/__whoami_accounts")
def __whoami_accounts():
    return "accounts_profiles_routes.py LOADED ✅ 2025-12-29", 200


# -------------------------
# Constants / Regex / Utils
# -------------------------
UPLOAD_FOLDER = 'uploads'
REQUIRED_COLUMNS = ['Full Name', 'Email', 'TRN', 'Mobile', 'Password']
ALLOWED_EXTENSIONS = {'xlsx'}

EMAIL_REGEX = re.compile(r'^[\w\.-]+@[\w\.-]+\.\w+$')
MOBILE_REGEX = re.compile(r'^\d{10}$')  # For Jamaican numbers like 876XXXXXXX

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# -------------------------
# ✅ NEW: Redirect helper to preserve active tab + filters/pagination
# -------------------------
def _back_to_view_user_url(user_id: int, fallback_tab: str = "packages") -> str:
    """
    Build a redirect URL back to view_user that preserves:
    - tab=...
    - tab-specific query params (pagination + filters)
    Works even if route is POST because request.values checks args+form.
    """
    tab = (
        request.args.get("tab")
        or request.form.get("tab")
        or request.values.get("tab")
        or fallback_tab
    )

    kwargs = {"id": user_id, "tab": tab}

    # preserve tab-specific params
    if tab == "packages":
        for k in ("pkg_page", "pkg_per_page", "pkg_from", "pkg_to", "pkg_awb", "pkg_tn"):
            v = request.values.get(k)
            if v not in (None, "", "None"):
                kwargs[k] = v

    elif tab == "invoices":
        for k in ("inv_page", "inv_per_page", "inv_from", "inv_to"):
            v = request.values.get(k)
            if v not in (None, "", "None"):
                kwargs[k] = v

    elif tab == "payments":
        for k in ("pay_page", "pay_per_page", "pay_from", "pay_to"):
            v = request.values.get(k)
            if v not in (None, "", "None"):
                kwargs[k] = v

    elif tab == "messages":
        for k in ("msg_page", "msg_per_page", "msg_from", "msg_to", "msg_q"):
            v = request.values.get(k)
            if v not in (None, "", "None"):
                kwargs[k] = v

    return url_for("accounts_profiles.view_user", **kwargs)


# -------------------------
# Preview Blob
# -------------------------
def _user_preview_dir() -> str:
    try:
        base = current_app.instance_path
    except RuntimeError:
        base = os.path.join(os.getcwd(), "instance")
    path = os.path.join(base, "tmp_user_previews")
    os.makedirs(path, exist_ok=True)
    return path

def _save_user_preview_blob(data: dict) -> str:
    token = f"userprev-{uuid.uuid4().hex}"
    path = os.path.join(_user_preview_dir(), f"{token}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return token

def _load_user_preview_blob(token: str) -> dict | None:
    path = os.path.join(_user_preview_dir(), f"{token}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _delete_user_preview_blob(token: str) -> None:
    path = os.path.join(_user_preview_dir(), f"{token}.json")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# -------------------------
# Helpers
# -------------------------
def _current_max_fafl_number() -> int:
    """Scan existing registration_number values like 'FAFL12345' and return max suffix (default 10000)."""
    max_num = 10000
    try:
        q = db.session.query(User.registration_number).filter(User.registration_number.ilike("FAFL%"))
        for (reg,) in q:
            try:
                n = int(str(reg)[4:])
                max_num = max(max_num, n)
            except Exception:
                continue
    except Exception:
        db.session.rollback()
    return max_num

def _safe_commit():
    try:
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False

def create_audit_log(
    module,
    action,
    admin_id=None,
    user_id=None,
    entity_type=None,
    entity_id=None,
    reason=None,
    description=None,
    old_value=None,
    new_value=None,
):
    log = AuditLog(
        module=module,
        action=action,
        admin_id=admin_id,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        description=description,
        old_value=old_value,
        new_value=new_value,
    )

    db.session.add(log)


# -------------------------
# Manage Users
# -------------------------

@accounts_bp.route('/manage-users', methods=['GET', 'POST'])
@admin_required
def manage_users():
    page        = request.args.get('page', 1, type=int)
    per_page    = 20
    search      = (request.args.get('search') or '').strip()
    sort_by     = (request.args.get('sort') or 'recent').strip()
    date_from   = request.args.get('date_from')
    date_to     = request.args.get('date_to')

    unpaid_only = (request.args.get('unpaid_only') == '1')

    upload_form  = UploadUsersForm()
    confirm_form = ConfirmUploadForm()

    preview_token = session.get('preview_users_token')
    excel_preview = None

    if preview_token:
        blob = _load_user_preview_blob(preview_token)
        if blob:
            excel_preview = blob.get("preview_rows", [])
        else:
            session.pop('preview_users_token', None)

    # ---------------------------------------------------------
    # ✅ Metrics for stats cards
    # ---------------------------------------------------------
    total_users = User.query.count()

    active_users = 0
    new_this_month = 0
    users_with_unpaid_invoices = 0

    try:
        if hasattr(User, "last_login"):
            active_users = User.query.filter(User.last_login.isnot(None)).count()
        else:
            active_users = total_users
    except Exception:
        db.session.rollback()
        active_users = 0

    try:
        today = datetime.utcnow().date()
        first_day_this_month = today.replace(day=1).strftime("%Y-%m-%d")

        new_this_month = (
            User.query
            .filter(User.date_registered >= first_day_this_month)
            .count()
        )
    except Exception:
        db.session.rollback()
        new_this_month = 0

    try:
        users_with_unpaid_invoices = (
            db.session.query(Invoice.user_id)
            .filter(
                Invoice.user_id.isnot(None),
                func.lower(Invoice.status).in_(('pending', 'unpaid', 'issued', 'partial'))
            )
            .group_by(Invoice.user_id)
            .count()
        )
    except Exception:
        db.session.rollback()
        users_with_unpaid_invoices = 0

    # ---------------------------------------------------------
    # Build base query
    # ---------------------------------------------------------
    q = User.query

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            User.full_name.ilike(like),
            User.email.ilike(like),
            User.registration_number.ilike(like),
            User.address.ilike(like)
        ))

    if date_from:
        q = q.filter(User.date_registered >= date_from)

    if date_to:
        q = q.filter(User.date_registered <= date_to)

    # ---------------------------------------------------------
    # Unpaid-only filter
    # ---------------------------------------------------------
    if unpaid_only:
        unpaid_user_ids_subq = (
            db.session.query(Invoice.user_id)
            .filter(
                Invoice.user_id.isnot(None),
                func.lower(Invoice.status).in_(('pending', 'unpaid', 'issued', 'partial'))
            )
            .group_by(Invoice.user_id)
            .subquery()
        )

        q = q.filter(User.id.in_(unpaid_user_ids_subq))

    # ---------------------------------------------------------
    # Sort
    # ---------------------------------------------------------
    if sort_by == 'alphabetical_asc':
        q = q.order_by(User.registration_number.asc())
    elif sort_by == 'alphabetical_desc':
        q = q.order_by(User.registration_number.desc())
    else:
        if hasattr(User, 'date_registered'):
            q = q.order_by(User.date_registered.desc(), User.id.desc())
        elif hasattr(User, 'created_at'):
            q = q.order_by(User.created_at.desc(), User.id.desc())
        else:
            q = q.order_by(User.id.desc())

    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    user_ids = [u.id for u in pagination.items]

    # ---------------------------------------------------------
    # unpaid_count per user
    # ---------------------------------------------------------
    unpaid_map = {uid: 0 for uid in user_ids}

    if user_ids:
        try:
            sub = (
                db.session.query(Invoice.user_id, func.count(Invoice.id))
                .filter(
                    Invoice.user_id.in_(user_ids),
                    func.lower(Invoice.status).in_(('pending', 'unpaid', 'issued', 'partial'))
                )
                .group_by(Invoice.user_id)
                .all()
            )

            for uid, cnt in sub:
                unpaid_map[uid] = int(cnt or 0)

        except Exception:
            db.session.rollback()

    users = []

    for u in pagination.items:
        users.append({
            "id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "registration_number": u.registration_number,
            "date_registered": u.date_registered,
            "address": u.address,
            "mobile": u.mobile,
            "trn": u.trn,
            "unpaid_count": unpaid_map.get(u.id, 0),
        })

    total_pages = max(pagination.pages or 1, 1)

    return render_template(
        'admin/accounts_profiles/manage_users.html',
        users=users,
        page=page,
        total_pages=total_pages,
        search=search,
        sort_by=sort_by,
        date_from=date_from,
        date_to=date_to,
        unpaid_only=unpaid_only,
        form=upload_form,
        confirm_form=confirm_form,
        excel_preview=excel_preview,

        # ✅ Real database metrics for stats cards
        total_users=total_users,
        active_users=active_users,
        users_with_unpaid_invoices=users_with_unpaid_invoices,
        new_this_month=new_this_month
    )

# -------------------------
# Add User
# -------------------------
@accounts_bp.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    full_name = request.form.get('full_name', '').strip()
    email = request.form.get('email', '').strip()
    trn = request.form.get('trn', '').strip()
    mobile = request.form.get('mobile', '').strip()
    raw_password = request.form.get('password', '')
    date_registered = request.form.get('date_registered') or datetime.now().strftime('%Y-%m-%d')

    if not all([full_name, email, trn, mobile, raw_password]):
        flash("All fields are required.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    exists = User.query.filter(or_(User.email == email, User.trn == trn)).first()
    if exists:
        flash("Email or TRN already exists.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    reg_no = next_registration_number()

    # store as bytes (LargeBinary)
    hashed = bcrypt.hashpw(raw_password.encode('utf-8'), bcrypt.gensalt())

    # ✅ generate unique referral code
    referral_code = User.generate_referral_code()
    for _ in range(10):
        if not User.query.filter_by(referral_code=referral_code).first():
            break
        referral_code = User.generate_referral_code()

    u = User(
        full_name=full_name,
        email=email,
        trn=trn,
        mobile=mobile,
        password=hashed,
        registration_number=reg_no,
        date_registered=date_registered,
        referral_code=referral_code,
    )
    db.session.add(u)
    _safe_commit()

    flash(f"User {full_name} added with registration number {reg_no}.", "success")
    return redirect(url_for('accounts_profiles.manage_users'))


# -------------------------
# Upload Users (Excel) -> Preview
# -------------------------
@accounts_bp.route('/upload-users', methods=['POST'])
@admin_required
def upload_users():
    form = UploadUsersForm()
    if not form.validate_on_submit():
        flash("Please upload a valid Excel (.xlsx) file.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    file = form.file.data
    if not (file and allowed_file(file.filename)):
        flash("Please upload a valid Excel (.xlsx) file.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.xlsx"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        df = pd.read_excel(filepath)
    except Exception as e:
        flash(f"Error reading Excel file: {e}", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if 'Date Registered' not in df.columns:
        df['Date Registered'] = ""
    if missing:
        flash(f"Missing columns: {', '.join(missing)}", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    def to_iso(val):
        if pd.isna(val) or str(val).strip() == "":
            return None
        if isinstance(val, (int, float)):
            try:
                base = pd.Timestamp('1899-12-30')
                dt = base + pd.to_timedelta(int(val), unit='D')
                return dt.strftime('%Y-%m-%d')
            except Exception:
                return None
        dt = pd.to_datetime(str(val), errors='coerce')
        return dt.strftime('%Y-%m-%d') if not pd.isna(dt) else None

    def pretty(val):
        if not val:
            return ""
        try:
            return pd.to_datetime(val).strftime('%b %d, %Y')
        except Exception:
            return str(val)

    df['_date_iso'] = df['Date Registered'].apply(to_iso)

    

    session_rows = []
    preview_rows = []

    for _, r in df.iterrows():
        full_name = str(r.get('Full Name', '')).strip()
        email     = str(r.get('Email', '')).strip()
        trn       = str(r.get('TRN', '')).strip()
        mobile    = str(r.get('Mobile', '')).strip()
        password  = str(r.get('Password', '')).strip()
        iso_date  = r.get('_date_iso') or None

        if not any([full_name, email, trn, mobile, password, iso_date]):
            continue

        exist = User.query.filter(or_(User.email == email, User.trn == trn)).first()
        if exist:
            assigned_reg = exist.registration_number or "(keep)"
            will_update = True
        else:
            assigned_reg = next_registration_number()
            will_update = False

        session_rows.append({
            "full_name": full_name,
            "email": email,
            "trn": trn,
            "mobile": mobile,
            "password": password,
            "date_registered": iso_date,
            "assigned_reg": assigned_reg,
            "will_update": will_update,
        })

        preview_rows.append({
            "Full Name": full_name,
            "Email": email,
            "TRN": trn,
            "Mobile": mobile,
            "Password": password,
            "Date Registered": pretty(iso_date),
            "Assigned Reg #": assigned_reg + (" (keep)" if will_update else ""),
        })

    token = _save_user_preview_blob({
        "session_rows": session_rows,
        "preview_rows": preview_rows,
        "created_at": datetime.utcnow().isoformat()
    })
    session['preview_users_token'] = token

    flash("Excel uploaded successfully. Preview below before confirming.", "info")
    return redirect(url_for('accounts_profiles.manage_users'))


# -------------------------
# Confirm Upload Users
# -------------------------
@accounts_bp.route('/confirm-upload-users', methods=['POST'])
@admin_required
def confirm_upload_users():
    token = session.get('preview_users_token')
    blob = _load_user_preview_blob(token) if token else None
    preview_data = blob.get('session_rows') if blob else None

    if not preview_data:
        flash("No data to import. Please upload again.", "warning")
        return redirect(url_for('accounts_profiles.manage_users'))

    imported = 0
    updated = 0
    errors = []

    for row in preview_data:
        full_name   = (row.get('full_name') or '').strip()
        email       = (row.get('email') or '').strip()
        trn         = (row.get('trn') or '').strip()
        mobile      = (row.get('mobile') or '').strip()
        password    = (row.get('password') or '')
        iso_date    = row.get('date_registered')
        assigned_reg= row.get('assigned_reg')
        will_update = bool(row.get('will_update'))

        if not full_name or not email or not trn or not mobile or not password:
            errors.append(f"Missing required fields for {email or trn}.")
            continue
        if not EMAIL_REGEX.match(email):
            errors.append(f"Invalid email: {email}")
            continue
        if not MOBILE_REGEX.match(mobile):
            errors.append(f"Invalid mobile: {mobile}")
            continue

        date_registered = iso_date or datetime.now().strftime('%Y-%m-%d')
        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())  # bytes

        try:
            existing = User.query.filter(or_(User.email == email, User.trn == trn)).first()
            if will_update and existing:
                existing.full_name = full_name
                existing.trn = trn
                existing.mobile = mobile
                existing.password = hashed_pw
                existing.date_registered = date_registered
                existing.email = email
                # ✅ ADD THIS BLOCK HERE — give old users a referral code if missing
                if not (existing.referral_code or "").strip():
                    ref_code = User.generate_referral_code(full_name)
                    for _ in range(10):
                        if not User.query.filter_by(referral_code=ref_code).first():
                            break
                        ref_code = User.generate_referral_code(full_name)
                    existing.referral_code = ref_code

                _safe_commit()
                updated += 1
            else:
                ref_code = User.generate_referral_code(full_name)
                for _ in range(10):
                    if not User.query.filter_by(referral_code=ref_code).first():
                        break
                    ref_code = User.generate_referral_code(full_name)

                u = User(
                    full_name=full_name,
                    email=email,
                    trn=trn,
                    mobile=mobile,
                    password=hashed_pw,
                    date_registered=date_registered,
                    registration_number=assigned_reg,
                    referral_code=ref_code,
                )
                db.session.add(u)
                try:
                    db.session.commit()
                    imported += 1
                except IntegrityError:
                    db.session.rollback()
                    new_reg = f"FAFL{_current_max_fafl_number() + 1}"
                    u.registration_number = new_reg
                    db.session.add(u)
                    try:
                        db.session.commit()
                        imported += 1
                    except IntegrityError:
                        db.session.rollback()
                        errors.append(f"Could not assign unique reg # for {email}.")
        except Exception as e:
            db.session.rollback()
            errors.append(f"Error processing {email or trn}: {e}")

    if token:
        _delete_user_preview_blob(token)
        session.pop('preview_users_token', None)

    flash(f"Imported {imported} new users, updated {updated} existing users.", "success")
    if errors:
        flash("Some rows skipped:<br>" + "<br>".join(errors), "warning")

    return redirect(url_for('accounts_profiles.manage_users'))


# -------------------------
# Export Users (CSV / PDF / Excel)
# -------------------------
@accounts_bp.route('/export-users')
@admin_required
def export_users():
    export_format = request.args.get('format', 'csv')
    search = request.args.get('search', '').strip()
    sort_by = request.args.get('sort', 'recent')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    q = db.session.query(
        User.full_name, User.email, User.registration_number, User.trn, User.date_registered
    )

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            User.full_name.ilike(like),
            User.email.ilike(like),
            User.registration_number.ilike(like),
            User.address.ilike(like)
        ))
    if date_from:
        q = q.filter(User.date_registered >= date_from)
    if date_to:
        q = q.filter(User.date_registered <= date_to)

    if sort_by == 'alphabetical':
        q = q.order_by(User.full_name.asc())
    else:
        q = q.order_by(User.date_registered.desc())

    rows = q.all()

    # CSV
    if export_format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Name', 'Email', 'Reg #', 'TRN', 'Date Registered'])
        for full_name, email, reg, trn, date_registered in rows:
            writer.writerow([full_name, email, reg, trn, date_registered])
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name='users.csv'
        )

    # PDF
    elif export_format == 'pdf':
        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=letter)
        elements = [Paragraph("Registered Users", getSampleStyleSheet()['Heading1'])]
        data = [['Name', 'Email', 'Reg #', 'TRN', 'Date Registered']]
        for full_name, email, reg, trn, date_registered in rows:
            data.append([full_name, email, reg, trn, str(date_registered or "")])
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ]))
        elements.append(table)
        doc.build(elements)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/pdf',
            as_attachment=True,
            download_name='users.pdf'
        )

    # Excel
    elif export_format == 'excel':
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('Users')
        headers = ['Name', 'Email', 'Reg #', 'TRN', 'Date Registered']
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header)
        for row_num, (full_name, email, reg, trn, date_registered) in enumerate(rows, start=1):
            worksheet.write_row(row_num, 0, [
                full_name, email, reg, trn, str(date_registered or "")
            ])
        workbook.close()
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='users.xlsx'
        )

    return "Invalid export format", 400


# -------------------------
# View User (packages + invoices/payments + prealerts)
# -------------------------
@accounts_bp.route('/users/<int:id>', methods=['GET', 'POST'])
@admin_required
def view_user(id):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    # 🔹 US warehouse address pulled from Settings (row id=1)
    settings = db.session.get(Settings, 1)

    if settings:
        street       = settings.us_street or "559 NE 42ND ST"
        suite_prefix = settings.us_suite_prefix or "FAFL# "
        city         = settings.us_city or "Oakland Park"
        state        = settings.us_state or "Florida"
        zip_code     = settings.us_zip or "33334"
    else:
        # Fallback defaults if settings row missing
        street       = "559 NE 42ND ST"
        suite_prefix = ""
        city         = "Oakland Park"
        state        = "Florida"
        zip_code     = "33334"

    us_address = {
        "recipient":      user.full_name or "",
        "address_line1":  street,
        "address_line2":  f"{suite_prefix}{user.registration_number or ''}",
        "city":           city,
        "state":          state,
        "zip":            zip_code,
    }
    home_address = user.address

    # -------------------------
    # Live Status + Last Login
    # -------------------------
    now = datetime.utcnow()
    last = getattr(user, "last_login", None)

    # Active if last login within 90 days (~3 months)
    login_active = bool(last and last >= (now - timedelta(days=90)))

    def fmt_last_login(dt):
        if not dt:
            return "Never"

        try:
            days = (now.date() - dt.date()).days
        except Exception:
            return str(dt)

        date_part = dt.strftime("%Y-%m-%d")
        time_part = dt.strftime("%I:%M %p").lstrip("0")

        if days == 0:
            return f"Today ({date_part} {time_part})"
        if days == 1:
            return f"Yesterday ({date_part} {time_part})"
        if days == 2:
            return f"2 days ago ({date_part} {time_part})"

        return f"{date_part} {time_part}"

    activity_status = "Active" if login_active else "Inactive"
    activity_badge  = "bg-success" if login_active else "bg-secondary"
    last_login_display = fmt_last_login(last)

    # -------------------------
    # Messages (server-side pagination + date filter)
    # -------------------------
    msg_page = request.args.get("msg_page", 1, type=int)
    msg_per_page = request.args.get("msg_per_page", 10, type=int)
    if msg_per_page not in (10, 20, 50, 100, 500, 1000):
        msg_per_page = 10

    msg_from = (request.args.get("msg_from") or "").strip()
    msg_to   = (request.args.get("msg_to") or "").strip()
    msg_q = (request.args.get("msg_q") or "").strip()


    messages = []
    total_messages = 0

    try:
        q = (
            db.session.query(
                DBMessage.subject,
                DBMessage.body,
                DBMessage.created_at
            )
            .filter(or_(DBMessage.recipient_id == id, DBMessage.sender_id == id))
        )

        if msg_from:
            dt_from = datetime.fromisoformat(msg_from)  # 00:00
            q = q.filter(DBMessage.created_at >= dt_from)

        if msg_to:
            dt_to = datetime.fromisoformat(msg_to) + timedelta(days=1)  # next day 00:00
            q = q.filter(DBMessage.created_at < dt_to)

        if msg_q:
            like = f"%{msg_q}%"
            q = q.filter(or_(
                DBMessage.subject.ilike(like),
                DBMessage.body.ilike(like),
            ))


        total_messages = q.count()

        page_obj = q.order_by(DBMessage.created_at.desc()).paginate(
            page=msg_page, per_page=msg_per_page, error_out=False
        )
        messages = page_obj.items

    except Exception:
        db.session.rollback()
        messages = []
        total_messages = 0
        msg_from = msg_to = ""

    msg_total_pages = max((total_messages + msg_per_page - 1) // msg_per_page, 1)
    msg_show_from = 0 if total_messages == 0 else ((msg_page - 1) * msg_per_page + 1)
    msg_show_to   = min(msg_page * msg_per_page, total_messages)

    # Prealerts
    try:
        prealerts = (
            db.session.query(
                Prealert.prealert_number,
                Prealert.vendor_name,
                Prealert.courier_name,
                Prealert.tracking_number,
                Prealert.purchase_date,
                Prealert.package_contents,
                Prealert.item_value_usd,
                Prealert.invoice_filename,
                Prealert.created_at
            )
            .filter(Prealert.customer_id == id)
            .order_by(Prealert.created_at.desc())
            .all()
        )
    except Exception:
        db.session.rollback()
        prealerts = []

    # Packages (paginated + filters)
    pkg_page = request.args.get("pkg_page", 1, type=int)
    pkg_per_page = request.args.get("pkg_per_page", 10, type=int)
    if pkg_per_page not in (10, 25, 50, 100, 500, 1000):
        pkg_per_page = 10

    pkg_from = (request.args.get("pkg_from") or "").strip()
    pkg_to   = (request.args.get("pkg_to") or "").strip()
    pkg_awb  = (request.args.get("pkg_awb") or "").strip()
    pkg_tn   = (request.args.get("pkg_tn") or "").strip()
    pkg_status = (request.args.get("pkg_status") or "").strip()  # ✅ NEW

    packages = []
    total_pkgs = 0

    def _parse_ymd(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    try:
        base = Package.query.filter(Package.user_id == id)

        # Choose the best date column available on Package
        date_col = None
        date_attr = None
        for attr in ("date_received", "received_date", "created_at"):
            if hasattr(Package, attr):
                date_col = getattr(Package, attr)
                date_attr = attr
                break       

        dt_from = _parse_ymd(pkg_from) if pkg_from else None
        dt_to   = _parse_ymd(pkg_to) if pkg_to else None

        # ✅ Apply date filters safely (works for Date OR DateTime columns)
        if date_col is not None:
            if dt_from:
                # If column is Date, compare using date()
                if hasattr(date_col.type, "python_type") and date_col.type.python_type.__name__ == "date":
                    base = base.filter(date_col >= dt_from.date())
                else:
                    base = base.filter(date_col >= dt_from)  # datetime start of day

            if dt_to:
                # inclusive end-date:
                # - Date: <= dt_to.date()
                # - DateTime: < next day (so it includes whole dt_to day)
                if hasattr(date_col.type, "python_type") and date_col.type.python_type.__name__ == "date":
                    base = base.filter(date_col <= dt_to.date())
                else:
                    base = base.filter(date_col < (dt_to + timedelta(days=1)))

        if pkg_awb and hasattr(Package, "house_awb"):
            base = base.filter(Package.house_awb.ilike(f"%{pkg_awb}%"))

        if pkg_tn and hasattr(Package, "tracking_number"):
            base = base.filter(Package.tracking_number.ilike(f"%{pkg_tn}%"))

        if pkg_status and hasattr(Package, "status"):
            # Option A: exact match (strict)
            # base = base.filter(Package.status == pkg_status)

            # Option B: partial match (recommended because your statuses vary like "Ready for Pickup", "Out for Delivery")
            base = base.filter(Package.status.ilike(f"%{pkg_status}%"))

        total_pkgs = base.count()

        order_col = getattr(Package, "created_at", Package.id)
        page_obj = base.order_by(order_col.desc()).paginate(
            page=pkg_page, per_page=pkg_per_page, error_out=False
        )

        def _fmt_date(v):
            if not v:
                return None
            try:
                return v if isinstance(v, str) else v.strftime("%Y-%m-%d")
            except Exception:
                return str(v)

        for p in page_obj.items:
            date_received = None
            for attr in ("date_received", "received_date", "created_at"):
                if getattr(p, attr, None):
                    date_received = getattr(p, attr)
                    break

            declared_value = 0.0
            for attr in ("declared_value", "value"):
                if getattr(p, attr, None) is not None:
                    try:
                        declared_value = float(getattr(p, attr))
                        break
                    except Exception:
                        pass

            amt_due = 0.0
            if getattr(p, "amount_due", None) is not None:
                try:
                    amt_due = float(p.amount_due)
                except Exception:
                    amt_due = 0.0

            weight = 0
            if getattr(p, "weight", None) is not None:
                try:
                    weight = math.ceil(float(p.weight))
                except Exception:
                    weight = 0

            attachments = (
                db.session.query(PackageAttachment.id, PackageAttachment.original_name, PackageAttachment.file_name)
                .filter(PackageAttachment.package_id == p.id)
                .order_by(PackageAttachment.id.desc())
                .all()
            )

            attachments = [{"id": a.id, "original_name": a.original_name or a.file_name} for a in attachments]

            packages.append({
                "id": p.id,
                "user_id": p.user_id,
                "house_awb": getattr(p, "house_awb", None),
                "status": getattr(p, "status", None),
                "description": getattr(p, "description", None),
                "tracking_number": getattr(p, "tracking_number", None),
                "received_scan_status": getattr(p, "received_scan_status", "not_scanned"),
                "received_scanned_at": getattr(p, "received_scanned_at", None),
                "received_scanned_by_id": getattr(p, "received_scanned_by_id", None),

                "delivery_scan_status": getattr(p, "delivery_scan_status", "not_scanned"),
                "delivery_scanned_at": getattr(p, "delivery_scanned_at", None),
                "delivery_scanned_by_id": getattr(p, "delivery_scanned_by_id", None),
                "weight": weight,
                "date_received": _fmt_date(date_received),
                "declared_value": declared_value,
                "amount_due": amt_due,
                "invoice_file": getattr(p, "invoice_file", None),
                "attachments": attachments,
                "epc": getattr(p, "epc", 0),
                "bad_address": getattr(p, "bad_address", False),
                "subscription_applied": getattr(p, "subscription_applied", False),
                "subscription_result": getattr(p, "subscription_result", None),
                "customs_total": getattr(p, "customs_total", 0),
            })

    except Exception:
        db.session.rollback()
        packages = []
        total_pkgs = 0
        pkg_from = pkg_to = pkg_awb = pkg_tn = pkg_status = ""

    # Attachments lookup for packages
    pkg_ids = [p["id"] for p in packages if p.get("id")]

    attachments_by_pkg = {}
    if pkg_ids:
        att_rows = (
            db.session.query(
                PackageAttachment.id,
                PackageAttachment.package_id,
                PackageAttachment.original_name,
                PackageAttachment.file_name,
            )
            .filter(PackageAttachment.package_id.in_(pkg_ids))
            .order_by(PackageAttachment.id.desc())
            .all()
        )

        for att_id, pkg_id, original_name, file_name in att_rows:
            attachments_by_pkg.setdefault(pkg_id, []).append({
                "id": att_id,
                "original_name": original_name or file_name,
                "file_name": file_name,
            })

    for p in packages:
        p["attachments"] = attachments_by_pkg.get(p["id"], [])

    pkg_total_pages = max((total_pkgs + pkg_per_page - 1) // pkg_per_page, 1)
    pkg_show_from = 0 if total_pkgs == 0 else ((pkg_page - 1) * pkg_per_page + 1)
    pkg_show_to   = min(pkg_page * pkg_per_page, total_pkgs)

    # ---------------- Invoices (server-side pagination + date filter) ----------------
    inv_page = request.args.get("inv_page", 1, type=int)
    inv_per_page = request.args.get("inv_per_page", 10, type=int)
    if inv_per_page not in (10, 20, 50, 100, 500, 1000):
        inv_per_page = 10

    inv_from = (request.args.get("inv_from") or "").strip()
    inv_to   = (request.args.get("inv_to") or "").strip()

    total_invoices = 0
    inv_total_pages = 1
    inv_show_from = 0
    inv_show_to = 0

    invoices_rows = []
    total_owed = 0.0
    total_paid = 0.0
    all_rows = []  # keep defined for template safety

    try:
        inv_query = Invoice.query

        conds = []
        if hasattr(Invoice, "user_id"):
            conds.append(Invoice.user_id == id)
        if hasattr(Invoice, "customer_id"):
            conds.append(Invoice.customer_id == id)
        if hasattr(Invoice, "customer_code"):
            conds.append(Invoice.customer_code == (user.registration_number or ""))

        if conds:
            inv_query = inv_query.filter(or_(*conds))

        pay_amount_col = getattr(Payment, "amount_jmd", None) or getattr(Payment, "amount", None)
        if pay_amount_col is None:
            raise RuntimeError("Payment model has no amount_jmd/amount column")

        paid_sum_col = func.coalesce(
            func.sum(pay_amount_col),
            0.0
        ).label("paid_sum")

        completed_payment_join = (
            (Payment.invoice_id == Invoice.id)
            & (
                func.lower(Payment.status)
                == "completed"
            )
            & (
                Payment.transaction_type
                == "invoice_payment"
            )
        )

        q = (
            db.session.query(
                Invoice,
                paid_sum_col,
            )
            .outerjoin(
                Payment,
                completed_payment_join,
            )
            .filter(*inv_query._where_criteria)
            .group_by(Invoice.id)
        )

        date_col = (
            getattr(Invoice, "date_issued", None)
            or getattr(Invoice, "date_submitted", None)
            or getattr(Invoice, "created_at", None)
        )
        if date_col is not None:
            if inv_from:
                try:
                    dt_from = datetime.fromisoformat(inv_from)  # 00:00
                    q = q.filter(date_col >= dt_from)
                except Exception:
                    pass

            if inv_to:
                try:
                    dt_to = datetime.fromisoformat(inv_to) + timedelta(days=1)  # next day 00:00
                    q = q.filter(date_col < dt_to)
                except Exception:
                    pass

        total_invoices = q.count()

        order_col = (
            getattr(Invoice, "date_issued", None)
            or getattr(Invoice, "date_submitted", None)
            or getattr(Invoice, "created_at", None)
            or Invoice.id
        )
        page_obj = q.order_by(order_col.desc()).paginate(
            page=inv_page, per_page=inv_per_page, error_out=False
        )

        invoices_rows = page_obj.items

        inv_total_pages = max((total_invoices + inv_per_page - 1) // inv_per_page, 1)
        inv_show_from = 0 if total_invoices == 0 else ((inv_page - 1) * inv_per_page + 1)
        inv_show_to   = min(inv_page * inv_per_page, total_invoices)

        def _inv_due(inv):
            for attr in ("grand_total", "amount_due", "total"):
                if hasattr(inv, attr) and getattr(inv, attr) is not None:
                    try:
                        return float(getattr(inv, attr))
                    except Exception:
                        pass
            return 0.0

        all_rows = (
            db.session.query(
                Invoice,
                paid_sum_col,
            )
            .outerjoin(
                Payment,
                completed_payment_join,
            )
            .filter(*inv_query._where_criteria)
            .group_by(Invoice.id)
            .all()
        )

        total_owed = sum(max(_inv_due(inv) - float(paid_sum or 0), 0.0) for inv, paid_sum in all_rows)
        total_paid = sum(float(paid_sum or 0) for _, paid_sum in all_rows)

    except Exception as e:
        current_app.logger.exception("Error loading invoices for user %s: %s", id, e)
        db.session.rollback()
        invoices_rows = []
        total_owed = 0.0
        total_paid = 0.0
        all_rows = []

    balance = max(total_owed, 0.0)

    # Payments (server-side pagination + date filter)
    pay_page = request.args.get("pay_page", 1, type=int)
    pay_per_page = request.args.get("pay_per_page", 10, type=int)
    if pay_per_page not in (10, 20, 50, 100, 500, 1000):
        pay_per_page = 10

    pay_from = (request.args.get("pay_from") or "").strip()
    pay_to   = (request.args.get("pay_to") or "").strip()

    payments = []
    total_payments = 0

    try:
        pay_amount_col = getattr(Payment, "amount_jmd", None) or getattr(Payment, "amount", None)
        if pay_amount_col is None:
            raise RuntimeError("Payment model has no amount_jmd/amount column")

        q = (
            db.session.query(
                Payment.id.label("bill_number"),
                Payment.created_at.label("payment_date"),
                Payment.method.label("payment_type"),
                pay_amount_col.label("amount"),
                Payment.invoice_id.label("invoice_id"),
                Payment.transaction_type.label("transaction_type"),
                Payment.status.label("transaction_status"),
                Payment.reference.label("reference"),
                Payment.notes.label("notes"),
                Invoice.invoice_number.label("invoice_number"),
                User.full_name.label("authorized_by"),
            )
            .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
            .outerjoin(User, Payment.authorized_by_admin_id == User.id)
            .filter(Payment.user_id == id)
        )

        if pay_from:
            dt_from = datetime.fromisoformat(pay_from)
            q = q.filter(Payment.created_at >= dt_from)

        if pay_to:
            dt_to = datetime.fromisoformat(pay_to) + timedelta(days=1)
            q = q.filter(Payment.created_at < dt_to)

        total_payments = q.count()

        page_obj = q.order_by(Payment.created_at.desc()).paginate(
            page=pay_page, per_page=pay_per_page, error_out=False
        )

        payments = page_obj.items

    except Exception:
        db.session.rollback()
        payments = []
        total_payments = 0
        pay_from = pay_to = ""

    pay_total_pages = max((total_payments + pay_per_page - 1) // pay_per_page, 1)
    pay_show_from = 0 if total_payments == 0 else ((pay_page - 1) * pay_per_page + 1)
    pay_show_to   = min(pay_page * pay_per_page, total_payments)

    wallet_balance = user.wallet_balance or 0.0
    referral_code  = user.referral_code or ''

    # -------------------------
    # Wallet History
    # -------------------------
    wallet_transactions = []

    try:
        wallet_transactions = (
            WalletTransaction.query
            .filter(WalletTransaction.user_id == id)
            .order_by(
                WalletTransaction.created_at.desc(),
                WalletTransaction.id.desc()
            )
            .all()
        )
    except Exception as e:
        current_app.logger.exception(
            "Error loading wallet transactions for user %s: %s",
            id,
            e
        )
        db.session.rollback()
        wallet_transactions = []

    # ✅ UPDATED: supports tab coming from GET or POST
    active_tab = request.values.get("tab", "packages")

    categories = list(CATEGORIES.keys())

    jl = to_jamaica(getattr(user, "last_login", None))
    last_login_display = None
    if jl:
        last_login_display = jl.strftime("%Y-%m-%d %I:%M %p")   
   
    # -------------------------
    # Scheduled Deliveries (for this user)
    # -------------------------
    scheduled_deliveries = []
    try:
        q = ScheduledDelivery.query.filter(ScheduledDelivery.user_id == id)

        # pick a good date column to sort by
        date_col = getattr(ScheduledDelivery, "scheduled_date", None) or getattr(ScheduledDelivery, "created_at", None)

        if date_col is not None:
            scheduled_deliveries = q.order_by(date_col.desc(), ScheduledDelivery.id.desc()).all()
        else:
            scheduled_deliveries = q.order_by(ScheduledDelivery.id.desc()).all()

    except Exception as e:
        current_app.logger.exception("Error loading scheduled deliveries for user %s: %s", id, e)
        db.session.rollback()
        scheduled_deliveries = []

    today = date.today()
    tomorrow = today + timedelta(days=1)

    # -------------------------
    # Claims (for this user)
    # -------------------------
    user_claims = []
    try:
        user_claims = (
            Claim.query
            .filter(Claim.user_id == id)
            .order_by(Claim.created_at.desc(), Claim.id.desc())
            .all()
        )
    except Exception as e:
        current_app.logger.exception("Error loading claims for user %s: %s", id, e)
        db.session.rollback()
        user_claims = []

    subscription_summary = get_subscription_summary(id)

    if subscription_summary and subscription_summary.get("is_family_plan"):
        owner_member = next(
            (m for m in subscription_summary.get("members", []) if getattr(m, "role", None) == "owner"),
            None
        )

        subscription_summary["is_owner"] = bool(
            owner_member and owner_member.user and owner_member.user.id == id
        )

        subscription_summary["owner_name"] = (
            owner_member.user.full_name
            if owner_member and owner_member.user
            else None
        )
    subscription_plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.price_usd.asc()).all()
    pending_subscription = (
        Subscription.query
        .filter_by(user_id=id, status="pending_payment")
        .order_by(Subscription.created_at.desc())
        .first()
    )

    return render_template(
        'admin/accounts_profiles/view_user.html',
        user={
            "id": user.id,
            "full_name": user.full_name or "",
            "email": user.email or "",
            "registration_number": user.registration_number or "",
            "address": user.address or "",
            "mobile": user.mobile or "",
            "trn": user.trn,
            "wallet_balance": wallet_balance,
            "referral_code": referral_code,
            "is_active": bool(getattr(user, "is_active", True)),
            "activity_status": activity_status,
            "activity_badge": activity_badge,
            "last_login": user.last_login,
            "last_login_display": last_login_display,
            "authorized_person": getattr(user, "authorized_person", None),
            "profile_pic": getattr(user, "profile_pic", None),
            "profile_picture": getattr(user, "profile_pic", None),
            "referrer": getattr(user, "referrer", None),
        },
        user_id=id,
        prealerts=prealerts,
        packages=packages,
        invoices_rows=invoices_rows,
        all_rows=all_rows,
        inv_page=inv_page,
        inv_per_page=inv_per_page,
        inv_total_pages=inv_total_pages,
        inv_show_from=inv_show_from,
        inv_show_to=inv_show_to,
        total_invoices=total_invoices,
        inv_from=inv_from,
        inv_to=inv_to,
        payments=payments,
        total_payments=total_payments,
        pay_page=pay_page,
        pay_per_page=pay_per_page,
        pay_total_pages=pay_total_pages,
        pay_show_from=pay_show_from,
        pay_show_to=pay_show_to,
        pay_from=pay_from,
        pay_to=pay_to,
        total_owed=total_owed,
        total_paid=total_paid,
        msg_page=msg_page,
        msg_per_page=msg_per_page,
        msg_total_pages=msg_total_pages,
        msg_show_from=msg_show_from,
        msg_show_to=msg_show_to,
        total_messages=total_messages,
        msg_from=msg_from,
        msg_to=msg_to,
        msg_q=msg_q,
        balance=balance,
        messages=messages,
        wallet_balance=wallet_balance,
        wallet_transactions=wallet_transactions,
        referral_code=referral_code,
        us_address=us_address,
        home_address=home_address,
        active_tab=active_tab,
        pkg_page=pkg_page,
        pkg_per_page=pkg_per_page,
        pkg_total_pages=pkg_total_pages,
        pkg_show_from=pkg_show_from,
        pkg_show_to=pkg_show_to,
        total_pkgs=total_pkgs,
        pkg_from=pkg_from,
        pkg_to=pkg_to,
        pkg_awb=pkg_awb,
        pkg_tn=pkg_tn,
        pkg_status=pkg_status,
        attachments_by_pkg=attachments_by_pkg,
        categories=categories,                   
        scheduled_deliveries=scheduled_deliveries,
        today=today,
        tomorrow=tomorrow,
        user_claims=user_claims,
        subscription_summary=subscription_summary,
        subscription_plans=subscription_plans,
        pending_subscription=pending_subscription,

    )


@accounts_bp.route("/users/<int:id>/packages/create", methods=["POST"])
@admin_required
def create_single_package_for_user(id):
    user = db.session.get(User, id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    def to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    def to_date(v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except Exception:
            return None

    house_awb       = (request.form.get("house_awb") or "").strip()
    description     = (request.form.get("description") or "").strip()
    tracking_number = (request.form.get("tracking_number") or "").strip()
    status          = (request.form.get("status") or "Overseas").strip()

    weight         = to_float(request.form.get("weight"))
    declared_value = to_float(request.form.get("declared_value"))
    date_received  = to_date(request.form.get("date_received"))

    pkg = Package(
        user_id=user.id,
        house_awb=house_awb or None,
        description=description or None,
        tracking_number=tracking_number or None,
        status=status or None,
        weight=weight,
        declared_value=declared_value,
        date_received=date_received,
    )

    db.session.add(pkg)

    try:
        db.session.flush()  # ✅ gives pkg.id without committing yet

        from app.utils.prealert_sync import sync_prealert_invoice_to_package
        sync_prealert_invoice_to_package(pkg)  # attaches invoice (if found)
        from app.utils.subscription_utils import apply_subscription_usage

        try:
            result = apply_subscription_usage(pkg)

            if result in ("subscription_applied", "already_applied"):
                pass
            else:
                pkg.subscription_applied = False
                pkg.subscription_result = result or "no_subscription"
                pkg.subscription_applied_at = None
                pkg.subscription_id = None

        except Exception as e:
            current_app.logger.exception(
                f"Subscription application failed for admin-created package {pkg.id}: {e}"
            )
            pkg.subscription_applied = False
            pkg.subscription_result = "subscription_error"
            pkg.subscription_applied_at = None

        db.session.commit()  # ✅ commit ONCE at the end
    except Exception:
        current_app.logger.exception("[ADMIN PACKAGE CREATE] failed after single package create")
        db.session.rollback()
        return jsonify({"success": False, "error": "Could not create package"}), 500

    # ✅ IMPORTANT: refresh pkg so attachments relationship includes anything just added
    try:
        db.session.refresh(pkg)
    except Exception:
        pass

    def serialize_attachment(a):
        return {
            "id": a.id,
            "original_name": getattr(a, "original_name", None) or "Attachment",
            "view_url": url_for("logistics.admin_view_package_attachment", attachment_id=a.id),
            "delete_url": url_for("logistics.delete_package_attachment_admin", attachment_id=a.id),
        }

    attachments = []
    try:
        attachments = [serialize_attachment(a) for a in (pkg.attachments or [])]
    except Exception:
        attachments = []

    return jsonify({
        "success": True,
        "pkg": {
            "id": pkg.id,
            "house_awb": pkg.house_awb or "",
            "status": pkg.status or "Overseas",
            "description": pkg.description or "",
            "tracking_number": pkg.tracking_number or "",
            "weight": float(pkg.weight or 0),
            "date_received": (pkg.date_received.strftime("%Y-%m-%d") if getattr(pkg, "date_received", None) else ""),
            "declared_value": float(getattr(pkg, "declared_value", 0) or 0),
            "amount_due": float(getattr(pkg, "amount_due", 0) or 0),
            "attachments": attachments
        }
    })


@accounts_bp.route("/users/<int:id>/packages/bulk-delete", methods=["POST"])
@admin_required
def bulk_delete_user_packages(id):
    user = db.session.get(User, id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    ids = data.get("package_ids") or []

    try:
        ids = [int(x) for x in ids]
    except Exception:
        return jsonify({"success": False, "error": "Invalid package ids"}), 400

    if not ids:
        return jsonify({"success": False, "error": "No packages selected"}), 400

    q = Package.query.filter(Package.user_id == user.id, Package.id.in_(ids))
    deleted_count = q.count()

    q.delete(synchronize_session=False)
    db.session.commit()

    return jsonify({"success": True, "deleted": deleted_count})


@accounts_bp.route("/users/<int:id>/messages/send", methods=["POST"])
@admin_required
def send_message_to_user(id):
    other = db.session.get(User, id)
    if not other:
        flash("User not found.", "danger")
        return redirect(url_for("accounts_profiles.manage_users"))

    subject = (request.form.get("subject") or "").strip()
    body    = (request.form.get("body") or "").strip()

    if not subject or not body:
        flash("Subject and Body are required.", "warning")
        return redirect(_back_to_view_user_url(id, fallback_tab="messages"))

    # NOTE: you used DBMessage alias for queries above, but you're adding Message() here.
    # If your actual model name is Message, keep as-is. If not, change to DBMessage or import Message.
    tk = make_thread_key(current_user.id, other.id)

    db.session.add(DBMessage(
        sender_id=current_user.id,
        recipient_id=other.id,
        subject=subject,
        body=body,
        thread_key=tk,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    ))
    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    flash("Message sent.", "success")
    return redirect(_back_to_view_user_url(id, fallback_tab="messages"))


# -------------------------
# Change Password
# -------------------------
@accounts_bp.route('/change-password/<int:id>', methods=['GET', 'POST'])
@admin_required
def change_password(id: int):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    if request.method == 'POST':
        new_password = request.form['password']
        confirm_password = request.form.get('confirm_password')

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template(
                'admin/accounts_profiles/change_password.html',
                user={"id": user.id, "full_name": user.full_name}
            )

        hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
        user.password = hashed_pw

        db.session.add(AuditLog(
            module="Admin Activity",
            action="Customer Password Changed",
            admin_id=current_user.id,
            user_id=user.id,
            entity_type="User",
            entity_id=user.id,
            reason="Admin changed customer password",
            description=(
                f"Password was changed for "
                f"{user.full_name or user.email or ('User #' + str(user.id))}."
            ),
            old_value="Password: Hidden",
            new_value="Password: Updated",
        ))

        _safe_commit()

        flash(f"Password updated for {user.full_name or 'user'}.", "success")
        return redirect(_back_to_view_user_url(id))

    return render_template(
        'admin/accounts_profiles/change_password.html',
        user={"id": user.id, "full_name": user.full_name or ''}
    )


# -------------------------
# Delete Account
# -------------------------
def _is_safe_next_url(target: str) -> bool:
    """Prevent open-redirects: only allow relative or same-host URLs."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


@accounts_bp.route('/delete-account/<int:id>', methods=['GET', 'POST'])
@admin_required
def delete_account(id: int):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    raw_next = request.args.get("next") or request.form.get("next")
    next_url = raw_next if _is_safe_next_url(raw_next) else url_for("accounts_profiles.manage_users")

    if request.method == 'POST':
        user_name = user.full_name or "user"
        user_email = user.email
        user_reg = user.registration_number
        user_role = user.role

        try:
            db.session.add(AuditLog(
                module="Admin Activity",
                action="Account Deleted",
                admin_id=current_user.id,
                user_id=user.id,
                entity_type="User",
                entity_id=user.id,
                reason="Admin deleted user account",
                description=(
                    f"Account deleted for {user_name}. "
                    f"Email: {user_email or '—'}. "
                    f"FAFL: {user_reg or '—'}."
                ),
                old_value=(
                    f"Name: {user_name}; "
                    f"Email: {user_email or '—'}; "
                    f"FAFL: {user_reg or '—'}; "
                    f"Role: {user_role or '—'}"
                ),
                new_value="Account deleted",
            ))

            DBMessage.query.filter(
                (DBMessage.recipient_id == user.id) |
                (DBMessage.sender_id == user.id)
            ).delete(synchronize_session=False)

            db.session.delete(user)
            db.session.commit()

            flash(f"Account for {user_name} has been deleted.", "success")

        except Exception as e:
            db.session.rollback()
            flash(f"Failed to delete account: {e}", "danger")

        return redirect(next_url)

    return render_template(
        'admin/accounts_profiles/delete_account.html',
        user={"id": user.id, "full_name": user.full_name or ''},
        next=next_url
    )

@accounts_bp.route("/users/<int:id>/claims/create", methods=["POST"])
@admin_required
def create_claim_for_user(id):
    user = db.session.get(User, id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    house_awb = (request.form.get("house_awb") or "").strip()
    tracking_number = (request.form.get("tracking_number") or "").strip()
    description = (request.form.get("description") or "").strip()
    refund_method = (request.form.get("refund_method") or "cash").strip().lower()
    try:
        package_id = int(request.form.get("package_id") or 0)
    except Exception:
        package_id = 0

    bank_account_name = (request.form.get("bank_account_name") or "").strip()
    bank_branch = (request.form.get("bank_branch") or "").strip()
    bank_account_number = (request.form.get("bank_account_number") or "").strip()
    bank_account_type = (request.form.get("bank_account_type") or "").strip()

    try:
        item_value_jmd = float(request.form.get("item_value_jmd") or 0)
    except Exception:
        item_value_jmd = 0.0

    if not house_awb:
        return jsonify({"success": False, "error": "House AWB is required."}), 400

    if item_value_jmd <= 0:
        return jsonify({"success": False, "error": "Item value must be greater than 0."}), 400

    if refund_method not in ("cash", "bank_transfer", "wallet_credit"):
        refund_method = "cash"

    if refund_method == "bank_transfer":
        if not bank_account_name or not bank_branch or not bank_account_number or not bank_account_type:
            return jsonify({
                "success": False,
                "error": "All bank details are required for bank transfer refunds."
            }), 400

    selected_package = None
    if package_id:
        selected_package = (
            Package.query
            .filter(
                Package.id == package_id,
                Package.user_id == user.id
            )
            .first()
        )

        if not selected_package:
            return jsonify({
                "success": False,
                "error": "Selected package was not found for this customer."
            }), 400

    invoice_url = (request.form.get("invoice_url") or "").strip()

    bank_statement_url = (request.form.get("bank_statement_url") or "").strip()

    # placeholders allowed for admin-created claims
    if not invoice_url:
        invoice_url = "#"

    if not bank_statement_url:
        bank_statement_url = "#"

    try:
        claim = Claim(
            case_id=generate_claim_case_id(),
            user_id=user.id,
            package_id=selected_package.id if selected_package else None,
            house_awb=house_awb,
            tracking_number=tracking_number or None,
            item_value_jmd=item_value_jmd,
            description=description or None,

            invoice_url=invoice_url,
            invoice_public_id=None,
            bank_statement_url=bank_statement_url,
            bank_statement_public_id=None,

            refund_method=refund_method,
            bank_account_name=bank_account_name or None,
            bank_branch=bank_branch or None,
            bank_account_number=bank_account_number or None,
            bank_account_type=bank_account_type or None,

            status="submitted",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        db.session.add(claim)
        db.session.flush()

        db.session.add(
            ClaimAuditLog(
                claim_id=claim.id,
                action="created",
                from_status=None,
                to_status="submitted",
                actor_admin_id=current_user.id,
                message=f"Admin created claim {claim.case_id} on behalf of customer {user.full_name}"
            )
        )

        db.session.commit()

        return jsonify({
            "success": True,
            "claim_id": claim.id,
            "case_id": claim.case_id,
            "status": claim.status,
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to create admin claim for user %s", id)
        return jsonify({"success": False, "error": f"Could not create claim: {e}"}), 500

# -------------------------
# Manage Account (simple editor)
# -------------------------
@accounts_bp.route('/manage_account/<int:id>', methods=['POST'])
@admin_required
def manage_account(id: int):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    # --------- pull form values (trim) ----------
    full_name = (request.form.get('full_name') or '').strip()
    email     = (request.form.get('email') or '').strip().lower()
    mobile    = (request.form.get('mobile') or '').strip()
    address   = (request.form.get('address') or '').strip()
    referral  = (request.form.get('referral_code') or '').strip()
    trn       = (request.form.get('trn') or '').strip()

    # UI checkbox/radio -> store into DB field
    # (default enabled if missing)
    is_enabled_val = bool(int(request.form.get('is_active', 1)))

    # --------- basic validation ----------
    if not email:
        flash("Email is required.", "danger")
        return redirect(_back_to_view_user_url(id))

    # --------- email uniqueness check ----------
    current_email = (user.email or '').strip().lower()

    if email != current_email:
        existing = db.session.execute(
            select(User.id).where(
                User.email == email,
                User.id != user.id
            )
        ).scalar_one_or_none()

        if existing:
            flash("That email is already in use by another account.", "danger")
            return redirect(_back_to_view_user_url(id))

    # -------------------------------------------------
    # Capture old values for audit logging
    # -------------------------------------------------
    old_full_name = user.full_name or ""
    old_email = user.email or ""
    old_mobile = user.mobile or ""
    old_address = user.address or ""
    old_referral = user.referral_code or ""
    old_trn = user.trn or ""
    old_status = "Enabled" if user.is_enabled else "Disabled"

    # -------------------------------------------------
    # Apply updates
    # -------------------------------------------------
    user.full_name = full_name
    user.email = email
    user.mobile = mobile
    user.address = address
    user.referral_code = referral or None
    user.trn = trn or None
    user.is_enabled = is_enabled_val

    new_status = "Enabled" if user.is_enabled else "Disabled"

    # -------------------------------------------------
    # Build audit description
    # -------------------------------------------------
    changes = []

    if old_full_name != (user.full_name or ""):
        changes.append(
            f"Name: {old_full_name} → {user.full_name or ''}"
        )

    if old_email != (user.email or ""):
        changes.append(
            f"Email: {old_email} → {user.email or ''}"
        )

    if old_mobile != (user.mobile or ""):
        changes.append(
            f"Mobile: {old_mobile} → {user.mobile or ''}"
        )

    if old_address != (user.address or ""):
        changes.append("Address updated")

    if old_referral != (user.referral_code or ""):
        changes.append(
            f"Referral Code: {old_referral or 'None'} → {user.referral_code or 'None'}"
        )

    if old_trn != (user.trn or ""):
       changes.append("TRN updated")

    if old_status != new_status:
        changes.append(
            f"Status: {old_status} → {new_status}"
        )

    # -------------------------------------------------
    # Create audit log if anything changed
    # -------------------------------------------------
    if changes:
        create_audit_log(
            module="Admin Activity",
            action="Account Updated",
            admin_id=current_user.id,
            user_id=user.id,
            entity_type="User",
            entity_id=user.id,
            reason="Account profile update",
            description="; ".join(changes),
            old_value=old_status,
            new_value=new_status,
        )

    try:
        _safe_commit()
        flash("Account updated successfully.", "success")

    except IntegrityError:
        db.session.rollback()
        flash(
            "Could not save changes: referral code or email already exists.",
            "danger"
        )

    return redirect(_back_to_view_user_url(id))

@accounts_bp.route('/users/<int:id>/wallet', methods=['POST'])
@admin_required
def update_wallet(id):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    wallet_action = (request.form.get("wallet_action") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    description = (request.form.get("description") or "").strip()
    invoice_number = (request.form.get("invoice_number") or "").strip()

    try:
        amount = abs(float(request.form.get("amount", 0) or 0))
    except ValueError:
        amount = 0

    if wallet_action not in ("credit", "debit", "invoice_payment"):
        flash("Please select whether this is an add funds, deduct funds, or invoice/package payment transaction.", "danger")
        return redirect(_back_to_view_user_url(id))

    if amount <= 0:
        flash("Please enter an amount greater than 0.", "danger")
        return redirect(_back_to_view_user_url(id))

    if not reason:
        flash("Please select a wallet reason.", "danger")
        return redirect(_back_to_view_user_url(id))

    if not description:
        flash("Please enter a wallet description.", "danger")
        return redirect(_back_to_view_user_url(id))

    if wallet_action == "invoice_payment" and not invoice_number:
        flash("Invoice number is required when wallet funds are used to pay for a package/invoice.", "danger")
        return redirect(_back_to_view_user_url(id))

    signed_amount = amount

    if wallet_action in ("debit", "invoice_payment"):
        signed_amount = -amount

    new_balance = float(user.wallet_balance or 0) + signed_amount

    if new_balance < 0:
        flash("Wallet deduction denied. This transaction would make the wallet balance negative.", "danger")
        return redirect(_back_to_view_user_url(id))

    user.wallet_balance = new_balance

    txn_description = description
    if invoice_number:
        txn_description = f"{description} | Invoice #: {invoice_number}"

    wallet_txn = WalletTransaction(
        user_id=user.id,
        amount=signed_amount,
        description=txn_description,
        type=wallet_action,
        action=wallet_action,
        reason=reason,
        invoice_number=invoice_number or None,
        admin_id=current_user.id if current_user and current_user.is_authenticated else None,
    )

    db.session.add(wallet_txn)

    create_audit_log(
        module="Wallet",
        action=wallet_action,
        admin_id=current_user.id,
        user_id=user.id,
        entity_type="Wallet",
        entity_id=user.id,
        reason=reason,
        description=txn_description,
        old_value=str(user.wallet_balance - signed_amount),
        new_value=str(user.wallet_balance),
    )

    _safe_commit()

    flash(
        f"Wallet updated by {signed_amount:+.2f}. New balance: {user.wallet_balance:.2f}",
        "success"
    )
    return redirect(_back_to_view_user_url(id))


@accounts_bp.route("/view_user/<int:id>/messages/bulk-delete", methods=["POST"])
@admin_required
def bulk_delete_user_messages(id):
    user = User.query.get_or_404(id)

    print("FORM DATA:", request.form)

    raw_ids = request.form.getlist("message_ids")
    print("RAW IDS:", raw_ids)

    message_ids = [int(x) for x in raw_ids if str(x).isdigit()]
    print("PARSED IDS:", message_ids)

    if not message_ids:
        flash("No messages selected.", "warning")
        return redirect(url_for("accounts_profiles.view_user", id=id, tab="messages"))

    try:
        msgs = (
            Message.query
            .filter(Message.id.in_(message_ids))
            .all()
        )
        print("FOUND MSG IDS:", [m.id for m in msgs])

        deleted_count = 0

        for msg in msgs:
            print("CHECKING MSG:", msg.id, "sender:", msg.sender_id, "recipient:", msg.recipient_id)

            if msg.sender_id == user.id or msg.recipient_id == user.id:
                db.session.delete(msg)
                deleted_count += 1

        db.session.commit()

        if deleted_count:
            flash(f"{deleted_count} message(s) deleted successfully.", "success")
        else:
            flash("No matching messages were deleted.", "warning")

    except Exception as e:
        db.session.rollback()
        print(f"Error bulk deleting messages: {e}")
        flash("An error occurred while deleting messages.", "danger")

    return redirect(url_for("accounts_profiles.view_user", id=id, tab="messages"))

@accounts_bp.route("/users/<int:id>/subscriptions/manual-add", methods=["POST"])
@admin_required
def manual_add_subscription(id):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("accounts_profiles.manage_users"))

    plan_id = request.form.get("plan_id", type=int)
    duration_days = request.form.get("duration_days", 30, type=int)

    plan = SubscriptionPlan.query.get(plan_id)
    if not plan:
        flash("Invalid subscription plan.", "danger")
        return redirect(url_for("accounts_profiles.view_user", id=id))

    # -------------------------
    # Capture previous active/exhausted subscription info
    # -------------------------
    old_subs = (
        Subscription.query
        .filter(
            Subscription.user_id == id,
            Subscription.status.in_(["active", "exhausted"])
        )
        .all()
    )

    old_plan_names = []
    for old_sub in old_subs:
        old_plan_names.append(old_sub.plan.name if old_sub.plan else f"Subscription #{old_sub.id}")

    old_value = ", ".join(old_plan_names) if old_plan_names else "No active subscription"

    # expire old active subs
    for s in old_subs:
        s.status = "expired"

    sub = Subscription(
        user_id=id,
        plan_id=plan.id,
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=duration_days),
        status="active",
    )

    db.session.add(sub)
    db.session.flush()

    usage = SubscriptionUsage(subscription_id=sub.id)
    db.session.add(usage)

    # -------------------------
    # Audit log
    # -------------------------
    create_audit_log(
        module="Subscription",
        action="Subscription Activated",
        admin_id=current_user.id,
        user_id=user.id,
        entity_type="Subscription",
        entity_id=sub.id,
        reason="Manual subscription activation",
        description=(
            f"{plan.name} subscription activated manually for "
            f"{user.full_name or user.email} for {duration_days} day(s)."
        ),
        old_value=old_value,
        new_value=f"{plan.name} - Active until {sub.end_date.strftime('%Y-%m-%d')}",
    )

    db.session.commit()

    flash(f"{plan.name} subscription activated for {user.full_name}.", "success")
    return redirect(url_for("accounts_profiles.view_user", id=id, tab="packages"))

def sync_expired_subscriptions():
    now = datetime.now(timezone.utc)

    expired_subs = (
        Subscription.query
        .filter(
            Subscription.status.in_(["active", "exhausted"]),
            Subscription.end_date < now
        )
        .all()
    )

    for sub in expired_subs:
        sub.status = "expired"

    if expired_subs:
        db.session.commit()

@accounts_bp.route("/subscriptions")
@admin_required
def admin_subscriptions():
    sync_expired_subscriptions()

    status_filter = (request.args.get("status") or "").strip()
    search = (request.args.get("search") or "").strip()
    created_filter = (request.args.get("created") or "").strip()
    sort_by = (request.args.get("sort") or "newest").strip()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 10, type=int)

    if per_page not in [10, 25, 50, 100]:
        per_page = 10

    base_q = (
        Subscription.query
        .join(User, Subscription.user_id == User.id)
        .join(SubscriptionPlan, Subscription.plan_id == SubscriptionPlan.id)
    )

    q = base_q

    if status_filter:
        q = q.filter(Subscription.status == status_filter)

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            User.full_name.ilike(like),
            User.email.ilike(like),
            User.registration_number.ilike(like),
            SubscriptionPlan.name.ilike(like),
        ))

    today = datetime.utcnow().date()

    if created_filter == "today":
        q = q.filter(func.date(Subscription.created_at) == today)

    elif created_filter == "7_days":
        start_date = today - timedelta(days=7)
        q = q.filter(func.date(Subscription.created_at) >= start_date)

    elif created_filter == "30_days":
        start_date = today - timedelta(days=30)
        q = q.filter(func.date(Subscription.created_at) >= start_date)

    if sort_by == "oldest":
        q = q.order_by(Subscription.created_at.asc())
    elif sort_by == "customer_asc":
        q = q.order_by(User.full_name.asc())
    elif sort_by == "customer_desc":
        q = q.order_by(User.full_name.desc())
    elif sort_by == "amount_desc":
        q = q.order_by(SubscriptionPlan.price_usd.desc())
    elif sort_by == "amount_asc":
        q = q.order_by(SubscriptionPlan.price_usd.asc())
    else:
        q = q.order_by(Subscription.created_at.desc())


    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    subscriptions = pagination.items

    counts = {
        "all": Subscription.query.count(),
        "pending_payment": Subscription.query.filter_by(status="pending_payment").count(),
        "active": Subscription.query.filter_by(status="active").count(),
        "exhausted": Subscription.query.filter_by(status="exhausted").count(),
        "expired": Subscription.query.filter_by(status="expired").count(),
    }

    plans = (
        SubscriptionPlan.query
        .filter_by(is_active=True)
        .order_by(SubscriptionPlan.price_usd.asc())
        .all()
    )

    return render_template(
        "admin/accounts_profiles/subscriptions.html",
        subscriptions=subscriptions,
        status_filter=status_filter,
        search=search,
        created_filter=created_filter,
        sort_by=sort_by,
        counts=counts,
        plans=plans,
        page=page,
        per_page=per_page,
        total_pages=max(pagination.pages or 1, 1),
        total_results=pagination.total,
        start_index=((page - 1) * per_page) + 1 if pagination.total else 0,
        end_index=min(page * per_page, pagination.total),
        to_jamaica=to_jamaica
    )

@accounts_bp.route("/subscriptions/bulk-activate", methods=["POST"])
@admin_required
def bulk_activate_subscriptions():
    subscription_ids = request.form.getlist("subscription_ids")
    subscription_ids = [int(x) for x in subscription_ids if str(x).isdigit()]

    if not subscription_ids:
        flash("No pending subscriptions selected.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    activated_count = 0

    for sub_id in subscription_ids:
        sub = Subscription.query.get(sub_id)

        if not sub or sub.status != "pending_payment":
            continue

        plan = sub.plan
        user = sub.user

        amount_jmd = float(plan.price_usd or 0) * 162

        # expire any other active subscription for this user
        old_active = (
            Subscription.query
            .filter(
                Subscription.user_id == sub.user_id,
                Subscription.status.in_(["active", "exhausted"]),
                Subscription.id != sub.id
            )
            .all()
        )

        for old in old_active:
            old.status = "expired"

        # activate selected subscription
        sub.status = "active"
        sub.start_date = datetime.utcnow()
        sub.end_date = datetime.utcnow() + timedelta(days=30)

        # -------------------------------
        # Add owner for Family plan
        # -------------------------------
        if getattr(plan, "is_family_plan", False):
            existing_owner = SubscriptionMember.query.filter_by(
                subscription_id=sub.id,
                user_id=user.id,
                role="owner"
            ).first()

            if not existing_owner:
                owner_member = SubscriptionMember(
                    subscription_id=sub.id,
                    user_id=user.id,
                    role="owner",
                    status="active"
                )
                db.session.add(owner_member)

        # create usage if missing
        if not sub.usage:
            db.session.add(SubscriptionUsage(subscription_id=sub.id))

        # create invoice
        invoice = Invoice(
            user_id=user.id,
            invoice_number=f"SUB-{sub.id:05d}",
            description=f"{plan.name} Subscription Plan",
            amount=amount_jmd,
            grand_total=amount_jmd,
            amount_due=0.0,
            status="paid",
            date_issued=datetime.utcnow(),
            date_paid=datetime.utcnow(),
        )

        db.session.add(invoice)
        db.session.flush()

        # create payment record
        payment = Payment(
            user_id=user.id,
            invoice_id=invoice.id,
            amount_jmd=amount_jmd,
            method="Bank Transfer / Cash",
            status="completed",
            transaction_type="subscription_payment",
            reference=f"Subscription {plan.name}",
            notes=f"Activated subscription #{sub.id}",
            authorized_by_admin_id=current_user.id,
        )

        db.session.add(payment)
        activated_count += 1

    db.session.commit()

    flash(f"{activated_count} subscription(s) activated and payment recorded.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))

@accounts_bp.route("/subscriptions/<int:sub_id>/add-member", methods=["POST"])
@admin_required
def add_subscription_member(sub_id):
    from app.models import Subscription, SubscriptionMember, User

    sub = Subscription.query.get_or_404(sub_id)

    if not sub.plan.is_family_plan:
        flash("This is not a family plan.", "danger")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    reg = (request.form.get("registration_number") or "").strip().upper()

    if not reg:
        flash("Registration number required.", "warning")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    user = User.query.filter_by(registration_number=reg).first()

    if not user:
        flash("User not found.", "danger")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    # Prevent adding the subscription owner again
    if user.id == sub.user_id:
        flash("This user is already the owner of this subscription.", "info")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    # Max 4 persons total: owner + 3 members
    active_member_count = SubscriptionMember.query.filter_by(
        subscription_id=sub.id,
        status="active"
    ).count()

    if active_member_count >= 4:
        flash("Family plan already has the maximum 4 persons.", "warning")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    # Prevent duplicate on same subscription
    existing_same_subscription = SubscriptionMember.query.filter_by(
        subscription_id=sub.id,
        user_id=user.id,
        status="active"
    ).first()

    if existing_same_subscription:
        flash("User is already added to this family plan.", "info")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    # Prevent user being in another active family subscription
    existing_membership = (
        SubscriptionMember.query
        .filter(
            SubscriptionMember.user_id == user.id,
            SubscriptionMember.status == "active"
        )
        .first()
    )

    if existing_membership:
        flash("User already belongs to another subscription.", "warning")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

    member = SubscriptionMember(
        subscription_id=sub.id,
        user_id=user.id,
        role="member",
        status="active"
    )

    db.session.add(member)
    db.session.flush()

    owner = sub.user
    plan_name = sub.plan.name if sub.plan else "Family Plan"

    create_audit_log(
        module="Subscription",
        action="Family Member Added",
        admin_id=current_user.id,
        user_id=user.id,
        entity_type="SubscriptionMember",
        entity_id=member.id,
        reason="Family plan member added",
        description=(
            f"{user.full_name or user.email} ({user.registration_number or 'No FAFL #'}) "
            f"was added as a member to {plan_name} subscription owned by "
            f"{owner.full_name or owner.email if owner else 'Unknown owner'}."
        ),
        old_value="Not a member",
        new_value=f"Active family member on subscription #{sub.id}",
    )

    db.session.commit()

    flash(f"{user.full_name} added to family plan.", "success")
    return redirect(request.referrer or url_for("accounts_profiles.view_user", id=sub.user_id))

@accounts_bp.route("/subscriptions/bulk-waive", methods=["POST"])
@admin_required
def bulk_waive_subscriptions():
    subscription_ids = request.form.getlist("subscription_ids")
    reason = (request.form.get("waiver_reason") or "").strip()

    subscription_ids = [int(x) for x in subscription_ids if str(x).isdigit()]

    if not subscription_ids:
        flash("No subscriptions selected.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    if not reason:
        flash("Please enter a reason for waiving the subscription charge.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    waived_count = 0

    for sub_id in subscription_ids:
        sub = Subscription.query.get(sub_id)

        if not sub or sub.status != "pending_payment":
            continue

        plan = sub.plan
        user = sub.user

        old_value = f"Status: {sub.status}"

        old_active = (
            Subscription.query
            .filter(
                Subscription.user_id == sub.user_id,
                Subscription.status.in_(["active", "exhausted"]),
                Subscription.id != sub.id
            )
            .all()
        )

        old_active_names = []
        for old in old_active:
            old_active_names.append(old.plan.name if old.plan else f"Subscription #{old.id}")
            old.status = "expired"

        sub.status = "active"
        sub.start_date = datetime.utcnow()
        sub.end_date = datetime.utcnow() + timedelta(days=30)

        sub.is_admin_waived = True
        sub.waiver_reason = reason
        sub.waived_at = datetime.utcnow()
        sub.waived_by_admin_id = current_user.id

        if not sub.usage:
            db.session.add(SubscriptionUsage(subscription_id=sub.id))

        invoice = Invoice(
            user_id=user.id,
            invoice_number=f"SUB-WAIVED-{sub.id:05d}",
            description=f"{plan.name} Subscription Plan - Admin Waived",
            amount=0.0,
            grand_total=0.0,
            amount_due=0.0,
            status="paid",
            date_issued=datetime.utcnow(),
            date_paid=datetime.utcnow(),
        )

        db.session.add(invoice)
        db.session.flush()

        payment = Payment(
            user_id=user.id,
            invoice_id=invoice.id,
            amount_jmd=0.0,
            method="Admin Waiver",
            status="completed",
            transaction_type="subscription_waiver",
            reference=f"Waived subscription {plan.name}",
            notes=f"Admin waived subscription #{sub.id}. Reason: {reason}",
            authorized_by_admin_id=current_user.id,
        )

        db.session.add(payment)

        expired_note = ""
        if old_active_names:
            expired_note = f" Previous active subscription(s) expired: {', '.join(old_active_names)}."

        create_audit_log(
            module="Subscription",
            action="Subscription Waived",
            admin_id=current_user.id,
            user_id=user.id,
            entity_type="Subscription",
            entity_id=sub.id,
            reason=reason,
            description=(
                f"{plan.name} subscription was waived and activated for "
                f"{user.full_name or user.email}. Invoice {invoice.invoice_number} created at $0.00."
                f"{expired_note}"
            ),
            old_value=old_value,
            new_value=(
                f"Status: active; Waived: yes; "
                f"End Date: {sub.end_date.strftime('%Y-%m-%d')}; "
                f"Invoice: {invoice.invoice_number}"
            ),
        )

        waived_count += 1

    db.session.commit()

    flash(f"{waived_count} subscription(s) activated as free/waived.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))

@accounts_bp.route("/subscriptions/<int:sub_id>/remove-member/<int:user_id>", methods=["POST"])
@admin_required
def remove_subscription_member(sub_id, user_id):
    member = (
        SubscriptionMember.query
        .filter_by(subscription_id=sub_id, user_id=user_id, status="active")
        .first()
    )

    if not member:
        flash("Member not found.", "warning")
        return redirect(request.referrer)

    if member.role == "owner":
        flash("Cannot remove owner.", "danger")
        return redirect(request.referrer)

    sub = member.subscription
    removed_user = member.user
    owner = sub.user if sub else None
    plan_name = sub.plan.name if sub and sub.plan else "Family Plan"

    old_value = f"Active family member on subscription #{sub_id}"

    member.status = "removed"
    member.removed_at = datetime.utcnow()

    create_audit_log(
        module="Subscription",
        action="Family Member Removed",
        admin_id=current_user.id,
        user_id=removed_user.id if removed_user else user_id,
        entity_type="SubscriptionMember",
        entity_id=member.id,
        reason="Family plan member removed",
        description=(
            f"{removed_user.full_name or removed_user.email if removed_user else 'Unknown user'} "
            f"was removed from {plan_name} subscription owned by "
            f"{owner.full_name or owner.email if owner else 'Unknown owner'}."
        ),
        old_value=old_value,
        new_value="Removed from family plan",
    )

    db.session.commit()

    flash("Member removed successfully.", "success")
    return redirect(request.referrer)

@accounts_bp.route("/subscriptions/<int:sub_id>/cancel-pending", methods=["POST"])
@admin_required
def cancel_pending_subscription(sub_id):
    sub = Subscription.query.get_or_404(sub_id)

    if sub.status != "pending_payment":
        flash("Only pending payment subscriptions can be cancelled here.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    sub.status = "cancelled"
    db.session.commit()

    flash("Pending subscription cancelled.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))


@accounts_bp.route("/subscriptions/<int:sub_id>/cancel-refund-unused", methods=["POST"])
@admin_required
def cancel_refund_unused_subscription(sub_id):
    sub = Subscription.query.get_or_404(sub_id)

    if sub.status != "active":
        flash("Only active subscriptions can be cancelled/refunded.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    usage = sub.usage
    packages_used = int(getattr(usage, "packages_used", 0) or 0)
    weight_used = float(getattr(usage, "weight_used", 0) or 0)

    if packages_used > 0 or weight_used > 0:
        flash("This subscription has already been used. Refund blocked. Use admin override if needed.", "danger")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    amount_jmd = float(sub.plan.price_usd or 0) * 162

    sub.status = "cancelled"

    refund_payment = Payment(
        user_id=sub.user_id,
        amount_jmd=-amount_jmd,
        method="Subscription Refund",
        status="completed",
        transaction_type="subscription_refund",
        reference=f"Refund for {sub.plan.name} subscription",
        notes=f"Unused subscription #{sub.id} cancelled and refunded.",
        authorized_by_admin_id=current_user.id,
    )

    db.session.add(refund_payment)
    db.session.commit()

    flash("Unused subscription cancelled and refund recorded.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))


@accounts_bp.route("/subscriptions/<int:sub_id>/override-cancel", methods=["POST"])
@admin_required
def override_cancel_subscription(sub_id):
    sub = Subscription.query.get_or_404(sub_id)

    reason = (request.form.get("reason") or "").strip()

    if not reason:
        flash("Override cancellation requires a reason.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    user = sub.user
    plan_name = sub.plan.name if sub.plan else "Subscription Plan"

    old_value = f"Status: {sub.status}; Plan: {plan_name}"

    sub.status = "cancelled"

    note = Payment(
        user_id=sub.user_id,
        amount_jmd=0,
        method="Admin Override",
        status="completed",
        transaction_type="subscription_cancel_override",
        reference=f"Override cancel subscription #{sub.id}",
        notes=reason,
        authorized_by_admin_id=current_user.id,
    )

    db.session.add(note)

    create_audit_log(
        module="Subscription",
        action="Subscription Cancelled",
        admin_id=current_user.id,
        user_id=sub.user_id,
        entity_type="Subscription",
        entity_id=sub.id,
        reason=reason,
        description=(
            f"{plan_name} subscription for "
            f"{user.full_name or user.email if user else 'Unknown user'} "
            f"was cancelled by admin override."
        ),
        old_value=old_value,
        new_value="Status: cancelled",
    )

    db.session.commit()

    flash("Subscription cancelled by admin override.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))


@accounts_bp.route("/subscriptions/bulk-cancel-pending", methods=["POST"])
@admin_required
def bulk_cancel_pending_subscriptions():
    ids = request.form.getlist("subscription_ids")

    count = 0

    for sid in ids:
        sub = Subscription.query.get(sid)

        if not sub or sub.status != "pending_payment":
            continue

        user = sub.user
        plan_name = sub.plan.name if sub.plan else "Subscription Plan"

        old_value = f"Status: {sub.status}; Plan: {plan_name}"

        sub.status = "cancelled"

        create_audit_log(
            module="Subscription",
            action="Pending Subscription Cancelled",
            admin_id=current_user.id,
            user_id=sub.user_id,
            entity_type="Subscription",
            entity_id=sub.id,
            reason="Bulk cancellation",
            description=(
                f"Pending subscription {plan_name} for "
                f"{user.full_name or user.email if user else 'Unknown user'} "
                f"was cancelled through bulk cancellation."
            ),
            old_value=old_value,
            new_value="Status: cancelled",
        )

        count += 1

    db.session.commit()

    flash(f"{count} pending subscriptions cancelled.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))


@accounts_bp.route("/subscriptions/bulk-refund-unused", methods=["POST"])
@admin_required
def bulk_refund_unused_subscriptions():
    ids = request.form.getlist("subscription_ids")

    count = 0

    for sid in ids:
        sub = Subscription.query.get(sid)
        if not sub or sub.status != "active":
            continue

        usage = sub.usage
        packages_used = int(getattr(usage, "packages_used", 0) or 0)
        weight_used = float(getattr(usage, "weight_used", 0) or 0)

        if packages_used == 0 and weight_used == 0:
            user = sub.user
            plan_name = sub.plan.name if sub.plan else "Subscription Plan"
            amount_jmd = float(sub.plan.price_usd or 0) * 162

            old_value = f"Status: {sub.status}; Plan: {plan_name}; Used: {packages_used} package(s), {weight_used:.1f} lb"

            sub.status = "cancelled"

            refund = Payment(
                user_id=sub.user_id,
                amount_jmd=-amount_jmd,
                method="Subscription Refund",
                status="completed",
                transaction_type="subscription_refund",
                reference=f"Refund for {plan_name}",
                authorized_by_admin_id=current_user.id,
            )

            db.session.add(refund)

            create_audit_log(
                module="Subscription",
                action="Subscription Refunded",
                admin_id=current_user.id,
                user_id=sub.user_id,
                entity_type="Subscription",
                entity_id=sub.id,
                reason="Unused subscription refund",
                description=(
                    f"{plan_name} subscription for "
                    f"{user.full_name or user.email if user else 'Unknown user'} "
                    f"was refunded because no packages/weight were used. Refund amount: JMD {amount_jmd:.2f}."
                ),
                old_value=old_value,
                new_value=f"Status: cancelled; Refund: JMD {amount_jmd:.2f}",
            )

            count += 1

    db.session.commit()

    flash(f"{count} subscriptions refunded.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))

@accounts_bp.route("/subscriptions/bulk-override-cancel", methods=["POST"])
@admin_required
def bulk_override_cancel_subscriptions():
    ids = request.form.getlist("subscription_ids")

    count = 0

    for sid in ids:
        sub = Subscription.query.get(sid)
        if not sub:
            continue

        user = sub.user
        plan_name = sub.plan.name if sub.plan else "Subscription Plan"
        old_value = f"Status: {sub.status}; Plan: {plan_name}"

        sub.status = "cancelled"

        note = Payment(
            user_id=sub.user_id,
            amount_jmd=0,
            method="Admin Override",
            status="completed",
            transaction_type="subscription_cancel_override",
            reference=f"Override cancel subscription #{sub.id}",
            authorized_by_admin_id=current_user.id,
        )

        db.session.add(note)

        create_audit_log(
            module="Subscription",
            action="Subscription Cancelled",
            admin_id=current_user.id,
            user_id=sub.user_id,
            entity_type="Subscription",
            entity_id=sub.id,
            reason="Bulk admin override cancellation",
            description=(
                f"{plan_name} subscription for "
                f"{user.full_name or user.email if user else 'Unknown user'} "
                f"was cancelled by bulk admin override."
            ),
            old_value=old_value,
            new_value="Status: cancelled",
        )

        count += 1

    db.session.commit()

    flash(f"{count} subscriptions override cancelled.", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))

@accounts_bp.route("/subscriptions/bulk-upgrade", methods=["POST"])
@admin_required
def bulk_upgrade_subscriptions():
    subscription_ids = [
        int(x)
        for x in request.form.getlist("subscription_ids")
        if str(x).isdigit()
    ]

    new_plan_id = request.form.get("new_plan_id", type=int)

    if not subscription_ids:
        flash("No subscriptions selected.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    if not new_plan_id:
        flash("Please select a plan to upgrade to.", "warning")
        return redirect(url_for("accounts_profiles.admin_subscriptions"))

    new_plan = SubscriptionPlan.query.get_or_404(new_plan_id)

    upgraded = 0
    skipped = 0
    blocked_usage = 0

    for sub_id in subscription_ids:
        old_sub = Subscription.query.get(sub_id)

        if not old_sub or old_sub.status != "active":
            skipped += 1
            continue

        old_plan = old_sub.plan

        if not old_plan:
            skipped += 1
            continue

        old_price = float(old_plan.price_usd or 0)
        new_price = float(new_plan.price_usd or 0)

        if new_price <= old_price:
            skipped += 1
            continue

        old_usage = old_sub.usage

        packages_used = int(getattr(old_usage, "packages_used", 0) or 0)
        weight_used = float(getattr(old_usage, "weight_used", 0) or 0)

        old_package_limit = int(old_plan.package_limit or 0)
        old_weight_limit = float(old_plan.weight_limit or 0)

        package_used_percent = (
            packages_used / old_package_limit
        ) if old_package_limit else 0

        weight_used_percent = (
            weight_used / old_weight_limit
        ) if old_weight_limit else 0

        # Block upgrade after 60% usage
        if package_used_percent >= 0.60 or weight_used_percent >= 0.60:
            blocked_usage += 1
            continue

        difference_usd = new_price - old_price
        difference_jmd = difference_usd * 162

        user = old_sub.user

        old_value = (
            f"Subscription #{old_sub.id}; "
            f"Plan: {old_plan.name}; "
            f"Status: {old_sub.status}; "
            f"Price: US${old_price:.2f}; "
            f"Usage: {packages_used} package(s), {weight_used:.1f} lb"
        )

        old_sub.status = "expired"

        new_sub = Subscription(
            user_id=old_sub.user_id,
            plan_id=new_plan.id,
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc) + timedelta(days=30),
            status="active"
        )

        db.session.add(new_sub)
        db.session.flush()

        db.session.add(SubscriptionUsage(
            subscription_id=new_sub.id,
            packages_used=packages_used,
            weight_used=weight_used,
        ))

        if getattr(new_plan, "is_family_plan", False):
            db.session.add(SubscriptionMember(
                subscription_id=new_sub.id,
                user_id=old_sub.user_id,
                role="owner",
                status="active"
            ))

        invoice = Invoice(
            user_id=old_sub.user_id,
            invoice_number=f"SUB-UPG-{new_sub.id:05d}",
            description=f"Upgrade from {old_plan.name} to {new_plan.name} Subscription Plan",
            amount=difference_jmd,
            grand_total=difference_jmd,
            amount_due=0.0,
            status="paid",
            date_issued=datetime.now(timezone.utc),
            date_paid=datetime.now(timezone.utc),
        )

        db.session.add(invoice)
        db.session.flush()

        db.session.add(Payment(
            user_id=old_sub.user_id,
            invoice_id=invoice.id,
            amount_jmd=difference_jmd,
            method="Subscription Upgrade",
            status="completed",
            transaction_type="subscription_upgrade_payment",
            reference=f"Upgrade from {old_plan.name} to {new_plan.name}",
            notes=(
                f"Paid difference only: US${difference_usd:.2f}. "
                f"Usage carried forward: {packages_used} package(s), {weight_used:.1f} lb."
            ),
            authorized_by_admin_id=current_user.id,
        ))

        create_audit_log(
            module="Subscription",
            action="Subscription Upgraded",
            admin_id=current_user.id,
            user_id=old_sub.user_id,
            entity_type="Subscription",
            entity_id=new_sub.id,
            reason="Subscription upgrade",
            description=(
                f"{user.full_name or user.email if user else 'Unknown user'} "
                f"was upgraded from {old_plan.name} to {new_plan.name}. "
                f"Paid difference: JMD {difference_jmd:.2f}. "
                f"Usage carried forward: {packages_used} package(s), {weight_used:.1f} lb. "
                f"Invoice created: {invoice.invoice_number}."
            ),
            old_value=old_value,
            new_value=(
                f"Subscription #{new_sub.id}; "
                f"Plan: {new_plan.name}; "
                f"Status: active; "
                f"Price: US${new_price:.2f}; "
                f"Invoice: {invoice.invoice_number}"
            ),
        )

        upgraded += 1

    db.session.commit()

    if upgraded:
        flash(
            f"{upgraded} subscription(s) upgraded successfully. "
            f"{blocked_usage} blocked because they are over 60% usage. "
            f"{skipped} skipped.",
            "success"
        )
    else:
        flash(
            f"No subscriptions upgraded. "
            f"{blocked_usage} blocked because they are over 60% usage. "
            f"{skipped} skipped.",
            "warning"
        )

    return redirect(url_for("accounts_profiles.admin_subscriptions"))

@accounts_bp.route("/subscriptions/process-reminders")
@login_required
@admin_required
def process_subscription_reminders():
    from datetime import datetime, timezone
    from app.utils.email_utils import send_email

    now = datetime.now(timezone.utc)

    subscriptions = (
        Subscription.query
        .filter(Subscription.status.in_(["active", "exhausted"]))
        .all()
    )

    sent_count = 0

    for sub in subscriptions:
        if not sub.end_date:
            continue

        end_date = sub.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        delta = end_date - now
        days_remaining = delta.days

        user = sub.user
        if not user or not user.email:
            continue

        # 5-day reminder
        if days_remaining <= 5 and days_remaining > 2 and not sub.renewal_reminder_5d_sent:
            send_email(
                to_email=user.email,
                subject="Your FAFL Subscription Expires Soon",
                plain_body=f"""
Hi {user.full_name},

Your Foreign A Foot Logistics subscription expires in {days_remaining} day(s).

Please renew soon to continue enjoying your subscription benefits.

Login here:
https://app.faflcourier.com/customer/subscriptions

— Foreign A Foot Logistics Limited
""".strip(),
                html_body=f"""
<p>Hi {user.full_name},</p>

<p>Your <strong>Foreign A Foot Logistics</strong> subscription expires in <strong>{days_remaining} day(s)</strong>.</p>

<p>Please renew soon to continue enjoying your subscription benefits.</p>

<p>
  <a href="https://app.faflcourier.com/customer/subscriptions"
     style="background:#4A148C;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;font-weight:600;">
    Renew Subscription
  </a>
</p>
""".strip(),
                recipient_user_id=user.id,
            )

            sub.renewal_reminder_5d_sent = True
            sent_count += 1

        # 2-day reminder
        elif days_remaining <= 2 and days_remaining >= 0 and not sub.renewal_reminder_2d_sent:
            send_email(
                to_email=user.email,
                subject="Final Reminder: Your FAFL Subscription Is Expiring",
                plain_body=f"""
Hi {user.full_name},

Your Foreign A Foot Logistics subscription expires in {days_remaining} day(s).

Please renew to avoid interruption to your subscription benefits.

Login here:
https://app.faflcourier.com/customer/subscriptions

— Foreign A Foot Logistics Limited
""".strip(),
                html_body=f"""
<p>Hi {user.full_name},</p>

<p>Your <strong>Foreign A Foot Logistics</strong> subscription expires in <strong>{days_remaining} day(s)</strong>.</p>

<p>Please renew to avoid interruption to your subscription benefits.</p>

<p>
  <a href="https://app.faflcourier.com/customer/subscriptions"
     style="background:#4A148C;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;font-weight:600;">
    Renew Subscription
  </a>
</p>
""".strip(),
                recipient_user_id=user.id,
            )

            sub.renewal_reminder_2d_sent = True
            sent_count += 1

        # Expired notice
        elif days_remaining < 0 and not sub.expiry_notice_sent:
            sub.status = "expired"
            sub.renewal_reminder_5d_sent = True
            sub.renewal_reminder_2d_sent = True

            send_email(
                to_email=user.email,
                subject="Your FAFL Subscription Has Expired",
                plain_body=f"""
Hi {user.full_name},

Your Foreign A Foot Logistics subscription has expired.

Normal package pricing now applies. You may renew anytime from your dashboard.

Login here:
https://app.faflcourier.com/customer/subscriptions

— Foreign A Foot Logistics Limited
""".strip(),
                html_body=f"""
<p>Hi {user.full_name},</p>

<p>Your <strong>Foreign A Foot Logistics</strong> subscription has expired.</p>

<p>Normal package pricing now applies. You may renew anytime from your dashboard.</p>

<p>
  <a href="https://app.faflcourier.com/customer/subscriptions"
     style="background:#4A148C;color:#fff;padding:12px 18px;text-decoration:none;border-radius:6px;font-weight:600;">
    Renew Subscription
  </a>
</p>
""".strip(),
                recipient_user_id=user.id,
            )

            sub.expiry_notice_sent = True
            sent_count += 1

    db.session.commit()

    flash(f"Processed subscription reminders. Emails sent: {sent_count}", "success")
    return redirect(url_for("accounts_profiles.admin_subscriptions"))


@accounts_bp.route("/audit-logs")
@admin_required
def audit_logs():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 25, type=int)

    if per_page not in [10, 25, 50, 100]:
        per_page = 25

    module = (request.args.get("module") or "").strip()
    search = (request.args.get("search") or "").strip()

    q = AuditLog.query

    if module:
        q = q.filter(AuditLog.module == module)

    if search:
        like = f"%{search}%"
        q = q.outerjoin(User, AuditLog.user_id == User.id).filter(or_(
            AuditLog.action.ilike(like),
            AuditLog.description.ilike(like),
            AuditLog.entity_type.ilike(like),
            User.full_name.ilike(like),
            User.email.ilike(like),
            User.registration_number.ilike(like),
        ))

    q = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())

    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        "admin/audit_logs/index.html",
        logs=pagination.items,
        pagination=pagination,
        page=page,
        per_page=per_page,
        module=module,
        search=search,
    )