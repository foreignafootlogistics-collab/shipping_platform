from flask import Blueprint, render_template, request, redirect, url_for, session, flash, make_response, jsonify
import sqlite3
import os
from werkzeug.utils import secure_filename
import openpyxl
from datetime import datetime, timedelta
import smtplib
from email.message import EmailMessage
import bcrypt
import math
from weasyprint import HTML
from app.forms import InvoiceForm
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email
from app.forms import LoginForm, SendMessageForm, AdminLoginForm, BulkMessageForm, UploadPackageForm, SingleRateForm, BulkRateForm, MiniRateForm, AdminProfileForm, AdminRegisterForm, ExpenseForm, WalletUpdateForm, AdminCalculatorForm, PackageBulkActionForm # make sure your LoginForm is imported if in another file
from app.utils import email_utils
from app.routes.admin_auth_routes import admin_required
from app.utils.wallet import update_wallet, update_wallet_balance
from app.config import get_db_connection  # Assuming you have this helper for DB connection
from flask import jsonify, abort
from app.models import db, User, Wallet, Message, ScheduledDelivery, WalletTransaction, Package, Invoice, Notification
from app.utils.invoice_utils import generate_invoice  # We'll define this
from flask_login import login_required, current_user, login_user, logout_user

from app.calculator import calculate_charges
from app.calculator_data import categories
from app.utils.rates import get_rate_for_weight
from app.utils.invoice_pdf import generate_invoice_pdf
from datetime import datetime
from app.calculator_data import calculate_charges, CATEGORIES, USD_TO_JMD
from app.models import Package, Invoice, db
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import io
from flask import send_file
from app.utils.counters import ensure_counters_table, next_bill_number_tx
from calendar import monthrange
from collections import OrderedDict




admin_bp = Blueprint(
    'admin', __name__,
    url_prefix='/admin',
    template_folder='templates/admin'
)

