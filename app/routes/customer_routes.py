# app/routes/customer_routes.py (imports)
import os, re, io
import math
from math import ceil
from datetime import datetime, date, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    current_app, flash, jsonify, send_from_directory, 
    send_file, abort
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.utils import secure_filename
import mimetypes
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
from app.utils.invoice_pdf import generate_invoice_pdf
from app.utils.messages import make_thread_key
from app.utils.message_notify import send_new_message_email
from app.utils.email_utils import pick_admin_recipient
from app.calculator_data import categories
from app import allowed_file, mail
from app.extensions import db
from app.calculator_data import calculate_charges, CATEGORIES, USD_TO_JMD
from app.calculator_data import get_freight
from app.services.package_view import fetch_packages_normalized

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet


# Models — NO Bill model; alias Message to avoid clash with Flask-Mail's Message
from app.models import (
    User, Package, Invoice,
    AuthorizedPickup, ScheduledDelivery,
    Notification,
    Message as DBMessage,  # 👈 avoid name clash with Flask-Mail
    Wallet, WalletTransaction, Payment, Settings,
    Prealert, PackageAttachment,
)
from sqlalchemy import func, or_
# Email class from Flask-Mail (alias to avoid clash)
from flask_mail import Message as MailMessage
from sqlalchemy.orm import selectinload

customer_bp = Blueprint('customer', __name__, template_folder='templates/customer')


def _calc_handling(weight_lbs: float) -> float:
    """
    Match your calculator handling rules.
    Weight is in lbs (rounded up).
    """
    try:
        w = int(math.ceil(float(weight_lbs or 0)))
    except Exception:
        w = 0

    if 40 < w <= 50:
        return 2000.0
    elif 51 <= w <= 60:
        return 3000.0
    elif 61 <= w <= 80:
        return 5000.0
    elif 81 <= w <= 100:
        return 10000.0
    elif w > 100:
        return 20000.0
    return 0.0


# -----------------------------
# Upload folders (from config)
# -----------------------------
def _profile_folder():
    # keep profile pics in static (fine)
    return os.path.join(current_app.root_path, "static", "profile_pics")

