# app/routes/customer_routes.py (imports)
import os, re, io
import math
from math import ceil
from datetime import datetime, date, timezone, timedelta
import base64
import secrets


from flask import (
    Blueprint, render_template, request, redirect, url_for,
    current_app, flash, jsonify, send_from_directory, 
    send_file, abort
)
from io import BytesIO
from flask import Response, stream_with_context
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.utils import secure_filename
import mimetypes
import bcrypt
import sqlalchemy as sa
import requests

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
from app import mail
from app.utils.files import allowed_file
from app.extensions import db, csrf
from app.calculator_data import calculate_charges, CATEGORIES, USD_TO_JMD
from app.calculator_data import get_freight
from app.services.package_view import fetch_packages_normalized

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from urllib.parse import urlsplit, urlunsplit


# Models — NO Bill model; alias Message to avoid clash with Flask-Mail's Message
from app.models import (
    User, Package, Invoice,
    AuthorizedPickup, ScheduledDelivery,
    Notification,
    Message as DBMessage,  # 👈 avoid name clash with Flask-Mail
    MessageAttachment,
    Wallet, WalletTransaction, Payment, Settings,
    Prealert, PackageAttachment,
)
from app.models import normalize_tracking
from sqlalchemy import func, or_
# Email class from Flask-Mail (alias to avoid clash)
from flask_mail import Message as MailMessage
from sqlalchemy.orm import selectinload
from app.utils.cloudinary_storage import serve_prealert_invoice_file
from decimal import Decimal

customer_bp = Blueprint('customer', __name__, template_folder='templates/customer')

DELIVERY_FEE_AMOUNT = Decimal("1000.00")
DELIVERY_FEE_CURRENCY = "JMD"



def _static_image_data_uri(filename: str):
    """
    Convert a static image into a base64 data URI so WeasyPrint
    can embed it reliably inside PDFs.
    """
    try:
        path = os.path.join(current_app.static_folder, filename)
        if not os.path.exists(path):
            return None

        mime, _ = mimetypes.guess_type(path)
        if not mime:
            mime = "image/png"

        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")

        return f"data:{mime};base64,{data}"
    except Exception:
        return None

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


def _redirect_or_send_attachment(path_or_url: str):
    u = (path_or_url or "").strip()
    if u.startswith(("http://", "https://")):
        return redirect(u)

    upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not upload_folder:
        abort(500)

    fp = os.path.join(upload_folder, u)
    if not os.path.exists(fp):
        abort(404)

    return send_from_directory(upload_folder, u, as_attachment=False)


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

def get_api_user():
    auth_header = (request.headers.get("Authorization") or "").strip()

    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.replace("Bearer ", "", 1).strip()
    if not token:
        return None

    return User.query.filter_by(api_token=token).first()

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

    status_norm = func.lower(func.trim(Package.status))

    total_shipped = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id,
            status_norm.notin_(('cancelled', 'canceled', 'deleted', 'draft'))
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
        invoice_url = None
        invoice_public_id = None
        invoice_resource_type = None
        invoice_original_name = None

        # ----------------------------
        # Upload invoice (optional)
        # ----------------------------
        if form.invoice.data and getattr(form.invoice.data, "filename", ""):
            f = form.invoice.data
            original = (f.filename or "").strip()

            if original and allowed_file(original):
                from app.utils.cloudinary_storage import upload_prealert_invoice

                invoice_original_name = original
                invoice_url, invoice_public_id, invoice_resource_type = upload_prealert_invoice(f)
            else:
                flash("Invalid invoice file type. Allowed: pdf, jpg, jpeg, png.", "warning")
                return render_template('customer/prealerts_create.html', form=form)

        prealert_number = generate_prealert_number()

        # ✅ normalize tracking so it matches Package consistently
        tracking = normalize_tracking((form.tracking_number.data or "").strip())

        pa = Prealert(
            prealert_number=prealert_number,
            customer_id=current_user.id,
            vendor_name=form.vendor_name.data,
            courier_name=form.courier_name.data,
            tracking_number=tracking,
            purchase_date=form.purchase_date.data,
            package_contents=form.package_contents.data,
            item_value_usd=float(form.item_value_usd.data or 0),

            # invoice fields
            invoice_filename=invoice_url,
            invoice_original_name=invoice_original_name,
            invoice_public_id=invoice_public_id,
            invoice_resource_type=invoice_resource_type,

            created_at=datetime.now(timezone.utc),
        )

        db.session.add(pa)
        db.session.commit()

        # ----------------------------
        # Try to link to a Package + attach invoice
        # ----------------------------
        try:
            if tracking and invoice_url:
                from app.utils.prealert_sync import sync_prealert_invoice_to_package

                # ✅ IMPORTANT: exact match on normalized tracking
                pkg = (Package.query
                       .filter(Package.user_id == current_user.id)
                       .filter(Package.tracking_number == tracking)
                       .order_by(Package.id.desc())
                       .first())

                if pkg:
                    synced = sync_prealert_invoice_to_package(pkg)
                    if synced:
                        db.session.commit()

        except Exception:
            current_app.logger.exception("[PREALERT->PACKAGE SYNC] failed")
            db.session.rollback()

        flash(f"Pre-alert PA-{prealert_number} submitted successfully!", "success")
        return redirect(url_for('customer.prealerts_view'))

    return render_template('customer/prealerts_create.html', form=form)

@customer_bp.route("/prealerts/invoice/<int:prealert_id>")
@login_required
def prealert_invoice(prealert_id):
    pa = Prealert.query.filter_by(id=prealert_id, customer_id=current_user.id).first_or_404()
    return serve_prealert_invoice_file(pa, download_name_prefix="prealert", as_attachment=False)

@customer_bp.route("/prealerts/invoice/<int:prealert_id>/download")
@login_required
def prealert_invoice_download(prealert_id):
    pa = Prealert.query.filter_by(id=prealert_id, customer_id=current_user.id).first_or_404()
    return serve_prealert_invoice_file(pa, download_name_prefix="prealert", as_attachment=True)

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

        if invoice_file and getattr(invoice_file, "filename", ""):
            original = invoice_file.filename.strip()

            from app.utils.files import allowed_file
            if allowed_file(original):
                from app.utils.cloudinary_storage import upload_package_attachment

                try:
                    url, public_id, rtype = upload_package_attachment(invoice_file)
                except Exception:
                    current_app.logger.exception("[PACKAGE DETAIL UPLOAD] cloud upload failed")
                    flash("Upload failed. Please try again.", "danger")
                    return redirect(url_for("customer.package_detail", pkg_id=pkg_id))

                if url:
                    db.session.add(PackageAttachment(
                        package_id=pkg.id,
                        file_name=url,          # legacy
                        file_url=url,           # ✅ NOT NULL in DB
                        original_name=original,
                        cloud_public_id=public_id,
                        cloud_resource_type=rtype,
                    ))

                    # optional: keep main invoice field in sync
                    pkg.invoice_file = url
            else:
                flash("File type not allowed.", "warning")
                return redirect(url_for("customer.package_detail", pkg_id=pkg_id))


        try:
            declared_value = float(declared_value or 65)
        except Exception:
            declared_value = 65.0

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

    return render_template('customer/package_detail.html', pkg=pkg, pkg_dict=d, form=form)

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
        request.files.get("invoice_file"),     # ✅ allow WTForms single file too
        request.files.get("invoice_file_1"),
        request.files.get("invoice_file_2"),
        request.files.get("invoice_file_3"),
    ]

    from app.utils.files import allowed_file
    from app.utils.cloudinary_storage import upload_package_attachment

    saved_any = False
    seen = set()

    for f in files:
        if not (f and f.filename):
            continue

        original = f.filename.strip()

        key = original.lower()
        if key in seen:
            continue
        seen.add(key)

        if not allowed_file(original):
            continue

        try:
            url, public_id, rtype = upload_package_attachment(f)
        except Exception:
            current_app.logger.exception("[CUSTOMER DOC UPLOAD] cloud upload failed")
            flash("Upload failed. Please try again.", "danger")
            return redirect(url_for("customer.view_packages"))

        if not url:
            flash("Upload failed (no URL returned).", "danger")
            return redirect(url_for("customer.view_packages"))

        db.session.add(PackageAttachment(
            package_id=pkg.id,
            file_name=url,           # keep existing behavior
            file_url=url,            # ✅ REQUIRED (NOT NULL in DB)
            original_name=original,
            cloud_public_id=public_id,
            cloud_resource_type=rtype,
        ))
        saved_any = True

        # OPTIONAL: keep “main invoice” field in sync too
        if not getattr(pkg, "invoice_file", None):
            pkg.invoice_file = url

    db.session.commit()
    flash(
        "Updated package documents successfully.",
        "success" if (saved_any or bool(dv)) else "info"
    )
    return redirect(url_for("customer.view_packages"))


