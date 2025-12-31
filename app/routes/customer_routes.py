# app/routes/customer_routes.py (imports)

import os, re
from math import ceil
from datetime import datetime, date

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    current_app, flash, jsonify
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.utils import secure_filename

import bcrypt
import sqlalchemy as sa

from app.forms import ReferralForm  
from app.forms import (
    LoginForm, PersonalInfoForm, AddressForm, PasswordChangeForm,
    PreAlertForm, PackageUpdateForm, SendMessageForm, CalculatorForm
)
from app.utils import email_utils
from app.utils.email_utils import send_referral_email
from app.utils.referrals import ensure_user_referral_code
from app.utils.helpers import customer_required
from app.utils.invoice_utils import generate_invoice
from app.calculator_data import categories
from app import allowed_file, mail
from app.extensions import db
from app.calculator import calculate_charges
from app.calculator_data import CATEGORIES, USD_TO_JMD


# Models — NO Bill model; alias Message to avoid clash with Flask-Mail's Message
from app.models import (
    User, Package, Invoice,
    AuthorizedPickup, ScheduledDelivery,
    Notification,
    Message as DBMessage,  # 👈 avoid name clash with Flask-Mail
    Wallet, WalletTransaction, Payment, Settings,
    Prealert,
)
from sqlalchemy import func
# Email class from Flask-Mail (alias to avoid clash)
from flask_mail import Message as MailMessage

customer_bp = Blueprint('customer', __name__, template_folder='templates/customer')

# Upload folders
PROFILE_UPLOAD_FOLDER = os.path.join('static', 'profile_pics')
INVOICE_UPLOAD_FOLDER = os.path.join('static', 'invoices')
ALLOWED_INVOICE_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}
os.makedirs(PROFILE_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(INVOICE_UPLOAD_FOLDER, exist_ok=True)

EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")

# -----------------------------
# Jinja filters
# -----------------------------
def format_jmd(v):
    try:
        return f"JMD {float(v):,.2f}"
    except Exception:
        return f"JMD {v}"

customer_bp.add_app_template_filter(format_jmd, 'jmd')


# -----------------------------
# Helpers
# -----------------------------
def generate_prealert_number() -> int:
    """Next prealert_number starting 100001."""
    max_num = db.session.scalar(sa.select(func.max(Prealert.prealert_number)))
    return int(max_num or 100000) + 1


# -----------------------------
# Auth
# -----------------------------
@customer_bp.route('/login', methods=['GET', 'POST'])
def customer_login():
    # Always use the main auth.login route
    return redirect(url_for('auth.login'))


@customer_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('auth.login'))