ALLOWED_EXTENSIONS = {'xlsx'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'xlsx'


def format_datetime(value, format='%Y-%m-%d %H:%M:%S'):
    if not value:
        return ''
    if isinstance(value, (int, float)):  # Unix timestamp
        dt = datetime.fromtimestamp(value)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value  # return as-is if not ISO format
    elif isinstance(value, datetime):
        dt = value
    else:
        return value
    return dt.strftime(format)

# Register the filter AFTER defining it
admin_bp.add_app_template_filter(format_datetime, 'datetimeformat')

def format_jmd(v):
    try:
        return f"JMD {float(v):,.2f}"
    except Exception:
        return f"JMD {v}"

admin_bp.add_app_template_filter(format_jmd, 'jmd')

def _num(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0

def _build_invoice_view_dict(inv):
    packages = []
    for p in Package.query.filter_by(invoice_id=inv.id).all():
        packages.append({
            "house_awb":     getattr(p, "house_awb", "") or "",
            "description":   getattr(p, "description", "") or "",
            "weight":        _num(getattr(p, "weight", 0)),
            # value field can be named variously
            "value":         _num(getattr(p, "value", getattr(p, "invoice_value", getattr(p, "value_usd", 0)))),
            # fees – support both naming styles
            "freight":       _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
            "storage":       _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges": _num(getattr(p, "other_charges", 0)),
            # customs breakdown
            "duty":          _num(getattr(p, "duty", 0)),
            "scf":           _num(getattr(p, "scf", 0)),
            "envl":          _num(getattr(p, "envl", 0)),
            "caf":           _num(getattr(p, "caf", 0)),
            "gct":           _num(getattr(p, "gct", 0)),
            "discount_due":  _num(getattr(p, "discount_due", 0)),
        })
    invoice_dict = {
        "id":            inv.id,
        "number":        inv.invoice_number,
        "date":          inv.date_submitted or datetime.utcnow(),
        "customer_code": getattr(inv.user, "registration_number", "") if getattr(inv, "user", None) else "",
        "customer_name": getattr(inv.user, "full_name", "") if getattr(inv, "user", None) else "",
        "subtotal":      _num(getattr(inv, "subtotal", getattr(inv, "grand_total", 0))),
        "discount_total":_num(getattr(inv, "discount_total", 0)),
        "total_due":     _num(getattr(inv, "grand_total", getattr(inv, "amount", 0))),
        "packages":      packages,  
    
# Optional:
        # "branch": "Main Branch",
        # "staff": "FAFL ADMIN",
        # "notes": "Thanks for your business!"
    }
    return invoice_dict

def _money(n):
    try:
        return f"{float(n):,.2f}"
    except Exception:
        return "0.00"

def _column_exists(conn, table, column) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return column in cols

def _fetch_invoice_totals_sql(invoice_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get the invoice row (for bill_id fallback)
    c.execute("SELECT id, bill_id FROM invoices WHERE id=?", (invoice_id,))
    inv = c.fetchone()
    if not inv:
        conn.close()
        return 0.0, 0.0, 0.0, 0.0
    bill_id = inv["bill_id"]

    # ---------- SUBTOTAL ----------
    # Prefer packages.invoice_id if it exists; otherwise subtotal = invoices.grand_total
    if _column_exists(conn, "packages", "invoice_id"):
        c.execute("SELECT COALESCE(SUM(amount_due),0) AS subtotal FROM packages WHERE invoice_id=?", (invoice_id,))
        subtotal = float((c.fetchone() or {"subtotal":0})["subtotal"])
    else:
        c.execute("SELECT COALESCE(grand_total,0) AS subtotal FROM invoices WHERE id=?", (invoice_id,))
        subtotal = float((c.fetchone() or {"subtotal":0})["subtotal"])

    # ---------- DISCOUNTS ----------
    if _column_exists(conn, "invoices", "discount_total"):
        c.execute("SELECT COALESCE(discount_total,0) AS disc FROM invoices WHERE id=?", (invoice_id,))
        discount_total = float((c.fetchone() or {"disc":0})["disc"])
    else:
        discount_total = 0.0

    # ---------- PAYMENTS ----------
    payments_total = 0.0
    if _column_exists(conn, "payments", "invoice_id"):
        c.execute("SELECT COALESCE(SUM(amount),0) AS paid FROM payments WHERE invoice_id=?", (invoice_id,))
        payments_total = float((c.fetchone() or {"paid":0})["paid"])
    elif _column_exists(conn, "payments", "bill_id"):
        # fallback: sum by bill_id via the invoice’s bill_id
        c.execute("SELECT COALESCE(SUM(amount),0) AS paid FROM payments WHERE bill_id=?", (bill_id,))
        payments_total = float((c.fetchone() or {"paid":0})["paid"])

    conn.close()

    total_due = max(subtotal - discount_total - payments_total, 0.0)
    return subtotal, discount_total, payments_total, total_due

def _has_col(conn, table: str, col: str) -> bool:
    """Check if a column exists in a given SQLite table."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return col in [r[1] for r in cur.fetchall()]

def _ensure_counters_table():
    global _COUNTORS_READY
    if _COUNTORS_READY:
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
              name  TEXT PRIMARY KEY,
              value INTEGER NOT NULL
            )
        """)

        # migrate old (key,value) -> (name,value) if needed
        cols = [r[1] for r in c.execute("PRAGMA table_info(counters)").fetchall()]
        if ("key" in cols) and ("name" not in cols):
            c.execute("ALTER TABLE counters RENAME TO counters_old")
            c.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            c.execute("INSERT INTO counters(name,value) SELECT key,value FROM counters_old")
            c.execute("DROP TABLE counters_old")

        # seed rows
        for k in ("shipment_seq", "invoice_seq", "bill_seq"):
            c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,0)", (k,))

        # init shipment_seq from existing sl_id suffix
        mx_ship = c.execute("""
            SELECT COALESCE(MAX(CAST(substr(sl_id, -5) AS INTEGER)), 0) FROM shipment_log
        """).fetchone()[0] or 0
        c.execute("UPDATE counters SET value=? WHERE name='shipment_seq' AND value < ?", (int(mx_ship), int(mx_ship)))

        # 🔧 init bill_seq from existing bills.bill_number suffix (handles BILL00042 or 00042)
        mx_bill = c.execute("""
            SELECT COALESCE(MAX(CAST(substr(bill_number, -5) AS INTEGER)), 0)
            FROM bills
            WHERE bill_number IS NOT NULL AND bill_number <> ''
        """).fetchone()[0] or 0
        c.execute("UPDATE counters SET value=? WHERE name='bill_seq' AND value < ?", (int(mx_bill), int(mx_bill)))

        # helpful indexes
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package ON shipment_packages(package_id)""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_bill_number ON bills(bill_number)""")

        conn.commit()
        _COUNTORS_READY = True
    finally:
        conn.close()

def _next_bill_number_tx(conn) -> str:
    # returns e.g. BILL00001
    cur = conn.cursor()
    cur.execute("UPDATE counters SET value = value + 1 WHERE name='bill_seq'")
    if cur.rowcount == 0:
        cur.execute("INSERT INTO counters(name,value) VALUES('bill_seq',1)")
        seq = 1
    else:
        seq = int(cur.execute("SELECT value FROM counters WHERE name='bill_seq'").fetchone()[0])
    return f"BILL{seq:05d}"

def _create_bill(conn, user_id: int, pkg_ids: list[int], total_amount: float) -> tuple[int, str]:
    """
    Create one row in bills with a globally unique bill_number (BILL00001…).
    Safe against duplicates by allocating inside a short transaction.
    """
    _ensure_counters_table()   # no-op after first call

    now_iso = datetime.utcnow().isoformat()
    first_pkg_id = int(pkg_ids[0])  # satisfy NOT NULL if schema requires one pkg id

    cur = conn.cursor()
    try:
        # allocate a unique number atomically
        conn.isolation_level = None
        cur.execute("BEGIN IMMEDIATE")

        bill_no = _next_bill_number_tx(conn)   # returns e.g. BILL00001

        cur.execute("""
            INSERT INTO bills (
                user_id, package_id, description, amount, status, due_date,
                bill_number, total_amount, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'unpaid', NULL, ?, ?, ?, ?)
        """, (
            user_id,
            first_pkg_id,
            f"Auto bill for {len(pkg_ids)} package(s)",
            float(total_amount),
            bill_no,
            float(total_amount),
            now_iso,
            now_iso
        ))

        conn.commit()
        return cur.lastrowid, bill_no

    except sqlite3.IntegrityError:
        # ultra-rare race: retry once with a new number
        try: cur.execute("ROLLBACK")
        except Exception: pass
        cur.execute("BEGIN IMMEDIATE")
        bill_no = _next_bill_number_tx(conn)
        cur.execute("""
            INSERT INTO bills (
                user_id, package_id, description, amount, status, due_date,
                bill_number, total_amount, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'unpaid', NULL, ?, ?, ?, ?)
        """, (
            user_id, first_pkg_id, f"Auto bill for {len(pkg_ids)} package(s)",
            float(total_amount), bill_no, float(total_amount), now_iso, now_iso
        ))
        conn.commit()
        return cur.lastrowid, bill_no

    finally:
        try: cur.execute("END")
        except Exception: pass

def _last_n_months(n: int = 12):
    """Return a list like ['2024-11','2024-12',...,'2025-10'] ending in the current month."""
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(months))

# ---------------------------
# DB Helper
# ---------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # access columns by name
    return conn



@admin_bp.route('/register-admin', methods=['GET', 'POST'])
@admin_required
def register_admin():
    form = AdminRegisterForm()
    if form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.strip()
        password = form.password.data.strip()

        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        role = "admin"

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Check if email exists
        c.execute("SELECT id FROM users WHERE email = ?", (email,))
        if c.fetchone():
            flash("Email already exists", "danger")
            conn.close()
            return render_template('admin/register_admin.html', form=form)

        # Check if any admin exists already
        c.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        existing_admin = c.fetchone()

        # If no admin yet → give FAFL10000
        if not existing_admin:
            registration_number = "FAFL10000"
        else:
            # Optional: Could block new admin creation or just assign no reg number
            registration_number = None

        try:
            c.execute("""
                INSERT INTO users (full_name, email, password, role, created_at, registration_number)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (full_name, email, hashed_pw, role, created_at, registration_number))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("An unexpected database error occurred.", "danger")
            return render_template('admin/register_admin.html', form=form)
        finally:
            conn.close()

        flash(f"Admin account for {full_name} created successfully!", "success")
        return redirect(url_for('admin.dashboard'))

    return render_template('admin/register_admin.html', form=form)

# ---------- Admin Dashboard ----------
@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    # TEMP: minimal response to confirm login works and redirect is fine
    return "OK: admin logged in", 200