@customer_bp.route("/package-attachment/<int:attachment_id>")
@login_required
def view_package_attachment(attachment_id):
    a = PackageAttachment.query.get_or_404(attachment_id)

    if not a.package or a.package.user_id != current_user.id:
        abort(403)

    url = (getattr(a, "file_url", None) or getattr(a, "file_name", None) or "").strip()

    display_name = (
        (getattr(a, "original_name", None) or "").strip()
        or (getattr(a, "file_name", None) or "").strip()
        or "attachment"
    )
    safe_name = secure_filename(display_name) or "attachment"

    if url.startswith(("http://", "https://")):
        try:
            r = requests.get(url, stream=True, timeout=30)
        except Exception:
            abort(502)

        if r.status_code != 200:
            abort(404)

        lower = safe_name.lower()
        if lower.endswith(".pdf"):
            content_type = "application/pdf"
        elif lower.endswith((".jpg", ".jpeg")):
            content_type = "image/jpeg"
        elif lower.endswith(".png"):
            content_type = "image/png"
        else:
            content_type = r.headers.get("Content-Type") or "application/octet-stream"

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp = Response(stream_with_context(generate()), mimetype=content_type)
        if content_type == "application/pdf":
            resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # legacy disk fallback
    upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not upload_folder:
        abort(500)

    fp = os.path.join(upload_folder, url)
    if not os.path.exists(fp):
        abort(404)

    guessed_type, _ = mimetypes.guess_type(fp)
    resp = send_from_directory(upload_folder, url, as_attachment=False)
    if guessed_type:
        resp.headers["Content-Type"] = guessed_type
    resp.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@customer_bp.route("/packages/attachments/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete_package_attachment_customer(attachment_id):
    a = PackageAttachment.query.get_or_404(attachment_id)

    if not a.package or a.package.user_id != current_user.id:
        abort(403)

    s = (a.package.status or "").strip()
    if s in ("Ready for Pick Up", "Delivered"):
        flash("This package is locked, so attachments can't be deleted.", "warning")
        return redirect(url_for("customer.view_packages"))

    # ✅ Delete from Cloudinary if we have public_id
    try:
        pub_id = getattr(a, "cloud_public_id", None)
        rtype  = getattr(a, "cloud_resource_type", None) or "raw"
        if pub_id:
            from app.utils.cloudinary_storage import delete_cloudinary_file
            delete_cloudinary_file(pub_id, resource_type=rtype)
    except Exception:
        pass

    # ✅ Legacy disk delete only if file_name is NOT a URL
    try:
        fname = (getattr(a, "file_name", "") or "").strip()
        if fname and not fname.startswith(("http://", "https://")):
            upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
            if upload_folder:
                fp = os.path.join(upload_folder, fname)
                if os.path.exists(fp):
                    os.remove(fp)
    except Exception:
        pass

    db.session.delete(a)
    db.session.commit()

    flash("Attachment deleted.", "success")
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
@customer_bp.route("/transactions/all", methods=["GET"])
@customer_required
def transactions_all():

    # -------------------------
    # pagination
    # -------------------------
    page = request.args.get("page", type=int, default=1)
    per_page = request.args.get("per_page", type=int, default=10)

    allowed = [10, 25, 50, 100, 500]
    if per_page not in allowed:
        per_page = 10
    if page < 1:
        page = 1

    # -------------------------
    # filters
    # -------------------------
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    days = request.args.get("days", type=int)

    invoices = (
        Invoice.query
        .filter(Invoice.user_id == current_user.id)
        .order_by(Invoice.date_submitted.desc().nullslast(), Invoice.id.desc())
        .all()
    )

    payments = (
        Payment.query
        .filter(Payment.user_id == current_user.id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .all()
    )

    def _dt(x):
        return x or datetime.utcnow()

    def _num(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    def normalize_method(m):
        m = (m or "").strip().lower()

        if m == "cash":
            return "Cash"

        if m in ("card", "credit", "debit"):
            return "Card"

        if m in ("bank", "bank transfer", "transfer"):
            return "Bank Transfer"

        if m in ("wallet", "wallet_credit"):
            return "Wallet"

        if m == "refund":
            return "Refund"

        return m.title() if m else ""

    def transaction_label(tx_type):
        labels = {
            "invoice_payment": "Invoice Payment",
            "delivery_payment": "Delivery Payment",
            "package_refund": "Package Refund",
            "delivery_refund": "Delivery Refund",
        }
        return labels.get(tx_type, "Transaction")

    rows = []

    # ======================================================
    # INVOICE ROWS
    # ======================================================
    for inv in invoices:

        inv_total = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))

        payments_list = [
            p for p in (getattr(inv, "payments", None) or [])
            if getattr(p, "transaction_type", "invoice_payment") == "invoice_payment"
            and getattr(p, "status", "completed") == "completed"
        ]

        paid_sum = sum(_num(getattr(p, "amount_jmd", 0)) for p in payments_list)

        latest_payment = None
        if payments_list:
            latest_payment = sorted(
                payments_list,
                key=lambda x: (getattr(x, "created_at", None) or datetime.utcnow(), getattr(x, "id", 0)),
                reverse=True
            )[0]

        owed = max(inv_total - paid_sum, 0.0)

        if owed <= 0 and inv_total > 0:
            inv_status = "paid"
        elif paid_sum > 0:
            inv_status = "partial"
        else:
            inv_status = "pending"

        inv_date = _dt(
            inv.date_issued
            or inv.date_submitted
            or getattr(inv, "created_at", None)
        )

        if inv_status in ("paid", "partial"):
            method_label = normalize_method(getattr(latest_payment, "method", "")) if latest_payment else ""
            method_label = method_label or "—"
        else:
            method_label = "Awaiting Payment"

        rows.append({
            "type": "invoice",
            "date": inv_date,
            "reference_main": inv.invoice_number or f"INV-{inv.id}",
            "reference_sub": "",
            "status": inv_status,
            "method": method_label,
            "amount_due": inv_total,
            "amount_paid": paid_sum,
            "amount_owed": owed,
            "view_url": url_for("customer.bill_invoice_modal", invoice_id=inv.id),
            "pdf_url": url_for("customer.invoice_pdf", invoice_id=inv.id),
            "full_url": url_for("customer.view_invoice_customer", invoice_id=inv.id),
            "is_paid": inv_status == "paid",
        })

    # ======================================================
    # PAYMENT / REFUND ROWS
    # ======================================================
    for p in payments:

        tx_type = (getattr(p, "transaction_type", "") or "").strip()
        tx_status = (getattr(p, "status", "completed") or "completed").strip().lower()

        amount = _num(getattr(p, "amount_jmd", 0))
        created = _dt(getattr(p, "created_at", None))
        method_label = normalize_method(getattr(p, "method", "")) or "—"

        reference_main = f"TX-{p.id}"
        reference_sub = transaction_label(tx_type)

        inv = None

        if tx_type == "invoice_payment":
            inv = Invoice.query.filter_by(
                id=p.invoice_id,
                user_id=current_user.id
            ).first()

            if inv:
                reference_sub = f"{transaction_label(tx_type)} • {inv.invoice_number or f'INV-{inv.id}'}"

        elif tx_type == "delivery_payment":

            if getattr(p, "scheduled_delivery_id", None):
                reference_sub = f"{transaction_label(tx_type)} • Delivery #{p.scheduled_delivery_id}"

        elif tx_type == "package_refund":

            if getattr(p, "claim_id", None):
                reference_sub = f"{transaction_label(tx_type)} • Claim #{p.claim_id}"

        elif tx_type == "delivery_refund":

            if getattr(p, "scheduled_delivery_id", None):
                reference_sub = f"{transaction_label(tx_type)} • Delivery #{p.scheduled_delivery_id}"

        if tx_type in ("package_refund", "delivery_refund"):
            amount_due = 0.0
            amount_paid = amount
            amount_owed = 0.0
        else:
            amount_due = amount
            amount_paid = amount if tx_status == "completed" else 0.0
            amount_owed = 0.0 if tx_status == "completed" else amount

        # ---------------------------
        # decide which modal / pdf / full page to open
        # ---------------------------
        if inv:
            view_url = url_for("customer.bill_invoice_modal", invoice_id=inv.id)
            pdf_url = url_for("customer.invoice_pdf", invoice_id=inv.id)
            full_url = url_for("customer.view_invoice_customer", invoice_id=inv.id)

        elif tx_type in ("package_refund", "delivery_refund"):
            view_url = url_for("customer.receipt_modal", payment_id=p.id)
            pdf_url = None
            full_url = None

        else:
            view_url = url_for("customer.receipt_modal", payment_id=p.id)
            pdf_url = url_for("customer.receipt_pdf_inline", payment_id=p.id)
            full_url = url_for("customer.view_receipt", payment_id=p.id)

        rows.append({
            "type": tx_type or "transaction",
            "date": created,
            "reference_main": reference_main,
            "reference_sub": reference_sub,
            "status": tx_status,
            "method": method_label,
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "amount_owed": amount_owed,
            "view_url": view_url,
            "pdf_url": pdf_url,
            "full_url": full_url,
            "is_paid": tx_status == "completed",
        })

    # ======================================================
    # DATE FILTER
    # ======================================================
    if days in (7, 30):
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = [r for r in rows if (r.get("date") or datetime.utcnow()) >= cutoff]

    # ======================================================
    # SEARCH FILTER
    # ======================================================
    if q:
        ql = q.lower()

        def _hay(r):
            return f"{r.get('reference_main','')} {r.get('reference_sub','')}".lower()

        rows = [r for r in rows if ql in _hay(r)]

    # ======================================================
    # STATUS FILTER
    # ======================================================
    if status:
        rows = [r for r in rows if (r.get("status") or "").lower() == status]

    # ======================================================
    # SORT + PAGINATE
    # ======================================================
    rows.sort(key=lambda r: r["date"], reverse=True)

    total = len(rows)
    total_pages = max((total + per_page - 1) // per_page, 1)

    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    page_rows = rows[start:end]

    # ======================================================
    # summary metrics
    # ======================================================
    total_shipments = len(invoices)
    billing_records = len(rows)
    total_owed = sum((r.get("amount_owed") or 0) for r in rows)
    pending_count = sum(1 for r in rows if (r.get("amount_owed") or 0) > 0)

    return render_template(
        "customer/transactions/all.html",
        rows=page_rows,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        per_page_options=allowed,
        q=q,
        status=status,
        days=days,
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
        bad_address_total = 0.0

        for pkg in pkgs:
            freight_total += _num(getattr(pkg, "freight_fee", getattr(pkg, "freight", 0)))
            handling_total += _num(getattr(pkg, "storage_fee", getattr(pkg, "handling", 0)))
            duty_total += _num(getattr(pkg, "duty", 0))
            gct_total += _num(getattr(pkg, "gct", 0))
            bad_address_total += _num(getattr(pkg, "bad_address_fee", 0))

        inv_total = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))
        if inv_total <= 0:
            inv_total = freight_total + handling_total + duty_total + gct_total

        breakdown = {
            "freight": float(freight_total),
            "duty": float(duty_total),
            "handling": float(handling_total),
            "gct": float(gct_total),
            "bad_address": float(bad_address_total),
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
        bad_address_fee = _num(getattr(p, "bad_address_fee", 0))

        packages.append({
            "house_awb": p.house_awb or "",
            "description": p.description or "",
            "weight": w,
            "value": _num(getattr(p, "value", 0)),
            "freight": freight,
            "handling": _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges": _num(getattr(p, "other_charges", 0)),
            "bad_address_fee": _num(getattr(p, "bad_address_fee", 0)),
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
            "bad_address_fee": _num(getattr(pkg, "bad_address_fee", 0)),
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
        return redirect(url_for('customer.transactions_all'))

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
            "house_awb":       p.house_awb or "",
            "description":     p.description or "",
            "weight":          int(math.ceil(_num(getattr(p, "weight", 0)))),
            "value":           _num(getattr(p, "value", getattr(p, "value_usd", 0))),
            "freight":         _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
            "storage":         _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges":   _num(getattr(p, "other_charges", 0)),
            "bad_address_fee": _num(getattr(p, "bad_address_fee", 0)),
            "duty":            _num(getattr(p, "duty", 0)),
            "scf":             _num(getattr(p, "scf", 0)),
            "envl":            _num(getattr(p, "envl", 0)),
            "caf":             _num(getattr(p, "caf", 0)),
            "gct":             _num(getattr(p, "gct", 0)),
            "discount_due":    _num(getattr(p, "discount_due", 0)),
        })

    subtotal = (
        _num(getattr(inv, "subtotal", None))
        or _num(getattr(inv, "grand_total", 0))
        or _num(getattr(inv, "amount", 0))
    )
    discount_total = _num(getattr(inv, "discount_total", 0))

    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
    payments_total = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    total_due = max(
        (_num(getattr(inv, "grand_total", subtotal)) or subtotal)
        - discount_total
        - float(payments_total),
        0.0
    )

    invoice_dict = {
        "id":             inv.id,
        "invoice_number": inv.invoice_number,
        "date":           inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code":  current_user.registration_number,
        "customer_name":  current_user.full_name,
        "status":         getattr(inv, "status", None),
        "subtotal":       float(subtotal),
        "discount_total": float(discount_total),
        "payments_total": float(payments_total),
        "total_due":      float(total_due),
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
            "house_awb":       p.house_awb or "",
            "description":     p.description or "",
            "weight":          int(math.ceil(_num(getattr(p, "weight", 0)))),

            "value":           _num(getattr(p, "value", getattr(p, "value_usd", 0))),
            "freight":         _num(getattr(p, "freight_fee", getattr(p, "freight", 0))),
            "storage":         _num(getattr(p, "storage_fee", getattr(p, "handling", 0))),
            "other_charges":   _num(getattr(p, "other_charges", 0)),
            "bad_address_fee": _num(getattr(p, "bad_address_fee", 0)),
            "duty":            _num(getattr(p, "duty", 0)),
            "scf":             _num(getattr(p, "scf", 0)),
            "envl":            _num(getattr(p, "envl", 0)),
            "caf":             _num(getattr(p, "caf", 0)),
            "gct":             _num(getattr(p, "gct", 0)),
            "discount_due":    _num(getattr(p, "discount_due", 0)),
        })

    subtotal = (
        _num(getattr(inv, "subtotal", None))
        or _num(getattr(inv, "grand_total", 0))
        or _num(getattr(inv, "amount", 0))
    )
    discount_total = _num(getattr(inv, "discount_total", 0))

    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
    payments_total = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    total_due = max(
        (_num(getattr(inv, "grand_total", subtotal)) or subtotal)
        - discount_total
        - float(payments_total),
        0.0
    )

    from app.models import Settings
    settings = Settings.query.get(1)

    raw_logo = (settings.logo_path if settings and settings.logo_path else "logo.png") or "logo.png"
    raw_logo = raw_logo.lstrip("/")
    if raw_logo.lower().startswith("static/"):
        raw_logo = raw_logo[7:]

    logo_data_uri = _static_image_data_uri(raw_logo)
    logo_url = url_for("static", filename=raw_logo, _external=True, _scheme="https")

    invoice_dict = {
        "id":             inv.id,
        "number":         inv.invoice_number,
        "date":           inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code":  current_user.registration_number,
        "customer_name":  current_user.full_name,
        "subtotal":       float(subtotal),
        "discount_total": float(discount_total),
        "payments_total": float(payments_total),
        "total_due":      float(total_due),
        "packages":       packages,

        # logo
        "logo_data_uri":  logo_data_uri,
        "logo_url":       logo_url,

        # template extras
        "settings":       settings,
        "USD_TO_JMD":     getattr(settings, "usd_to_jmd", None) or USD_TO_JMD,
    }

    from app.utils.invoice_pdf import generate_invoice_pdf
    rel = generate_invoice_pdf(invoice_dict)
    return redirect(url_for("static", filename=rel))

# -----------------------------
# Messaging
# -----------------------------

def _is_duplicate_customer_message(sender_id, recipient_id, subject, body, seconds=45):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    return DBMessage.query.filter(
        DBMessage.sender_id == sender_id,
        DBMessage.recipient_id == recipient_id,
        func.lower(func.trim(DBMessage.subject)) == (subject or "").strip().lower(),
        func.lower(func.trim(DBMessage.body)) == (body or "").strip().lower(),
        DBMessage.created_at >= cutoff
    ).first()

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

        dup = _is_duplicate_customer_message(current_user.id, admin.id, subject, body)
        if dup:
            flash("Duplicate message prevented.", "warning")
            return redirect(url_for("customer.view_messages"))

        msg = DBMessage(
            sender_id=current_user.id,
            recipient_id=admin.id,
            subject=subject,
            body=body,
            is_read=False,
            created_at=datetime.now(timezone.utc),
        )
        db.session.add(msg)
        db.session.flush()

        files = request.files.getlist("attachments")
        from app.utils.cloudinary_storage import upload_package_attachment

        for f in files:
            if not f or not f.filename:
                continue

            original_name = (f.filename or "").strip()
            if not allowed_file(original_name):
                continue

            try:
                f.stream.seek(0)
                url, public_id, rtype = upload_package_attachment(f)
            except Exception:
                current_app.logger.exception("[CUSTOMER MESSAGE ATTACHMENT] upload failed")
                continue

            if not url:
                continue

            db.session.add(MessageAttachment(
                message_id=msg.id,
                file_url=url,
                original_name=original_name,
                cloud_public_id=public_id,
                cloud_resource_type=rtype,
            ))

        db.session.commit()

        if admin.email:
            preview = (body[:120] + "…") if len(body) > 120 else body
            send_new_message_email(
                user_email=admin.email,
                user_name=admin.full_name or "Admin",
                message_subject=subject,
                message_body=preview,
                recipient_user_id=admin.id,
            )

        flash("Message sent successfully.", "success")
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

    if original.sender_id != current_user.id and original.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("customer.view_messages"))

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message can't be empty.", "warning")
        return redirect(url_for("customer.customer_message_detail", msg_id=msg_id))

    subject = (request.form.get("subject") or "").strip() or f"Re: {original.subject}"
    recipient_id = original.sender_id if original.sender_id != current_user.id else original.recipient_id

    dup = _is_duplicate_customer_message(current_user.id, recipient_id, subject, body)
    if dup:
        flash("Duplicate reply prevented.", "warning")
        return redirect(url_for("customer.customer_message_detail", msg_id=msg_id))

    msg = DBMessage(
        sender_id=current_user.id,
        recipient_id=recipient_id,
        subject=subject,
        body=body,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(msg)
    db.session.flush()

    files = request.files.getlist("attachments")
    from app.utils.cloudinary_storage import upload_package_attachment

    for f in files:
        if not f or not f.filename:
            continue

        original_name = (f.filename or "").strip()
        if not allowed_file(original_name):
            continue

        try:
            f.stream.seek(0)
            url, public_id, rtype = upload_package_attachment(f)
        except Exception:
            current_app.logger.exception("[CUSTOMER MESSAGE REPLY ATTACHMENT] upload failed")
            continue

        if not url:
            continue

        db.session.add(MessageAttachment(
            message_id=msg.id,
            file_url=url,
            original_name=original_name,
            cloud_public_id=public_id,
            cloud_resource_type=rtype,
        ))

    db.session.commit()

    flash("Reply sent.", "success")
    return redirect(url_for("customer.customer_message_detail", msg_id=msg.id))


@customer_bp.route("/messages/attachments/<int:attachment_id>")
@login_required
def view_message_attachment(attachment_id):
    a = MessageAttachment.query.get_or_404(attachment_id)
    m = a.message

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        abort(403)

    return redirect(a.file_url)


@customer_bp.route("/messages/attachments/<int:attachment_id>/download")
@login_required
def download_message_attachment(attachment_id):
    a = MessageAttachment.query.get_or_404(attachment_id)
    m = a.message

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        abort(403)

    return redirect(a.file_url)


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
    deliveries = (ScheduledDelivery.query
                  .filter_by(user_id=current_user.id)
                  .order_by(ScheduledDelivery.id.desc())
                  .all())

    return render_template(
        'customer/schedule_delivery_overview.html',
        deliveries=deliveries,
        delivery_fee=DELIVERY_FEE_AMOUNT,
        fee_currency=DELIVERY_FEE_CURRENCY
    )


@customer_bp.route('/schedule-delivery/add', methods=['POST'])
@login_required
def schedule_delivery_add():
    data = request.get_json(silent=True) or {}

    return jsonify({
        "success": False,
        "message": "Customer delivery scheduling is temporarily disabled. Please contact support."
    }), 403

    schedule_date = (data.get("schedule_date") or data.get("date") or "").strip()
    location      = (data.get("location") or "").strip()

    # ✅ NEW: area zone (required)
    area_zone = (data.get("area_zone") or "").strip()

    t_from = "09:00"
    t_to   = "17:00"
    scheduled_time_str = "09:00 - 17:00"

    current_app.logger.info(
        "[schedule_delivery_add] keys=%s date=%s location=%s area_zone=%s fixed_time=09:00-17:00",
        list(data.keys()), schedule_date, location, area_zone
    )

    if (not schedule_date) or (not location) or (not area_zone):
        return jsonify({
            "success": False,
            "message": "Missing required fields: schedule_date, location, area_zone.",
            "received_keys": list(data.keys()),
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

    # ---- Parse time helpers ----
    def _parse_time_to_24h_str(s: str):
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%H:%M", "%I:%M %p"):
            try:
                t_obj = datetime.strptime(s, fmt).time()
                return t_obj.strftime("%H:%M")  # always store "14:30"
            except Exception:
                continue
        return None

    # ✅ Decide if we’re using range or legacy single time
    t_from = _parse_time_to_24h_str(time_from_in)
    t_to   = _parse_time_to_24h_str(time_to_in)

    # If user only sent the old single time, keep that behavior
    if not (t_from and t_to) and schedule_time_single:
        t_single = _parse_time_to_24h_str(schedule_time_single)
        if not t_single:
            return jsonify({"success": False, "message": f"Invalid time format: {schedule_time_single}"}), 400

        # store legacy only
        t_from = None
        t_to = None
        scheduled_time_str = t_single
    else:
        # range path
        if not t_from or not t_to:
            return jsonify({
                "success": False,
                "message": "Invalid time range. Please use HH:MM or HH:MM AM/PM for Time From and Time To."
            }), 400

        # ✅ validate from < to
        try:
            tf_dt = datetime.strptime(t_from, "%H:%M")
            tt_dt = datetime.strptime(t_to, "%H:%M")
            if tt_dt <= tf_dt:
                return jsonify({"success": False, "message": "Time To must be after Time From."}), 400
        except Exception:
            pass

        scheduled_time_str = f"{t_from} - {t_to}"  # fallback display

    # ==========================================================
    # ✅ DELIVERY RULES (day + zone)
    # Zones:
    #   kgn_core, kgn_outside, pm_core, pm_outside
    #
    # Days:
    #   Tue/Thu: Kingston core FREE only
    #   Wed/Fri: Portmore/SpanishTown core FREE only
    #   Sat: Core=1000, Outside=1500 (both routes)
    #   Other days: not allowed
    # ==========================================================
    allowed_zones = {"kgn_core", "kgn_outside", "pm_core", "pm_outside"}
    if area_zone not in allowed_zones:
        return jsonify({"success": False, "message": "Invalid delivery area selected."}), 400

    dow = d.weekday()  # Mon=0 ... Sun=6
    is_tue = (dow == 1)
    is_wed = (dow == 2)
    is_thu = (dow == 3)
    is_fri = (dow == 4)
    is_sat = (dow == 5)

    # default outcome
    fee_amt = Decimal("1000.00")
    fee_status = "Unpaid"

    if is_sat:
        # Saturday: paid
        if area_zone.endswith("_outside"):
            fee_amt = Decimal("1500.00")
        else:
            fee_amt = Decimal("1000.00")
        fee_status = "Unpaid"

    elif is_tue or is_thu:
        # Tue/Thu: Kingston core FREE only
        if area_zone != "kgn_core":
            return jsonify({
                "success": False,
                "message": "On Tue/Thu we only deliver FREE within Kingston Core. Outside areas are Saturday only."
            }), 400
        fee_amt = Decimal("0.00")
        fee_status = "Waived"

    elif is_wed or is_fri:
        # Wed/Fri: Portmore/SpanishTown core FREE only
        if area_zone != "pm_core":
            return jsonify({
                "success": False,
                "message": "On Wed/Fri we only deliver FREE within Portmore/Spanish Town Core. Outside areas are Saturday only."
            }), 400
        fee_amt = Decimal("0.00")
        fee_status = "Waived"

    else:
        return jsonify({
            "success": False,
            "message": "Delivery is available Tue/Thu (Kingston), Wed/Fri (Portmore/Spanish Town), or Saturday."
        }), 400

    try:
        new_delivery = ScheduledDelivery(
            user_id=current_user.id,
            scheduled_date=d,

            # ✅ store zone
            area_zone=area_zone,

            # ✅ range fields
            scheduled_time_from=t_from,
            scheduled_time_to=t_to,

            # ✅ legacy field
            scheduled_time=scheduled_time_str,

            location=location,
            direction=(data.get("direction") or data.get("directions") or "").strip(),
            mobile_number=(data.get("mobile_number") or data.get("mobile") or "").strip(),
            person_receiving=(data.get("person_receiving") or "").strip(),

            # ✅ fee rules applied here
            delivery_fee=fee_amt,
            fee_currency=DELIVERY_FEE_CURRENCY,
            fee_status=fee_status,
        )

        db.session.add(new_delivery)
        db.session.commit()

        year = datetime.utcnow().year
        new_delivery.invoice_number = f"DEL-{year}-{new_delivery.id:06d}"
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Scheduled successfully",
            "delivery": {
                "id": new_delivery.id,
                "invoice_number": new_delivery.invoice_number,
                "scheduled_date": new_delivery.scheduled_date.isoformat(),

                "scheduled_time": new_delivery.scheduled_time or "",
                "scheduled_time_from": new_delivery.scheduled_time_from or "",
                "scheduled_time_to": new_delivery.scheduled_time_to or "",

                "location": new_delivery.location,
                "area_zone": new_delivery.area_zone,

                "person_receiving": new_delivery.person_receiving or "",
                "delivery_fee": str(new_delivery.delivery_fee or fee_amt),
                "fee_currency": new_delivery.fee_currency or DELIVERY_FEE_CURRENCY,
                "fee_status": new_delivery.fee_status or fee_status,
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


@customer_bp.route("/schedule-delivery/<int:delivery_id>/invoice", methods=["GET"])
@login_required
def delivery_invoice_view(delivery_id):
    d = ScheduledDelivery.query.filter_by(
        id=delivery_id,
        user_id=current_user.id
    ).first_or_404()

    settings = Settings.query.first()

    return render_template(
        "customer/delivery_invoice.html",
        d=d,
        settings=settings
    )

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


@customer_bp.route("/api/dashboard", methods=["GET"])
def api_customer_dashboard():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    settings = db.session.get(Settings, 1)

    us_street = getattr(settings, "us_street", None) or "3200 NW 112th Avenue"
    us_city = getattr(settings, "us_city", None) or "Doral"
    us_state = getattr(settings, "us_state", None) or "Florida"
    us_zip = getattr(settings, "us_zip", None) or "33172"

    reg = (getattr(user, "registration_number", "") or "").strip()
    reg = reg.replace("FAFL#", "").replace("FAFL ", "FAFL").replace(" ", "")

    overseas_packages = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id,
            Package.status == "Overseas"
        )
    ) or 0

    ready_to_pickup = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id,
            Package.status == "Ready for Pick Up"
        )
    ) or 0

    status_norm = func.lower(func.trim(Package.status))

    total_shipped = db.session.scalar(
        sa.select(func.count()).select_from(Package).where(
            Package.user_id == user.id,
            status_norm.notin_(('cancelled', 'canceled', 'deleted', 'draft'))
        )
    ) or 0

    ready_packages = (
        Package.query
        .filter_by(user_id=user.id, status="Ready for Pick Up")
        .order_by(Package.received_date.desc().nullslast())
        .limit(5)
        .all()
    )

    return jsonify({
        "user": {
            "full_name": user.full_name,
            "registration": user.registration_number,
            "email": user.email,
            "mobile": user.mobile
        },
        "addresses": {
            "overseas": {
                "recipient": user.full_name or "",
                "street": us_street,
                "suite": f"KCDA-{reg}" if reg else "KCDA",
                "city": us_city,
                "state": us_state,
                "zip": us_zip
            },
            "home": (getattr(user, "address", "") or "").strip()
        },
        "stats": {
            "overseas": overseas_packages,
            "ready": ready_to_pickup,
            "shipped": total_shipped,
            "wallet": float(user.wallet_balance or 0)
        },
        "ready_packages": [
            {
                "id": pkg.id,
                "house_awb": pkg.house_awb or "",
                "status": pkg.status or "",
                "description": pkg.description or "",
                "tracking_number": pkg.tracking_number or "",
                "weight": int(math.ceil(float(pkg.weight or 0))) if pkg.weight is not None else 0,
                "received_date": (
                    pkg.received_date.strftime("%Y-%m-%d")
                    if getattr(pkg, "received_date", None)
                    else ""
                ),
                "amount_due": float(pkg.amount_due or 0)
            }
            for pkg in ready_packages
        ]
    })