def _invoice_folder():
    # IMPORTANT: comes from config; should be /var/data/invoices on Render
    return current_app.config["INVOICE_UPLOAD_FOLDER"]


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
    us_suite_prefix = getattr(settings, "us_suite_prefix", None) or "KCDA-FAFL#"
    us_city         = getattr(settings, "us_city", None)         or "Doral"
    us_state        = getattr(settings, "us_state", None)        or "Florida"
    us_zip          = getattr(settings, "us_zip", None)          or "33172"

    # Build the address dict used by the template
    reg = (getattr(user, "registration_number", "") or "").strip()
    reg = reg.replace("FAFL#", "").replace("FAFL ", "FAFL").replace(" ", "")

    # If you want: KCDA-FAFL10059 (recommended)
    us_address = {
        "recipient": user.full_name,
        "address_line1": us_street,
        "address_line2": f"KCDA-{reg}" if reg else "KCDA",
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
            if file and getattr(file, "filename", ""):
                original = file.filename.strip()
                if original and allowed_file(original):
                    from app.utils.cloudinary_storage import upload_invoice_image
                    filename = upload_invoice_image(file)   # store URL in invoice_filename



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
            created_at=datetime.now(timezone.utc),
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
@customer_bp.route("/packages", methods=["GET"])
@login_required
def view_packages():
    """
    Customer Packages page (SYNCED with Admin View Packages)

    ✅ Uses SAME normalized package data as admin
    ✅ Same value / weight / date logic
    ✅ Attachments included (view-only)
    ✅ Pagination + per_page selector
    """

    form = PackageUpdateForm()

    # -------------------------
    # Filters
    # -------------------------
    status_filter = (request.args.get("status") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    tracking_number = (request.args.get("tracking_number") or "").strip()

    page = request.args.get("page", 1, type=int)

    # per-page selector
    per_page = request.args.get("per_page", 10, type=int)
    allowed_per_page = [10, 25, 50, 100, 500]
    if per_page not in allowed_per_page:
        per_page = 10

    # -------------------------
    # Base query (MATCHES ADMIN)
    # -------------------------
    q = (
        db.session.query(
            Package,
            User.full_name,
            User.registration_number
        )
        .join(User, Package.user_id == User.id)
        .filter(Package.user_id == current_user.id)
    )

    # Status filter
    if status_filter:
        q = q.filter(Package.status == status_filter)

    # Date filters (safe)
    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
            q = q.filter(
                func.date(func.coalesce(Package.date_received, Package.created_at)) >= df
            )
        except ValueError:
            flash("Invalid start date.", "warning")

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
            q = q.filter(
                func.date(func.coalesce(Package.date_received, Package.created_at)) <= dt
            )
        except ValueError:
            flash("Invalid end date.", "warning")

    if tracking_number:
        q = q.filter(Package.tracking_number.ilike(f"%{tracking_number}%"))

    # Order EXACTLY like admin
    q = q.order_by(
        func.date(func.coalesce(Package.date_received, Package.created_at)).desc(),
        Package.id.desc()
    )

    # -------------------------
    # Pagination (manual, admin-style)
    # -------------------------
    total = q.count()
    total_pages = max((total + per_page - 1) // per_page, 1)

    q = q.limit(per_page).offset((page - 1) * per_page)

    # 🔑 NORMALIZED, SHARED DATA
    packages = fetch_packages_normalized(
        base_query=q,
        include_user=True,
        include_attachments=True,
    )

    return render_template(
        "customer/customer_packages.html",
        packages=packages,
        form=form,

        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        tracking_number=tracking_number,

        page=page,
        total_pages=total_pages,
        per_page=per_page,
    )



@customer_bp.route('/package/<int:pkg_id>', methods=['GET', 'POST'])
@login_required
def package_detail(pkg_id):
    form = PackageUpdateForm()
    pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()

    if form.validate_on_submit():
        declared_value = form.declared_value.data or 65
        invoice_file = form.invoice_file.data

        if invoice_file and getattr(invoice_file, "filename", "") and allowed_file(invoice_file.filename):
            from app.utils.cloudinary_storage import upload_invoice_image
            pkg.invoice_file = upload_invoice_image(invoice_file)  # store URL


        declared_value = float(declared_value or 65)

        pkg.declared_value = declared_value

        # ✅ keep admin View Packages in sync
        pkg.value = declared_value

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
    d["amount_due"] = float(pkg.amount_due or 0)


    return render_template('customer/package_detail.html', pkg=d, form=form)

@customer_bp.route("/packages/<int:pkg_id>/docs", methods=["POST"])
@login_required
def package_upload_docs(pkg_id):
    pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()

    # declared value
    dv = request.form.get("declared_value")
    if dv:
        try:
            dvf = float(dv)
            pkg.declared_value = dvf
            pkg.value = dvf  # mirror for admin View Packages
        except ValueError:
            flash("Declared value must be a number.", "warning")
            return redirect(url_for("customer.view_packages"))

    files = [
        request.files.get("invoice_file_1"),
        request.files.get("invoice_file_2"),
        request.files.get("invoice_file_3"),
    ]

    from app.utils.cloudinary_storage import upload_invoice_image

    saved_any = False
    for f in files:
        if f and f.filename and allowed_file(f.filename):
            original = f.filename.strip()

            # ✅ upload to Cloudinary, store URL
            url = upload_invoice_image(f)

            db.session.add(PackageAttachment(
                package_id=pkg.id,
                file_name=url,           # ✅ store URL
                original_name=original
            ))
            saved_any = True

            # OPTIONAL: keep “main invoice” field in sync too
            # (only set if empty, or always overwrite — your choice)
            if not getattr(pkg, "invoice_file", None):
                pkg.invoice_file = url

    db.session.commit()
    flash(
        "Updated package documents successfully.",
        "success" if (saved_any or dv) else "info"
    )
    return redirect(url_for("customer.view_packages"))

@customer_bp.route("/package-attachment/<int:attachment_id>")
@login_required
def view_package_attachment(attachment_id):
    attachment = PackageAttachment.query.get_or_404(attachment_id)

    # only owner can view
    if attachment.package.user_id != current_user.id:
        abort(403)

    # ✅ NEW: if stored as a Cloudinary URL, redirect to it
    if attachment.file_name and str(attachment.file_name).startswith(("http://", "https://")):
        return redirect(attachment.file_name)

    # otherwise: legacy disk file
    upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not upload_folder:
        current_app.logger.error("INVOICE_UPLOAD_FOLDER not configured")
        abort(500)

    file_path = os.path.join(upload_folder, attachment.file_name)
    if not os.path.exists(file_path):
        current_app.logger.warning("Attachment file missing: %s", file_path)
        abort(404)

    return send_from_directory(
        directory=upload_folder,
        path=attachment.file_name,
        as_attachment=False
    )

@customer_bp.route("/packages/attachments/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete_package_attachment_customer(attachment_id):
    """
    Customer deletes an attachment they uploaded, only if:
    - attachment belongs to a package owned by current_user
    - package is NOT Ready for Pick Up or Delivered (locked)
    Deletes from DB and disk.
    """
    a = PackageAttachment.query.get_or_404(attachment_id)

    # Ensure this attachment belongs to a package owned by this customer
    if not a.package or a.package.user_id != current_user.id:
        abort(403)

    # Lock if package is already finalized
    s = (a.package.status or "").strip()
    if s in ("Ready for Pick Up", "Delivered"):
        flash("This package is locked, so attachments can't be deleted.", "warning")
        return redirect(url_for("customer.view_packages"))

    # Delete file from disk (best effort)
    try:
        upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
        fp = os.path.join(upload_folder, a.file_name)
        if upload_folder and os.path.exists(fp):
            os.remove(fp)
    except Exception:
        pass

    db.session.delete(a)
    db.session.commit()
    flash("Attachment deleted.", "success")

    # Return to packages list (and optionally reopen modal via hash)
    return redirect(url_for("customer.view_packages"))

@customer_bp.route('/update_declared_value', methods=['POST'])
@login_required
def update_declared_value():
    data = request.get_json() or {}
    pkg_id = data.get('pkg_id', None)
    value = data.get('declared_value', None)

    if pkg_id is None or value is None:
        return jsonify(success=False, error="Missing fields"), 400

    pkg = Package.query.filter_by(id=pkg_id, user_id=current_user.id).first_or_404()
    try:
        value_f = float(value)
    except Exception:
        return jsonify(success=False, error="Declared value must be numeric"), 400

    pkg.declared_value = value_f
    pkg.value = value_f          # ✅ mirror for admin View Packages
    db.session.commit()
    return jsonify(success=True)


# -----------------------------
# Transactions (Bills & Payments)
# -----------------------------
from datetime import datetime, timedelta

@customer_bp.route("/transactions/all", methods=["GET"])
@customer_required
def transactions_all():
    # pagination
    page = request.args.get("page", type=int, default=1)
    per_page = request.args.get("per_page", type=int, default=10)

    allowed = [10, 25, 50, 100, 500]
    if per_page not in allowed:
        per_page = 10
    if page < 1:
        page = 1

    # filters
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()  # paid | pending | ""
    days = request.args.get("days", type=int)  # 7 | 30 | None

    invoices = (
        Invoice.query
        .filter(Invoice.user_id == current_user.id)
        .order_by(Invoice.date_submitted.desc().nullslast(), Invoice.id.desc())
        .all()
    )

    payments = (
        Payment.query
        .filter(Payment.user_id == current_user.id)
        .order_by(Payment.created_at.desc().nullslast(), Payment.id.desc())
        .all()
    )

    def _dt(x):
        return x or datetime.utcnow()

    def _num(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    def _is_paid(s: str) -> bool:
        s = (s or "").strip().lower()
        return s in ("paid", "complete", "completed", "success", "successful")

    rows = []

    # Bills / Invoices
    for inv in invoices:
        amount = _num(getattr(inv, "amount_due", None))
        if amount <= 0:
            amount = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))

        inv_status = (getattr(inv, "status", "") or "").strip() or "Pending"
        inv_date = _dt(inv.date_issued or inv.date_submitted or getattr(inv, "created_at", None))

        rows.append({
            "type": "invoice",
            "date": inv_date,
            "reference_main": inv.invoice_number or f"INV-{inv.id}",
            "reference_sub": "",
            "status": inv_status,
            "method": "—",
            "amount": amount,
            "view_url": url_for("customer.bill_invoice_modal", invoice_id=inv.id),
            "pdf_url": url_for("customer.invoice_pdf", invoice_id=inv.id),
        })

    # Payments
    for p in payments:
        pay_date = _dt(getattr(p, "created_at", None))
        rows.append({
            "type": "payment",
            "date": pay_date,
            "reference_main": f"RCPT-{p.id}",
            "reference_sub": f"Invoice ID: {p.invoice_id}" if p.invoice_id else "",
            "status": "Paid",
            "method": (p.method or "Cash"),
            "amount": _num(getattr(p, "amount_jmd", 0)),
            "view_url": url_for("customer.receipt_modal", payment_id=p.id),
            "pdf_url": url_for("customer.receipt_pdf_inline", payment_id=p.id),
        })

    # ---- DATE FILTER (applies to BOTH lists) ----
    if days in (7, 30):
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = [r for r in rows if (r.get("date") or datetime.utcnow()) >= cutoff]

    # ---- SEARCH FILTER ----
    if q:
        ql = q.lower()
        def _hay(r):
            return f"{r.get('reference_main','')} {r.get('reference_sub','')}".lower()
        rows = [r for r in rows if ql in _hay(r)]

    # ---- STATUS FILTER ----
    if status == "paid":
        rows = [r for r in rows if _is_paid(r.get("status"))]
    elif status == "pending":
        rows = [r for r in rows if not _is_paid(r.get("status"))]

    rows.sort(key=lambda r: r["date"], reverse=True)

    total = len(rows)
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    page_rows = rows[start:end]

    # summary metrics
    total_shipments = total
    total_owed = sum(r["amount"] for r in rows if not _is_paid(r.get("status")))
    pending_count = sum(1 for r in rows if not _is_paid(r.get("status")))
    billing_records = total

    return render_template(
        "customer/transactions/all.html",
        rows=page_rows,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        per_page_options=allowed,
        # keep filter state
        q=q,
        status=status,
        days=days,
        # cards
        total_shipments=total_shipments,
        total_owed=total_owed,
        pending_count=pending_count,
        billing_records=billing_records,
    )

@customer_bp.route("/transactions/receipts/<int:payment_id>/modal")
@customer_required
def receipt_modal(payment_id):
    p = Payment.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()
    inv = Invoice.query.filter_by(id=p.invoice_id, user_id=current_user.id).first()

    receipt_no = f"RCPT-{p.id}"

    def _num(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    breakdown = {"freight": 0.0, "duty": 0.0, "handling": 0.0, "gct": 0.0, "total": 0.0}

    if inv:
        pkgs = Package.query.filter_by(invoice_id=inv.id).all()

        freight_total = 0.0
        duty_total = 0.0
        handling_total = 0.0
        gct_total = 0.0

        for pkg in pkgs:
            freight_total += _num(getattr(pkg, "freight_fee", getattr(pkg, "freight", 0)))
            handling_total += _num(getattr(pkg, "storage_fee", getattr(pkg, "handling", 0)))
            duty_total += _num(getattr(pkg, "duty", 0))
            gct_total += _num(getattr(pkg, "gct", 0))

        inv_total = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))
        if inv_total <= 0:
            inv_total = freight_total + handling_total + duty_total + gct_total

        breakdown = {
            "freight": float(freight_total),
            "duty": float(duty_total),
            "handling": float(handling_total),
            "gct": float(gct_total),
            "total": float(inv_total),
        }

    wants_panel = request.headers.get("X-Panel") == "1"
    if wants_panel:
        return render_template(
            "customer/transactions/_receipt_panel_body.html",
            payment=p,
            invoice=inv,
            breakdown=breakdown,
            receipt_no=receipt_no
        )

    return render_template(
        "customer/transactions/_receipt_modal_body.html",
        payment=p,
        invoice=inv,
        receipt_no=receipt_no
    )



@customer_bp.route("/transactions/bills")
@customer_required
def view_bills():
    invoices = (
        Invoice.query
        .filter_by(user_id=current_user.id)
        .order_by(Invoice.id.desc())
        .all()
    )
    return render_template("customer/transactions/bills.html", invoices=invoices)

@customer_bp.route("/transactions/bills/<int:invoice_id>/modal")
@customer_required
def bill_invoice_modal(invoice_id):
    inv = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first_or_404()

    pkgs = Package.query.filter_by(invoice_id=inv.id).order_by(Package.id.asc()).all()

    def _num(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    packages = []
    for p in pkgs:
        w_raw = _num(getattr(p, "weight", 0))
        w = int(ceil(w_raw))
        if w < 1 and w_raw > 0:
            w = 1

        freight = float(get_freight(w) or 0)

        packages.append({
            "house_awb": p.house_awb or "",
            "description": p.description or "",
            "weight": w,
            "value": _num(getattr(p, "value", 0)),
            "freight": freight,
            "handling": _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges": _num(getattr(p, "other_charges", 0)),
            "duty": _num(getattr(p, "duty", 0)),
            "scf": _num(getattr(p, "scf", 0)),
            "envl": _num(getattr(p, "envl", 0)),
            "caf": _num(getattr(p, "caf", 0)),
            "gct": _num(getattr(p, "gct", 0)),
            "discount_due": _num(getattr(p, "discount_due", 0)),
        })

    subtotal = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))
    discount_total = _num(getattr(inv, "discount_total", 0))

    amount_due = _num(getattr(inv, "amount_due", 0))
    if amount_due <= 0:
        amount_due = max(subtotal - discount_total, 0)

    dt = inv.date_issued or inv.date_submitted or inv.created_at

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number or f"INV-{inv.id}",
        "date": dt,
        "date_display": dt.strftime("%b %d, %Y %I:%M %p") if dt else "",
        "status": (getattr(inv, "status", "") or "").strip() or ("Paid" if amount_due <= 0 else "Pending"),
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal": float(subtotal),
        "discount_total": float(discount_total),
        "total_due": float(amount_due),
        "packages": packages,
    }

    wants_panel = request.headers.get("X-Panel") == "1"
    if wants_panel:
        return render_template("customer/transactions/_invoice_panel_body.html", invoice=invoice_dict)

    return render_template("customer/transactions/_invoice_modal_body.html", invoice=invoice_dict)



