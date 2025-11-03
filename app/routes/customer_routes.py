import re
from flask import Blueprint, render_template, request, redirect, session, url_for, current_app, flash
import sqlite3
import bcrypt
from datetime import datetime
from werkzeug.utils import secure_filename
import os
from app.utils import email_utils
from app.utils.helpers import customer_required
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email
from app.forms import LoginForm, PersonalInfoForm, AddressForm, PasswordChangeForm, PreAlertForm, PackageUpdateForm, SendMessageForm
from app import allowed_file
from flask import request, jsonify
from app.models import AuthorizedPickup, Message, ScheduledDelivery, db
from flask_login import login_required, current_user
from flask_mail import Message
from app import mail
from flask_login import login_user, logout_user
from app.models import User  # your SQLAlchemy model that implements UserMixin
from app import db
from app.utils.invoice_utils import generate_invoice
from app.calculator_data import categories
from app.forms import CalculatorForm
from math import ceil
from app.models import Notification, Message

customer_bp = Blueprint('customer', __name__, template_folder='templates/customer')

DB_PATH = 'shipping_platform.db'
PROFILE_UPLOAD_FOLDER = os.path.join('static', 'profile_pics')
INVOICE_UPLOAD_FOLDER = os.path.join('static', 'invoices')
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
# Make sure the upload folder exists
os.makedirs(INVOICE_UPLOAD_FOLDER, exist_ok=True)
# after marking a bill as paid:
invoice_path = generate_invoice

EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")

def format_jmd(v):
    try:
        return f"JMD {float(v):,.2f}"
    except Exception:
        return f"JMD {v}"

customer_bp.add_app_template_filter(format_jmd, 'jmd')


def generate_prealert_number():
    """Generate a new prealert number starting at 100001, incrementing by 1."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT MAX(prealert_number) FROM prealerts")
    max_num = c.fetchone()[0]
    conn.close()
    if max_num is None:
        return 100001
    else:
        return max_num + 1

# ---------- AUTH ROUTES ----------


@customer_bp.route('/login', methods=['GET', 'POST'])
def customer_login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip()
        password_bytes = form.password.data.encode('utf-8')

        user = User.query.filter_by(email=email).first()

        if user and bcrypt.checkpw(password_bytes, user.password):
            login_user(user)  # ✅ Flask-Login sets current_user
            flash('Logged in successfully!', 'success')
            return redirect(url_for('customer.customer_dashboard'))

        flash('Invalid credentials', 'danger')

    return render_template('auth/login.html', form=form)

@customer_bp.route('/logout')
@login_required
def logout():
    logout_user()  # ✅ clears Flask-Login session
    flash('Logged out.', 'info')
    return redirect(url_for('auth.login'))



# -----------------------------
# Dashboard
# -----------------------------
@customer_bp.route('/dashboard')
@login_required  # ensures only logged-in users can access
def customer_dashboard():
    user_id = current_user.id  # ✅ use current_user instead of session

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fetch user info
    c.execute("""
        SELECT full_name, email, mobile, registration_number, profile_pic,
               address, wallet_balance, referral_code
        FROM users
        WHERE id = ?
    """, (user_id,))
    user = c.fetchone()

    # Static US address
    us_address = {
        "recipient": user["full_name"],
        "address_line1": "4652 N Hiatus Rd",
        "address_line2": f"{user['registration_number']} A",
        "city": "Sunrise",
        "state": "Florida",
        "zip": "33351",
    }

    home_address = user["address"] or "No address saved"

    # Package counts
    c.execute("SELECT COUNT(*) AS count FROM packages WHERE user_id=? AND status='Overseas'", (user_id,))
    overseas_packages = c.fetchone()["count"] or 0

    c.execute("SELECT COUNT(*) AS count FROM packages WHERE user_id=? AND status='Ready for Pick Up'", (user_id,))
    ready_to_pickup = c.fetchone()["count"] or 0

    c.execute("SELECT COUNT(*) AS count FROM packages WHERE user_id=? AND status IN ('Shipped','Delivered')", (user_id,))
    total_shipped = c.fetchone()["count"] or 0

    # Wallet
    wallet_balance = user["wallet_balance"] if user["wallet_balance"] is not None else 0.0
    c.execute("SELECT id, type, amount, description, created_at FROM wallet_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user_id,))
    wallet_transactions = c.fetchall()

    referral_code = user["referral_code"]

    # Ready-to-pickup packages (instead of recent)
    c.execute("""
        SELECT id, house_awb, status, description, tracking_number, weight, amount_due, received_date
        FROM packages
        WHERE user_id=? AND status='Ready for Pick Up'
        ORDER BY received_date DESC
        LIMIT 5
    """, (user_id,))
    ready_packages = c.fetchall()

    conn.close()
   
    # ✅ Initialize Calculator Form
    form = CalculatorForm()
    form.category.choices = [(c, c) for c in categories]

    return render_template(
        'customer/customer_dashboard.html',
        form=form,
        user=user,
        categories=categories,
        us_address=us_address,
        home_address=home_address,
        profile_picture=user["profile_pic"],
        ready_to_pickup=ready_to_pickup,
        overseas_packages=overseas_packages,
        total_shipped=total_shipped,
        wallet_balance=wallet_balance,
        wallet_transactions=wallet_transactions,
        referral_code=referral_code,
        ready_packages=ready_packages
    )


@customer_bp.route('/prealerts/create', methods=['GET', 'POST'])
def prealerts_create():
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))

    form = PreAlertForm()

    if form.validate_on_submit():
        filename = None
        if form.invoice.data:
            file = form.invoice.data
            filename = secure_filename(file.filename)
            file.save(os.path.join(INVOICE_UPLOAD_FOLDER, filename))

        prealert_number = generate_prealert_number()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO prealerts (
                prealert_number, customer_id, vendor_name, courier_name,
                tracking_number, purchase_date, package_contents,
                item_value_usd, invoice_filename, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            prealert_number, current_user.id, form.vendor_name.data, form.courier_name.data,
            form.tracking_number.data, form.purchase_date.data, form.package_contents.data,
            float(form.item_value_usd.data), filename
        ))
        conn.commit()
        conn.close()

        flash(f"Pre-alert PA-{prealert_number} submitted successfully!", "success")
        return redirect(url_for('customer.prealerts_view'))

    return render_template('customer/prealerts_create.html', form=form)

@customer_bp.route('/prealerts/view')
@login_required
def prealerts_view():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT prealert_number, vendor_name, courier_name, tracking_number, purchase_date,
               package_contents, item_value_usd, invoice_filename, created_at
        FROM prealerts
        WHERE customer_id = ?
        ORDER BY created_at DESC
    """, (current_user.id,))
    prealerts = c.fetchall()
    conn.close()

    return render_template('customer/prealerts_view.html', prealerts=prealerts)


