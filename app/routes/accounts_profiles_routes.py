# app/routes/accounts_profiles_routes.py
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, send_file, current_app, jsonify
)
import os
import uuid
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
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
from sqlalchemy import func, or_, and_, select
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlparse, urljoin

from app.forms import UploadUsersForm, ConfirmUploadForm
from app.extensions import db
from app.routes.admin_auth_routes import admin_required
from app.calculator_data import CATEGORIES
from app.utils.time import to_jamaica
from app.utils.messages import make_thread_key

# Models (these exist in your file)
from app.models import (
    User, Invoice, Payment, Package, Prealert,
    Message as DBMessage,  # ✅ alias it like customer_routes.py
    Settings, PackageAttachment, ScheduledDelivery
)

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

    # ✅ NEW: unpaid-only filter flag (checkbox sends unpaid_only=1)
    unpaid_only = (request.args.get('unpaid_only') == '1')

    upload_form  = UploadUsersForm()
    confirm_form = ConfirmUploadForm()

    # Load preview token (large payload on disk)
    preview_token = session.get('preview_users_token')
    excel_preview = None
    if preview_token:
        blob = _load_user_preview_blob(preview_token)
        if blob:
            excel_preview = blob.get("preview_rows", [])
        else:
            session.pop('preview_users_token', None)

    # ---------------------------------------------------------
    # Build base query (User)
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

    # Filter by date_registered (string in your model)
    if date_from:
        q = q.filter(User.date_registered >= date_from)
    if date_to:
        q = q.filter(User.date_registered <= date_to)

    # ---------------------------------------------------------
    # ✅ NEW: Unpaid-only filter (users with at least 1 unpaid/pending invoice)
    #     We apply this BEFORE pagination so page counts are correct.
    # ---------------------------------------------------------
    if unpaid_only:
        unpaid_user_ids_subq = (
            db.session.query(Invoice.user_id)
            .filter(func.lower(Invoice.status).in_(('pending', 'unpaid')))
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
        # prefer date_registered, then created_at, then id
        if hasattr(User, 'date_registered'):
            q = q.order_by(User.date_registered.desc(), User.id.desc())
        elif hasattr(User, 'created_at'):
            q = q.order_by(User.created_at.desc(), User.id.desc())
        else:
            q = q.order_by(User.id.desc())

    pagination  = q.paginate(page=page, per_page=per_page, error_out=False)
    user_ids    = [u.id for u in pagination.items]

    # ---------------------------------------------------------
    # unpaid_count per user: invoices with status in ('pending','unpaid')
    # ---------------------------------------------------------
    unpaid_map = {uid: 0 for uid in user_ids}
    if user_ids:
        try:
            sub = (
                db.session.query(Invoice.user_id, func.count(Invoice.id))
                .filter(
                    Invoice.user_id.in_(user_ids),
                    func.lower(Invoice.status).in_(('pending', 'unpaid'))
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
        unpaid_only=unpaid_only,  # ✅ NEW: so checkbox stays checked + links preserve it
        form=upload_form,
        confirm_form=confirm_form,
        excel_preview=excel_preview
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

    reg_no = f"FAFL{_current_max_fafl_number() + 1}"

    # store as bytes (LargeBinary)
    hashed = bcrypt.hashpw(raw_password.encode('utf-8'), bcrypt.gensalt())

    # ✅ generate unique referral code
    referral_code = User.generate_referral_code(full_name)
    for _ in range(10):
        if not User.query.filter_by(referral_code=referral_code).first():
            break
        referral_code = User.generate_referral_code(full_name)

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

    current_max = _current_max_fafl_number()
    next_num = current_max + 1

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
            assigned_reg = f"FAFL{next_num}"
            next_num += 1
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
        street       = settings.us_street or "3200 NW 112th Avenue"
        suite_prefix = settings.us_suite_prefix or "KCDA-FAFL# "
        city         = settings.us_city or "Doral"
        state        = settings.us_state or "Florida"
        zip_code     = settings.us_zip or "33172"
    else:
        # Fallback defaults if settings row missing
        street       = "3200 NW 112th Avenue"
        suite_prefix = "KCDA- "
        city         = "Doral"
        state        = "Florida"
        zip_code     = "33172"

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
                "weight": weight,
                "date_received": _fmt_date(date_received),
                "declared_value": declared_value,
                "amount_due": amt_due,
                "invoice_file": getattr(p, "invoice_file", None),
                "attachments": attachments,
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

        paid_sum_col = func.coalesce(func.sum(pay_amount_col), 0.0).label("paid_sum")

        q = (
            db.session.query(Invoice, paid_sum_col)
            .outerjoin(Payment, Payment.invoice_id == Invoice.id)
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
            db.session.query(Invoice, paid_sum_col)
            .outerjoin(Payment, Payment.invoice_id == Invoice.id)
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
                db.literal(None).label("authorized_by"),
                Invoice.invoice_number.label("invoice_number"),
             )
            .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
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
            "profile_picture": getattr(user, "profile_picture", None),
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

        db.session.commit()  # ✅ commit ONCE at the end
    except Exception:
        current_app.logger.exception("[PREALERT->PACKAGE SYNC] failed after single package create")
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
            return render_template('admin/accounts_profiles/change_password.html', user={"id": user.id, "full_name": user.full_name})

        hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())  # bytes
        user.password = hashed_pw
        _safe_commit()

        flash(f"Password updated for {user.full_name or 'user'}.", "success")
        return redirect(_back_to_view_user_url(id))

    return render_template('admin/accounts_profiles/change_password.html', user={"id": user.id, "full_name": user.full_name or ''})


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

    # ✅ where to go back to (from ?next=...)
    raw_next = request.args.get("next") or request.form.get("next")
    next_url = raw_next if _is_safe_next_url(raw_next) else url_for("accounts_profiles.manage_users")

    if request.method == 'POST':
        try:
            db.session.delete(user)
            db.session.commit()
            flash(f"Account for {user.full_name or 'user'} has been deleted.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Failed to delete account: {e}", "danger")

        return redirect(next_url)

    # ✅ include next so the confirm form can POST it back
    return render_template(
        'admin/accounts_profiles/delete_account.html',
        user={"id": user.id, "full_name": user.full_name or ''},
        next=next_url
    )

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

    # UI checkbox/radio -> store into DB field
    # (default enabled if missing)
    is_enabled_val = bool(int(request.form.get('is_active', 1)))

    # --------- basic validation ----------
    if not email:
        flash("Email is required.", "danger")
        return redirect(_back_to_view_user_url(id))

    # --------- email uniqueness check ----------
    # If changing email, ensure no other user already has it
    current_email = (user.email or '').strip().lower()
    if email != current_email:
        existing = db.session.execute(
            select(User.id).where(User.email == email, User.id != user.id)
        ).scalar_one_or_none()
        if existing:
            flash("That email is already in use by another account.", "danger")
            return redirect(_back_to_view_user_url(id))

    # --------- apply updates ----------
    user.full_name = full_name
    user.email = email
    user.mobile = mobile
    user.address = address
    user.referral_code = referral or None     # helps with unique constraint
    user.is_enabled = is_enabled_val          # ✅ real DB column

    try:
        _safe_commit()
        flash("Account updated successfully.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Could not save changes: referral code or email already exists.", "danger")

    return redirect(_back_to_view_user_url(id))


@accounts_bp.route('/users/<int:id>/wallet', methods=['POST'])
@admin_required
def update_wallet(id):
    user = db.session.get(User, id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    amount = float(request.form.get('amount', 0) or 0)
    desc   = (request.form.get('description') or '').strip()

    user.wallet_balance = (user.wallet_balance or 0) + amount
    _safe_commit()

    flash(f"Wallet updated by {amount:+.2f}. New balance: {user.wallet_balance:.2f}", "success")
    return redirect(_back_to_view_user_url(id))