# -----------------------------
# Dashboard
# -----------------------------
@customer_bp.route('/dashboard')
@login_required
def customer_dashboard():
    user = current_user

    # Load global settings row (id=1)
    settings = db.session.get(Settings, 1)

    # Graceful defaults if settings row or fields are missing
    us_street       = getattr(settings, "us_street", None)       or "3200 NW 112th Avenue"
    us_suite_prefix = getattr(settings, "us_suite_prefix", None) or "KCDA-FAFL# "
    us_city         = getattr(settings, "us_city", None)         or "Doral"
    us_state        = getattr(settings, "us_state", None)        or "Florida"
    us_zip          = getattr(settings, "us_zip", None)          or "33172"

    # Build the address dict used by the template
    us_address = {
        "recipient": user.full_name,
        "address_line1": us_street,
        "address_line2": (
            f"{us_suite_prefix}{user.registration_number}"
            if getattr(user, "registration_number", None)
            else us_suite_prefix
        ),
        "city": us_city,
        "state": us_state,
        "zip": us_zip,
    }


    # Package counts
    overseas_packages = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id, Package.status == 'Overseas'
        )
    ) or 0

    ready_to_pickup = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id, Package.status == 'Ready for Pick Up'
        )
    ) or 0

    total_shipped = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id, Package.status.in_(('Shipped', 'Delivered'))
        )
    ) or 0

    # Wallet
    wallet_balance = getattr(user, "wallet_balance", 0.0) or 0.0
    wallet_transactions = (WalletTransaction.query
                           .filter_by(user_id=user.id)
                           .order_by(WalletTransaction.created_at.desc())
                           .limit(5).all())

    # Ready-to-pickup packages (latest 5)
    ready_packages = (Package.query
                      .filter_by(user_id=user.id, status='Ready for Pick Up')
                      .order_by(Package.received_date.desc().nullslast())
                      .limit(5).all())

    # Calculator form
    form = CalculatorForm()
    form.category.choices = [(c, c) for c in CATEGORIES.keys()]

    return render_template(
        'customer/customer_dashboard.html',
        form=form,
        user=user,
        categories=CATEGORIES,
        us_address=us_address,
        home_address=getattr(user, "address", "No address saved"),
        profile_picture=getattr(user, "profile_pic", None),
        ready_to_pickup=ready_to_pickup,
        overseas_packages=overseas_packages,
        total_shipped=total_shipped,
        wallet_balance=wallet_balance,
        wallet_transactions=wallet_transactions,
        referral_code=getattr(user, "referral_code", None),
        ready_packages=ready_packages
    )


# -----------------------------
# Pre-Alerts
# -----------------------------
@customer_bp.route('/prealerts/create', methods=['GET', 'POST'])
@login_required
def prealerts_create():
    form = PreAlertForm()
    if form.validate_on_submit():
        filename = None
        if form.invoice.data:
            file = form.invoice.data
            if allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(INVOICE_UPLOAD_FOLDER, filename))

        prealert_number = generate_prealert_number()
        pa = Prealert(
            prealert_number=prealert_number,
            customer_id=current_user.id,
            vendor_name=form.vendor_name.data,
            courier_name=form.courier_name.data,
            tracking_number=form.tracking_number.data,
            purchase_date=form.purchase_date.data,
            package_contents=form.package_contents.data,
            item_value_usd=float(form.item_value_usd.data or 0),
            invoice_filename=filename,
            created_at=datetime.utcnow(),
        )
        db.session.add(pa)
        db.session.commit()

        flash(f"Pre-alert PA-{prealert_number} submitted successfully!", "success")
        return redirect(url_for('customer.prealerts_view'))
    return render_template('customer/prealerts_create.html', form=form)


@customer_bp.route('/prealerts/view')
@login_required
def prealerts_view():
    prealerts = (Prealert.query
                 .filter_by(customer_id=current_user.id)
                 .order_by(Prealert.created_at.desc()).all())
    return render_template('customer/prealerts_view.html', prealerts=prealerts)