@customer_bp.route("/transactions/payments")
@customer_required
def view_payments():
    payments = (
        Payment.query
        .filter(Payment.user_id == current_user.id)
        .order_by(Payment.id.desc())
        .all()
    )

    invoice_ids = [p.invoice_id for p in payments if p.invoice_id]
    invoices = {}
    if invoice_ids:
        for inv in Invoice.query.filter(Invoice.id.in_(invoice_ids)).all():
            invoices[inv.id] = inv

    return render_template(
        "customer/transactions/payments.html",
        payments=payments,
        invoices=invoices
    )


@customer_bp.route("/transactions/receipts/<int:payment_id>", endpoint="view_receipt")
@customer_required
def view_receipt(payment_id):
    p = Payment.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()
    inv = Invoice.query.filter_by(id=p.invoice_id, user_id=current_user.id).first()
    return render_template("customer/transactions/receipt_view.html", payment=p, invoice=inv)


@customer_bp.route("/transactions/receipts/<int:payment_id>/pdf-inline", endpoint="receipt_pdf_inline")
@customer_required
def receipt_pdf_inline(payment_id):
    p = Payment.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()
    inv = Invoice.query.filter_by(id=p.invoice_id, user_id=current_user.id).first()

    if not inv:
        abort(404)

    pkgs = Package.query.filter_by(invoice_id=inv.id).order_by(Package.id.asc()).all()

    def _num(val, default=0.0):
        try:
            return float(val or 0)
        except Exception:
            return float(default)

    packages = []
    for pkg in pkgs:
        w_raw = _num(getattr(pkg, "weight", 0))
        w_lbs = int(ceil(w_raw))
        freight = float(get_freight(w_lbs) or 0.0)
        handling = float(_calc_handling(w_lbs) or 0.0)

        packages.append({
            "house_awb": pkg.house_awb or "",
            "description": pkg.description or "",
            "weight": w_lbs,
            "value": _num(getattr(pkg, "value", 0)),
            "freight": freight,
            "handling": handling,
            "other_charges": _num(getattr(pkg, "other_charges", 0)),
            "duty": _num(getattr(pkg, "duty", 0)),
            "scf": _num(getattr(pkg, "scf", 0)),
            "envl": _num(getattr(pkg, "envl", 0)),
            "caf": _num(getattr(pkg, "caf", 0)),
            "gct": _num(getattr(pkg, "gct", 0)),
            "discount_due": _num(getattr(pkg, "discount_due", 0)),
        })

    subtotal = _num(getattr(inv, "subtotal", None)) or _num(getattr(inv, "grand_total", 0)) or _num(getattr(inv, "amount", 0))
    discount_total = _num(getattr(inv, "discount_total", 0))
    payments_total = _num(getattr(p, "amount_jmd", 0))
    balance = max((subtotal - discount_total) - payments_total, 0.0)

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number,
        "date": p.created_at or datetime.utcnow(),
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal": float(subtotal),
        "discount_total": float(discount_total),
        "payments_total": float(payments_total),
        "total_due": float(balance),
        "packages": packages,
        "receipt_no": f"RCPT-{p.id}",
        "payment_method": p.method or "Cash",
        "payment_reference": p.reference or "",
        "payment_notes": p.notes or "",
        "doc_type": "receipt",
    }

    rel = generate_invoice_pdf(invoice_dict)  # returns something like "invoices/xxx.pdf"
    abs_path = os.path.join(current_app.static_folder, rel)

    if not os.path.exists(abs_path):
        abort(404)

    return send_file(
        abs_path,
        mimetype="application/pdf",
        as_attachment=False,                 # IMPORTANT: inline preview
        download_name=os.path.basename(abs_path),
        conditional=True
    )



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

        from app.utils.cloudinary_storage import upload_invoice_image
        filename = upload_invoice_image(invoice_file)

        # persist to this package
        try:
            if declared_value:
                try:
                    pkg.declared_value = float(declared_value)
                    pkg.value = pkg.declared_value
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

    pkgs = (
        Package.query
        .filter_by(invoice_id=inv.id)
        .order_by(Package.id.asc())
        .all()
    )

    def _num(val, default=0.0):
        try:
            return float(val or 0)
        except Exception:
            return float(default)

    packages = []
    for p in pkgs:
        packages.append({
            "house_awb":     p.house_awb,
            "description":   p.description,
            "weight":        int(math.ceil(_num(p.weight))),
            "value":         _num(getattr(p, "value", 0)),
            "freight":       _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
            "storage":       _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges": _num(getattr(p, "other_charges", 0)),
            "duty":          _num(getattr(p, "duty", 0)),
            "scf":           _num(getattr(p, "scf", 0)),
            "envl":          _num(getattr(p, "envl", 0)),
            "caf":           _num(getattr(p, "caf", 0)),
            "gct":           _num(getattr(p, "gct", 0)),
            "discount_due":  _num(getattr(p, "discount_due", 0)),
        })

    invoice_dict = {
        "id":             inv.id,
        "invoice_number":         inv.invoice_number,
        "date":           inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code":  current_user.registration_number,
        "customer_name":  current_user.full_name,
        "subtotal":       _num(getattr(inv, "subtotal", 0)),
        "discount_total": _num(getattr(inv, "discount_total", 0)),
        "total_due":      _num(getattr(inv, "grand_total", getattr(inv, "amount", 0))),
        "packages":       packages,
        "branch":         getattr(inv, "branch", None),
        "staff":          getattr(inv, "staff", None),
        "notes":          getattr(inv, "notes", None),
    }

    return render_template("customer/invoices/view_invoice.html", invoice=invoice_dict)