# ---------------------------
# VIEW RATES
# ---------------------------
@admin_bp.route('/rates')
@admin_required
def view_rates():
    search_query = request.args.get('search', '').strip()
    page = int(request.args.get('page', 1))
    per_page = 10

    conn = get_db()
    c = conn.cursor()

    sql = "SELECT id, max_weight, rate FROM rate_brackets"
    params = []

    if search_query:
        sql += " WHERE CAST(max_weight AS TEXT) LIKE ? OR CAST(rate AS TEXT) LIKE ?"
        params.extend([f"%{search_query}%", f"%{search_query}%"])

    sql += " ORDER BY max_weight ASC LIMIT ? OFFSET ?"
    params.extend([per_page, (page - 1) * per_page])
    c.execute(sql, tuple(params))
    rates = c.fetchall()

    # Count total
    count_sql = "SELECT COUNT(*) FROM rate_brackets"
    if search_query:
        count_sql += " WHERE CAST(max_weight AS TEXT) LIKE ? OR CAST(rate AS TEXT) LIKE ?"
        c.execute(count_sql, (f"%{search_query}%", f"%{search_query}%"))
    else:
        c.execute(count_sql)
    total_count = c.fetchone()[0]
    conn.close()

    total_pages = (total_count + per_page - 1) // per_page

    return render_template(
        'admin/rates/view_rates.html',
        rates=rates,
        page=page,
        total_pages=total_pages,
        search_query=search_query
    )

# ---------------------------
# ADD RATE
# ---------------------------
@admin_bp.route('/add-rate', methods=['GET', 'POST'])
@admin_required
def add_rate():    
    form = SingleRateForm()

    if form.validate_on_submit():
        max_weight = float(form.max_weight.data)
        rate = float(form.rate.data)

        conn = get_db()
        c = conn.cursor()

        # Prevent duplicate max_weight
        c.execute("SELECT id FROM rate_brackets WHERE max_weight = ?", (max_weight,))
        if c.fetchone():
            flash(f"A rate for {max_weight} lb already exists.", "danger")
            conn.close()
            return redirect(url_for('admin.view_rates'))

        c.execute("INSERT INTO rate_brackets (max_weight, rate) VALUES (?, ?)", (max_weight, rate))
        conn.commit()
        conn.close()

        flash(f"Rate added: Up to {max_weight} lb → ${rate} JMD", "success")
        return redirect(url_for('admin.view_rates'))

    return render_template('admin/rates/add_rate.html', form=form)

# ---------------------------
# BULK ADD RATES
# ---------------------------
@admin_bp.route('/bulk-add-rates', methods=['GET', 'POST'])
@admin_required
def bulk_add_rates():    
    form = BulkRateForm()

    while len(form.rates) < 10:  # show at least 10 rows
        form.rates.append_entry()

    if form.validate_on_submit():
        conn = get_db()
        c = conn.cursor()
        inserted_count = 0

        for rate_form in form.rates:
            try:
                max_weight = float(rate_form.max_weight.data or 0)
                rate = float(rate_form.rate.data or 0)
                if max_weight <= 0 or rate <= 0:
                    continue

                # Skip duplicates
                c.execute("SELECT id FROM rate_brackets WHERE max_weight = ?", (max_weight,))
                if c.fetchone():
                    continue

                c.execute("INSERT INTO rate_brackets (max_weight, rate) VALUES (?, ?)", (max_weight, rate))
                inserted_count += 1
            except Exception:
                continue

        conn.commit()
        conn.close()
        flash(f"Successfully added {inserted_count} rates.", "success")
        return redirect(url_for('admin.view_rates'))

    return render_template('admin/rates/bulk_add_rates.html', form=form)

# ---------------------------
# EDIT RATE
# ---------------------------
@admin_bp.route('/edit-rate/<int:rate_id>', methods=['GET', 'POST'])
@admin_required
def edit_rate(rate_id):    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, max_weight, rate FROM rate_brackets WHERE id = ?", (rate_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        flash("Rate not found.", "danger")
        return redirect(url_for('admin.view_rates'))

    form = SingleRateForm()

    if form.validate_on_submit():
        max_weight = float(form.max_weight.data)
        rate = float(form.rate.data)

        # Check duplicates for other records
        c.execute("SELECT id FROM rate_brackets WHERE max_weight = ? AND id != ?", (max_weight, rate_id))
        if c.fetchone():
            flash(f"A rate for {max_weight} lb already exists.", "warning")
            conn.close()
            return redirect(url_for('admin.view_rates'))

        c.execute("UPDATE rate_brackets SET max_weight = ?, rate = ? WHERE id = ?", (max_weight, rate, rate_id))
        conn.commit()
        conn.close()

        flash("Rate updated successfully.", "success")
        return redirect(url_for('admin.view_rates'))

    # Pre-fill form on GET
    if request.method == 'GET':
        form.max_weight.data = row['max_weight']
        form.rate.data = row['rate']

    conn.close()
    return render_template('admin/rates/edit_rate.html', form=form)


# ----- Admin Inbox / Sent + Bulk Messaging -----
@admin_bp.route("/messages", methods=["GET", "POST"])
@admin_required
def view_messages():            
    form = SendMessageForm()

    # Populate multi-select choices with all users except admin
    form.recipient_ids.choices = [
        (u.id, f"{u.full_name} ({u.email})") 
        for u in User.query.order_by(User.full_name).all()
    ]

    # Handle sending messages
    if form.validate_on_submit():
        subject = form.subject.data.strip()
        body = form.body.data.strip()
        selected_user_ids = form.recipient_ids.data  # list of ints

        if not selected_user_ids:
            flash("Please select at least one recipient.", "danger")
            return redirect(url_for("admin.view_messages"))

        # Insert messages for each selected user
        for uid in selected_user_ids:
            recipient = User.query.get(uid)
            if recipient:
                msg = Message(
                    sender_id=admin_id,
                    recipient_id=recipient.id,
                    subject=subject,
                    body=body,
                    created_at=datetime.now()
                )
                # Optionally: send email here using your email_utils
                Message.query.session.add(msg)

        Message.query.session.commit()
        flash(f"Message sent to {len(selected_user_ids)} user(s)!", "success")
        return redirect(url_for("admin.view_messages"))

    # Fetch inbox (messages received by admin)
    inbox = Message.query.filter_by(recipient_id=admin_id).order_by(Message.created_at.desc()).all()

    # Add sender label (Admin or Customer)
    for msg in inbox:
        sender = User.query.get(msg.sender_id)
        msg.sender_label = "Admin" if sender and sender.is_admin else "Customer"

    # Fetch sent messages
    sent = Message.query.filter_by(sender_id=admin_id).order_by(Message.created_at.desc()).all()

    return render_template(
        "admin/messages.html",
        form=form,
        inbox=inbox,
        sent=sent
    )

# ----- Mark message as read -----
@admin_bp.route("/messages/mark_read/<int:msg_id>", methods=["POST"])
@admin_required
def mark_message_read(msg_id):
    admin_id = session.get('admin_id')
    msg = Message.query.get_or_404(msg_id)
    if msg.recipient_id != admin_id:
        flash("Not authorized.", "danger")
        return redirect(url_for("admin.view_messages"))

    msg.is_read = True
    Message.query.session.commit()
    flash("Message marked as read.", "success")
    return redirect(url_for("admin.view_messages"))