@customer_bp.route("/api/packages", methods=["GET"])
def api_customer_packages():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    q = (
        db.session.query(
            Package,
            User.full_name,
            User.registration_number
        )
        .join(User, Package.user_id == User.id)
        .filter(Package.user_id == user.id)
        .order_by(
            func.date(func.coalesce(Package.date_received, Package.created_at)).desc(),
            Package.id.desc()
        )
    )

    packages = fetch_packages_normalized(
        base_query=q,
        include_user=True,
        include_attachments=True,
    )

    return jsonify({
        "packages": [
            {
                "id": pkg.get("id"),
                "house_awb": pkg.get("house_awb") or "",
                "status": pkg.get("status") or "",
                "description": pkg.get("description") or "",
                "tracking_number": pkg.get("tracking_number") or "",
                "weight": pkg.get("weight") or 0,
                "date_received": (
                    pkg.get("date_received").strftime("%Y-%m-%d")
                    if hasattr(pkg.get("date_received"), "strftime") and pkg.get("date_received")
                    else (pkg.get("date_received") or "")
                ),
                "amount_due": float(pkg.get("amount_due") or 0),
                "declared_value": float(pkg.get("declared_value") or 0),
            }
            for pkg in packages
        ]
    })

@customer_bp.route("/api/package/<int:pkg_id>", methods=["GET"])
def api_customer_package_detail(pkg_id):
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    pkg = Package.query.filter_by(id=pkg_id, user_id=user.id).first()
    if not pkg:
        return jsonify({"error": "Package not found"}), 404

    attachments = []
    try:
        for a in getattr(pkg, "attachments", []) or []:
            attachments.append({
                "id": a.id,
                "original_name": getattr(a, "original_name", "") or "",
                "file_url": getattr(a, "file_url", "") or getattr(a, "file_name", "") or "",
            })
    except Exception:
        attachments = []

    return jsonify({
        "id": pkg.id,
        "house_awb": pkg.house_awb or "",
        "status": pkg.status or "",
        "description": pkg.description or "",
        "tracking_number": pkg.tracking_number or "",
        "weight": float(pkg.weight or 0),
        "date_received": (
            pkg.received_date.strftime("%Y-%m-%d")
            if getattr(pkg, "received_date", None)
            else ""
        ),
        "declared_value": float(getattr(pkg, "declared_value", 0) or 0),
        "amount_due": float(pkg.amount_due or 0),
        "invoice_file": getattr(pkg, "invoice_file", "") or "",
        "attachments": attachments,
    })