@customer_bp.route("/invoices/<int:invoice_id>/pdf")
@customer_required
def invoice_pdf(invoice_id):
    inv = Invoice.query.filter_by(id=invoice_id, user_id=current_user.id).first()
    if not inv:
        flash("Invoice not found or you don't have permission to view it.", "danger")
        return redirect(url_for("customer.view_bills"))

    pkgs = (
        Package.query
        .filter_by(invoice_id=inv.id)
        .order_by(Package.id.asc())
        .all()
    )

    def _num(val, default=0.0):
        try:
            return float(val or 0)
        except Exception:
            return float(default)

    packages = []
    for p in pkgs:
        packages.append({
            "house_awb":     p.house_awb or "",
            "description":   p.description or "",
            "weight":        int(math.ceil(_num(getattr(p, "weight", 0)))),

            "value":         _num(getattr(p, "value", getattr(p, "value_usd", 0))),
            "freight":       _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
            "storage":       _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges": _num(getattr(p, "other_charges", 0)),
            "duty":          _num(getattr(p, "duty", 0)),
            "scf":           _num(getattr(p, "scf", 0)),
            "envl":          _num(getattr(p, "envl", 0)),
            "caf":           _num(getattr(p, "caf", 0)),
            "gct":           _num(getattr(p, "gct", 0)),
            "discount_due":  _num(getattr(p, "discount_due", 0)),
        })

    # ✅ totals should NOT be 0 just because inv.subtotal is blank
    subtotal = _num(getattr(inv, "subtotal", None)) or _num(getattr(inv, "grand_total", 0)) or _num(getattr(inv, "amount", 0))
    discount_total = _num(getattr(inv, "discount_total", 0))

    # ✅ if you store payments in Payment.amount_jmd, use it
    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
    payments_total = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    total_due = max((_num(getattr(inv, "grand_total", subtotal)) or subtotal) - discount_total - float(payments_total), 0.0)

    invoice_dict = {
        "id":            inv.id,
        "number":        inv.invoice_number,  # ✅ IMPORTANT: use "number" (matches your PDF templates)
        "date":          inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code": current_user.registration_number,
        "customer_name": current_user.full_name,
        "subtotal":      float(subtotal),
        "discount_total": float(discount_total),
        "payments_total": float(payments_total),
        "total_due":     float(total_due),
        "packages":      packages,
    }

    from app.utils.invoice_pdf import generate_invoice_pdf
    rel = generate_invoice_pdf(invoice_dict)  # ✅ correct generator
    return redirect(url_for("static", filename=rel))


