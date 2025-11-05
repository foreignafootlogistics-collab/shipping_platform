# app/routes/accounts_profiles_routes.py
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, send_file, current_app
)
import sqlite3
import os
import uuid
import json
import pandas as pd
from datetime import datetime
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

from app.forms import UploadUsersForm, ConfirmUploadForm
from app.utils import next_registration_number

from app.models import User, db
from app.routes.admin_auth_routes import admin_required

accounts_bp = Blueprint('accounts_profiles', __name__)

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
# Preview Blob (large data lives on disk instead of cookie/session)
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
# SQL Helpers
# -------------------------
def _table_cols(cur: sqlite3.Cursor, table: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}

# -------------------------
# Manage Users (list/search/sort/paginate + upload preview)
# -------------------------
@accounts_bp.route('/manage-users', methods=['GET', 'POST'])
@admin_required
def manage_users():
    page      = request.args.get('page', 1, type=int)
    per_page  = 20
    offset    = (page - 1) * per_page
    search    = (request.args.get('search') or '').strip()
    sort_by   = (request.args.get('sort') or 'recent').strip()
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')

    upload_form   = UploadUsersForm()
    confirm_form  = ConfirmUploadForm()

    # Load preview token (large payload on disk)
    preview_token = session.get('preview_users_token')
    excel_preview = None
    if preview_token:
        blob = _load_user_preview_blob(preview_token)
        if blob:
            excel_preview = blob.get("preview_rows", [])
        else:
            session.pop('preview_users_token', None)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # WHERE
    where = "WHERE 1=1"
    params = []
    if search:
        like = f"%{search.lower()}%"
        where += """
            AND (
                LOWER(u.full_name) LIKE ?
                OR LOWER(u.email) LIKE ?
                OR LOWER(u.registration_number) LIKE ?
                OR LOWER(IFNULL(u.address, '')) LIKE ?
            )
        """
        params.extend([like, like, like, like])

    if date_from:
        where += " AND date(u.date_registered) >= ?"
        params.append(date_from)
    if date_to:
        where += " AND date(u.date_registered) <= ?"
        params.append(date_to)

    # Total
    c.execute(f"SELECT COUNT(*) AS cnt FROM users u {where}", params)
    total_users = int(c.fetchone()['cnt'] or 0)
    total_pages = max((total_users + per_page - 1) // per_page, 1)

    # ORDER BY
    if sort_by == 'alphabetical_asc':
        order_by = "u.registration_number COLLATE NOCASE ASC"
    elif sort_by == 'alphabetical_desc':
        order_by = "u.registration_number COLLATE NOCASE DESC"
    else:
        order_by = """
            (u.date_registered IS NULL) ASC,
            u.date_registered DESC,
            u.created_at DESC,
            u.id DESC
        """

    # Fetch page
    c.execute(f"""
        SELECT
            u.id, u.full_name, u.email, u.registration_number,
            u.date_registered, u.address, u.mobile,
            (
                SELECT COUNT(*)
                FROM invoices i
                WHERE i.user_id = u.id AND i.status IN ('pending','unpaid')
            ) AS unpaid_count
        FROM users u
        {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """, params + [per_page, offset])
    users = [dict(row) for row in c.fetchall()]
    conn.close()

    for u in users:
        u['unpaid_count'] = int(u.get('unpaid_count') or 0)

    return render_template(
        'admin/accounts_profiles/manage_users.html',
        users=users,
        page=page,
        total_pages=total_pages,
        search=search,
        sort_by=sort_by,
        date_from=date_from,
        date_to=date_to,
        form=upload_form,
        confirm_form=confirm_form,
        excel_preview=excel_preview
    )

# -------------------------
# Sensitive Info (TRN/Address)
# -------------------------
@accounts_bp.route('/users/<int:id>/sensitive', methods=['GET'])
@admin_required
def sensitive_info(id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT trn, address, full_name FROM users WHERE id = ?", (id,))
    user = c.fetchone()
    conn.close()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.view_users'))

    return render_template('admin/accounts_profiles/sensitive_info.html', user=user)

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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email=? OR trn=?", (email, trn))
    if c.fetchone():
        conn.close()
        flash("Email or TRN already exists.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))

    reg_no = next_registration_number()
    hashed = bcrypt.hashpw(raw_password.encode(), bcrypt.gensalt()).decode()

    c.execute("""
        INSERT INTO users (full_name, email, trn, mobile, password, registration_number, date_registered)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (full_name, email, trn, mobile, hashed, reg_no, date_registered))
    conn.commit()
    conn.close()

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
            # Excel ordinal hack
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

    # compute next FAFL*
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT CAST(SUBSTR(registration_number, 5) AS INTEGER) AS n
        FROM users
        WHERE registration_number LIKE 'FAFL%' AND LENGTH(registration_number) > 4
        ORDER BY n DESC
        LIMIT 1
    """)
    row = c.fetchone()
    current_max = int(row['n']) if row and row['n'] is not None else 10000
    next_num = current_max + 1
    conn.close()

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

        # decide reg #
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT registration_number FROM users WHERE email = ? OR trn = ?", (email, trn))
        exist = c.fetchone()
        conn.close()

        if exist:
            assigned_reg = exist['registration_number'] or "(keep)"
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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_reg_unique ON users(registration_number)")

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
        hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode('utf-8')

        if will_update:
            c.execute("SELECT id FROM users WHERE email = ? OR trn = ?", (email, trn))
            rec = c.fetchone()
            if rec:
                user_id = rec[0]
                c.execute("""
                    UPDATE users
                       SET full_name = ?, trn = ?, mobile = ?, password = ?, date_registered = ?, email = ?
                     WHERE id = ?
                """, (full_name, trn, mobile, hashed_pw, date_registered, email, user_id))
                updated += 1
            else:
                # treat as new if vanished meanwhile
                try:
                    c.execute("""
                        INSERT INTO users (full_name, email, trn, mobile, password, date_registered, registration_number)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (full_name, email, trn, mobile, hashed_pw, date_registered, assigned_reg))
                    imported += 1
                except sqlite3.IntegrityError:
                    errors.append(f"Reg # conflict for {email} ({assigned_reg}).")
        else:
            # insert with assigned_reg; on conflict, pick next available
            try:
                c.execute("""
                    INSERT INTO users (full_name, email, trn, mobile, password, date_registered, registration_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (full_name, email, trn, mobile, hashed_pw, date_registered, assigned_reg))
                imported += 1
            except sqlite3.IntegrityError:
                c.execute("""
                    SELECT CAST(SUBSTR(registration_number, 5) AS INTEGER) AS n
                    FROM users
                    WHERE registration_number LIKE 'FAFL%' AND LENGTH(registration_number) > 4
                    ORDER BY n DESC
                    LIMIT 1
                """)
                rowmax = c.fetchone()
                max_now = int(rowmax[0]) if rowmax and rowmax[0] is not None else 10000
                new_reg = f"FAFL{max_now + 1}"
                try:
                    c.execute("""
                        INSERT INTO users (full_name, email, trn, mobile, password, date_registered, registration_number)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (full_name, email, trn, mobile, hashed_pw, date_registered, new_reg))
                    imported += 1
                except sqlite3.IntegrityError:
                    errors.append(f"Could not assign unique reg # for {email}.")
                    continue

    conn.commit()
    conn.close()

    # Clear token + file
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

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = "SELECT full_name, email, registration_number, trn, date_registered FROM users WHERE 1=1"
    params = []

    if search:
        like = f"%{search}%"
        query += " AND (full_name LIKE ? OR email LIKE ? OR registration_number LIKE ? OR address LIKE ?)"
        params.extend([like, like, like, like])
    if date_from:
        query += " AND date(date_registered) >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date(date_registered) <= ?"
        params.append(date_to)

    if sort_by == 'alphabetical':
        query += " ORDER BY full_name COLLATE NOCASE ASC"
    else:
        query += " ORDER BY date_registered DESC"

    c.execute(query, params)
    users = c.fetchall()
    conn.close()

    # CSV
    if export_format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Name', 'Email', 'Reg #', 'TRN', 'Date Registered'])
        for u in users:
            writer.writerow([u['full_name'], u['email'], u['registration_number'], u['trn'], u['date_registered']])
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
        for u in users:
            data.append([u['full_name'], u['email'], u['registration_number'], u['trn'], u['date_registered']])
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
        for row_num, user in enumerate(users, start=1):
            worksheet.write_row(row_num, 0, [
                user['full_name'], user['email'], user['registration_number'], user['trn'], user['date_registered']
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
# View User (with safe/dynamic packages + invoices/payments)
# -------------------------
@accounts_bp.route('/users/<int:id>', methods=['GET', 'POST'])
@admin_required
def view_user(id):
    user_id = int(id)
    import math
    from datetime import datetime as _dt

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # -- User
    c.execute("SELECT * FROM users WHERE id = ?", (id,))
    user_row = c.fetchone()
    if not user_row:
        conn.close()
        flash("User not found.", "danger")
        return redirect(url_for('accounts_profiles.manage_users'))
    user = dict(user_row)
    user_id = user['id']

    # Addresses
    us_address = {
        "recipient": user["full_name"],
        "address_line1": "4652 N Hiatus Rd",
        "address_line2": f"{user.get('registration_number', '')} A",
        "city": "Sunrise",
        "state": "Florida",
        "zip": "33351",
    }
    home_address = user.get('address')

    # -- Messages
    c.execute("""
        SELECT subject, body, date_sent
        FROM messages
        WHERE recipient_id = ? OR sender_id = ?
        ORDER BY date_sent DESC
    """, (id, id))
    messages = c.fetchall()

    # -- Prealerts
    c.execute("""
        SELECT
            prealert_number,
            vendor_name,
            courier_name,
            tracking_number,
            purchase_date,
            package_contents,
            item_value_usd,
            invoice_filename,
            created_at
        FROM prealerts
        WHERE customer_id = ?
        ORDER BY created_at DESC
    """, (id,))
    prealerts = c.fetchall()

    # ------- Helpers for safe column usage
    def _table_cols(cur, table):
        cur.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}

    pkg_cols = _table_cols(c, "packages")

    date_candidates = ["date_received", "received_date", "received_at", "created_at"]
    date_parts = [f"p.{col}" for col in date_candidates if col in pkg_cols]
    date_expr = "COALESCE(" + ", ".join(date_parts + ["NULL"]) + ")"

    declared_candidates = ["value", "invoice_value", "declared_value", "item_value_usd"]
    decl_parts = [f"p.{col}" for col in declared_candidates if col in pkg_cols]
    if not decl_parts:
        decl_parts = ["0"]
    declared_expr = "COALESCE(" + ", ".join(decl_parts) + ", 0)"

    # -------- PACKAGES PAGINATION (10 per page)
    pkg_page = request.args.get("pkg_page", 1, type=int)
    pkg_per_page = 10
    pkg_offset = (pkg_page - 1) * pkg_per_page

    # total count
    c.execute("SELECT COUNT(*) AS cnt FROM packages p WHERE p.user_id = ?", (id,))
    total_pkgs = int(c.fetchone()["cnt"] or 0)
    pkg_total_pages = max((total_pkgs + pkg_per_page - 1) // pkg_per_page, 1)

    # paged rows
    sql = f"""
        SELECT
            p.id,
            p.user_id,
            p.house_awb,
            p.status,
            p.description,
            p.tracking_number,
            p.weight,
            {date_expr}     AS date_received,
            {declared_expr} AS declared_value,
            COALESCE(p.amount_due, 0) AS amount_due,
            p.invoice_file
        FROM packages p
        WHERE p.user_id = ?
        ORDER BY {date_expr} DESC, p.id DESC
        LIMIT ? OFFSET ?
    """
    c.execute(sql, (id, pkg_per_page, pkg_offset))
    pkg_rows = c.fetchall()

    def _fmt_date(v):
        if not v: return None
        if isinstance(v, _dt): return v.strftime("%Y-%m-%d")
        try:
            return _dt.fromisoformat(str(v)).strftime("%Y-%m-%d")
        except Exception:
            return str(v)

    packages = []
    for r in pkg_rows:
        d = dict(r)
        d["date_received"] = _fmt_date(d.get("date_received"))
        # round weight UP to whole number
        try:
            d["weight"] = math.ceil(float(d.get("weight") or 0))
        except Exception:
            d["weight"] = 0
        # ensure declared_value is numeric
        try:
            d["declared_value"] = float(d.get("declared_value") or 0)
        except Exception:
            d["declared_value"] = 0.0
        packages.append(d)

    user_id = int(id)

    # -- Invoices (uses only existing columns + consistent ordering)
    c.execute("""
        SELECT
            id,
            invoice_number,
            LOWER(status) AS status,
            /* For display, prefer amount_due (open) -> grand_total -> amount */
            COALESCE(amount_due, grand_total, amount, 0) AS amount_display,
            /* Normalize a display date: prefer issued -> submitted -> created */
            COALESCE(date_issued, date_submitted, created_at) AS date_display
        FROM invoices
        WHERE user_id = ?
        ORDER BY DATE(COALESCE(date_issued, date_submitted, created_at)) DESC,
                 id DESC
    """, (user_id,))  # <-- make sure you're passing user_id here
    invoices = c.fetchall()

    # Totals
    # Outstanding (open) balance = sum of amount_due for open statuses
    c.execute("""
        SELECT IFNULL(SUM(amount_due), 0)
        FROM invoices
        WHERE user_id = ?
          AND amount_due > 0
          AND LOWER(status) IN ('pending','issued')
    """, (user_id,))
    row = c.fetchone()
    total_owed = float(row[0] if row else 0.0)

    # Total paid to date = sum of amount on paid invoices
    c.execute("""
        SELECT IFNULL(SUM(amount), 0)
        FROM invoices
        WHERE user_id = ?
          AND LOWER(status) = 'paid'
    """, (user_id,))
    total_paid = float(c.fetchone()[0] or 0.0)

    # What you call “balance” in the UI:
    # If you intend “current outstanding,” just show total_owed.
    # If you intend “lifetime owed minus lifetime paid,” keep this, but it mixes time windows.
    balance = total_owed  # or: total_owed - 0.0 if you prefer to be explicit



    # -- Payments (joined by invoice_id -> invoices.user_id)
    c.execute("""
        SELECT
            p.id AS bill_number,
            p.payment_date,
            p.payment_type,
            p.amount,
            p.authorized_by,
            i.file_path AS invoice_path
        FROM payments p
        LEFT JOIN invoices i ON p.invoice_id = i.id
        WHERE i.user_id = ?
        ORDER BY p.payment_date DESC
    """, (id,))
    payments = c.fetchall()

    # Wallet & referral defaults
    wallet_balance = user.get('wallet_balance') if user.get('wallet_balance') is not None else 0.0
    referral_code  = user.get('referral_code') or ''
    user['wallet_balance'] = wallet_balance
    user['referral_code']  = referral_code

    conn.close()

    active_tab = request.args.get("tab", "packages")

    # For "showing X–Y" helper
    pkg_show_from = 0 if total_pkgs == 0 else ((pkg_page - 1) * pkg_per_page + 1)
    pkg_show_to   = min(pkg_page * pkg_per_page, total_pkgs)

    return render_template(
        'admin/accounts_profiles/view_user.html',
        user=user,
        user_id=id,
        prealerts=prealerts,
        packages=packages,               # paginated slice
        invoices=invoices,
        payments=payments,
        total_owed=total_owed,
        total_paid=total_paid,
        balance=balance,
        messages=messages,
        wallet_balance=wallet_balance,
        referral_code=referral_code,        
        us_address=us_address,
        home_address=home_address,
        active_tab=active_tab,

        # pagination vars for Packages tab
        pkg_page=pkg_page,
        pkg_total_pages=pkg_total_pages,
        pkg_show_from=pkg_show_from,
        pkg_show_to=pkg_show_to,
        total_pkgs=total_pkgs
    )


# -------------------------
# Change Password
# -------------------------
@accounts_bp.route('/change-password/<int:id>', methods=['GET', 'POST'])
@admin_required
def change_password(id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, full_name FROM users WHERE id = ?", (id,))
    user = c.fetchone()
    conn.close()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("accounts_profiles.manage_users"))

    if request.method == 'POST':
        new_password = request.form['password']
        confirm_password = request.form.get('confirm_password')

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('admin/accounts_profiles/change_password.html', user=user)

        hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, id))
        conn.commit()
        conn.close()

        flash(f"Password updated for {user[1]}.", "success")
        return redirect(url_for("accounts_profiles.view_user", id=id))

    return render_template('admin/accounts_profiles/change_password.html', user=user)

# -------------------------
# Delete Account
# -------------------------
@accounts_bp.route('/delete-account/<int:id>', methods=['GET', 'POST'])
@admin_required
def delete_account(id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, full_name FROM users WHERE id = ?", (id,))
    user = c.fetchone()
    conn.close()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("accounts_profiles.manage_users"))

    if request.method == 'POST':
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE id = ?", (id,))
        conn.commit()
        conn.close()

        flash(f"Account for {user[1]} has been deleted.", "success")
        return redirect(url_for("accounts_profiles.manage_users"))

    return render_template('admin/accounts_profiles/delete_account.html', user=user)

# -------------------------
# Manage Account (SQLAlchemy-backed simple editor)
# -------------------------
@accounts_bp.route('/manage_account/<int:id>', methods=['GET', 'POST'])
@admin_required
def manage_account(id: int):
    user = User.query.get_or_404(id)

    if request.method == 'POST':
        user.full_name = request.form.get('full_name')
        user.email = request.form.get('email')
        user.mobile = request.form.get('mobile')
        user.address = request.form.get('address')
        user.referral_code = request.form.get('referral_code')
        user.authorized_person = request.form.get('authorized_person')
        user.is_active = bool(int(request.form.get('is_active', 0)))
        db.session.commit()

        flash('Account updated successfully.', 'success')
        return redirect(url_for('accounts_profiles.manage_account', id=id))

    return render_template('accounts_profiles/manage_account.html', user=user)