@customer_bp.route('/packages', methods=['GET', 'POST'])
@login_required
def view_packages():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Flask-WTF form for declared value update
    form = PackageUpdateForm()
    if form.validate_on_submit():
        pkg_id = request.form.get('pkg_id')
        declared_value = form.declared_value.data
        invoice_file = request.files.get('invoice_file')

        filename = None
        if invoice_file and allowed_file(invoice_file.filename):
            filename = secure_filename(invoice_file.filename)
            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'static/invoices')
            os.makedirs(upload_folder, exist_ok=True)
            invoice_file.save(os.path.join(upload_folder, filename))

        c.execute("""
            UPDATE packages
            SET declared_value = ?, invoice_file = ?
            WHERE id = ? AND user_id = ?
        """, (declared_value, filename, pkg_id, current_user.id))
        conn.commit()
        flash("Invoice and declared value submitted successfully!", "success")
        return redirect(url_for('customer.view_packages'))

    # Filters
    status_filter = request.args.get('status', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    tracking_number = request.args.get('tracking_number', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 10

    where_clauses = ["user_id = ?"]
    params = [current_user.id]

    if status_filter:
        where_clauses.append("status = ?")
        params.append(status_filter)
    if date_from:
        where_clauses.append("received_date >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("received_date <= ?")
        params.append(date_to)
    if tracking_number:
        where_clauses.append("tracking_number LIKE ?")
        params.append(f"%{tracking_number}%")

    where_sql = " AND ".join(where_clauses)

    # Total count for pagination
    c.execute(f"SELECT COUNT(*) FROM packages WHERE {where_sql}", params)
    total_packages = c.fetchone()[0]

    offset = (page - 1) * per_page

    c.execute(f"""
        SELECT 
            id, house_awb, status, description, tracking_number,
            weight, amount_due, received_date, invoice_file, declared_value
        FROM packages
        WHERE {where_sql}
        ORDER BY received_date DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, offset])
    packages = c.fetchall()


    # ===========================
    # Apply rounding, defaults, and amount_due rules
    # ===========================
    updated_packages = []
    for pkg in packages:
        pkg_dict = dict(pkg)

        # Weight rounded up
        try:
            pkg_dict['weight'] = ceil(float(pkg_dict['weight'])) if pkg_dict['weight'] else 0
        except ValueError:
            pkg_dict['weight'] = 0

        # Default declared value 65 USD if None
        try:
            pkg_dict['declared_value'] = float(pkg_dict['declared_value']) if pkg_dict['declared_value'] else 65
        except ValueError:
            pkg_dict['declared_value'] = 65

        # Amount due: 0 until status is ready_to_pick_up
        if pkg_dict['status'] != 'ready_to_pick_up':
            pkg_dict['amount_due'] = 0

        updated_packages.append(pkg_dict)
    conn.close()

    total_pages = (total_packages + per_page - 1) // per_page

    return render_template(
        'customer/customer_packages.html',
        packages=packages,
        form=form,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        tracking_number=tracking_number,
        page=page,
        total_pages=total_pages,
    )

@customer_bp.route('/package/<int:pkg_id>', methods=['GET', 'POST'])
@login_required
def package_detail(pkg_id):
    user_id = current_user.id
    form = PackageUpdateForm()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # POST: update declared value and invoice
    if form.validate_on_submit():
        declared_value = form.declared_value.data or 65
        invoice_file = form.invoice_file.data

        filename = None
        if invoice_file:
            filename = secure_filename(invoice_file.filename)
            upload_folder = current_app.config['UPLOAD_FOLDER']
            os.makedirs(upload_folder, exist_ok=True)
            invoice_file.save(os.path.join(upload_folder, filename))

        c.execute("""
            UPDATE packages
            SET declared_value = ?, invoice_file = ?
            WHERE id = ? AND user_id = ?
        """, (declared_value, filename, pkg_id, user_id))
        conn.commit()
        flash("Invoice and declared value updated successfully!", "success")
        return redirect(url_for('customer.package_detail', pkg_id=pkg_id))

    # GET: fetch package
    c.execute("""
        SELECT *
        FROM packages
        WHERE id = ? AND user_id = ?
    """, (pkg_id, user_id))
    pkg = c.fetchone()
    conn.close()

    if not pkg:
        flash("Package not found or access denied.", "danger")
        return redirect(url_for('customer.view_packages'))

    # Convert to dict to modify values
    pkg_dict = dict(pkg)

    # Round weight up
    try:
        pkg_dict['weight'] = ceil(float(pkg_dict['weight'])) if pkg_dict['weight'] else 0
    except ValueError:
        pkg_dict['weight'] = 0

    # Default declared value = 65 USD if None
    try:
        pkg_dict['declared_value'] = float(pkg_dict['declared_value']) if pkg_dict['declared_value'] else 65
    except ValueError:
        pkg_dict['declared_value'] = 65

    # Amount due = 0 until ready_to_pick_up
    if pkg_dict['status'] != 'ready_to_pick_up':
        pkg_dict['amount_due'] = 0

    return render_template('customer/package_detail.html', pkg=pkg, form=form)


@customer_bp.route('/update_declared_value', methods=['POST'])
@login_required
def update_declared_value():
    data = request.get_json()
    pkg_id = data.get('pkg_id')
    value = data.get('declared_value')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE packages SET declared_value=? WHERE id=? AND user_id=?", (value, pkg_id, current_user.id))
    conn.commit()
    conn.close()
    return jsonify(success=True)

@customer_bp.route('/bills')
@customer_required
def view_bills():
    user_id = current_user.id  # ✅ use flask-login user

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ✅ Fetch bills and match with invoices via invoice_number
    c.execute("""
        SELECT 
            b.id,
            b.user_id,
            b.package_id,
            b.description,
            b.amount,
            b.status,
            b.due_date,
            b.created_at,
            p.description AS package_description,
            p.status AS package_status,
            i.id AS invoice_id
        FROM bills b
        LEFT JOIN packages p ON b.package_id = p.id
        LEFT JOIN invoices i 
            ON i.user_id = b.user_id 
            AND i.description = b.description
        WHERE b.user_id = ?
        ORDER BY b.created_at DESC
    """, (user_id,))
    bills = c.fetchall()

    conn.close()

    return render_template("customer/bills.html", bills=bills)

@customer_bp.route('/payments')
@customer_required
def view_payments():
    user_id = current_user.id

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT id, bill_number, payment_date, payment_type, amount,  invoice_path
        FROM payments
        WHERE user_id = ?
        ORDER BY payment_date DESC
    """, (user_id,))
    
    payments = c.fetchall()
    conn.close()

    return render_template('customer/payments.html', payments=payments)


@customer_bp.route('/submit-invoice', methods=['GET', 'POST'])
@customer_required
def submit_invoice():
    user_id = current_user.id   # ✅ use flask-login user
    bill_id = request.args.get('bill_id', type=int)

    if request.method == 'POST':
        declared_value = request.form.get('declared_value')
        description = request.form.get('description')
        invoice_file = request.files.get('invoice_file')

        filename = None
        if invoice_file and allowed_file(invoice_file.filename):
            filename = secure_filename(invoice_file.filename)

            upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join("static", "invoices")
            os.makedirs(upload_folder, exist_ok=True)

            file_path = os.path.join(upload_folder, filename)
            invoice_file.save(file_path)
        else:
            flash("Invalid file type. Please upload PDF, JPG, or PNG.", "danger")
            return redirect(request.url)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # ✅ Ensure bill belongs to this user
        if bill_id:
            c.execute("SELECT package_id FROM bills WHERE id = ? AND user_id = ?", (bill_id, user_id))
            bill = c.fetchone()
            if not bill:
                flash("Bill not found or unauthorized.", "danger")
                conn.close()
                return redirect(url_for('customer.view_bills'))
            package_id = bill[0]
        else:
            flash("Bill ID is required to submit invoice.", "danger")
            conn.close()
            return redirect(url_for('customer.view_bills'))

        c.execute("""
            UPDATE packages
            SET declared_value = ?, invoice_file = ?
            WHERE id = ? AND user_id = ?
        """, (declared_value, filename, package_id, user_id))

        conn.commit()
        conn.close()

        flash("Invoice submitted successfully and package updated.", "success")
        return redirect(url_for('customer.view_bills'))

    # GET request
    return render_template('customer/submit_invoice.html', bill_id=bill_id)


@customer_bp.route('/invoice/<int:invoice_id>')
@customer_required
def view_invoice(invoice_id):
    user_id = current_user.id

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fetch invoice and verify ownership
    c.execute("""
        SELECT i.*
        FROM invoices i
        WHERE i.id = ? AND i.user_id = ?
    """, (invoice_id, user_id))
    inv = c.fetchone()
    if not inv:
        conn.close()
        flash("Invoice not found or you don't have permission to view it.", "danger")
        return redirect(url_for('customer.view_bills'))

    # Fetch packages linked to this invoice (most common schema)
    c.execute("""
        SELECT p.*
        FROM packages p
        WHERE p.invoice_id = ?
        ORDER BY p.id ASC
    """, (inv["id"],))
    rows = c.fetchall()
    conn.close()

    # Map packages to the keys expected by _invoice_core.html
    def _num(row, key, default=0):
        try:
            return float(row.get(key)) if isinstance(row, dict) else float(row[key])
        except Exception:
            return float(default)

    packages = []
    for r in rows:
        rd = dict(r)
        packages.append({
            "house_awb":      rd.get("house_awb"),
            "description":    rd.get("description"),
            "weight":         _num(rd, "weight", 0),
            "value":          _num(rd, "value", 0),                # USD value
            "freight":        _num(rd, "freight_fee", 0),          # map to your column names
            "storage":        _num(rd, "storage_fee", 0),
            "other_charges":  _num(rd, "other_charges", 0),
            "duty":           _num(rd, "duty", 0),
            "scf":            _num(rd, "scf", 0),
            "envl":           _num(rd, "envl", 0),
            "caf":            _num(rd, "caf", 0),
            "gct":            _num(rd, "gct", 0),
            "discount_due":   _num(rd, "discount_due", 0),
        })

    # Build a dict exactly like the admin page uses
    invoice_dict = {
        "id":            inv["id"],
        "number":        inv.get("invoice_number") if isinstance(inv, dict) else inv["invoice_number"],
        "date":          inv.get("date_submitted") if isinstance(inv, dict) else inv["date_submitted"],
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        # Totals: prefer explicit columns; fall back safely
        "subtotal":      inv.get("subtotal", 0) if isinstance(inv, dict) else (inv["subtotal"] if "subtotal" in inv.keys() else 0),
        "discount_total":inv.get("discount_total", 0) if isinstance(inv, dict) else (inv["discount_total"] if "discount_total" in inv.keys() else 0),
        "total_due":     inv.get("grand_total", inv.get("amount", 0)) if isinstance(inv, dict) else (inv["grand_total"] if "grand_total" in inv.keys() else inv["amount"] if "amount" in inv.keys() else 0),
        "packages":      packages,
        # Optional fields used by header
        "branch":        inv.get("branch") if isinstance(inv, dict) else (inv["branch"] if "branch" in inv.keys() else None),
        "staff":         inv.get("staff") if isinstance(inv, dict) else (inv["staff"] if "staff" in inv.keys() else None),
        "notes":         inv.get("notes") if isinstance(inv, dict) else (inv["notes"] if "notes" in inv.keys() else None),
    }

    return render_template("customer/invoices/view_invoice.html", invoice=invoice_dict)

@customer_bp.route('/invoices/<int:invoice_id>/pdf')
@customer_required
def invoice_pdf(invoice_id):
    user_id = current_user.id

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT * FROM invoices WHERE id = ? AND user_id = ?", (invoice_id, user_id))
    inv = c.fetchone()
    if not inv:
        conn.close()
        flash("Invoice not found or you don't have permission to view it.", "danger")
        return redirect(url_for('customer.view_bills'))

    # Reuse the same builder from the view route to keep things in sync
    c.execute("SELECT * FROM packages WHERE invoice_id = ? ORDER BY id ASC", (invoice_id,))
    rows = c.fetchall()
    conn.close()

    def _num(row, key, default=0):
        try:
            return float(row.get(key)) if isinstance(row, dict) else float(row[key])
        except Exception:
            return float(default)

    packages = []
    for r in rows:
        rd = dict(r)
        packages.append({
            "house_awb":      rd.get("house_awb"),
            "description":    rd.get("description"),
            "weight":         _num(rd, "weight", 0),
            "value":          _num(rd, "value", 0),
            "freight":        _num(rd, "freight_fee", 0),
            "storage":        _num(rd, "storage_fee", 0),
            "other_charges":  _num(rd, "other_charges", 0),
            "duty":           _num(rd, "duty", 0),
            "scf":            _num(rd, "scf", 0),
            "envl":           _num(rd, "envl", 0),
            "caf":            _num(rd, "caf", 0),
            "gct":            _num(rd, "gct", 0),
            "discount_due":   _num(rd, "discount_due", 0),
        })

    invoice_dict = {
        "id":            inv["id"],
        "number":        inv["invoice_number"],
        "date":          inv["date_submitted"],
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal":      inv["subtotal"] if "subtotal" in inv.keys() else 0,
        "discount_total":inv["discount_total"] if "discount_total" in inv.keys() else 0,
        "total_due":     inv["grand_total"] if "grand_total" in inv.keys() else (inv["amount"] if "amount" in inv.keys() else 0),
        "packages":      packages,
    }

    rel = generate_invoice_pdf(invoice_dict)   # your existing util
    return redirect(url_for('static', filename=rel))


@customer_bp.route("/messages", methods=["GET", "POST"])
@login_required
def view_messages():
    form = SendMessageForm()

    # Who receives messages from customers? (pick an admin)
    admin = User.query.filter_by(is_admin=True).first()
    if not admin:
        # fallback — adjust if you have a different admin selection logic
        admin = User.query.order_by(User.id.asc()).first()

    if request.method == "POST" and form.validate_on_submit():
        if not admin:
            flash("No admin user found to receive messages.", "danger")
            return redirect(url_for("customer.view_messages"))

        msg = Message(
            sender_id=current_user.id,
            recipient_id=admin.id,
            subject=form.subject.data.strip(),
            body=form.body.data.strip(),
        )
        db.session.add(msg)
        db.session.commit()
        flash("Message sent!", "success")
        return redirect(url_for("customer.view_messages"))

    # Inbox (received by current user)
    inbox = Message.query.filter_by(recipient_id=current_user.id).order_by(Message.created_at.desc()).all()
    # Sent (sent by current user)
    sent = Message.query.filter_by(sender_id=current_user.id).order_by(Message.created_at.desc()).all()

    return render_template("customer/messages.html", form=form, inbox=inbox, sent=sent)

@customer_bp.route("/messages/mark_read/<int:msg_id>", methods=["POST"])
@login_required
def mark_message_read(msg_id):
    msg = Message.query.get_or_404(msg_id)
    if msg.recipient_id != current_user.id:
        flash("Not authorized.", "danger")
        return redirect(url_for("customer.view_messages"))

    msg.is_read = True
    db.session.commit()
    flash("Message marked as read.", "success")
    return redirect(url_for("customer.view_messages"))

@customer_bp.app_context_processor
def inject_message_counts():
    if not current_user.is_authenticated:
        return dict(unread_messages_count=0)
    count = Message.query.filter_by(recipient_id=current_user.id, is_read=False).count()
    return dict(unread_messages_count=count)



@customer_bp.route("/notifications", methods=["GET", "POST"])
@login_required
def view_notifications():
    notes = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    return render_template("customer/notifications.html", notes=notes)

@customer_bp.route("/notifications/mark_read/<int:nid>", methods=["POST"])
@login_required
def mark_notification_read(nid):
    n = Notification.query.get_or_404(nid)
    if n.user_id != current_user.id:
        flash("Not authorized.", "danger")
        return redirect(url_for("customer.view_notifications"))
    n.is_read = True
    db.session.commit()
    flash("Notification marked as read.", "success")
    return redirect(url_for("customer.view_notifications"))

@customer_bp.app_context_processor
def inject_notification_counts():
    if not current_user.is_authenticated:
        return dict(unread_notifications_count=0)
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return dict(unread_notifications_count=count)

@customer_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user_id = current_user.id  # ✅ use current_user

    # Load user data
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT full_name, email, mobile, trn FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()

    form = PersonalInfoForm()

    if request.method == 'GET' and user:
        form.full_name.data = user[0]
        form.email.data = user[1]
        form.mobile.data = user[2]
        form.trn.data = user[3]

    if form.validate_on_submit():
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            UPDATE users 
            SET full_name = ?, email = ?, mobile = ?, trn = ? 
            WHERE id = ?
        """, (form.full_name.data, form.email.data, form.mobile.data, form.trn.data, user_id))
        conn.commit()
        conn.close()

        flash("Your personal information has been updated.", "success")
        return redirect(url_for('customer.profile'))

    return render_template('customer/profile.html', form=form)


@customer_bp.route('/address', methods=['GET', 'POST'])
@login_required
def address():
    user_id = current_user.id  # ✅

    form = AddressForm()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT address FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    current_address = result[0] if result else ""

    if request.method == 'GET':
        form.address.data = current_address

    if form.validate_on_submit():
        c.execute("UPDATE users SET address = ? WHERE id = ?", (form.address.data, user_id))
        conn.commit()
        conn.close()
        flash("Address updated successfully.", "success")
        return redirect(url_for("customer.address"))

    conn.close()
    return render_template('customer/address.html', form=form)


@customer_bp.route('/update_delivery_address', methods=['GET', 'POST'])
@login_required
def update_delivery_address():
    user_id = current_user.id  # ✅

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if request.method == 'POST':
        address = request.form.get('address', '').strip()
        c.execute("UPDATE users SET address = ? WHERE id = ?", (address, user_id))
        conn.commit()
        conn.close()
        flash('Delivery address updated successfully!', 'success')
        return redirect(url_for('customer.customer_dashboard'))

    # GET request — load existing address
    c.execute("SELECT address FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()

    return render_template(
        'customer/update_delivery_address.html',
        address=user['address'] if user else ''
    )


@customer_bp.route('/security', methods=['GET', 'POST'])
@login_required
def security():
    form = PasswordChangeForm()

    if form.validate_on_submit():
        current_password = form.current_password.data.encode('utf-8')
        new_password = form.new_password.data.encode('utf-8')

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE id = ?", (current_user.id,))
        user = c.fetchone()

        if user and bcrypt.checkpw(current_password, user[0].encode('utf-8')):
            hashed = bcrypt.hashpw(new_password, bcrypt.gensalt())
            c.execute("UPDATE users SET password = ? WHERE id = ?", (hashed.decode('utf-8'), current_user.id))
            conn.commit()
            conn.close()
            flash("Password updated successfully.", "success")
            return redirect(url_for("customer.security"))
        else:
            conn.close()
            flash("Incorrect current password.", "danger")

    return render_template('customer/security.html', form=form)


# Authorized Pickup
@customer_bp.route('/authorized-pickup', methods=['GET'])
@login_required
def authorized_pickup_overview():
    pickups = AuthorizedPickup.query.filter_by(user_id=current_user.id).all()
    return render_template('customer/authorized_pickup_overview.html', pickups=pickups)


@customer_bp.route('/authorized-pickup/add', methods=['POST'])
@login_required
def authorized_pickup_add():
    data = request.json
    try:
        new_person = AuthorizedPickup(
            user_id=current_user.id,
            full_name=data['full_name'],
            email=data.get('email'),
            phone_number=data.get('phone_number')
        )
        db.session.add(new_person)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Authorized person added successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400


# Scheduled Delivery
@customer_bp.route('/schedule-delivery', methods=['GET'])
@login_required
def schedule_delivery_overview():
    deliveries = ScheduledDelivery.query.filter_by(user_id=current_user.id).all()
    return render_template('customer/schedule_delivery_overview.html', deliveries=deliveries)


@customer_bp.route('/schedule-delivery/add', methods=['POST'])
@login_required
def schedule_delivery_add():
    data = request.json
    try:
        new_delivery = ScheduledDelivery(
            user_id=current_user.id,
            scheduled_date=datetime.strptime(data['schedule_date'], '%Y-%m-%d').date(),
            scheduled_time=datetime.strptime(data['schedule_time'], '%H:%M').time(),
            location=data['location'],
            direction=data.get('direction', ''),
            mobile_number=data.get('mobile_number', ''),
            person_receiving=data.get('person_receiving', '')
        )
        db.session.add(new_delivery)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Schedule added successfully'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400


# Referrals
@customer_bp.route('/referrals', methods=['GET', 'POST'])
@login_required
def referrals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        c.execute("SELECT full_name, email, referral_code FROM users WHERE id = ?", (current_user.id,))
        user = c.fetchone()
        if not user:
            flash("User not found.", "danger")
            return redirect(url_for('customer.customer_dashboard'))

        referral_code = user["referral_code"]
        full_name = user["full_name"]

        if request.method == 'POST':
            friend_email = request.form.get('friend_email', '').strip()
            if not friend_email or not EMAIL_REGEX.match(friend_email):
                flash("Please enter a valid email address.", "warning")
            else:
                try:
                    send_referral_email(friend_email, referral_code, full_name)
                    flash(f"Referral email sent to {friend_email}.", "success")
                except Exception as e:
                    flash("Failed to send referral email. Please try again later.", "danger")
    finally:
        conn.close()

    return render_template('customer/referrals.html', referral_code=referral_code, full_name=full_name)


# Upload profile pic
@customer_bp.route('/upload-profile-pic', methods=['POST'])
@login_required
def upload_profile_pic():
    file = request.files['profile_pic']
    if file and file.filename:
        filename = f"{current_user.id}.jpg"
        file.save(os.path.join(PROFILE_UPLOAD_FOLDER, filename))

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET profile_pic = ? WHERE id = ?", (filename, current_user.id))
        conn.commit()
        conn.close()

    return redirect(url_for('customer.customer_dashboard'))


@customer_bp.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message_body = request.form.get('message')

        try:
            msg = Message(
                subject=f"[Contact Form] {subject}",
                sender=email,
                recipients=['foreignafootlogistics@gmail.com']
            )
            msg.body = f"From: {name}\nEmail: {email}\n\n{message_body}"
            mail.send(msg)

            flash('Your message has been sent successfully!', 'success')
        except Exception as e:
            flash(f'Error sending message: {str(e)}', 'danger')

        return redirect(url_for('customer.contact'))

    return render_template('customer/contact.html')


@customer_bp.route('/terms')
def terms():
    return render_template('customer/terms.html', current_year=datetime.now().year)

@customer_bp.route('/privacy')
def privacy():
    return render_template('customer/privacy.html', current_year=datetime.now().year)