# -----------------------------
# Messaging
# -----------------------------

@customer_bp.route("/messages", methods=["GET", "POST"])
@login_required
def view_messages():
    form = SendMessageForm()

    # Pick an admin recipient (prefer superadmin, then admin, then first user)
    admin = (
        (User.query.filter(User.is_superadmin.is_(True)).order_by(User.id.asc()).first()
         if hasattr(User, "is_superadmin") else None)
        or (User.query.filter(User.role == "admin").order_by(User.id.asc()).first()
            if hasattr(User, "role") else None)
        or User.query.order_by(User.id.asc()).first()
    )

    # ---- Send new message to admin ----
    if request.method == "POST" and form.validate_on_submit():
        if not admin:
            flash("No admin user found to receive messages.", "danger")
            return redirect(url_for("customer.view_messages"))

        subject = (form.subject.data or "").strip() or "Message"
        body = (form.body.data or "").strip()

        if not body:
            flash("Message can't be empty.", "warning")
            return redirect(url_for("customer.view_messages"))

        msg = DBMessage(
            sender_id=current_user.id,
            recipient_id=admin.id,
            subject=subject,
            body=body,
            is_read=False,
            created_at=datetime.now(timezone.utc),  # ✅ timezone-aware UTC
        )
        db.session.add(msg)
        db.session.commit()

        # Email notify admin (notification only)
        if admin.email:
            preview = (body[:120] + "…") if len(body) > 120 else body
            send_new_message_email(
                user_email=admin.email,
                user_name=admin.full_name or "Admin",
                message_subject=subject,
                message_body=preview,
                recipient_user_id=admin.id
            )

        flash("Message sent!", "success")
        return redirect(url_for("customer.view_messages", box="sent"))

    # ---- Gmail-style mailbox controls ----
    box = (request.args.get("box") or "inbox").lower()   # inbox | sent | all
    q = (request.args.get("q") or "").strip()
    try:
        per_page = int(request.args.get("per_page") or 20)
    except Exception:
        per_page = 20
    per_page = per_page if per_page in (10, 20, 50, 100) else 20

    # Base: customer mailbox messages only (no threads)
    base = DBMessage.query.filter(
        sa.or_(
            DBMessage.sender_id == current_user.id,
            DBMessage.recipient_id == current_user.id
        )
    )

    # Box filter
    if box == "inbox":
        base = base.filter(DBMessage.recipient_id == current_user.id)
    elif box == "sent":
        base = base.filter(DBMessage.sender_id == current_user.id)
    else:
        box = "all"  # normalize

    # Search (subject/body)
    if q:
        like = f"%{q}%"
        base = base.filter(
            sa.or_(
                DBMessage.subject.ilike(like),
                DBMessage.body.ilike(like),
            )
        )

    messages_list = base.order_by(DBMessage.created_at.desc()).limit(per_page).all()

    # For display (always show "Administrator" as the other side)
    rows = []
    for m in messages_list:
        other_id = m.recipient_id if m.sender_id == current_user.id else m.sender_id
        other = User.query.get(other_id)
        rows.append((m, other))

    return render_template(
        "customer/messages.html",
        form=form,
        admin=admin,
        rows=rows,
        box=box,
        q=q,
        per_page=per_page,
    )