@admin_bp.route("/notifications", methods=["GET"])
@admin_required
def view_notifications():
    # Get all notifications for admin (or global notifications)
    notes = Notification.query.order_by(Notification.created_at.desc()).all()
    return render_template("admin/notifications.html", notes=notes)

@admin_bp.route("/notifications/mark_read/<int:nid>", methods=["POST"])
@admin_required
def mark_notification_read(nid):
    n = Notification.query.get_or_404(nid)
    n.is_read = True
    db.session.commit()
    flash("Notification marked as read.", "success")
    return redirect(url_for('admin.view_notifications'))



# ---------- GENERATE INVOICE ----------

@admin_bp.route('/generate-invoice/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def generate_invoice(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # --- Fetch user ---
    c.execute("SELECT id, full_name, registration_number FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        flash("User not found.", "danger")
        return redirect(url_for('admin.dashboard'))

    # --- Fetch uninvoiced packages for this user ---
    c.execute("""
        SELECT * FROM packages
        WHERE user_id = ? AND invoice_id IS NULL
        ORDER BY created_at ASC
    """, (user_id,))
    packages = c.fetchall()
    if not packages:
        conn.close()
        flash("No packages available to invoice.", "warning")
        return redirect(url_for('admin.dashboard'))

    # Only generate on POST; on GET just show a minimal confirm page
    if request.method != 'POST':
        conn.close()
        return render_template(
            "admin/invoice_confirm.html",
            user=user,
            packages=packages
        )

    try:
        # ---------- CREATE INVOICE SHELL ----------
        invoice_number = f"INV-{user['id']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        today = datetime.now().strftime("%Y-%m-%d")

        # Insert invoice row (amount_due=0 for now, set after totals)
        c.execute("""
            INSERT INTO invoices (user_id, invoice_number, date_submitted, date_issued, status, amount_due)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user['id'], invoice_number, today, today, "unpaid", 0))
        invoice_id = c.lastrowid

        # ---------- CALCULATE TOTALS + UPDATE PACKAGES ----------
        total_duty = total_scf = total_envl = total_caf = total_gct = total_stamp = 0.0
        total_freight = total_handling = grand_total = 0.0
        package_details = []

        for pkg in packages:
            category = pkg['description'] or "Miscellaneous"
            weight = float(pkg['weight'] or 0)
            value_usd = float(pkg['value'] or 0)

            charges = calculate_charges(category, value_usd, weight)

            # Attach package to invoice + store per-package amount_due
            c.execute("""
                UPDATE packages
                SET invoice_id = ?, amount_due = ?
                WHERE id = ?
            """, (invoice_id, charges["grand_total"], pkg['id']))

            # Aggregate totals
            total_duty     += charges["duty"]
            total_scf      += charges["scf"]
            total_envl     += charges["envl"]
            total_caf      += charges["caf"]
            total_gct      += charges["gct"]
            total_stamp    += charges["stamp"]
            total_freight  += charges["freight"]
            total_handling += charges["handling"]
            grand_total    += charges["grand_total"]

            package_details.append({
                "house_awb":  pkg["house_awb"],
                "description": pkg["description"],
                "weight":      weight,
                "value_usd":   value_usd,
                **charges
            })

        # ---------- FINALIZE INVOICE TOTALS ----------
        c.execute("""
            UPDATE invoices
            SET total_duty = ?, total_scf = ?, total_envl = ?, total_caf = ?,
                total_gct = ?, total_stamp = ?, total_freight = ?, total_handling = ?,
                grand_total = ?, amount_due = ?
            WHERE id = ?
        """, (total_duty, total_scf, total_envl, total_caf,
              total_gct, total_stamp, total_freight, total_handling,
              grand_total, grand_total, invoice_id))

        # ---------- CREATE/LINK BILL (prevents duplicate bill_number) ----------
        # Ensure a unique index (cheap if it already exists)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_bill_number ON bills(bill_number)")

        # Collect the package ids we just invoiced
        pkg_ids = [int(p['id']) for p in packages]  # 'packages' is the list you queried above

        # Allocate a fresh BILL00001… and insert into bills atomically
        bill_id, bill_no = _create_bill(conn, user['id'], pkg_ids, grand_total)

        # Link bill back to the invoice (if these columns exist in your schema)
        c.execute(
            "UPDATE invoices SET bill_id = ?, bill_number = ? WHERE id = ?",
            (bill_id, bill_no, invoice_id)
        )

        conn.commit()

        # ---------- BUILD UNIFIED CONTEXT FOR THE VIEW ----------
        invoice_dict = {
            "id":             invoice_id,
            "number":         invoice_number,
            "date":           today,
            "customer_code":  user["registration_number"],
            "customer_name":  user["full_name"],
            "subtotal":       grand_total,   # if you want a true subtotal, compute and store it too
            "total_due":      grand_total,   # equals amount_due at generation
            "packages": [
                {
                    "house_awb": p["house_awb"],
                    "description": p["description"],
                    "weight": p["weight"],
                    "value": p["value_usd"],
                    "freight": p["freight"],
                    "storage": p.get("storage", 0),
                    "duty": p["duty"],
                    "scf": p["scf"],
                    "envl": p["envl"],
                    "caf": p["caf"],
                    "gct": p["gct"],
                    "other_charges": p.get("other_charges", 0),
                    "discount_due": p.get("discount_due", 0),
                } for p in package_details
            ],
            "totals": {
                "duty": total_duty,
                "scf": total_scf,
                "envl": total_envl,
                "caf": total_caf,
                "gct": total_gct,
                "stamp": total_stamp,
                "freight": total_freight,
                "handling": total_handling,
                "grand_total": grand_total
            }
        }

        flash(f"Invoice {invoice_number} generated successfully!", "success")
        return render_template("admin/invoice_view.html", invoice=invoice_dict)

    except Exception as e:
        conn.rollback()
        flash(f"Error generating invoice: {e}", "danger")
        return redirect(url_for('admin.dashboard'))
    finally:
        conn.close()

@admin_bp.route("/invoice/create/<int:package_id>", methods=["GET", "POST"])
@admin_required
def invoice_create(package_id):
    pkg = Package.query.get_or_404(package_id)
    calc = None

    if request.method == "POST":
        try:
            category      = request.form.get("category") or (pkg.description or "Miscellaneous")
            invoice_usd   = float(request.form.get("invoice_usd", pkg.value or 0))
            weight        = float(request.form.get("weight", pkg.weight or 0))
            # ✅ NEW: capture other charges from the form
            other_charges = float(request.form.get("other_charges", 0))

            # Calculate charges
            calc = calculate_charges(category, invoice_usd, weight)

            # Create invoice (one invoice; link package to it)
            inv = Invoice(
                user_id=pkg.user_id,
                invoice_number=f"INV-{pkg.user_id}-{int(datetime.utcnow().timestamp())}",
                date_submitted=datetime.utcnow(),
                status="pending",
                subtotal=(calc.get("subtotal") or calc.get("grand_total") or 0),
                # ✅ include other_charges in invoice grand total
                grand_total=(calc.get("grand_total") or 0) + other_charges,
            )
            db.session.add(inv)
            db.session.flush()  # get inv.id

            # Persist breakdown to the package and link it
            pkg.category        = category
            pkg.value           = invoice_usd
            pkg.weight          = weight
            pkg.duty            = calc.get("duty", 0)
            pkg.scf             = calc.get("scf", 0)
            pkg.envl            = calc.get("envl", 0)
            pkg.caf             = calc.get("caf", 0)
            pkg.gct             = calc.get("gct", 0)
            pkg.stamp           = calc.get("stamp", 0)
            pkg.customs_total   = calc.get("customs_total", 0)


            # handle freight/storage alternate column names
            freight_val  = calc.get("freight", 0)
            storage_val  = calc.get("handling", 0)
            if hasattr(pkg, "freight_fee"):
                pkg.freight_fee = freight_val
            else:
                pkg.freight = freight_val
            if hasattr(pkg, "storage_fee"):
                pkg.storage_fee = storage_val
            else:
                pkg.handling = storage_val

            pkg.freight_total = calc.get("freight_total", 0)
            pkg.other_charges = calc.get("other_charges", 0)
            pkg.amount_due    = calc.get("grand_total", 0)
            pkg.invoice_id    = inv.id

            db.session.commit()
            flash("Invoice created successfully!", "success")
            return redirect(url_for("admin.invoice_view", invoice_id=inv.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")

    # GET or error state
    return render_template(
        "admin/create_invoice.html",
        package=pkg,
        categories=CATEGORIES.keys(),
        result=calc
    )


# ----- View a customer's current (uninvoiced) items as an invoice-style page -----
@admin_bp.route('/invoices/user/<int:user_id>', methods=['GET'], endpoint='view_customer_invoice')
@admin_required
def view_customer_invoice(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get the user
    c.execute("SELECT id, full_name, registration_number FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        flash("User not found.", "danger")
        return redirect(url_for('admin.dashboard'))

    # Only packages that are NOT yet attached to an invoice (pro-forma style)
    c.execute("""
        SELECT id, house_awb, description, weight, value, created_at
        FROM packages
        WHERE user_id=? AND invoice_id IS NULL
        ORDER BY created_at ASC
    """, (user_id,))
    pkgs = [dict(x) for x in c.fetchall()]
    conn.close()

    # Build line items with the same calculator you already use
    items = []
    totals = dict(duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0, freight=0, handling=0, grand_total=0)
    for p in pkgs:
        desc = p.get("description") or "Miscellaneous"
        wt   = float(p.get("weight") or 0)
        val  = float(p.get("value")  or 0)
        ch   = calculate_charges(desc, val, wt)

        items.append({
            "id": p.get("id"),
            "house_awb": p.get("house_awb"),
            "description": desc,
            "weight": wt,
            "value_usd": val,
            **ch
        })

        # accumulate totals
        for k in totals:
            totals[k] += float(ch.get(k, 0))

    invoice_dict = {
        "id": None,
        "number": f"PROFORMA-{user['id']}",
        "date": datetime.utcnow(),
        "customer_code": user["registration_number"],
        "customer_name": user["full_name"],
        "subtotal": totals["grand_total"],   # if you later split subtotal/fees, adjust here
        "total_due": totals["grand_total"],
        "packages": [{
            "id": i.get("id"),
            "house_awb": i["house_awb"],
            "description": i["description"],
            "weight": i["weight"],
            "value": i["value_usd"],
            "freight": i["freight"],
            "storage": i.get("storage", 0),
            "duty": i["duty"],
            "scf": i["scf"],
            "envl": i["envl"],
            "caf": i["caf"],
            "gct": i["gct"],
            "other_charges": i["other_charges"],
            "discount_due": i.get("discount_due", 0),
        } for i in items]
    }

    # Reuse your invoice template (adjust the path if yours differs)
    return render_template("admin/invoices/_invoice_inline.html",
                           invoice=invoice_dict,
                           USD_TO_JMD=USD_TO_JMD)

# Mark Invoice as Paid
# --------------------------
@admin_bp.route('/invoice/mark_paid', methods=['POST'])
@admin_required
def mark_invoice_paid():
    try:
        invoice_id = int(request.form.get('invoice_id'))
        amount = float(request.form.get('payment_amount'))
        payment_type = request.form.get('payment_type')
        authorized_by = request.form.get('authorized_by')

        invoice = Invoice.query.get_or_404(invoice_id)

        # Mark invoice as paid
        invoice.status = 'paid'

        # Create payment record
        payment = Payment(
            bill_number=f'BILL-{invoice.id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}',
            payment_date=datetime.utcnow(),
            payment_type=payment_type,
            amount=amount,
            authorized_by=authorized_by,
            invoice_id=invoice.id,
            invoice_path=None  # optional: path to payment invoice file if exists
        )
        db.session.add(payment)
        db.session.commit()

        # Build the invoice path for frontend if needed
        invoice_path = payment.invoice_path
        if invoice_path:
            invoice_path = url_for('static', filename=invoice_path)

        return jsonify({
            'success': True,
            'invoice_id': invoice.id,
            'bill_number': payment.bill_number,
            'payment_date': payment.payment_date.strftime('%Y-%m-%d %H:%M:%S'),
            'payment_type': payment.payment_type,
            'amount': payment.amount,
            'authorized_by': payment.authorized_by,
            'invoice_path': invoice_path
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@admin_bp.route('/generate-pdf-invoice/<int:user_id>')
@admin_required
def generate_pdf_invoice(user_id): 
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT full_name, registration_number FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        flash("User not found", "danger")
        return redirect(url_for('admin.dashboard'))

    full_name, registration_number = user

    c.execute("""
        SELECT description, amount, date_submitted, status
        FROM invoices
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 10
    """, (user_id,))
    invoices = c.fetchall()
    conn.close()

    items = []
    total = 0
    for desc, amount, date_submitted, status in invoices:
        items.append({
            "description": f"{desc} ({status})",
            "weight": "—",
            "rate": "—",
            "total": round(amount, 2)
        })
        total += amount

    today = datetime.now().strftime('%B %d, %Y')

    html = render_template("invoice.html",
                           full_name=full_name,
                           registration_number=registration_number,
                           date=today,
                           items=items,
                           grand_total=round(total, 2))

    pdf = HTML(string=html).write_pdf()

    response = make_response(pdf)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'inline; filename=invoice_{registration_number}.pdf'
    return response

@admin_bp.route('/proforma-invoice/<int:user_id>')
@admin_required
def proforma_invoice(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT id, full_name, registration_number FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        flash("User not found.", "danger")
        return redirect(url_for('admin.dashboard'))

    # Use only NOT-YET-INVOICED packages (like your screenshots)
    c.execute("""
        SELECT id, house_awb, description, weight, value, created_at
        FROM packages
        WHERE user_id=? AND (invoice_id IS NULL)
        ORDER BY created_at ASC
    """, (user_id,))
    packages = [dict(x) for x in c.fetchall()]
    conn.close()

    # Compute line-by-line charges for display (no DB updates here)
    items = []
    totals = dict(duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0, freight=0, handling=0, grand_total=0)
    for p in packages:
        desc = p.get("description") or "Miscellaneous"
        wt   = float(p.get("weight") or 0)
        val  = float(p.get("value")  or 0)
        ch   = calculate_charges(desc, val, wt)

        items.append({
            "house_awb": p.get("house_awb"),
            "description": desc,
            "weight": wt,
            "value_usd": val,
            **ch
        })
        for k in totals:
            totals[k] += float

@admin_bp.route('/invoice/new-item')
@admin_required
def new_invoice_item():
    index = request.args.get('index', type=int, default=0)
    form = InvoiceForm()
    item = InvoiceItemForm(prefix=f'items-{index}')
    html = render_template('admin/_invoice_item.html', item=item)
    return jsonify({'html': html})

@admin_bp.route('/generate-bulk-invoice', methods=['POST'])
@admin_required
def generate_bulk_invoice():
    shipment_id = request.form.get('shipment_id')
    user_ids = request.form.getlist('user_ids')

    if not user_ids:
        flash("⚠️ No customers selected.", "warning")
        return redirect(url_for('logistics.shipment_log', shipment_id=shipment_id))

    # Logic to create invoices for each user
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for uid in user_ids:
        c.execute("INSERT INTO invoices (user_id, date_submitted, status) VALUES (?, DATE('now'), 'Pending')", (uid,))
    conn.commit()
    conn.close()

    flash("✅ Invoices successfully generated!", "success")
    return redirect(url_for('logistics.shipment_log', shipment_id=shipment_id))



@admin_bp.route('/invoices/<int:invoice_id>/pdf')
@admin_required
def invoice_pdf(invoice_id):
    # Build a dict compatible with _invoice_core.html
    # TODO: Replace with real fetch from your DB
    inv = Invoice.query.get_or_404(invoice_id)

    # Build packages list—make sure keys match the template
    packages = []
    for p in Package.query.filter_by(invoice_id=invoice_id).all():
        packages.append({
            "house_awb": p.house_awb,
            "description": p.description,
            "weight": p.weight or 0,
            "value": p.value or 0,
            "freight": p.freight_fee or 0,
            "storage": p.storage_fee or 0,
            "duty": p.duty or 0,
            "scf": p.scf or 0,
            "envl": p.envl or 0,
            "caf": p.caf or 0,
            "gct": p.gct or 0,
            "other_charges": p.other_charges or 0,
            "discount_due": getattr(p, 'discount_due', 0) or 0,
        })

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number,
        "date": inv.date_submitted or datetime.utcnow(),
        "customer_code": inv.user.registration_number if getattr(inv, 'user', None) else '',
        "customer_name": inv.user.full_name if getattr(inv, 'user', None) else '',
        "subtotal": inv.subtotal or inv.grand_total or 0,
        "discount_total": getattr(inv, 'discount_total', 0) or 0,
        "total_due": inv.grand_total or 0,
        "packages": packages,
    }

    rel = generate_invoice_pdf(invoice_dict)
    return redirect(url_for('static', filename=rel))


# ---------- VIEW (Image 1 style) ----------
@admin_bp.route('/invoices/<int:invoice_id>', methods=['GET'], endpoint='view_invoice')
@admin_required
def view_invoice(invoice_id):
    def _parse_dt(s: str | None):
        """Safely parse SQLite timestamps like 'YYYY-MM-DD HH:MM:SS' or return None."""
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # --- invoice + user
        c.execute("""
            SELECT i.*, u.full_name, u.registration_number
            FROM invoices i
            JOIN users u ON u.id = i.user_id
            WHERE i.id = ?
        """, (invoice_id,))
        inv = c.fetchone()
        if not inv:
            flash("Invoice not found.", "danger")
            return redirect(url_for('admin.dashboard'))

        # --- packages for the invoice
        c.execute("""
            SELECT id, house_awb, description, weight, value, merchant,
                   COALESCE(amount_due, 0) AS amount_due
            FROM packages
            WHERE invoice_id = ?
        """, (invoice_id,))
        rows = c.fetchall()

    finally:
        # ensure the connection closes even if something fails above
        try:
            conn.close()
        except Exception:
            pass

    # Build packages list for the template
    packages = [{
        "id": r["id"],
        "house_awb": r["house_awb"],
        "description": r["description"],
        "merchant": r["merchant"],
        "weight": float(r["weight"] or 0),
        "value_usd": float(r["value"] or 0),
        "amount_due": float(r["amount_due"] or 0),
    } for r in rows]

    # Totals (uses your helper that queries SQLite)
    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_sql(invoice_id)

    invoice_dict = {
        "id": inv["id"],
        "number": inv["invoice_number"],
        # prefer created_at; fall back to date_submitted
        "date": _parse_dt(inv["created_at"]) or _parse_dt(inv["date_submitted"]),
        "customer_code": inv["registration_number"],
        "customer_name": inv["full_name"],
        "subtotal": subtotal,
        "discount_total": discount_total,
        "payments_total": payments_total,
        "total_due": total_due,
        "packages": packages,
        # Optional: pass through description/notes if you show them in the UI
        "description": inv["description"] if "description" in inv.keys() else "",
    }

    is_inline = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        request.args.get('inline') == '1'
    )
    tpl = "admin/invoices/_invoice_inline.html" if is_inline else "admin/invoice_view.html"
    return render_template(tpl, invoice=invoice_dict, USD_TO_JMD=USD_TO_JMD)
    

# ---------- BREAKDOWN (Lightning icon) ----------
@admin_bp.route("/invoice/breakdown/<int:package_id>")
@admin_required
def invoice_breakdown(package_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT description, weight, value FROM packages WHERE id=?", (package_id,))
    pkg = c.fetchone()
    conn.close()
    if not pkg:
        return jsonify({"error": "Package not found"}), 404

    desc   = pkg["description"] or (pkg["category"] if "category" in pkg.keys() else "Miscellaneous")
    weight = float(pkg["weight"] or 0)
    value  = float(pkg["value"]  or 0)

    ch = calculate_charges(desc, value, weight)
    # normalize keys expected by the modal
    payload = {
        "duty":      float(ch.get("duty", 0)),
        "gct":       float(ch.get("gct", 0)),
        "freight":   float(ch.get("freight", 0)),
        "handling":  float(ch.get("handling", 0)),
        "scf":       float(ch.get("scf", 0)),
        "envl":      float(ch.get("envl", 0)),
        "caf":       float(ch.get("caf", 0)),
        "stamp":     float(ch.get("stamp", 0)),
        "other":     float(ch.get("other_charges", 0)),
        "total_jmd": float(ch.get("grand_total", 0)),
    }
    return jsonify(payload)

@admin_bp.route("/invoice/add-payment/<int:invoice_id>", methods=["POST"])
@admin_required
def add_payment(invoice_id):
    amount        = float(request.form.get("amount_jmd", 0))
    payment_type  = request.form.get("method", "Cash")
    authorized_by = request.form.get("authorized_by", "Admin")
    if amount <= 0:
        flash("Payment amount must be greater than 0.", "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    bill_number = f'BILL-{invoice_id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}'
    c.execute("""
        INSERT INTO payments (bill_number, payment_date, payment_type, amount, authorized_by, invoice_id, invoice_path)
        VALUES (?, DATETIME('now'), ?, ?, ?, ?, NULL)
    """, (bill_number, payment_type, amount, authorized_by, invoice_id))
    conn.commit()
    conn.close()

    flash("Payment recorded.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

@admin_bp.route("/invoice/add-discount/<int:invoice_id>", methods=["POST"])
@admin_required
def add_discount(invoice_id):
    amount = float(request.form.get("amount_jmd", 0))
    if amount <= 0:
        flash("Discount amount must be greater than 0.", "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE invoices SET discount_total = COALESCE(discount_total,0) + ? WHERE id=?", (amount, invoice_id))
    conn.commit()
    conn.close()

    flash("Discount added.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

@admin_bp.route("/invoice/save/<int:invoice_id>", methods=["POST"])
@admin_required
def save_invoice_notes(invoice_id):
    notes = request.form.get("notes", "")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # prefer notes column; fall back to description if you don’t add notes
    try:
        c.execute("UPDATE invoices SET notes=? WHERE id=?", (notes, invoice_id))
    except sqlite3.OperationalError:
        c.execute("UPDATE invoices SET description=? WHERE id=?", (notes, invoice_id))
    conn.commit()
    conn.close()

    flash("Invoice saved.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

@admin_bp.route('/invoices/<int:invoice_id>/inline', methods=['GET'], endpoint='invoice_inline')
@admin_required
def invoice_inline(invoice_id):
    # Build the same invoice_dict you already use for view/receipt
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
      SELECT i.*, u.full_name, u.registration_number
      FROM invoices i
      JOIN users u ON u.id=i.user_id
      WHERE i.id=?
    """, (invoice_id,))
    inv = c.fetchone()
    if not inv:
        conn.close()
        return "<div class='alert alert-warning m-0'>Invoice not found.</div>"

    c.execute("""
      SELECT id, house_awb, description, weight, value, merchant, COALESCE(amount_due,0) AS amount_due
      FROM packages
      WHERE invoice_id=?
    """, (invoice_id,))
    rows = c.fetchall()
    conn.close()

    packages = [{
      "id": r["id"],
      "house_awb": r["house_awb"],
      "description": r["description"],
      "merchant": r["merchant"],
      "weight": float(r["weight"] or 0),
      "value_usd": float(r["value"] or 0),
      "amount_due": float(r["amount_due"] or 0),
    } for r in rows]

    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_sql(invoice_id)

    invoice_dict = {
      "id": inv["id"],
      "number": inv["invoice_number"],
      "date": _safe_dt(inv["created_at"]),
      "customer_code": inv["registration_number"],
      "customer_name": inv["full_name"],
      "subtotal": subtotal,
      "discount_total": discount_total,
      "payments_total": payments_total,
      "total_due": total_due,
      "packages": packages,
      "description": inv["description"] or "",
    }
    # USD_TO_JMD import where you define it (e.g., from app.calculator_data import USD_TO_JMD)
    from app.calculator_data import USD_TO_JMD
    return render_template("admin/invoices/_invoice_inline.html",
                           invoice=invoice_dict, USD_TO_JMD=USD_TO_JMD)

def _safe_dt(val):
    if not val: return None
    try:
        return datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.fromisoformat(str(val))
        except Exception:
            return None


@admin_bp.route("/invoice/receipt/<int:invoice_id>")
@admin_required
def invoice_receipt(invoice_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # invoice + user
    c.execute("""
        SELECT i.*, u.full_name, u.registration_number, u.email, u.mobile
        FROM invoices i
        JOIN users u ON u.id = i.user_id
        WHERE i.id = ?
    """, (invoice_id,))
    inv = c.fetchone()
    if not inv:
        conn.close()
        flash("Invoice not found.", "danger")
        return redirect(url_for("admin.dashboard"))

    # ---------- Build a SAFE SELECT for packages ----------
    parts = []

    # tracking -> alias to 'tracking'
    if _has_col(conn, "packages", "tracking_number"):
        parts.append("tracking_number AS tracking")
    elif _has_col(conn, "packages", "tracking"):
        parts.append("tracking AS tracking")
    else:
        parts.append("NULL AS tracking")

    # house_awb (optional)
    if _has_col(conn, "packages", "house_awb"):
        parts.append("house_awb")
    else:
        parts.append("NULL AS house_awb")

    # description
    parts.append("description") if _has_col(conn, "packages", "description") else parts.append("NULL AS description")

    # weight
    parts.append("weight") if _has_col(conn, "packages", "weight") else parts.append("0.0 AS weight")

    # value (USD)
    parts.append("value") if _has_col(conn, "packages", "value") else parts.append("0.0 AS value")

    # freight -> alias to 'freight'
    has_fee = _has_col(conn, "packages", "freight_fee")
    has_fr  = _has_col(conn, "packages", "freight")
    if has_fee and has_fr:
        parts.append("CASE WHEN freight_fee IS NOT NULL THEN freight_fee ELSE freight END AS freight")
    elif has_fee:
        parts.append("COALESCE(freight_fee, 0) AS freight")
    elif has_fr:
        parts.append("COALESCE(freight, 0) AS freight")
    else:
        parts.append("0.0 AS freight")

    # other_charges -> alias to 'other_charges'
    parts.append("COALESCE(other_charges, 0) AS other_charges") if _has_col(conn, "packages", "other_charges") else parts.append("0.0 AS other_charges")

    # amount_due -> alias to 'amount_due' (fallback to 0)
    parts.append("COALESCE(amount_due, 0) AS amount_due") if _has_col(conn, "packages", "amount_due") else parts.append("0.0 AS amount_due")

    select_sql = f"SELECT {', '.join(parts)} FROM packages WHERE invoice_id = ?"
    c.execute(select_sql, (invoice_id,))
    rows = c.fetchall()

    # totals
    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_sql(invoice_id)
    conn.close()

    # --------- build PDF as you had (unchanged below this line) ----------
    table_data = [["#", "Tracking", "House AWB", "Description", "Weight (lb)", "Item (USD)", "Freight", "Other", "Amount Due"]]
    for i, r in enumerate(rows, start=1):
        table_data.append([
            i,
            r["tracking"] or "—",
            r["house_awb"] or "—",
            r["description"] or "—",
            f"{float(r['weight'] or 0):.2f}",
            f"{float(r['value'] or 0):.2f}",
            _money(r["freight"] or 0),
            _money(r["other_charges"] or 0),
            _money(r["amount_due"] or 0),
        ])

    # ... (rest of your PDF generation stays the same)


    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    flow = []
    flow.append(Paragraph("<b>FOREIGN A FOOT LOGISTICS</b>", styles['Title']))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("12 Port Lane<br/>Kingston, JM<br/>(876) 955-0123<br/>accounts@foreignafoot.com", styles['Normal']))
    flow.append(Spacer(1, 10))

    meta = [
        ["Invoice #", inv["invoice_number"] or f"INV-{invoice_id}"],
        ["Date", (inv["date_submitted"] or datetime.utcnow().strftime("%b %d, %Y"))],
        ["Currency", "JMD"],
    ]
    mt = Table(meta, colWidths=[120, 240])
    mt.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0,0), (0,-1), colors.HexColor("#4a148c")),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    flow.append(mt); flow.append(Spacer(1, 12))

    flow.append(Paragraph("<b>Bill To</b>", styles['Heading3']))
    flow.append(Paragraph(
        f"{inv['full_name']}<br/>{inv['registration_number']}<br/>{inv['email'] or ''}<br/>{inv['mobile'] or ''}",
        styles['Normal']
    ))
    flow.append(Spacer(1, 10))

    tbl = Table(table_data, repeatRows=1, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#4a148c")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("GRID", (0,0), (-1,-1), 0.3, colors.grey),
        ("ALIGN", (4,1), (-1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    flow.append(tbl); flow.append(Spacer(1, 10))

    totals = [
        ["Subtotal", _money(subtotal)],
        ["Discounts", f"- {_money(discount_total)}"],
        ["Payments", f"- {_money(payments_total)}"],
        ["Total Due", _money(subtotal - discount_total - payments_total)],
    ]
    t2 = Table(totals, colWidths=[350, 150])
    t2.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-2), "Helvetica"),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0,-1), (-1,-1), colors.HexColor("#4a148c")),
        ("ALIGN", (1,0), (-1,-1), "RIGHT"),
    ]))
    flow.append(t2); flow.append(Spacer(1, 18))
    flow.append(Paragraph("Thank you for your business!", styles['Italic']))

    doc.build(flow)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"receipt_{inv['invoice_number'] or invoice_id}.pdf",
                     mimetype="application/pdf")