@customer_bp.route("/api/prealerts", methods=["GET"])
def api_customer_prealerts():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    prealerts = (
        Prealert.query
        .filter_by(customer_id=user.id)
        .order_by(Prealert.created_at.desc())
        .all()
    )

    return jsonify({
        "prealerts": [
            {
                "id": p.id,
                "prealert_number": p.prealert_number,
                "vendor_name": p.vendor_name or "",
                "courier_name": p.courier_name or "",
                "tracking_number": p.tracking_number or "",
                "purchase_date": (
                    p.purchase_date.strftime("%Y-%m-%d")
                    if getattr(p, "purchase_date", None)
                    else ""
                ),
                "package_contents": p.package_contents or "",
                "item_value_usd": float(getattr(p, "item_value_usd", 0) or 0),
                "created_at": (
                    p.created_at.strftime("%Y-%m-%d")
                    if getattr(p, "created_at", None)
                    else ""
                ),
            }
            for p in prealerts
        ]
    })

@customer_bp.route("/api/prealerts", methods=["POST"])
def api_customer_prealerts_create():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    # Accept either JSON or multipart/form-data
    if request.content_type and "multipart/form-data" in request.content_type:
        data = request.form
        invoice_file = request.files.get("invoice")
    else:
        data = request.get_json(silent=True) or {}
        invoice_file = None

    vendor_name = (data.get("vendor_name") or "").strip()
    courier_name = (data.get("courier_name") or "").strip()
    tracking_number = normalize_tracking((data.get("tracking_number") or "").strip())
    package_contents = (data.get("package_contents") or "").strip()

    purchase_date_raw = (data.get("purchase_date") or "").strip()
    item_value_raw = data.get("item_value_usd")

    if not vendor_name:
        return jsonify({"error": "Vendor name is required"}), 400
    if not tracking_number:
        return jsonify({"error": "Tracking number is required"}), 400
    if not package_contents:
        return jsonify({"error": "Package contents are required"}), 400

    purchase_date = None
    if purchase_date_raw:
        try:
            purchase_date = datetime.strptime(purchase_date_raw, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "Purchase date must be YYYY-MM-DD"}), 400

    try:
        item_value_usd = float(item_value_raw or 0)
    except Exception:
        return jsonify({"error": "Item value must be numeric"}), 400

    invoice_url = None
    invoice_public_id = None
    invoice_resource_type = None
    invoice_original_name = None

    if invoice_file and getattr(invoice_file, "filename", ""):
        original = (invoice_file.filename or "").strip()

        if not allowed_file(original):
            return jsonify({"error": "Invalid invoice file type. Allowed: pdf, jpg, jpeg, png."}), 400

        from app.utils.cloudinary_storage import upload_prealert_invoice

        invoice_original_name = original
        invoice_url, invoice_public_id, invoice_resource_type = upload_prealert_invoice(invoice_file)

    prealert_number = generate_prealert_number()

    pa = Prealert(
        prealert_number=prealert_number,
        customer_id=user.id,
        vendor_name=vendor_name,
        courier_name=courier_name,
        tracking_number=tracking_number,
        purchase_date=purchase_date,
        package_contents=package_contents,
        item_value_usd=item_value_usd,
        invoice_filename=invoice_url,
        invoice_original_name=invoice_original_name,
        invoice_public_id=invoice_public_id,
        invoice_resource_type=invoice_resource_type,
        created_at=datetime.now(timezone.utc),
    )

    db.session.add(pa)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": f"Pre-alert PA-{prealert_number} submitted successfully",
        "prealert": {
            "id": pa.id,
            "prealert_number": pa.prealert_number,
            "vendor_name": pa.vendor_name or "",
            "courier_name": pa.courier_name or "",
            "tracking_number": pa.tracking_number or "",
            "purchase_date": pa.purchase_date.strftime("%Y-%m-%d") if pa.purchase_date else "",
            "package_contents": pa.package_contents or "",
            "item_value_usd": float(pa.item_value_usd or 0),
            "invoice_filename": pa.invoice_filename or "",
            "invoice_original_name": pa.invoice_original_name or "",
        }
    }), 201