@customer_bp.route("/messages/<int:msg_id>", methods=["GET"])
@login_required
def customer_message_detail(msg_id):
    msg = DBMessage.query.get_or_404(msg_id)

    # ✅ Authorization: customer must be sender or recipient
    if msg.sender_id != current_user.id and msg.recipient_id != current_user.id:
        flash("Not authorized.", "danger")
        return redirect(url_for("customer.view_messages"))

    # ✅ Mark as read when OPENED (Gmail behavior)
    if msg.recipient_id == current_user.id and not msg.is_read:
        msg.is_read = True
        db.session.commit()

    # figure out the "other" person for display
    other_id = msg.recipient_id if msg.sender_id == current_user.id else msg.sender_id
    other = User.query.get(other_id)

    return render_template(
        "customer/message_detail.html",
        msg=msg,
        other=other,
    )


@customer_bp.route("/messages/<int:msg_id>/reply", methods=["POST"])
@login_required
def customer_message_reply(msg_id):
    original = DBMessage.query.get_or_404(msg_id)

    # ✅ Authorization
    if original.sender_id != current_user.id and original.recipient_id != current_user.id:
        flash("Not authorized.", "danger")
        return redirect(url_for("customer.view_messages"))

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message can't be empty.", "warning")
        return redirect(url_for("customer.customer_message_detail", msg_id=msg_id))

    subject = (request.form.get("subject") or "").strip() or f"Re: {original.subject or 'Message'}"

    # reply goes to the other person
    recipient_id = original.sender_id if original.sender_id != current_user.id else original.recipient_id
    recipient = User.query.get(recipient_id)

    msg = DBMessage(
        sender_id=current_user.id,
        recipient_id=recipient_id,
        subject=subject,
        body=body,
        is_read=False,
        created_at=datetime.now(timezone.utc),  # ✅ timezone-aware UTC
    )
    db.session.add(msg)
    db.session.commit()

    # ✅ Email notify admin (or other recipient)
    if recipient and recipient.email:
        preview = (body[:120] + "…") if len(body) > 120 else body
        send_new_message_email(
            user_email=recipient.email,
            user_name=recipient.full_name or "User",
            message_subject=subject,
            message_body=preview,
            recipient_user_id=recipient.id
        )

    flash("Reply sent.", "success")
    return redirect(url_for("customer.customer_message_detail", msg_id=msg.id))