@admin_bp.route('/wallet/<int:user_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_wallet(user_id):
    user = User.query.get_or_404(user_id)
    wallet = user.wallet
    if not wallet:
        wallet = Wallet(user_id=user.id, ewallet_balance=0, bucks_balance=0)
        db.session.add(wallet)
        db.session.commit()

    form = WalletUpdateForm(obj=wallet)  # prepopulate with wallet data

    if form.validate_on_submit():
        old_balance = wallet.ewallet_balance
        new_balance = form.ewallet_balance.data
        wallet.ewallet_balance = new_balance

        diff = new_balance - old_balance
        if diff != 0:
            transaction = WalletTransaction(
                user_id=user.id,
                amount=diff,
                description=form.description.data or f"Manual wallet update by admin: {diff:+.2f}",
                type='adjustment'
            )
            db.session.add(transaction)

        db.session.commit()
        return jsonify({'success': True, 'new_balance': wallet.ewallet_balance})

    # GET or form errors
    if request.method == 'GET' or not form.validate():
        return render_template('admin/edit_wallet_form.html', form=form, user=user)

@admin_bp.route('/wallet/update', methods=['GET','POST'])
@admin_required
def admin_update_wallet():
    if request.method == 'POST':
        user_id = request.form.get('user_id')
        amount_str = request.form.get('amount')
        description = request.form.get('description', 'Admin update')

        if not user_id or not amount_str:
            flash("User ID and amount are required.", "danger")
            return redirect(url_for('admin.update_wallet'))

        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount value.", "danger")
            return redirect(url_for('admin.update_wallet'))

        # Verify user exists before updating wallet
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        user_exists = c.fetchone()
        conn.close()

        if not user_exists:
            flash("User not found.", "danger")
            return redirect(url_for('admin.update_wallet'))

       
        update_wallet_balance(user_id, amount, description)

        flash(f"Wallet updated successfully for user ID {user_id}.", "success")
        # Redirect to the user profile page after update
        return redirect(url_for('admin.user_profile', id=user_id))


    # For GET: show form to update wallet with a user dropdown
    conn = get_db_connection()
    users = conn.execute("SELECT id, full_name FROM users ORDER BY full_name").fetchall()
    conn.close()
    return render_template('admin_update_wallet.html', users=users)

# ---------- ADMIN PROFILE ----------

@admin_bp.route('/profile', methods=['GET', 'POST'])
@admin_required
def admin_profile():
    form = AdminProfileForm()

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Fetch current admin data for pre-filling form
        c.execute("SELECT name, email FROM admin WHERE id = ?", (session['admin_id'],))
        admin_data = c.fetchone()

        if request.method == 'GET' and admin_data:
            form.name.data = admin_data[0]
            form.email.data = admin_data[1]

        if form.validate_on_submit():
            name = form.name.data.strip()
            email = form.email.data.strip()
            password = form.password.data.strip()

            if password:
                hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                c.execute(
                    "UPDATE admin SET name = ?, email = ?, password = ? WHERE id = ?",
                    (name, email, hashed_pw, session['admin_id'])
                )
            else:
                c.execute(
                    "UPDATE admin SET name = ?, email = ? WHERE id = ?",
                    (name, email, session['admin_id'])
                )

            conn.commit()
            flash("Profile updated successfully!", "success")
            return redirect(url_for('admin.admin_profile'))

        elif request.method == 'POST':
            flash("Please correct the errors and try again.", "warning")

    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"An error occurred: {str(e)}", "danger")

    finally:
        if conn:
            conn.close()

    return render_template('admin/admin_profile.html', form=form, admin=admin_data)