@customer_bp.route("/api/transactions", methods=["GET"])
def api_customer_transactions():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    invoices = (
        Invoice.query
        .filter(Invoice.user_id == user.id)
        .order_by(Invoice.date_submitted.desc().nullslast(), Invoice.id.desc())
        .all()
    )

    payments = (
        Payment.query
        .filter(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .all()
    )

    deliveries = (
        ScheduledDelivery.query
        .filter(ScheduledDelivery.user_id == user.id)
        .order_by(ScheduledDelivery.created_at.desc(), ScheduledDelivery.id.desc())
        .all()
    )

    def _dt(x):
        return x or datetime.utcnow()

    def _num(x):
        try:
            return float(x or 0)
        except Exception:
            return 0.0

    def normalize_method(m):
        m = (m or "").strip().lower()

        if m == "cash":
            return "Cash"
        if m in ("card", "credit", "debit"):
            return "Card"
        if m in ("bank", "bank transfer", "transfer", "bank_transfer"):
            return "Bank Transfer"
        if m in ("wallet", "wallet_credit"):
            return "Wallet"
        if m == "refund":
            return "Refund"

        return m.title() if m else ""

    def transaction_label(tx_type):
        labels = {
            "invoice_payment": "Invoice Payment",
            "delivery_payment": "Delivery Payment",
            "package_refund": "Package Refund",
            "delivery_refund": "Delivery Refund",
        }
        return labels.get(tx_type, "Transaction")

    rows = []

    # Invoice rows
    for inv in invoices:
        inv_total = _num(getattr(inv, "grand_total", getattr(inv, "subtotal", 0)))

        payments_list = [
            p for p in (getattr(inv, "payments", None) or [])
            if getattr(p, "transaction_type", "invoice_payment") == "invoice_payment"
            and getattr(p, "status", "completed") == "completed"
        ]

        paid_sum = sum(_num(getattr(p, "amount_jmd", 0)) for p in payments_list)

        latest_payment = None
        if payments_list:
            latest_payment = sorted(
                payments_list,
                key=lambda x: (
                    getattr(x, "created_at", None) or datetime.utcnow(),
                    getattr(x, "id", 0)
                ),
                reverse=True
            )[0]

        owed = max(inv_total - paid_sum, 0.0)

        if owed <= 0 and inv_total > 0:
            inv_status = "paid"
        elif paid_sum > 0:
            inv_status = "partial"
        else:
            inv_status = "pending"

        inv_date = _dt(
            getattr(inv, "date_issued", None)
            or getattr(inv, "date_submitted", None)
            or getattr(inv, "created_at", None)
        )

        if inv_status in ("paid", "partial"):
            method_label = normalize_method(getattr(latest_payment, "method", "")) if latest_payment else ""
            method_label = method_label or "—"
        else:
            method_label = "Awaiting Payment"

        rows.append({
            "type": "invoice",
            "reference_main": inv.invoice_number or f"INV-{inv.id}",
            "reference_sub": "",
            "date": inv_date.strftime("%Y-%m-%d %H:%M:%S") if inv_date else "",
            "status": inv_status,
            "method": method_label,
            "amount_due": inv_total,
            "amount_paid": paid_sum,
            "amount_owed": owed,
            "invoice_id": inv.id,
            "pdf_url": url_for("customer.invoice_pdf", invoice_id=inv.id, _external=True),
        })

    # Payment / refund rows
    for p in payments:
        tx_type = (getattr(p, "transaction_type", "") or "").strip()
        tx_status = (getattr(p, "status", "completed") or "completed").strip().lower()

        amount = _num(getattr(p, "amount_jmd", 0))
        created = _dt(getattr(p, "created_at", None))
        method_label = normalize_method(getattr(p, "method", "")) or "—"

        reference_main = f"TX-{p.id}"
        reference_sub = transaction_label(tx_type)

        inv = None

        if tx_type == "invoice_payment":
            inv = Invoice.query.filter_by(
                id=p.invoice_id,
                user_id=user.id
            ).first()

            if inv:
                reference_sub = f"{transaction_label(tx_type)} • {inv.invoice_number or f'INV-{inv.id}'}"

        elif tx_type == "delivery_payment" and getattr(p, "scheduled_delivery_id", None):
            reference_sub = f"{transaction_label(tx_type)} • Delivery #{p.scheduled_delivery_id}"

        elif tx_type == "package_refund" and getattr(p, "claim_id", None):
            reference_sub = f"{transaction_label(tx_type)} • Claim #{p.claim_id}"

        elif tx_type == "delivery_refund" and getattr(p, "scheduled_delivery_id", None):
            reference_sub = f"{transaction_label(tx_type)} • Delivery #{p.scheduled_delivery_id}"

        if tx_type in ("package_refund", "delivery_refund"):
            amount_due = 0.0
            amount_paid = amount
            amount_owed = 0.0
        else:
            amount_due = amount
            amount_paid = amount if tx_status == "completed" else 0.0
            amount_owed = 0.0 if tx_status == "completed" else amount

        rows.append({
            "type": tx_type or "transaction",
            "reference_main": reference_main,
            "reference_sub": reference_sub,
            "date": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
            "status": tx_status,
            "method": method_label,
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "amount_owed": amount_owed,
            "payment_id": p.id,
            "pdf_url": (
                url_for("customer.invoice_pdf", invoice_id=inv.id, _external=True)
                if inv
                else (
                    url_for("customer.receipt_pdf_inline", payment_id=p.id, _external=True)
                    if tx_type not in ("package_refund", "delivery_refund")
                    else ""
                )
            ),
        })

    # Delivery invoice rows
    for d in deliveries:
        delivery_fee = _num(getattr(d, "delivery_fee", 0))
        fee_status = (getattr(d, "fee_status", "") or "").strip().lower()
        delivery_status = (getattr(d, "status", "") or "").strip()

        created_dt = _dt(getattr(d, "created_at", None))
        scheduled_dt = getattr(d, "scheduled_date", None)

        if fee_status in ("paid", "waived"):
            amount_paid = delivery_fee
            amount_owed = 0.0
        else:
            amount_paid = 0.0
            amount_owed = delivery_fee

        if fee_status == "waived":
            status_value = "paid"
            method_label = "Waived"
        elif fee_status == "paid":
            status_value = "paid"
            method_label = "Paid"
        else:
            status_value = "pending"
            method_label = "Awaiting Payment"

        reference_main = getattr(d, "invoice_number", None) or f"DEL-{d.id}"
        reference_sub = "Delivery Invoice"
        if scheduled_dt:
            reference_sub = f"Delivery Invoice • {scheduled_dt.strftime('%Y-%m-%d')}"

        rows.append({
            "type": "delivery_invoice",
            "reference_main": reference_main,
            "reference_sub": reference_sub,
            "date": created_dt.strftime("%Y-%m-%d %H:%M:%S") if created_dt else "",
            "status": status_value,
            "method": method_label,
            "amount_due": delivery_fee,
            "amount_paid": amount_paid,
            "amount_owed": amount_owed,
            "delivery_id": d.id,
            "pdf_url": "",
            "fee_status": getattr(d, "fee_status", "") or "",
            "delivery_status": delivery_status,
        })        

    rows.sort(key=lambda r: r["date"], reverse=True)

    total_shipments = len(invoices)
    billing_records = len(rows)
    total_owed = sum((r.get("amount_owed") or 0) for r in rows)
    pending_count = sum(1 for r in rows if (r.get("amount_owed") or 0) > 0)

    return jsonify({
        "summary": {
            "total_shipments": total_shipments,
            "total_owed": total_owed,
            "billing_records": billing_records,
            "pending_count": pending_count,
        },
        "rows": rows,
    })

@customer_bp.route("/api/deliveries", methods=["GET"])
def api_customer_deliveries():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    deliveries = (
        ScheduledDelivery.query
        .filter_by(user_id=user.id)
        .order_by(ScheduledDelivery.created_at.desc())
        .all()
    )

    rows = []

    for d in deliveries:
        rows.append({
            "id": d.id,
            "invoice_number": d.invoice_number or f"DEL-{d.id}",
            "date": d.scheduled_date.strftime("%Y-%m-%d") if d.scheduled_date else "",
            "time": d.scheduled_time or "",
            "location": d.location or "",
            "receiver": d.person_receiving or "",
            "delivery_fee": float(d.delivery_fee or 0),
        })

    return jsonify({
        "delivery_fee": 1000.0,
        "enabled": True,
        "rows": rows,
    })

@customer_bp.route("/api/deliveries/<int:delivery_id>/invoice", methods=["GET"])
def api_customer_delivery_invoice(delivery_id):
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    d = ScheduledDelivery.query.filter_by(
        id=delivery_id,
        user_id=user.id
    ).first()

    if not d:
        return jsonify({"error": "Delivery not found"}), 404

    created_at = getattr(d, "created_at", None)
    created_display = created_at.strftime("%Y-%m-%d %H:%M") if created_at else ""

    return jsonify({
        "invoice_number": d.invoice_number or f"DEL-{d.id}",
        "created_at": created_display,
        "customer": {
            "name": user.full_name or "",
            "registration": user.registration_number or "",
            "email": user.email or "",
        },
        "fee_status": getattr(d, "fee_status", "") or "",
        "delivery_status": getattr(d, "status", "") or "",
        "details": {
            "scheduled_date": d.scheduled_date.strftime("%Y-%m-%d") if getattr(d, "scheduled_date", None) else "",
            "scheduled_time": getattr(d, "scheduled_time", "") or "",
            "location": getattr(d, "location", "") or "",
            "direction": getattr(d, "direction", "") or "",
            "mobile_number": getattr(d, "mobile_number", "") or "",
            "person_receiving": getattr(d, "person_receiving", "") or "",
        },
        "charges": {
            "description": "Delivery Request Fee",
            "amount": float(getattr(d, "delivery_fee", 0) or 0),
            "total_due": float(getattr(d, "delivery_fee", 0) or 0),
        }
    })

@customer_bp.route("/api/deliveries/create", methods=["POST"])
def api_customer_create_delivery():
    data = request.get_json(silent=True) or {}

    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    schedule_date = (data.get("schedule_date") or data.get("date") or "").strip()
    location = (data.get("location") or "").strip()
    area_zone = (data.get("area_zone") or "").strip()

    # Fixed delivery window from app
    time_from_in = (data.get("time_from") or "09:00").strip()
    time_to_in = (data.get("time_to") or "17:00").strip()
    schedule_time_single = (data.get("scheduled_time") or data.get("time") or "").strip()

    # Selected packages from Flutter
    raw_package_ids = data.get("package_ids") or []
    package_ids = []
    for x in raw_package_ids:
        try:
            package_ids.append(int(x))
        except Exception:
            pass
    package_ids = list(dict.fromkeys(package_ids))  # dedupe, preserve order

    t_from = "09:00"
    t_to = "17:00"
    scheduled_time_str = "09:00 - 17:00"

    current_app.logger.info(
        "[api_customer_create_delivery] keys=%s date=%s location=%s area_zone=%s package_ids=%s",
        list(data.keys()), schedule_date, location, area_zone, package_ids
    )

    if (not schedule_date) or (not location) or (not area_zone):
        return jsonify({
            "success": False,
            "message": "Missing required fields: schedule_date, location, area_zone.",
            "received_keys": list(data.keys()),
        }), 400

    if not package_ids:
        return jsonify({
            "success": False,
            "message": "Please select at least one eligible package."
        }), 400

    d = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            d = datetime.strptime(schedule_date, fmt).date()
            break
        except Exception:
            pass

    if not d:
        return jsonify({
            "success": False,
            "message": f"Invalid date format: {schedule_date}"
        }), 400

    def _parse_time_to_24h_str(s: str):
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%H:%M", "%I:%M %p"):
            try:
                t_obj = datetime.strptime(s, fmt).time()
                return t_obj.strftime("%H:%M")
            except Exception:
                continue
        return None

    t_from = _parse_time_to_24h_str(time_from_in)
    t_to = _parse_time_to_24h_str(time_to_in)

    if not (t_from and t_to) and schedule_time_single:
        t_single = _parse_time_to_24h_str(schedule_time_single)
        if not t_single:
            return jsonify({
                "success": False,
                "message": f"Invalid time format: {schedule_time_single}"
            }), 400

        t_from = None
        t_to = None
        scheduled_time_str = t_single
    else:
        if not t_from or not t_to:
            return jsonify({
                "success": False,
                "message": "Invalid delivery time."
            }), 400

        try:
            tf_dt = datetime.strptime(t_from, "%H:%M")
            tt_dt = datetime.strptime(t_to, "%H:%M")
            if tt_dt <= tf_dt:
                return jsonify({
                    "success": False,
                    "message": "Time To must be after Time From."
                }), 400
        except Exception:
            pass

        scheduled_time_str = f"{t_from} - {t_to}"

    # -----------------------------
    # Validate selected packages
    # Must belong to user and be Ready for Pick Up
    # -----------------------------
    selected_packages = (
        Package.query
        .filter(
            Package.id.in_(package_ids),
            Package.user_id == user.id,
            Package.status == "Ready for Pick Up"
        )
        .order_by(Package.id.asc())
        .all()
    )

    if len(selected_packages) != len(package_ids):
        return jsonify({
            "success": False,
            "message": "One or more selected packages are invalid or not eligible for delivery. Only packages marked Ready for Pick Up can be selected."
        }), 400

    # -----------------------------
    # Delivery rules
    # -----------------------------
    allowed_zones = {"kgn_core", "kgn_outside", "pm_core", "pm_outside"}
    if area_zone not in allowed_zones:
        return jsonify({
            "success": False,
            "message": "Invalid delivery area selected."
        }), 400

    dow = d.weekday()
    is_tue = (dow == 1)
    is_wed = (dow == 2)
    is_thu = (dow == 3)
    is_fri = (dow == 4)
    is_sat = (dow == 5)

    fee_amt = Decimal("1000.00")
    fee_status = "Unpaid"

    if is_sat:
        if area_zone.endswith("_outside"):
            fee_amt = Decimal("1500.00")
        else:
            fee_amt = Decimal("1000.00")
        fee_status = "Unpaid"

    elif is_tue or is_thu:
        if area_zone != "kgn_core":
            return jsonify({
                "success": False,
                "message": "On Tue/Thu we only deliver FREE within Kingston Core. Outside areas are Saturday only."
            }), 400
        fee_amt = Decimal("0.00")
        fee_status = "Waived"

    elif is_wed or is_fri:
        if area_zone != "pm_core":
            return jsonify({
                "success": False,
                "message": "On Wed/Fri we only deliver FREE within Portmore/Spanish Town Core. Outside areas are Saturday only."
            }), 400
        fee_amt = Decimal("0.00")
        fee_status = "Waived"

    else:
        return jsonify({
            "success": False,
            "message": "Delivery is available Tue/Thu (Kingston), Wed/Fri (Portmore/Spanish Town), or Saturday."
        }), 400

    try:
        new_delivery = ScheduledDelivery(
            user_id=user.id,
            scheduled_date=d,
            area_zone=area_zone,
            scheduled_time_from=t_from,
            scheduled_time_to=t_to,
            scheduled_time=scheduled_time_str,
            location=location,
            direction=(data.get("direction") or data.get("directions") or "").strip(),
            mobile_number=(data.get("mobile_number") or data.get("mobile") or "").strip(),
            person_receiving=(data.get("person_receiving") or "").strip(),
            delivery_fee=fee_amt,
            fee_currency=DELIVERY_FEE_CURRENCY,
            fee_status=fee_status,
            status="Scheduled",
        )

        db.session.add(new_delivery)
        db.session.commit()

        year = datetime.utcnow().year
        new_delivery.invoice_number = f"DEL-{year}-{new_delivery.id:06d}"

        # ----------------------------------------------------
        # Best-effort package linking
        # Works if ScheduledDelivery has a `packages` relationship
        # ----------------------------------------------------
        if hasattr(new_delivery, "packages"):
            try:
                new_delivery.packages = selected_packages
            except Exception:
                current_app.logger.warning(
                    "[api_customer_create_delivery] Could not assign packages relationship on ScheduledDelivery."
                )

        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Scheduled successfully",
            "delivery": {
                "id": new_delivery.id,
                "invoice_number": new_delivery.invoice_number,
                "scheduled_date": new_delivery.scheduled_date.isoformat(),
                "scheduled_time": new_delivery.scheduled_time or "",
                "scheduled_time_from": new_delivery.scheduled_time_from or "",
                "scheduled_time_to": new_delivery.scheduled_time_to or "",
                "location": new_delivery.location or "",
                "area_zone": new_delivery.area_zone or "",
                "person_receiving": new_delivery.person_receiving or "",
                "delivery_fee": str(new_delivery.delivery_fee or fee_amt),
                "fee_currency": new_delivery.fee_currency or DELIVERY_FEE_CURRENCY,
                "fee_status": new_delivery.fee_status or fee_status,
                "status": new_delivery.status or "Scheduled",
                "package_ids": [p.id for p in selected_packages],
                "packages": [
                    {
                        "id": p.id,
                        "house_awb": p.house_awb or "",
                        "tracking_number": p.tracking_number or "",
                        "description": p.description or "",
                        "amount_due": float(p.amount_due or 0),
                    }
                    for p in selected_packages
                ],
            }
        }), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("api_customer_create_delivery failed")
        return jsonify({
            "success": False,
            "message": f"{type(e).__name__}: {str(e)}"
        }), 500