# -----------------------------
# Notifications
# -----------------------------
@customer_bp.route("/notifications", methods=["GET"])
@login_required
def view_notifications():
    notes = (Notification.query
             .filter(
                 sa.or_(
                     Notification.user_id == current_user.id,
                     Notification.is_broadcast.is_(True)  # broadcast
                 )
             )
             .order_by(Notification.created_at.desc())
             .all())
    return render_template("customer/notifications.html", notes=notes)


@customer_bp.route("/notifications/mark_read/<int:nid>", methods=["POST"])
@login_required
def mark_notification_read(nid):
    n = Notification.query.get_or_404(nid)

    # customer can mark read if:
    # - it belongs to them OR it's a broadcast
    if n.user_id != current_user.id and not n.is_broadcast:
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
                    sa.and_(
                        sa.or_(
                            Notification.user_id == current_user.id,
                            Notification.is_broadcast.is_(True)
                        ),
                        Notification.is_read.is_(False)
                    )
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
        user.full_name = (form.full_name.data or "").strip()
        user.email     = (form.email.data or "").strip()
        user.mobile    = (form.mobile.data or "").strip()

        # TRN (safe even if missing)
        if hasattr(user, "trn"):
            user.trn = (form.trn.data or "").strip()

        # Track updated_at if your model supports it
        # (won't break if the column doesn't exist)
        try:
            from datetime import datetime
            if hasattr(user, "updated_at"):
                user.updated_at = datetime.utcnow()
        except Exception:
            pass

        db.session.commit()
        flash("Your personal information has been updated.", "success")
        return redirect(url_for('customer.profile'))

    # Optional: verified flag (template uses this)
    email_verified = False
    try:
        email_verified = bool(getattr(user, "email_verified", False))
    except Exception:
        email_verified = False

    # Optional: change email URL (only pass if you actually have that endpoint)
    change_email_url = None
    try:
        change_email_url = url_for("customer.change_email_modal")
    except Exception:
        change_email_url = None

    return render_template(
        'customer/profile.html',
        form=form,
        email_verified=email_verified,
        change_email_url=change_email_url
    )

@customer_bp.route("/profile/change-email", methods=["GET", "POST"])
@login_required
def change_email():
    user = current_user

    if request.method == "POST":
        current_password = request.form.get("current_password", "") or ""
        new_email = (request.form.get("new_email") or "").strip().lower()

        # Basic validation
        if not current_password:
            flash("Please enter your current password.", "warning")
            return redirect(url_for("customer.change_email"))

        if not new_email:
            flash("Please enter a new email address.", "warning")
            return redirect(url_for("customer.change_email"))

        # Check password (your User.password is bcrypt hash bytes)
        try:
            ok = bcrypt.checkpw(current_password.encode("utf-8"), user.password)
        except Exception:
            ok = False

        if not ok:
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("customer.change_email"))

        # Prevent duplicates
        exists = User.query.filter(User.email == new_email, User.id != user.id).first()
        if exists:
            flash("That email is already in use.", "danger")
            return redirect(url_for("customer.change_email"))

        # Update email
        user.email = new_email
        if hasattr(user, "updated_at"):
            user.updated_at = datetime.utcnow()

        db.session.commit()
        flash("Email updated successfully.", "success")
        return redirect(url_for("customer.profile"))

    return render_template("customer/change_email.html")


@customer_bp.route("/profile/change-email/modal", methods=["GET"])
@login_required
def change_email_modal():
    # partial template ONLY (no extends)
    return render_template("customer/_change_email_modal_body.html", user=current_user)