# -----------------------------
# Packages
# -----------------------------
@customer_bp.route('/packages', methods=['GET', 'POST'])
@login_required
def view_packages():
    form = PackageUpdateForm()

    if form.validate_on_submit():
        pkg_id = request.form.get('pkg_id', type=int)
        declared_value = form.declared_value.data
        invoice_file = request.files.get('invoice_file')

        pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()

        filename = None
        if invoice_file and allowed_file(invoice_file.filename):
            filename = secure_filename(invoice_file.filename)
            upload_folder = current_app.config.get('UPLOAD_FOLDER', INVOICE_UPLOAD_FOLDER)
            os.makedirs(upload_folder, exist_ok=True)
            invoice_file.save(os.path.join(upload_folder, filename))
            pkg.invoice_file = filename

        pkg.declared_value = declared_value
        db.session.commit()
        flash("Invoice and declared value submitted successfully!", "success")
        return redirect(url_for('customer.view_packages'))

    # Filters
    status_filter = (request.args.get('status') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    tracking_number = (request.args.get('tracking_number') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 10

    q = Package.query.filter_by(user_id=current_user.id)

    if status_filter:
        q = q.filter(Package.status == status_filter)
    if date_from:
        q = q.filter(Package.received_date >= date_from)
    if date_to:
        q = q.filter(Package.received_date <= date_to)
    if tracking_number:
        q = q.filter(Package.tracking_number.ilike(f"%{tracking_number}%"))

    q = q.order_by(Package.received_date.desc().nullslast())
    paginated = q.paginate(page=page, per_page=per_page, error_out=False)

    # Adjust values for display
    packages = []
    for pkg in paginated.items:
        d = {
            "id": pkg.id,
            "house_awb": pkg.house_awb,
            "status": pkg.status,
            "description": pkg.description,
            "tracking_number": pkg.tracking_number,
            "weight": 0,
            "amount_due": pkg.amount_due or 0,
            "received_date": pkg.received_date,
            "invoice_file": pkg.invoice_file,
            "declared_value": pkg.declared_value if pkg.declared_value is not None else 65.0,
        }
        # rounded up weight
        try:
            d["weight"] = ceil(float(pkg.weight or 0))
        except Exception:
            d["weight"] = 0

        # zero out amount due until ready_to_pick_up (keep your rule)
        if pkg.status != 'ready_to_pick_up':
            d["amount_due"] = 0

        packages.append(d)

    total_pages = paginated.pages or 1

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
    form = PackageUpdateForm()
    pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()

    if form.validate_on_submit():
        declared_value = form.declared_value.data or 65
        invoice_file = form.invoice_file.data

        if invoice_file and allowed_file(invoice_file.filename):
            filename = secure_filename(invoice_file.filename)
            upload_folder = current_app.config.get('UPLOAD_FOLDER', INVOICE_UPLOAD_FOLDER)
            os.makedirs(upload_folder, exist_ok=True)
            invoice_file.save(os.path.join(upload_folder, filename))
            pkg.invoice_file = filename

        pkg.declared_value = declared_value
        db.session.commit()
        flash("Invoice and declared value updated successfully!", "success")
        return redirect(url_for('customer.package_detail', pkg_id=pkg_id))

    # Prepare display dict
    d = {c.name: getattr(pkg, c.name, None) for c in pkg.__table__.columns}
    try:
        d['weight'] = ceil(float(pkg.weight or 0))
    except Exception:
        d['weight'] = 0
    try:
        d['declared_value'] = float(pkg.declared_value) if pkg.declared_value is not None else 65.0
    except Exception:
        d['declared_value'] = 65.0
    if pkg.status != 'ready_to_pick_up':
        d['amount_due'] = 0

    return render_template('customer/package_detail.html', pkg=d, form=form)


@customer_bp.route('/update_declared_value', methods=['POST'])
@login_required
def update_declared_value():
    data = request.get_json() or {}
    pkg_id = data.get('pkg_id', None)
    value = data.get('declared_value', None)

    if pkg_id is None or value is None:
        return jsonify(success=False, error="Missing fields"), 400

    pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()
    pkg.declared_value = value
    db.session.commit()
    return jsonify(success=True)


# -----------------------------
# Bills & Payments
# -----------------------------
# ===== Bills → Invoices list =====
@customer_bp.route('/bills')
@login_required
def view_bills():
    invoices = (Invoice.query
                .filter_by(user_id=current_user.id)
                .order_by(Invoice.date_submitted.desc().nullslast(), Invoice.id.desc())
                .all())
    return render_template("customer/bills.html", invoices=invoices)

@customer_bp.route('/payments')
@customer_required
def view_payments():
    # Get all payments for this user, newest first
    raw_payments = (
        Payment.query
        .filter_by(user_id=current_user.id)
        .order_by(Payment.created_at.desc())
        .all()
    )

    payments = []
    for p in raw_payments:
        inv = p.invoice  # via relationship

        payments.append({
            "invoice_id":    inv.id if inv else None,
            "invoice_number": getattr(inv, "invoice_number", "N/A") if inv else "N/A",
            "payment_date":  p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
            "payment_type":  p.method or "Unknown",
            "amount":        float(p.amount_jmd or 0),
        })

    return render_template('customer/payments.html', payments=payments)

@customer_bp.route('/submit-invoice', methods=['GET', 'POST'])
@login_required
def submit_invoice():
    # expect ?package_id=123 in the query string
    package_id = request.args.get('package_id', type=int)

    if request.method == 'POST':
        declared_value = request.form.get('declared_value')
        invoice_file   = request.files.get('invoice_file')

        if not package_id:
            flash("Missing package id.", "danger")
            return redirect(url_for('customer.view_packages'))

        # validate package belongs to current user
        pkg = Package.query.filter_by(id=package_id, user_id=current_user.id).first()
        if not pkg:
            flash("Package not found or unauthorized.", "danger")
            return redirect(url_for('customer.view_packages'))

        # basic file checks
        if not (invoice_file and invoice_file.filename):
            flash("Please attach an invoice file (PDF/JPG/PNG).", "warning")
            return redirect(request.url)

        if not allowed_file(invoice_file.filename):  # expects pdf/jpg/jpeg/png
            flash("Invalid file type. Please upload PDF, JPG, or PNG.", "danger")
            return redirect(request.url)

        # save the file
        filename = secure_filename(invoice_file.filename)
        upload_folder = current_app.config.get('UPLOAD_FOLDER', INVOICE_UPLOAD_FOLDER)
        os.makedirs(upload_folder, exist_ok=True)
        invoice_file.save(os.path.join(upload_folder, filename))

        # persist to this package
        try:
            if declared_value:
                try:
                    pkg.declared_value = float(declared_value)
                except ValueError:
                    flash("Declared value must be a number.", "warning")
                    return redirect(request.url)

            pkg.invoice_file = filename
            db.session.commit()
            flash("Invoice submitted successfully and package updated.", "success")
        except Exception as e:
            db.session.rollback()
            flash(f"Failed to save invoice: {e}", "danger")

        return redirect(url_for('customer.view_packages'))

    # GET — show form; keep passing the package_id so the form action keeps it
    return render_template('customer/submit_invoice.html', package_id=package_id)



# -----------------------------
# Invoice viewer / PDF (customer)
# -----------------------------
@customer_bp.route('/invoice/<int:invoice_id>')
@customer_required
def view_invoice_customer(invoice_id):
    inv = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first()
    if not inv:
        flash("Invoice not found or you don't have permission to view it.", "danger")
        return redirect(url_for('customer.view_bills'))

    pkgs = Package.query.filter_by(invoice_id=inv.id).order_by(Package.id.asc()).all()
    def _num(val, default=0.0):
        try:
            return float(val or 0)
        except Exception:
            return float(default)

    packages = [{
        "house_awb":      p.house_awb,
        "description":    p.description,
        "weight":         _num(p.weight),
        "value":          _num(getattr(p, "value", 0)),
        "freight":        _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
        "storage":        _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
        "other_charges":  _num(p.other_charges),
        "duty":           _num(p.duty),
        "scf":            _num(p.scf),
        "envl":           _num(p.envl),
        "caf":            _num(p.caf),
        "gct":            _num(p.gct),
        "discount_due":   _num(getattr(p, "discount_due", 0)),
    } for p in pkgs]

    invoice_dict = {
        "id":            inv.id,
        "number":        inv.invoice_number,
        "date":          inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal":      float(getattr(inv, "subtotal", 0) or 0),
        "discount_total":float(getattr(inv, "discount_total", 0) or 0),
        "total_due":     float(getattr(inv, "grand_total", getattr(inv, "amount", 0)) or 0),
        "packages":      packages,
        "branch":        getattr(inv, "branch", None),
        "staff":         getattr(inv, "staff", None),
        "notes":         getattr(inv, "notes", None),
    }
    return render_template("customer/invoices/view_invoice.html", invoice=invoice_dict)


@customer_bp.route('/invoices/<int:invoice_id>/pdf')
@customer_required
def invoice_pdf(invoice_id):
    inv = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first()
    if not inv:
        flash("Invoice not found or you don't have permission to view it.", "danger")
        return redirect(url_for('customer.view_bills'))

    pkgs = Package.query.filter_by(invoice_id=inv.id).order_by(Package.id.asc()).all()
    def _num(val, default=0.0):
        try:
            return float(val or 0)
        except Exception:
            return float(default)

    packages = [{
        "house_awb":      p.house_awb,
        "description":    p.description,
        "weight":         _num(p.weight),
        "value":          _num(getattr(p, "value", 0)),
        "freight":        _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
        "storage":        _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
        "other_charges":  _num(p.other_charges),
        "duty":           _num(p.duty),
        "scf":            _num(p.scf),
        "envl":           _num(p.envl),
        "caf":            _num(p.caf),
        "gct":            _num(p.gct),
        "discount_due":   _num(getattr(p, "discount_due", 0)),
    } for p in pkgs]

    invoice_dict = {
        "id":            inv.id,
        "number":        inv.invoice_number,
        "date":          inv.date_submitted or datetime.utcnow(),
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal":      float(getattr(inv, "subtotal", 0) or 0),
        "discount_total":float(getattr(inv, "discount_total", 0) or 0),
        "total_due":     float(getattr(inv, "grand_total", getattr(inv, "amount", 0)) or 0),
        "packages":      packages,
    }
    rel = generate_invoice(invoice_dict)  # your util that returns static path
    return redirect(url_for('static', filename=rel))


# -----------------------------
# Messaging
# -----------------------------
@customer_bp.route("/messages", methods=["GET", "POST"])
@login_required
def view_messages():
    form = SendMessageForm()

    # Choose an admin recipient (first admin, else first user)
    admin = (
        User.query.filter_by(is_admin=True).order_by(User.id.asc()).first()
        or User.query.order_by(User.id.asc()).first()
    )

    if request.method == "POST" and form.validate_on_submit():
        if not admin:
            flash("No admin user found to receive messages.", "danger")
            return redirect(url_for("customer.view_messages"))

        msg = DBMessage(  # 👈 use DBMessage (your ORM model)
            sender_id=current_user.id,
            recipient_id=admin.id,
            subject=form.subject.data.strip(),
            body=form.body.data.strip(),
            created_at=datetime.utcnow(),
        )
        db.session.add(msg)
        db.session.commit()
        flash("Message sent!", "success")
        return redirect(url_for("customer.view_messages"))

    inbox = (
        DBMessage.query
        .filter_by(recipient_id=current_user.id)
        .order_by(DBMessage.created_at.desc())
        .all()
    )
    sent = (
        DBMessage.query
        .filter_by(sender_id=current_user.id)
        .order_by(DBMessage.created_at.desc())
        .all()
    )
    return render_template("customer/messages.html", form=form, inbox=inbox, sent=sent)


@customer_bp.route("/messages/mark_read/<int:msg_id>", methods=["POST"])
@login_required
def mark_message_read(msg_id):
    msg = DBMessage.query.get_or_404(msg_id)  # 👈 use DBMessage
    if msg.recipient_id != current_user.id:
        flash("Not authorized.", "danger")
        return redirect(url_for("customer.view_messages"))

    msg.is_read = True
    db.session.commit()
    flash("Message marked as read.", "success")
    return redirect(url_for("customer.view_messages"))


@customer_bp.app_context_processor
def inject_message_counts():
    count = 0
    try:
        if current_user.is_authenticated:
            count = (
                db.session.scalar(
                    sa.select(sa.func.count())
                    .select_from(DBMessage)
                    .where(
                        DBMessage.recipient_id == current_user.id,
                        DBMessage.is_read.is_(False),
                    )
                )
                or 0
            )
    except Exception as e:
        db.session.rollback()
        current_app.logger.warning("inject_message_counts failed: %s", e)
        count = 0
    return dict(unread_messages_count=int(count))

# -----------------------------
# Notifications
# -----------------------------
@customer_bp.route("/notifications", methods=["GET"])
@login_required
def view_notifications():
    notes = (Notification.query
             .filter_by(user_id=current_user.id)
             .order_by(Notification.created_at.desc())
             .all())
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
    count = 0
    try:
        if current_user.is_authenticated:
            count = db.session.scalar(
                sa.select(func.count()).select_from(Notification).where(
                    Notification.user_id == current_user.id,
                    Notification.is_read.is_(False)
                )
            ) or 0
    except Exception as e:
        db.session.rollback()
        current_app.logger.warning("inject_notification_counts failed: %s", e)
        count = 0
    return dict(unread_notifications_count=int(count))


# -----------------------------
# Profile / Address / Security
# -----------------------------
@customer_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = current_user
    form = PersonalInfoForm(obj=user)

    if form.validate_on_submit():
        user.full_name = form.full_name.data.strip()
        user.email = form.email.data.strip()
        user.mobile = form.mobile.data.strip()
        user.trn = form.trn.data.strip() if hasattr(user, "trn") else getattr(user, "trn", None)
        db.session.commit()
        flash("Your personal information has been updated.", "success")
        return redirect(url_for('customer.profile'))

    return render_template('customer/profile.html', form=form)


@customer_bp.route('/address', methods=['GET', 'POST'])
@login_required
def address():
    user = current_user
    form = AddressForm()

    if request.method == 'GET':
        form.address.data = getattr(user, "address", "") or ""

    if form.validate_on_submit():
        user.address = form.address.data
        db.session.commit()
        flash("Address updated successfully.", "success")
        return redirect(url_for("customer.address"))

    return render_template('customer/address.html', form=form)


@customer_bp.route('/update_delivery_address', methods=['GET', 'POST'])
@login_required
def update_delivery_address():
    user = current_user
    if request.method == 'POST':
        address = (request.form.get('address') or '').strip()
        user.address = address
        db.session.commit()
        flash('Delivery address updated successfully!', 'success')
        return redirect(url_for('customer.customer_dashboard'))
    return render_template('customer/update_delivery_address.html', address=getattr(user, 'address', '') or '')


@customer_bp.route('/security', methods=['GET', 'POST'])
@login_required
def security():
    form = PasswordChangeForm()
    if form.validate_on_submit():
        current_password = form.current_password.data.encode('utf-8')
        new_password = form.new_password.data.encode('utf-8')

        # current_user.password may be str (hashed) or bytes; normalize
        stored = current_user.password.encode() if isinstance(current_user.password, str) else current_user.password
        if stored and bcrypt.checkpw(current_password, stored):
            hashed = bcrypt.hashpw(new_password, bcrypt.gensalt())
            current_user.password = hashed.decode('utf-8')
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("customer.security"))
        else:
            flash("Incorrect current password.", "danger")
    return render_template('customer/security.html', form=form)


# -----------------------------
# Authorized Pickup
# -----------------------------
@customer_bp.route('/authorized-pickup', methods=['GET'])
@login_required
def authorized_pickup_overview():
    pickups = AuthorizedPickup.query.filter_by(user_id=current_user.id).all()
    return render_template('customer/authorized_pickup_overview.html', pickups=pickups)


@customer_bp.route('/authorized-pickup/add', methods=['POST'])
@login_required
def authorized_pickup_add():
    data = request.json or {}
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


# -----------------------------
# Scheduled Delivery
# -----------------------------
@customer_bp.route('/schedule-delivery', methods=['GET'])
@login_required
def schedule_delivery_overview():
    deliveries = ScheduledDelivery.query.filter_by(user_id=current_user.id).all()
    return render_template('customer/schedule_delivery_overview.html', deliveries=deliveries)


from datetime import datetime

@customer_bp.route('/schedule-delivery/add', methods=['POST'])
@login_required
def schedule_delivery_add():
    data = request.get_json(silent=True) or {}

    # Accept both keys (so you don't break anything later)
    schedule_date = data.get("schedule_date") or data.get("date")
    schedule_time = data.get("schedule_time") or data.get("time")
    location      = (data.get("location") or "").strip()

    if not schedule_date or not schedule_time or not location:
        return jsonify({
            "success": False,
            "message": "Missing required fields: schedule_date, schedule_time, location"
        }), 400

    try:
        # ✅ Convert "HH:MM" string -> time object for SQLAlchemy Time column
        time_obj = datetime.strptime(schedule_time, "%H:%M").time()

        new_delivery = ScheduledDelivery(
            user_id=current_user.id,
            scheduled_date=datetime.strptime(schedule_date, "%Y-%m-%d").date(),
            scheduled_time=time_obj,  # ✅ FIXED

            location=location,

            # optional fields (handle multiple possible names)
            direction=(data.get("direction") or data.get("directions") or "").strip(),
            mobile_number=(data.get("mobile_number") or data.get("mobile") or "").strip(),
            person_receiving=(data.get("person_receiving") or "").strip(),
        )

        db.session.add(new_delivery)
        db.session.commit()

        return jsonify({"success": True, "message": "Schedule added successfully"}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("schedule_delivery_add failed")
        return jsonify({"success": False, "message": str(e)}), 400

# -----------------------------
# Referrals
# -----------------------------

@customer_bp.route('/referrals', methods=['GET', 'POST'])
@login_required
def referrals():
    user = current_user
    full_name = user.full_name or user.email  # fallback

    # ✅ Make sure this user actually has a referral code
    referral_code = ensure_user_referral_code(user)

    form = ReferralForm()

    if form.validate_on_submit():
        friend_email = form.friend_email.data.strip()

        if not friend_email or not EMAIL_REGEX.match(friend_email):
            flash("Please enter a valid email address.", "warning")
        elif not referral_code:
            # This shouldn't happen because of ensure_user_referral_code,
            # but just in case.
            flash("Your referral code is not set yet. Please try again later.", "danger")
        else:
            # Our send_referral_email now returns True/False instead of raising
            ok = send_referral_email(friend_email, referral_code, full_name)
            if ok:
                flash(f"Referral email sent to {friend_email}.", "success")
            else:
                flash("Failed to send referral email. Please try again later.", "danger")

    return render_template(
        'customer/referrals.html',
        referral_code=referral_code,
        full_name=full_name,
        form=form,
    )


# -----------------------------
# Profile Picture Upload
# -----------------------------
@customer_bp.route('/upload-profile-pic', methods=['POST'])
@login_required
def upload_profile_pic():
    file = request.files.get('profile_pic')
    if file and file.filename:
        filename = f"{current_user.id}.jpg"
        path = os.path.join(PROFILE_UPLOAD_FOLDER, filename)
        os.makedirs(PROFILE_UPLOAD_FOLDER, exist_ok=True)
        file.save(path)
        current_user.profile_pic = filename
        db.session.commit()
    return redirect(url_for('customer.customer_dashboard'))


# -----------------------------
# Contact page (emails support)
# -----------------------------
@customer_bp.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name') or ''
        email = request.form.get('email') or ''
        subject = request.form.get('subject') or ''
        message_body = request.form.get('message') or ''

        try:
            msg = MailMessage(
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


# -----------------------------
# Static policy pages
# -----------------------------
@customer_bp.route('/terms')
def terms():
    return render_template('customer/terms.html', current_year=datetime.now().year)

@customer_bp.route('/privacy')
def privacy():
    return render_template('customer/privacy.html', current_year=datetime.now().year)