@customer_bp.route("/api/deliveries/eligible-packages", methods=["GET"])
def api_delivery_eligible_packages():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    pkgs = (
        Package.query
        .filter_by(user_id=user.id, status="Ready for Pick Up")
        .order_by(Package.id.desc())
        .all()
    )

    return jsonify({
        "packages": [
            {
                "id": p.id,
                "house_awb": p.house_awb or "",
                "description": p.description or "",
                "tracking_number": p.tracking_number or "",
                "amount_due": float(p.amount_due or 0),
            }
            for p in pkgs
        ]
    })

@customer_bp.route("/api/login", methods=["POST"])
@csrf.exempt
def api_customer_login():
    data = request.get_json(silent=True) or {}

    identifier = (data.get("email") or data.get("registration") or "").strip()
    password_plain = (data.get("password") or "").strip()

    if not identifier or not password_plain:
        return jsonify({"error": "Email/registration and password are required"}), 400

    user = User.query.filter(
        or_(
            func.lower(User.email) == identifier.lower(),
            func.lower(User.registration_number) == identifier.lower(),
        )
    ).first()

    if not user or not user.password:
        return jsonify({"error": "Invalid credentials"}), 401

    stored_password = user.password

    if isinstance(stored_password, memoryview):
        stored_password = stored_password.tobytes()

    if isinstance(stored_password, str):
        stored_password = stored_password.encode("utf-8")

    try:
        ok = bcrypt.checkpw(password_plain.encode("utf-8"), stored_password)
    except Exception:
        ok = False

    if not ok:
        return jsonify({"error": "Invalid credentials"}), 401

    if not user.is_enabled:
        return jsonify({"error": "This account is disabled"}), 403

    token = secrets.token_urlsafe(48)
    user.api_token = token
    user.last_login = datetime.utcnow()
    db.session.commit()

    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id": user.id,
            "full_name": user.full_name or "",
            "email": user.email or "",
            "registration": user.registration_number or "",
            "mobile": user.mobile or "",
        }
    }), 200