@customer_bp.route("/profile/change-email/modal", methods=["POST"])
@login_required
def change_email_modal_submit():
    user = current_user

    current_password = request.form.get("current_password", "") or ""
    new_email = (request.form.get("new_email") or "").strip().lower()

    errors = {}

    if not current_password:
        errors["current_password"] = "Current password is required."

    if not new_email:
        errors["new_email"] = "New email is required."

    # Validate password
    if not errors.get("current_password"):
        try:
            ok = bcrypt.checkpw(current_password.encode("utf-8"), user.password)
        except Exception:
            ok = False
        if not ok:
            errors["current_password"] = "Current password is incorrect."

    # Validate uniqueness
    if new_email and not errors.get("new_email"):
        exists = User.query.filter(User.email == new_email, User.id != user.id).first()
        if exists:
            errors["new_email"] = "That email is already in use."

    if errors:
        # return 400 with structured errors for the modal to render
        return jsonify({"ok": False, "errors": errors}), 400

    # Save
    user.email = new_email
    if hasattr(user, "updated_at"):
        user.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        "ok": True,
        "message": "Email updated successfully.",
        "email": user.email
    })


@customer_bp.route("/address", methods=["GET", "POST"])
@login_required
def address():
    user = current_user
    form = AddressForm(obj=user)  # expects form.address

    if form.validate_on_submit():
        user.address = (form.address.data or "").strip()

        # update timestamp if column exists
        if hasattr(user, "updated_at"):
            user.updated_at = datetime.utcnow()

        db.session.commit()
        flash("Delivery address updated successfully.", "success")
        return redirect(url_for("customer.address"))

    return render_template("customer/address.html", form=form)


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


@customer_bp.route("/security", methods=["GET", "POST"])
@login_required
def security():
    form = PasswordChangeForm()

    if form.validate_on_submit():
        current_pw = form.current_password.data.encode("utf-8")
        new_pw     = form.new_password.data.encode("utf-8")

        # 🔐 Ensure stored password is bytes
        stored_pw = current_user.password
        if isinstance(stored_pw, str):
            stored_pw = stored_pw.encode("utf-8")

        # ❌ Wrong current password
        if not bcrypt.checkpw(current_pw, stored_pw):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("customer.security"))

        # ✅ Hash new password (BYTES)
        hashed_pw = bcrypt.hashpw(new_pw, bcrypt.gensalt())
        current_user.password = hashed_pw

        # optional timestamp
        if hasattr(current_user, "updated_at"):
            current_user.updated_at = datetime.utcnow()

        db.session.commit()
        flash("Password updated successfully.", "success")
        return redirect(url_for("customer.security"))

    return render_template("customer/security.html", form=form)


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


@customer_bp.route('/schedule-delivery/add', methods=['POST'])
@login_required
def schedule_delivery_add():
    data = request.get_json(silent=True) or {}

    schedule_date = (data.get("schedule_date") or data.get("date") or "").strip()
    schedule_time = (data.get("schedule_time") or data.get("time") or "").strip()
    location      = (data.get("location") or "").strip()

    # DEBUG: log what actually came in
    current_app.logger.info(f"[schedule_delivery_add] payload keys={list(data.keys())} date={schedule_date} time={schedule_time} location={location}")

    if not schedule_date or not schedule_time or not location:
        return jsonify({
            "success": False,
            "message": "Missing required fields: schedule_date, schedule_time, location",
            "received_keys": list(data.keys()),
            "received": {
                "schedule_date": schedule_date,
                "schedule_time": schedule_time,
                "location": location,
            }
        }), 400

    # ---- Parse date (YYYY-MM-DD or MM/DD/YYYY) ----
    d = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(schedule_date, fmt).date()
            break
        except Exception:
            pass
    if not d:
        return jsonify({"success": False, "message": f"Invalid date format: {schedule_date}"}), 400

    # ---- Normalize time to a STRING your model expects ----
    # Accept "HH:MM" or "HH:MM AM/PM"
    t_str = None
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            t_obj = datetime.strptime(schedule_time, fmt).time()
            t_str = t_obj.strftime("%H:%M")   # store as "14:30"
            break
        except Exception:
            pass
    if not t_str:
        return jsonify({"success": False, "message": f"Invalid time format: {schedule_time}"}), 400

    try:
        new_delivery = ScheduledDelivery(
            user_id=current_user.id,
            scheduled_date=d,
            scheduled_time=t_str,  # ✅ string
            location=location,
            direction=(data.get("direction") or data.get("directions") or "").strip(),
            mobile_number=(data.get("mobile_number") or data.get("mobile") or "").strip(),
            person_receiving=(data.get("person_receiving") or "").strip(),
        )
        db.session.add(new_delivery)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Scheduled successfully",
            "delivery": {
                "id": new_delivery.id,
                "scheduled_date": new_delivery.scheduled_date.isoformat(),
                "scheduled_time": new_delivery.scheduled_time,
                "location": new_delivery.location,
                "person_receiving": new_delivery.person_receiving or "",
            }
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("schedule_delivery_add failed")
        return jsonify({"success": False, "message": f"{type(e).__name__}: {str(e)}"}), 500

@customer_bp.route('/schedule-delivery/<int:delivery_id>/cancel', methods=['POST'])
@login_required
def schedule_delivery_cancel(delivery_id):
    d = ScheduledDelivery.query.filter_by(id=delivery_id, user_id=current_user.id).first_or_404()

    # only allow cancel if not already delivered
    if (d.status or "").lower() == "delivered":
        return jsonify({"success": False, "message": "This delivery is already delivered."}), 400

    d.status = "Cancelled"
    db.session.commit()
    return jsonify({"success": True, "message": "Delivery cancelled."}), 200



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
        folder = _profile_folder()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename)

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