@customer_bp.route("/api/package/<int:pkg_id>/docs", methods=["POST"])
@csrf.exempt
def api_package_upload_docs(pkg_id):
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    pkg = Package.query.filter_by(id=pkg_id, user_id=user.id).first()
    if not pkg:
        return jsonify({"error": "Package not found"}), 404

    dv = request.form.get("declared_value")
    if dv:
        try:
            dvf = float(dv)
            pkg.declared_value = dvf
            pkg.value = dvf
        except ValueError:
            return jsonify({"error": "Declared value must be a number"}), 400

    files = [
        request.files.get("invoice_file"),
        request.files.get("invoice_file_1"),
        request.files.get("invoice_file_2"),
        request.files.get("invoice_file_3"),
    ]

    saved_any = False
    seen = set()

    for f in files:
        if not (f and f.filename):
            continue

        original = f.filename.strip()
        key = original.lower()

        if key in seen:
            continue
        seen.add(key)

        if not allowed_file(original):
            continue

        try:
            from app.utils.cloudinary_storage import upload_package_attachment
            url, public_id, rtype = upload_package_attachment(f)
        except Exception:
            current_app.logger.exception("[API PACKAGE DOC UPLOAD] cloud upload failed")
            return jsonify({"error": "Upload failed. Please try again."}), 500

        if not url:
            return jsonify({"error": "Upload failed."}), 500

        db.session.add(PackageAttachment(
            package_id=pkg.id,
            file_name=url,
            file_url=url,
            original_name=original,
            cloud_public_id=public_id,
            cloud_resource_type=rtype,
        ))
        saved_any = True

        if not getattr(pkg, "invoice_file", None):
            pkg.invoice_file = url

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Updated package documents successfully.",
        "saved_any": saved_any,
    }), 200

@customer_bp.route("/api/account/profile", methods=["GET"])
def api_account_profile():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "full_name": user.full_name or "",
        "email": user.email or "",
        "mobile": user.mobile or "",
        "trn": getattr(user, "trn", "") or "",
        "registration": user.registration_number or "",
    }), 200

@customer_bp.route("/api/account/profile", methods=["POST"])
@csrf.exempt
def api_account_profile_update():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    full_name = (data.get("full_name") or "").strip()
    mobile = (data.get("mobile") or "").strip()
    trn = (data.get("trn") or "").strip()

    if not full_name:
        return jsonify({"error": "Full name is required"}), 400

    user.full_name = full_name
    user.mobile = mobile

    if hasattr(user, "trn"):
        user.trn = trn

    if hasattr(user, "updated_at"):
        user.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Profile updated successfully",
        "user": {
            "full_name": user.full_name or "",
            "email": user.email or "",
            "mobile": user.mobile or "",
            "trn": getattr(user, "trn", "") or "",
            "registration": user.registration_number or "",
        }
    }), 200


@customer_bp.route("/api/account/address", methods=["GET"])
def api_account_address():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "address": (getattr(user, "address", "") or "").strip(),
        "registration": user.registration_number or "",
        "full_name": user.full_name or "",
    }), 200

@customer_bp.route("/api/account/address", methods=["POST"])
@csrf.exempt
def api_account_address_update():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    address = (data.get("address") or "").strip()

    if not address:
        return jsonify({"error": "Delivery address is required"}), 400

    user.address = address

    if hasattr(user, "updated_at"):
        user.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Delivery address updated successfully",
        "address": user.address or "",
    }), 200


@customer_bp.route("/api/account/security/password", methods=["POST"])
@csrf.exempt
def api_account_change_password():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    current_password = (data.get("current_password") or "").strip()
    new_password = (data.get("new_password") or "").strip()
    confirm_password = (data.get("confirm_password") or "").strip()

    if not current_password:
        return jsonify({"error": "Current password is required"}), 400

    if not new_password:
        return jsonify({"error": "New password is required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400

    if new_password != confirm_password:
        return jsonify({"error": "Passwords do not match"}), 400

    stored_password = user.password

    if isinstance(stored_password, memoryview):
        stored_password = stored_password.tobytes()

    if isinstance(stored_password, str):
        stored_password = stored_password.encode("utf-8")

    try:
        ok = bcrypt.checkpw(current_password.encode("utf-8"), stored_password)
    except Exception:
        ok = False

    if not ok:
        return jsonify({"error": "Current password is incorrect"}), 400

    user.password = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt())

    if hasattr(user, "updated_at"):
        user.updated_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Password updated successfully"
    }), 200

@customer_bp.route("/api/account/referral", methods=["GET"])
def api_account_referral():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    referral_code = ensure_user_referral_code(user)

    return jsonify({
        "full_name": user.full_name or "",
        "email": user.email or "",
        "referral_code": referral_code or "",
    }), 200

@customer_bp.route("/api/account/referral/send", methods=["POST"])
@csrf.exempt
def api_account_referral_send():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    friend_email = (data.get("friend_email") or "").strip()

    if not friend_email:
        return jsonify({"error": "Friend email is required"}), 400

    if not EMAIL_REGEX.match(friend_email):
        return jsonify({"error": "Please enter a valid email address"}), 400

    referral_code = ensure_user_referral_code(user)
    full_name = user.full_name or user.email or "FAFL Customer"

    if not referral_code:
        return jsonify({"error": "Referral code is not available"}), 500

    ok = send_referral_email(friend_email, referral_code, full_name)

    if not ok:
        return jsonify({"error": "Failed to send referral email"}), 500

    return jsonify({
        "success": True,
        "message": f"Referral email sent to {friend_email}",
    }), 200


@customer_bp.route("/api/notifications", methods=["GET"])
def api_customer_notifications():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    notes = (
        Notification.query
        .filter(
            sa.or_(
                Notification.user_id == user.id,
                Notification.is_broadcast.is_(True)
            )
        )
        .order_by(Notification.created_at.desc())
        .all()
    )

    return jsonify({
        "notifications": [
            {
                "id": n.id,
                "title": getattr(n, "title", "") or "Notification",
                "message": getattr(n, "message", "") or getattr(n, "body", "") or "",
                "is_read": bool(getattr(n, "is_read", False)),
                "created_at": (
                    n.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    if getattr(n, "created_at", None)
                    else ""
                ),
                "is_broadcast": bool(getattr(n, "is_broadcast", False)),
            }
            for n in notes
        ]
    }), 200

@customer_bp.route("/api/notifications/<int:nid>/read", methods=["POST"])
@csrf.exempt
def api_customer_notification_mark_read(nid):
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    n = Notification.query.get_or_404(nid)

    if n.user_id != user.id and not n.is_broadcast:
        return jsonify({"error": "Not authorized"}), 403

    n.is_read = True
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Notification marked as read",
    }), 200

@customer_bp.route("/api/messages", methods=["GET"])
def api_customer_messages():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    messages_list = (
        DBMessage.query
        .filter(
            sa.or_(
                DBMessage.sender_id == user.id,
                DBMessage.recipient_id == user.id
            )
        )
        .order_by(DBMessage.created_at.desc())
        .all()
    )

    rows = []
    for m in messages_list:
        other_id = m.recipient_id if m.sender_id == user.id else m.sender_id
        other = User.query.get(other_id)

        rows.append({
            "id": m.id,
            "subject": (m.subject or "").strip() or "Message",
            "body": m.body or "",
            "is_read": bool(m.is_read),
            "created_at": (
                m.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if getattr(m, "created_at", None)
                else ""
            ),
            "direction": "sent" if m.sender_id == user.id else "received",
            "other_name": (other.full_name if other else "Administrator") or "Administrator",
        })

    return jsonify({"messages": rows}), 200

@customer_bp.route("/api/messages/send", methods=["POST"])
@csrf.exempt
def api_customer_send_message():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").strip() or "Message"
    body = (data.get("body") or "").strip()

    if not body:
        return jsonify({"error": "Message body is required"}), 400

    admin = (
        (User.query.filter(User.is_superadmin.is_(True)).order_by(User.id.asc()).first()
         if hasattr(User, "is_superadmin") else None)
        or (User.query.filter(User.role == "admin").order_by(User.id.asc()).first()
            if hasattr(User, "role") else None)
        or User.query.order_by(User.id.asc()).first()
    )

    if not admin:
        return jsonify({"error": "No admin available to receive message"}), 500

    msg = DBMessage(
        sender_id=user.id,
        recipient_id=admin.id,
        subject=subject,
        body=body,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )

    db.session.add(msg)
    db.session.commit()

    try:
        if admin.email:
            preview = (body[:120] + "…") if len(body) > 120 else body
            send_new_message_email(
                user_email=admin.email,
                user_name=admin.full_name or "Admin",
                message_subject=subject,
                message_body=preview,
                recipient_user_id=admin.id
            )
    except Exception:
        current_app.logger.exception("Failed to send message email notification")

    return jsonify({
        "success": True,
        "message": "Message sent successfully",
    }), 200


@customer_bp.route("/api/messages/<int:msg_id>/read", methods=["POST"])
@csrf.exempt
def api_customer_mark_message_read(msg_id):
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    msg = DBMessage.query.get_or_404(msg_id)

    if msg.sender_id != user.id and msg.recipient_id != user.id:
        return jsonify({"error": "Not authorized"}), 403

    if msg.recipient_id == user.id and not msg.is_read:
        msg.is_read = True
        db.session.commit()

    return jsonify({
        "success": True,
        "message": "Message marked as read",
    }), 200

@customer_bp.route("/api/calculator", methods=["POST"])
@csrf.exempt
def api_customer_calculator():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    try:
        weight = float(data.get("weight") or 0)
    except Exception:
        return jsonify({"error": "Weight must be numeric"}), 400

    try:
        value_usd = float(data.get("value_usd") or 0)
    except Exception:
        return jsonify({"error": "Item value must be numeric"}), 400

    category = (data.get("category") or "").strip()

    if weight <= 0:
        return jsonify({"error": "Weight must be greater than 0"}), 400

    if value_usd < 0:
        return jsonify({"error": "Item value cannot be negative"}), 400

    try:
        result = calculate_charges(category, value_usd, weight)
        return jsonify({
            "success": True,
            "result": result,
        }), 200
    except Exception as e:
        current_app.logger.exception("Calculator failed")
        return jsonify({
            "error": f"Calculator failed: {type(e).__name__}: {str(e)}"
        }), 500


@customer_bp.route("/api/calculator/categories", methods=["GET"])
@csrf.exempt
def api_calculator_categories():
    user = get_api_user()

    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "categories": list(CATEGORIES.keys())
    }), 200