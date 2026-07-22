from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, make_response, jsonify, send_file, abort, 
    current_app
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.utils import secure_filename

import os, io, math, smtplib
from email.message import EmailMessage
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange
from collections import OrderedDict
from collections import defaultdict
import base64
import mimetypes

import bcrypt
import openpyxl
from weasyprint import HTML
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from markupsafe import escape

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email
from app.forms import (
    LoginForm, SendMessageForm, AdminLoginForm, BulkMessageForm, UploadPackageForm,
    SingleRateForm, BulkRateForm, MiniRateForm, AdminProfileForm, AdminRegisterForm,
    ExpenseForm, WalletUpdateForm, AdminCalculatorForm, PackageBulkActionForm, InvoiceForm, InvoiceItemForm, BroadcastNotificationForm
)

from app.utils import email_utils
from app.utils.wallet import update_wallet, update_wallet_balance
from app.utils.invoice_utils import generate_invoice
from app.utils.rates import get_rate_for_weight
from app.utils.invoice_pdf import generate_invoice_pdf
from app.utils.messages import make_thread_key
from app.utils.message_notify import send_new_message_email
from app.utils.email_utils import send_bulk_message_email
from app.calculator import calculate_charges
from app.calculator_data import CATEGORIES, USD_TO_JMD
from app.utils.invoice_totals import (
    fetch_invoice_totals_pg,
    mark_invoice_packages_delivered,
    lock_delivered_packages_for_invoice,
)
from app.utils.time import to_jamaica
from app.utils.subscription_utils import (
    clear_package_subscription,
    get_subscription_discount_percent,
    reconcile_subscription_usage,
)


import sqlalchemy as sa
from sqlalchemy import func, extract, asc
from app.extensions import db
from app.models import (
    User, Wallet, Message, MessageAttachment, ScheduledDelivery,
    WalletTransaction, Package, Invoice, Notification, Payment, PurchaseRequest, 
    RateBracket, Discount, shipment_packages, Prealert, ShipmentLog, AuditLog
)
from app.routes.admin_auth_routes import admin_required


admin_bp = Blueprint(
    'admin', __name__,
    url_prefix='/admin',
    template_folder='templates/admin'
)

ALLOWED_EXTENSIONS = {"xlsx", "csv"}
MESSAGE_ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}

def allowed_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS

def allowed_message_attachment(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in MESSAGE_ALLOWED_EXTENSIONS

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

    return to_jamaica(dt).strftime(format)

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

        _is_subscription_covered = (
            bool(getattr(p, "subscription_applied", False))
            and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
        )

        _value_usd = _num(
            getattr(p, "value", getattr(p, "invoice_value", getattr(p, "value_usd", 0)))
        )

        _bad_address_fee = _num(getattr(p, "bad_address_fee", 0))

        if bool(getattr(p, "epc", False)) and _bad_address_fee <= 0:
            _bad_address_fee = 500.0

        if _is_subscription_covered:
            _freight = 0
            _storage = 0
        else:
            _freight = _num(getattr(p, "freight_fee", getattr(p, "freight", 0)))
            _storage = _num(
                getattr(
                    p,
                    "handling_fee",
                    getattr(
                        p,
                        "storage_fee",
                        getattr(p, "handling", 0),
                    ),
                )
            )

        packages.append({
            "house_awb":     getattr(p, "house_awb", "") or "",
            "description":   getattr(p, "description", "") or "",
            "weight":        _num(getattr(p, "weight", 0)),
            "value":         _value_usd,

            "other_charges": _num(getattr(p, "other_charges", 0)),
            "bad_address_fee": _bad_address_fee,
            "freight":       _freight,
            "storage":       _storage,

            "duty":          _num(getattr(p, "duty", 0)),
            "scf":           _num(getattr(p, "scf", 0)),
            "envl":          _num(getattr(p, "envl", 0)),
            "caf":           _num(getattr(p, "caf", 0)),
            "gct":           _num(getattr(p, "gct", 0)),
            "discount_due":  _num(getattr(p, "discount_due", 0)),

            "subscription_applied": bool(getattr(p, "subscription_applied", False)),
            "subscription_result": getattr(p, "subscription_result", None),
            "subscription_covered": _is_subscription_covered,

            "customs_only_due_to_subscription": (
                _is_subscription_covered
                and float(getattr(p, "customs_total", 0) or 0) > 0
            ),
        })

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number,
        "date": inv.date_submitted or datetime.utcnow(),
        "customer_code": getattr(inv.user, "registration_number", "") if getattr(inv, "user", None) else "",
        "customer_name": getattr(inv.user, "full_name", "") if getattr(inv, "user", None) else "",
        "subtotal": _num(getattr(inv, "subtotal", getattr(inv, "grand_total", 0))),
        "discount_total": _num(getattr(inv, "discount_total", 0)),
        "total_due": _num(getattr(inv, "grand_total", getattr(inv, "amount", 0))),
        "packages": packages,
    }

    return invoice_dict

def _money(n):
    try:
        return f"{float(n):,.2f}"
    except Exception:
        return "0.00"

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

def _static_image_data_uri(filename: str):
    """
    Convert a static image file into a base64 data URI for reliable PDF/HTML rendering.
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

def _is_duplicate_message(sender_id, recipient_id, subject, body, seconds=45):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)

    return Message.query.filter(
        Message.sender_id == sender_id,
        Message.recipient_id == recipient_id,
        func.lower(func.trim(Message.subject)) == (subject or "").strip().lower(),
        func.lower(func.trim(Message.body)) == (body or "").strip().lower(),
        Message.created_at >= cutoff
    ).first()

@admin_bp.route("/__routes")
@admin_required()
def admin_routes_dump():
    from flask import current_app
    lines = []
    for r in sorted(current_app.url_map.iter_rules(), key=lambda x: str(x)):
        if str(r.endpoint).startswith("admin."):
            lines.append(f"{r.endpoint:30}  {r.methods}  {r.rule}")
    return "<pre>" + "\n".join(lines) + "</pre>"


@admin_bp.route('/register-admin', methods=['GET', 'POST'])
@admin_required(roles=['superadmin'])
def register_admin():
    form = AdminRegisterForm()

    if form.validate_on_submit():
        full_name = (form.full_name.data or "").strip()
        email = (form.email.data or "").strip()
        password = (form.password.data or "").strip()
        role = (request.form.get("role") or "admin").strip()

        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return render_template('admin/register_admin.html', form=form)

        admin_roles = ["admin", "superadmin", "finance", "operations", "accounts_manager"]

        conds = [
            User.is_superadmin.is_(True),
            User.role.in_(admin_roles),
        ]

        if hasattr(User, "is_admin"):
            conds.append(User.is_admin.is_(True))

        has_any_admin = User.query.filter(sa.or_(*conds)).first() is not None

        is_admin = True
        is_superadmin = False

        if not has_any_admin:
            role = "superadmin"
            is_superadmin = True

        registration_number = "FAFL10000" if not has_any_admin else None
        hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

        u = User(
            full_name=full_name,
            email=email,
            password=hashed_pw,
            role=role,
            created_at=datetime.now(timezone.utc),
            registration_number=registration_number,
            is_admin=is_admin if hasattr(User, "is_admin") else True,
            is_superadmin=is_superadmin,
        )

        db.session.add(u)
        db.session.flush()

        u.employee_code = f"FAFL-{str(u.id).zfill(4)}"

        db.session.add(AuditLog(
            module="Admin Activity",
            action="Admin Account Created",
            admin_id=current_user.id,
            user_id=u.id,
            entity_type="User",
            entity_id=u.id,
            reason="Admin account creation",
            description=(
                f"Admin account created for {u.full_name or u.email}. "
                f"Role: {role}. Employee Code: {u.employee_code}."
            ),
            old_value="No admin account",
            new_value=f"Role: {role}; Superadmin: {bool(is_superadmin)}",
        ))

        db.session.commit()

        flash(
            f"Admin account for {full_name} created successfully "
            f"({role}{' / SUPERADMIN' if is_superadmin else ''}).",
            "success"
        )
        return redirect(url_for('admin.dashboard'))

    return render_template('admin/register_admin.html', form=form)

@admin_bp.route('/manage-admins')
@admin_required(roles=['superadmin'])
def manage_admins():
    """List all admin-type users so superadmin can edit them."""
    admins = User.query.filter_by(is_admin=True).all()
    return render_template('admin/manage_admins.html', admins=admins)

@admin_bp.route('/admins/<int:user_id>/update-role', methods=['POST'])
@admin_required(roles=['superadmin'])
def update_admin_role(user_id):
    new_role = (request.form.get("role") or "").strip().lower()

    admin = User.query.get_or_404(user_id)

    old_role = admin.role or "unknown"
    old_is_admin = bool(getattr(admin, "is_admin", False))
    old_is_superadmin = bool(getattr(admin, "is_superadmin", False))

    admin.role = new_role

    if new_role == "superadmin":
        admin.is_superadmin = True
        admin.is_admin = True
    elif new_role in ("admin", "finance", "operations", "accounts_manager"):
        admin.is_superadmin = False
        admin.is_admin = True
    else:
        admin.is_superadmin = False
        admin.is_admin = False

    db.session.add(AuditLog(
        module="Admin Activity",
        action="Admin Role Changed",
        admin_id=current_user.id,
        user_id=admin.id,
        entity_type="User",
        entity_id=admin.id,
        reason="Admin role update",
        description=(
            f"Admin role changed for {admin.full_name or admin.email} "
            f"from {old_role} to {new_role}."
        ),
        old_value=(
            f"Role: {old_role}; "
            f"is_admin: {old_is_admin}; "
            f"is_superadmin: {old_is_superadmin}"
        ),
        new_value=(
            f"Role: {admin.role}; "
            f"is_admin: {bool(admin.is_admin)}; "
            f"is_superadmin: {bool(admin.is_superadmin)}"
        ),
    ))

    db.session.commit()

    flash("Admin role updated successfully.", "success")
    return redirect(url_for('admin.manage_admins'))


# ---------- Admin Dashboard ----------
@admin_bp.route("/dashboard")
@admin_required()
def dashboard():
    from app.forms import AdminCalculatorForm

    admin_calculator_form = AdminCalculatorForm()

    # Current Jamaica date and time.
    jamaica_now = to_jamaica(datetime.now(timezone.utc))
    today = jamaica_now.date()
    current_year = today.year
    current_month = today.month
    window_start_90d = today - timedelta(days=90)

    # ---------------------------------
    # Top summary cards
    # ---------------------------------
    total_users = (
        db.session.scalar(
            sa.select(func.count()).select_from(User)
        )
        or 0
    )

    total_packages = (
        db.session.scalar(
            sa.select(func.count()).select_from(Package)
        )
        or 0
    )

    pending_invoices = (
        db.session.scalar(
            sa.select(func.count())
            .select_from(Invoice)
            .where(
                func.lower(Invoice.status).in_(
                    ("pending", "unpaid", "issued", "partial")
                )
            )
        )
        or 0
    )

    # ---------------------------------
    # Scheduled deliveries
    # ---------------------------------
    start_date_str = (request.args.get("start_date") or "").strip()
    end_date_str = (request.args.get("end_date") or "").strip()

    deliveries_q = (
        sa.select(ScheduledDelivery)
        .where(
            ~func.lower(ScheduledDelivery.status).in_(
                ("delivered", "cancelled")
            )
        )
        .order_by(
            ScheduledDelivery.scheduled_date.desc(),
            ScheduledDelivery.id.desc(),
        )
    )

    try:
        if start_date_str:
            start_date_value = datetime.fromisoformat(
                start_date_str
            ).date()

            deliveries_q = deliveries_q.where(
                ScheduledDelivery.scheduled_date >= start_date_value
            )

        if end_date_str:
            end_date_value = datetime.fromisoformat(
                end_date_str
            ).date()

            deliveries_q = deliveries_q.where(
                ScheduledDelivery.scheduled_date <= end_date_value
            )

    except (TypeError, ValueError):
        flash(
            "One of the delivery date filters was invalid and was ignored.",
            "warning",
        )

    deliveries = (
        db.session.execute(
            deliveries_q.limit(10)
        )
        .scalars()
        .all()
    )

    # ---------------------------------
    # Jamaica date parsing helper
    # ---------------------------------
    def _parse_any_dt_jamaica(value):
        """
        Convert database timestamps to Jamaica time.

        Rules:
        - timezone-aware datetime values are converted to Jamaica time;
        - naive datetime values are treated as stored UTC and converted;
        - date-only values remain Jamaica calendar dates;
        - date-only strings are not shifted to the previous day.
        """
        if value is None or value == "":
            return None

        if isinstance(value, datetime):
            return to_jamaica(value)

        if isinstance(value, date):
            return datetime(
                value.year,
                value.month,
                value.day,
            )

        text_value = str(value).strip()

        if not text_value:
            return None

        # Date-only fields such as date_registered should retain
        # their original calendar date.
        for date_format in (
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%m/%d/%Y",
        ):
            try:
                parsed_date = datetime.strptime(
                    text_value,
                    date_format,
                )

                return parsed_date

            except ValueError:
                continue

        # Timestamp strings are assumed to represent stored UTC.
        try:
            parsed_datetime = datetime.fromisoformat(
                text_value.replace("Z", "+00:00")
            )

            return to_jamaica(parsed_datetime)

        except (TypeError, ValueError):
            pass

        for datetime_format in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed_datetime = datetime.strptime(
                    text_value,
                    datetime_format,
                )

                return to_jamaica(parsed_datetime)

            except ValueError:
                continue

        return None

    # ---------------------------------
    # Monthly chart containers
    # ---------------------------------
    month_map = {
        1: "Jan",
        2: "Feb",
        3: "Mar",
        4: "Apr",
        5: "May",
        6: "Jun",
        7: "Jul",
        8: "Aug",
        9: "Sep",
        10: "Oct",
        11: "Nov",
        12: "Dec",
    }

    user_data_dict = {
        month_number: 0
        for month_number in month_map
    }

    package_data_dict = {
        month_number: 0
        for month_number in month_map
    }

    # ---------------------------------
    # User statistics
    # ---------------------------------
    today_new_users = 0

    users = User.query.with_entities(
        User.id,
        User.date_registered,
        User.created_at,
    ).all()

    for user in users:
        # date_registered is preferred because it represents the
        # customer's intended registration calendar date.
        registered_datetime = _parse_any_dt_jamaica(
            getattr(user, "date_registered", None)
        )

        if registered_datetime is None:
            registered_datetime = _parse_any_dt_jamaica(
                getattr(user, "created_at", None)
            )

        if registered_datetime is None:
            continue

        registered_date = registered_datetime.date()

        if registered_date == today:
            today_new_users += 1

        if registered_datetime.year == current_year:
            user_data_dict[registered_datetime.month] += 1

    # ---------------------------------
    # Package statistics
    # ---------------------------------
    today_new_packages = 0
    active_customer_ids_90d = set()

    packages = Package.query.with_entities(
        Package.id,
        Package.user_id,
        Package.date_received,
        Package.created_at,
    ).all()

    for package in packages:
        received_datetime = _parse_any_dt_jamaica(
            getattr(package, "date_received", None)
        )

        if received_datetime is None:
            received_datetime = _parse_any_dt_jamaica(
                getattr(package, "created_at", None)
            )

        if received_datetime is None:
            continue

        received_date = received_datetime.date()

        if received_date == today:
            today_new_packages += 1

        if received_datetime.year == current_year:
            package_data_dict[received_datetime.month] += 1

        if (
            received_date >= window_start_90d
            and package.user_id
        ):
            active_customer_ids_90d.add(package.user_id)

    # ---------------------------------
    # Current-month and activity totals
    # ---------------------------------
    this_month_new_users = user_data_dict.get(
        current_month,
        0,
    )

    this_month_new_packages = package_data_dict.get(
        current_month,
        0,
    )

    active_customers_90d = len(active_customer_ids_90d)

    # Ensure the template always receives integers.
    total_users = int(total_users or 0)
    total_packages = int(total_packages or 0)
    pending_invoices = int(pending_invoices or 0)
    today_new_users = int(today_new_users or 0)
    today_new_packages = int(today_new_packages or 0)
    this_month_new_users = int(this_month_new_users or 0)
    this_month_new_packages = int(this_month_new_packages or 0)
    active_customers_90d = int(active_customers_90d or 0)

    return render_template(
        "admin/admin_dashboard.html",

        # Summary cards
        total_users=total_users,
        total_packages=total_packages,
        pending_invoices=pending_invoices,

        # Scheduled deliveries
        deliveries=deliveries,
        start_date=start_date_str,
        end_date=end_date_str,

        # Chart information
        user_labels=[
            month_map[month_number]
            for month_number in month_map
        ],
        user_data=[
            user_data_dict[month_number]
            for month_number in month_map
        ],
        pkg_labels=[
            month_map[month_number]
            for month_number in month_map
        ],
        pkg_data=[
            package_data_dict[month_number]
            for month_number in month_map
        ],

        # Live statistics
        today_new_users=today_new_users,
        today_new_packages=today_new_packages,
        this_month_new_users=this_month_new_users,
        this_month_new_packages=this_month_new_packages,
        active_customers_90d=active_customers_90d,

        # Jamaica time supplied to the template
        jamaica_now=jamaica_now,
        jamaica_today=today,

        admin_calculator_form=admin_calculator_form,
    )

@admin_bp.route('/rates')
@admin_required
def view_rates():
    try:
        from app.models import RateBracket
    except Exception:
        flash("RateBracket model not found. Tell me and I’ll add it.", "danger")
        return redirect(url_for('admin.dashboard'))

    search_query = (request.args.get('search') or '').strip()
    page = max(1, int(request.args.get('page', 1)))
    per_page = 10

    q = RateBracket.query
    if search_query:
        like = f"%{search_query}%"
        q = q.filter(sa.or_(sa.cast(RateBracket.max_weight, sa.String).ilike(like),
                            sa.cast(RateBracket.rate, sa.String).ilike(like)))

    paginated = q.order_by(RateBracket.max_weight.asc()).paginate(page=page, per_page=per_page, error_out=False)
    rates = paginated.items
    total_pages = paginated.pages or 1

    return render_template('admin/rates/view_rates.html',
                           rates=rates, page=page, total_pages=total_pages, search_query=search_query)


@admin_bp.route('/add-rate', methods=['GET', 'POST'])
@admin_required
def add_rate():
    from app.models import RateBracket
    form = SingleRateForm()
    if form.validate_on_submit():
        max_weight = float(form.max_weight.data)
        rate = float(form.rate.data)

        exists = RateBracket.query.filter_by(max_weight=max_weight).first()
        if exists:
            flash(f"A rate for {max_weight} lb already exists.", "danger")
            return redirect(url_for('admin.view_rates'))

        db.session.add(RateBracket(max_weight=max_weight, rate=rate))
        db.session.commit()
        flash(f"Rate added: Up to {max_weight} lb → ${rate} JMD", "success")
        return redirect(url_for('admin.view_rates'))
    return render_template('admin/rates/add_rate.html', form=form)


@admin_bp.route('/bulk-add-rates', methods=['GET', 'POST'])
@admin_required
def bulk_add_rates():
    from app.models import RateBracket
    form = BulkRateForm()
    while len(form.rates) < 10:
        form.rates.append_entry()

    if form.validate_on_submit():
        inserted = 0
        for rf in form.rates:
            try:
                mw = float(rf.max_weight.data or 0)
                r  = float(rf.rate.data or 0)
                if mw <= 0 or r <= 0:
                    continue
                if RateBracket.query.filter_by(max_weight=mw).first():
                    continue
                db.session.add(RateBracket(max_weight=mw, rate=r))
                inserted += 1
            except Exception:
                continue
        db.session.commit()
        flash(f"Successfully added {inserted} rates.", "success")
        return redirect(url_for('admin.view_rates'))
    return render_template('admin/rates/bulk_add_rates.html', form=form)


@admin_bp.route('/edit-rate/<int:rate_id>', methods=['GET', 'POST'])
@admin_required
def edit_rate(rate_id):
    from app.models import RateBracket
    rb = RateBracket.query.get_or_404(rate_id)
    form = SingleRateForm(obj=rb)

    if form.validate_on_submit():
        mw = float(form.max_weight.data)
        r  = float(form.rate.data)
        dup = RateBracket.query.filter(RateBracket.max_weight == mw, RateBracket.id != rate_id).first()
        if dup:
            flash(f"A rate for {mw} lb already exists.", "warning")
            return redirect(url_for('admin.view_rates'))
        rb.max_weight = mw
        rb.rate = r
        db.session.commit()
        flash("Rate updated successfully.", "success")
        return redirect(url_for('admin.view_rates'))

    return render_template('admin/rates/edit_rate.html', form=form, rate_id=rate_id)




# ----- Admin Inbox / Sent + Bulk Messaging (Gmail-style, NO THREADS) -----

@admin_bp.route("/messages", methods=["GET", "POST"])
@admin_required
def messages():
    form = BulkMessageForm()

    customers = (
        User.query
        .filter(User.role == "customer")
        .order_by(User.full_name.asc())
        .all()
    )
    form.user_ids.choices = [(u.id, f"{u.full_name} ({u.email})") for u in customers]

    # ---- Bulk send ----
    if request.method == "POST" and form.validate_on_submit():
        ids = form.user_ids.data or []
        if not ids:
            flash("Please select at least one recipient.", "warning")
            return redirect(url_for("admin.messages"))

        subject = (form.subject.data or "").strip() or "Announcement"
        body = (form.message.data or "").strip()
        if not body:
            flash("Message can't be empty.", "warning")
            return redirect(url_for("admin.messages"))

        files = request.files.getlist("attachments")
        recipients = User.query.filter(User.id.in_(ids)).all()
        now = datetime.now(timezone.utc)

        sent_count = 0
        dup_count = 0

        from app.utils.cloudinary_storage import upload_package_attachment

        for u in recipients:
            dup = _is_duplicate_message(current_user.id, u.id, subject, body)
            if dup:
                dup_count += 1
                continue

            msg = Message(
                sender_id=current_user.id,
                recipient_id=u.id,
                subject=subject,
                body=body,
                thread_key=None,
                is_read=False,
                created_at=now,
            )
            db.session.add(msg)
            db.session.flush()

            email_attachments = []

            for f in files:
                if not f or not f.filename:
                    continue

                original = (f.filename or "").strip()
                if not allowed_message_attachment(original):
                    continue

                try:
                    f.stream.seek(0)
                    file_bytes = f.read()
                    f.stream.seek(0)

                    url, public_id, rtype = upload_package_attachment(f)
                except Exception:
                    current_app.logger.exception("[ADMIN MESSAGE ATTACHMENT] upload failed")
                    continue

                if not url:
                    continue

                db.session.add(MessageAttachment(
                    message_id=msg.id,
                    file_url=url,
                    original_name=original,
                    cloud_public_id=public_id,
                    cloud_resource_type=rtype,
                ))

                import mimetypes
                email_attachments.append({
                    "filename": original,
                    "content": file_bytes,
                    "mimetype": mimetypes.guess_type(original)[0] or "application/octet-stream",
                })

            if u.email:
                send_bulk_message_email(
                    to_email=u.email,
                    full_name=u.full_name,
                    subject=subject,
                    message_body=body,
                    recipient_user_id=None,
                    attachments=email_attachments,
                )

            sent_count += 1

        db.session.commit()

        if dup_count and sent_count:
            flash(f"Sent to {sent_count} customer(s). Skipped {dup_count} duplicate message(s).", "success")
        elif dup_count and not sent_count:
            flash("All selected messages were blocked as duplicates.", "warning")
        else:
            flash(f"Message + Email sent to {sent_count} customer(s).", "success")

        return redirect(url_for("admin.messages", box="sent"))

    # ---- Mailbox controls ----
    box = (request.args.get("box") or "inbox").lower()
    q = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread") == "1"
    include_archived = request.args.get("archived") == "1"
    include_deleted = request.args.get("trash") == "1"

    page = request.args.get("page", type=int) or 1
    per_page = request.args.get("per_page", type=int) or 20
    per_page = max(10, min(per_page, 200))

    from sqlalchemy.orm import selectinload

    base = Message.query.options(
        selectinload(Message.attachments)
    )

    if box == "sent":
        base = base.filter(Message.sender_id == current_user.id)
    elif box == "all":
        base = base.filter(sa.or_(
            Message.sender_id == current_user.id,
            Message.recipient_id == current_user.id
        ))
    else:
        base = base.filter(Message.recipient_id == current_user.id)

    if include_deleted:
        base = base.filter(sa.or_(
            sa.and_(Message.sender_id == current_user.id, Message.deleted_by_sender.is_(True)),
            sa.and_(Message.recipient_id == current_user.id, Message.deleted_by_recipient.is_(True)),
        ))
    else:
        base = base.filter(sa.and_(
            sa.or_(Message.sender_id != current_user.id, Message.deleted_by_sender.is_(False)),
            sa.or_(Message.recipient_id != current_user.id, Message.deleted_by_recipient.is_(False)),
        ))

    if include_archived:
        base = base.filter(sa.or_(
            sa.and_(Message.sender_id == current_user.id, Message.archived_by_sender.is_(True)),
            sa.and_(Message.recipient_id == current_user.id, Message.archived_by_recipient.is_(True)),
        ))
    else:
        base = base.filter(sa.and_(
            sa.or_(Message.sender_id != current_user.id, Message.archived_by_sender.is_(False)),
            sa.or_(Message.recipient_id != current_user.id, Message.archived_by_recipient.is_(False)),
        ))

    if unread_only:
        base = base.filter(
            Message.recipient_id == current_user.id,
            Message.is_read.is_(False)
        )

    if q:
        base = base.filter(sa.or_(
            Message.subject.ilike(f"%{q}%"),
            Message.body.ilike(f"%{q}%"),
        ))

    base = base.order_by(Message.created_at.desc())

    pagination = base.paginate(page=page, per_page=per_page, error_out=False)
    messages_list = pagination.items

    user_ids = set()

    for m in messages_list:
        user_ids.add(m.sender_id)
        user_ids.add(m.recipient_id)

    users = {
        u.id: u
        for u in User.query.filter(User.id.in_(user_ids)).all()
    }

    rows = []

    for m in messages_list:
        is_sent = (m.sender_id == current_user.id)
        other_id = m.recipient_id if is_sent else m.sender_id

        rows.append({
            "m": m,
            "other": users.get(other_id),
            "is_sent": is_sent,
        })

    # ==================================
    # Selected Message for Reading Pane
    # ==================================
    selected_id = request.args.get("message_id", type=int)

    selected_message = None

    if selected_id:
        selected_message = (
            Message.query.filter(
                sa.or_(
                    Message.sender_id == current_user.id,
                    Message.recipient_id == current_user.id
                ),
                Message.id == selected_id
            )
            .first()
        )

    if not selected_message and rows:
        selected_message = rows[0]["m"]

    selected_other = None

    if selected_message:
        other_id = (
            selected_message.recipient_id
            if selected_message.sender_id == current_user.id
            else selected_message.sender_id
        )

        selected_other = User.query.get(other_id)

    # Auto mark as read
    if (
        selected_message
        and selected_message.recipient_id == current_user.id
        and not selected_message.is_read
    ):
        selected_message.is_read = True
        db.session.commit()    

    return render_template(
        "admin/messages_v2.html",
        form=form,
        rows=rows,
        pagination=pagination,
        box=box,
        q=q,
        unread_only=unread_only,
        include_archived=include_archived,
        include_deleted=include_deleted,
        per_page=per_page,
        selected_message=selected_message,
        selected_other=selected_other,
    )

@admin_bp.route("/messages/<int:message_id>", methods=["GET"])
@admin_required
def message_detail(message_id):
    m = Message.query.get_or_404(message_id)

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("admin.messages"))

    if m.sender_id == current_user.id and m.deleted_by_sender:
        flash("That message was deleted.", "warning")
        return redirect(url_for("admin.messages"))
    if m.recipient_id == current_user.id and m.deleted_by_recipient:
        flash("That message was deleted.", "warning")
        return redirect(url_for("admin.messages"))

    other_id = m.recipient_id if m.sender_id == current_user.id else m.sender_id
    other = User.query.get(other_id)

    if m.recipient_id == current_user.id and not m.is_read:
        m.is_read = True
        db.session.commit()

    customers = (User.query
                 .filter(User.role == "customer")
                 .order_by(User.full_name.asc())
                 .all())

    return render_template("admin/message_detail.html", m=m, other=other, customers=customers)


@admin_bp.route("/messages/<int:message_id>/reply", methods=["POST"])
@admin_required
def message_reply(message_id):
    original = Message.query.get_or_404(message_id)

    if original.sender_id != current_user.id and original.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("admin.messages"))

    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Message can't be empty.", "warning")
        return redirect(url_for("admin.messages", message_id=message_id))

    subject = (request.form.get("subject") or "").strip() or f"Re: {original.subject}"
    recipient_id = original.sender_id if original.sender_id != current_user.id else original.recipient_id

    dup = _is_duplicate_message(current_user.id, recipient_id, subject, body)
    if dup:
        flash("Duplicate reply prevented.", "warning")
        return redirect(url_for("admin.message_detail", message_id=message_id))

    msg = Message(
        sender_id=current_user.id,
        recipient_id=recipient_id,
        subject=subject,
        body=body,
        thread_key=None,
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
        if not allowed_message_attachment(original_name):
            continue

        try:
            f.stream.seek(0)
            url, public_id, rtype = upload_package_attachment(f)
        except Exception:
            current_app.logger.exception("[ADMIN MESSAGE REPLY ATTACHMENT] upload failed")
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

    other = User.query.get(recipient_id)
    if other and other.email:
        preview = (body[:120] + "…") if len(body) > 120 else body
        send_new_message_email(other.email, other.full_name, subject, preview, other.id)

    flash("Reply sent.", "success")
    return redirect(url_for("admin.messages", box="sent", message_id=msg.id))



@admin_bp.route("/messages/<int:message_id>/archive", methods=["POST"])
@admin_required
def message_archive(message_id):
    m = Message.query.get_or_404(message_id)

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("admin.messages"))

    if m.sender_id == current_user.id:
        m.archived_by_sender = True
    if m.recipient_id == current_user.id:
        m.archived_by_recipient = True

    db.session.commit()
    flash("Message archived.", "success")
    return redirect(url_for(
        "admin.messages",
        box=request.form.get("box") or "inbox",
        page=request.form.get("page") or 1,
        q=request.form.get("q") or None,
        unread=request.form.get("unread") or None,
        archived=request.form.get("archived") or None,
        trash=request.form.get("trash") or None,
        per_page=request.form.get("per_page") or None,
    ))


@admin_bp.route("/messages/<int:message_id>/delete", methods=["POST"])
@admin_required
def message_delete(message_id):
    m = Message.query.get_or_404(message_id)

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("admin.messages"))

    if m.sender_id == current_user.id:
        m.deleted_by_sender = True
    if m.recipient_id == current_user.id:
        m.deleted_by_recipient = True

    db.session.commit()
    flash("Message deleted.", "success")
    return redirect(url_for(
        "admin.messages",
        box=request.form.get("box") or "inbox",
        page=request.form.get("page") or 1,
        q=request.form.get("q") or None,
        unread=request.form.get("unread") or None,
        archived=request.form.get("archived") or None,
        trash=request.form.get("trash") or None,
        per_page=request.form.get("per_page") or None,
    ))


@admin_bp.route("/messages/<int:message_id>/forward", methods=["POST"])
@admin_required
def message_forward(message_id):
    m = Message.query.get_or_404(message_id)

    # Must belong to admin mailbox
    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        flash("You do not have access to that message.", "danger")
        return redirect(url_for("admin.messages"))

    # Forward to: customer dropdown OR manual email
    raw_user_id = (request.form.get("to_user_id") or "").strip()
    to_user_id = int(raw_user_id) if raw_user_id.isdigit() else None

    to_email = (request.form.get("to_email") or "").strip()
    note = (request.form.get("note") or "").strip()

    # Resolve email by user_id if provided
    if to_user_id:
        to_user = User.query.get(to_user_id)
        if to_user and to_user.email:
            to_email = to_user.email

    if not to_email:
        flash("Please select a customer or enter an email address to forward to.", "warning")
        return redirect(url_for("admin.messages", message_id=message_id))

    # Date label
    created_label = ""
    try:
        created_label = to_jamaica(m.created_at).strftime("%A, %B %d, %Y • %I:%M %p") if m.created_at else ""
    except Exception:
        created_label = str(m.created_at or "")

    original_subject = (m.subject or "Message").strip()

    # Avoid double "Fwd:"
    if original_subject.lower().startswith("fwd:"):
        email_subject = f"{original_subject} - Foreign A Foot Logistics"
    else:
        email_subject = f"Fwd: {original_subject} - Foreign A Foot Logistics"

    # Plain
    forwarded_plain = (
        (note + "\n\n" if note else "")
        + "---- Forwarded message ----\n"
        + f"Subject: {original_subject}\n"
        + (f"Date: {created_label}\n" if created_label else "")
        + "\n"
        + (m.body or "")
    )

    # ✅ HTML BODY ONLY (send_email wraps it with your FAFL header/footer/logo URL)
    note_html = ""
    if note:
        note_html = f"""
        <p style="margin:0 0 12px 0;">
          <b>Note:</b><br>
          {escape(note).replace("\\n", "<br>")}
        </p>
        """

    forwarded_html_body_only = f"""
{note_html}

<div style="border:1px solid #e5e7eb; border-radius:12px; padding:14px; background:#f9fafb;">
  <div style="font-size:13px; color:#6b7280; margin-bottom:10px;">
    <b>Forwarded message</b><br>
    <b>Subject:</b> {escape(original_subject)}<br>
    {"<b>Date:</b> " + escape(created_label) + "<br>" if created_label else ""}
  </div>

  <div style="white-space:pre-wrap; line-height:1.6; color:#111827;">
    {escape(m.body or "")}
  </div>
</div>
""".strip()

    ok = email_utils.send_email(
        to_email=to_email,
        subject=email_subject,
        plain_body=forwarded_plain,
        html_body=forwarded_html_body_only,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
        attachments=None,
        recipient_user_id=None,               # forwarding should NOT log into messages
    )

    if ok:
        flash("Message forwarded successfully.", "success")
    else:
        flash("Forward failed. Please try again.", "danger")

    return redirect(url_for("admin.messages", message_id=message_id))


@admin_bp.route("/messages/attachments/<int:attachment_id>")
@admin_required
def view_message_attachment(attachment_id):
    import requests
    from flask import Response
    from werkzeug.utils import secure_filename

    a = MessageAttachment.query.get_or_404(attachment_id)
    m = a.message

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        abort(403)

    filename = secure_filename(a.original_name or f"message_attachment_{a.id}") or f"message_attachment_{a.id}"

    # ✅ If Cloudinary/raw file lost the extension, force PDF fallback
    if "." not in filename:
        filename = f"{filename}.pdf"

    try:
        r = requests.get(a.file_url, stream=True, timeout=30)
    except Exception:
        abort(502)

    if r.status_code != 200:
        abort(404)

    content_type = r.headers.get("Content-Type") or "application/octet-stream"

    # ✅ Force correct PDF handling
    if filename.lower().endswith(".pdf"):
        content_type = "application/pdf"
    elif filename.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif filename.lower().endswith(".png"):
        content_type = "image/png"
    elif filename.lower().endswith(".webp"):
        content_type = "image/webp"

    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    resp = Response(generate(), mimetype=content_type)
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@admin_bp.route("/messages/attachments/<int:attachment_id>/download")
@admin_required
def download_message_attachment(attachment_id):
    import requests
    from flask import Response
    from werkzeug.utils import secure_filename

    a = MessageAttachment.query.get_or_404(attachment_id)
    m = a.message

    if m.sender_id != current_user.id and m.recipient_id != current_user.id:
        abort(403)

    filename = secure_filename(a.original_name or f"message_attachment_{a.id}") or f"message_attachment_{a.id}"

    # ✅ If Cloudinary/raw file lost the extension, force PDF fallback
    if "." not in filename:
        filename = f"{filename}.pdf"

    try:
        r = requests.get(a.file_url, stream=True, timeout=30)
    except Exception:
        abort(502)

    if r.status_code != 200:
        abort(404)

    content_type = r.headers.get("Content-Type") or "application/octet-stream"

    # ✅ Force correct PDF/image content type
    if filename.lower().endswith(".pdf"):
        content_type = "application/pdf"
    elif filename.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif filename.lower().endswith(".png"):
        content_type = "image/png"
    elif filename.lower().endswith(".webp"):
        content_type = "image/webp"

    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    resp = Response(generate(), mimetype=content_type)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# -------------------------
# BULK ACTION HELPERS
# -------------------------
def _bulk_ids_from_form():
    ids = request.form.getlist("message_ids")
    try:
        return [int(x) for x in ids if str(x).isdigit()]
    except Exception:
        return []

def _redirect_back_to_mailbox(default_box="inbox"):
    return redirect(url_for(
        "admin.messages",
        box=(request.form.get("box") or default_box),
        q=(request.form.get("q") or None),
        unread=(request.form.get("unread") or None),
        archived=(request.form.get("archived") or None),
        trash=(request.form.get("trash") or None),
        per_page=(request.form.get("per_page") or None),
        page=(request.form.get("page") or None),
    ))


# -------------------------
# BULK: DELETE
# -------------------------
@admin_bp.route("/messages/bulk-delete", methods=["POST"])
@admin_required
def messages_bulk_delete():
    ids = _bulk_ids_from_form()
    if not ids:
        flash("Select at least one message to delete.", "warning")
        return _redirect_back_to_mailbox()

    msgs = Message.query.filter(Message.id.in_(ids)).all()

    changed = 0
    for m in msgs:
        if m.sender_id != current_user.id and m.recipient_id != current_user.id:
            continue

        if m.sender_id == current_user.id:
            m.deleted_by_sender = True
        if m.recipient_id == current_user.id:
            m.deleted_by_recipient = True

        changed += 1

    db.session.commit()
    flash(f"Deleted {changed} message(s).", "success")
    return _redirect_back_to_mailbox()


# -------------------------
# BULK: ARCHIVE
# -------------------------
@admin_bp.route("/messages/bulk-archive", methods=["POST"])
@admin_required
def messages_bulk_archive():
    ids = _bulk_ids_from_form()
    if not ids:
        flash("Select at least one message to archive.", "warning")
        return _redirect_back_to_mailbox()

    msgs = Message.query.filter(Message.id.in_(ids)).all()

    changed = 0
    for m in msgs:
        if m.sender_id != current_user.id and m.recipient_id != current_user.id:
            continue

        if m.sender_id == current_user.id:
            m.archived_by_sender = True
        if m.recipient_id == current_user.id:
            m.archived_by_recipient = True

        changed += 1

    db.session.commit()
    flash(f"Archived {changed} message(s).", "success")
    return _redirect_back_to_mailbox()


# -------------------------
# BULK: UNARCHIVE (Move to Inbox)
# -------------------------
@admin_bp.route("/messages/bulk-unarchive", methods=["POST"])
@admin_required
def messages_bulk_unarchive():
    ids = _bulk_ids_from_form()
    if not ids:
        flash("Select at least one message to move to inbox.", "warning")
        return _redirect_back_to_mailbox()

    msgs = Message.query.filter(Message.id.in_(ids)).all()

    changed = 0
    for m in msgs:
        if m.sender_id != current_user.id and m.recipient_id != current_user.id:
            continue

        if m.sender_id == current_user.id:
            m.archived_by_sender = False
            m.deleted_by_sender = False

        if m.recipient_id == current_user.id:
            m.archived_by_recipient = False
            m.deleted_by_recipient = False

        changed += 1

    db.session.commit()
    if request.form.get("trash") == "1":
        flash(f"Restored {changed} message(s).", "success")
    else:
        flash(f"Moved {changed} message(s) to inbox.", "success")
    return _redirect_back_to_mailbox()


# -------------------------
# BULK: MARK READ
# (Only meaningful when you are recipient)
# -------------------------
@admin_bp.route("/messages/bulk-mark-read", methods=["POST"])
@admin_required
def messages_bulk_mark_read():
    ids = _bulk_ids_from_form()
    if not ids:
        flash("Select at least one message.", "warning")
        return _redirect_back_to_mailbox()

    msgs = Message.query.filter(Message.id.in_(ids)).all()

    changed = 0
    for m in msgs:
        if m.recipient_id == current_user.id and not m.is_read:
            m.is_read = True
            changed += 1

    db.session.commit()
    flash(f"Marked {changed} message(s) as read.", "success")
    return _redirect_back_to_mailbox()


# -------------------------
# BULK: MARK UNREAD
# -------------------------
@admin_bp.route("/messages/bulk-mark-unread", methods=["POST"])
@admin_required
def messages_bulk_mark_unread():
    ids = _bulk_ids_from_form()
    if not ids:
        flash("Select at least one message.", "warning")
        return _redirect_back_to_mailbox()

    msgs = Message.query.filter(Message.id.in_(ids)).all()

    changed = 0
    for m in msgs:
        if m.recipient_id == current_user.id and m.is_read:
            m.is_read = False
            changed += 1

    db.session.commit()
    flash(f"Marked {changed} message(s) as unread.", "success")
    return _redirect_back_to_mailbox()

# --- Admin Notifications: list + broadcast ---
@admin_bp.route("/notifications", methods=["GET", "POST"])
@admin_required
def view_notifications():
    form = BroadcastNotificationForm()

    if request.method == "POST" and form.validate_on_submit():
        subject = form.subject.data.strip()
        message = form.message.data.strip()

        customers = (
            User.query
            .filter(User.role == "customer")
            .with_entities(User.id)
            .all()
        )

        rows = []
        now = datetime.now(timezone.utc)

        for (uid,) in customers:
            rows.append({
                "user_id": uid,
                "subject": subject,
                "message": message,
                "is_read": False,
                "is_broadcast": True,
                "created_at": now,
            })

        db.session.bulk_insert_mappings(Notification, rows)
        db.session.commit()

        flash(f"Broadcast sent to {len(customers)} customers.", "success")
        return redirect(url_for("admin.view_notifications"))

    notes = (
        Notification.query
        .filter(Notification.is_broadcast.is_(True))
        .order_by(Notification.created_at.desc())
        .limit(300)
        .all()
    )

    delivered_map = {}
    for n in notes:
        delivered_map[n.id] = db.session.scalar(
            sa.select(func.count()).select_from(Notification).where(
                Notification.is_broadcast.is_(True),
                Notification.subject == n.subject,
                Notification.message == n.message,
                Notification.created_at == n.created_at
            )
        ) or 0

    return render_template("admin/notifications.html", notes=notes, form=form, datetime=datetime, delivered_map=delivered_map)



@admin_bp.route("/notifications/mark_read/<int:nid>", methods=["POST"])
@admin_required
def mark_notification_read(nid):
    n = Notification.query.get_or_404(nid)
    n.is_read = True
    db.session.commit()
    flash("Notification marked as read.", "success")
    return redirect(url_for("admin.view_notifications"))


# ---------- GENERATE INVOICE ----------

@admin_bp.route('/generate-invoice/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def generate_invoice_route(user_id):
    user = User.query.get_or_404(user_id)

    packages = (
        Package.query
        .filter_by(user_id=user.id, invoice_id=None)
        .order_by(Package.created_at.asc())
        .all()
    )

    from app.utils.unassigned import is_pkg_unassigned

    # ✅ BLOCK: if any package is UNASSIGNED, stop
    bad = [p for p in packages if is_pkg_unassigned(p)]
    if bad:
        flash(
            f"Cannot generate invoice: {len(bad)} package(s) are UNASSIGNED. "
            "Assign them to a customer first.",
            "danger"
        )
        return redirect(url_for('admin.dashboard'))

    if not packages:
        flash("No packages available to invoice.", "warning")
        return redirect(url_for('admin.dashboard'))

    if request.method != 'POST':
        return render_template("admin/invoice_confirm.html", user=user, packages=packages)

    # create invoice shell
    invoice_number = f"INV-{user.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    now = datetime.utcnow()

    inv = Invoice(
        user_id=user.id,
        invoice_number=invoice_number,
        date_submitted=now,   # ✅ use same timestamp
        date_issued=now,      # ✅ header date will now show
        created_at=now,
        status="unpaid",
        amount_due=0,
    )
    db.session.add(inv)
    db.session.flush()  # get inv.id

    totals = dict(
        duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0,
        freight=0, handling=0, other_charges=0, bad_address=0, grand_total=0,
    )
    view_lines = []

    for p in packages:
        desc = (
            getattr(p, "category", None)
            or p.description
            or "Miscellaneous"
        )

        wt = float(p.weight or 0)

        val = float(
            getattr(p, "declared_value", None)
            or getattr(p, "value", 0)
            or 0
        )

        is_subscription_covered = bool(
            getattr(p, "subscription_applied", False)
            and (
                getattr(
                    p,
                    "subscription_result",
                    "",
                )
                or ""
            )
            == "subscription_applied"
        )

        # A covered package can correctly have zero saved charges.
        # It must never fall through to normal charge calculation.
        has_saved_charges = (
            is_subscription_covered
            or any([
            float(getattr(p, "duty", 0) or 0) > 0,
            float(getattr(p, "scf", 0) or 0) > 0,
            float(getattr(p, "envl", 0) or 0) > 0,
            float(getattr(p, "caf", 0) or 0) > 0,
            float(getattr(p, "gct", 0) or 0) > 0,
            float(getattr(p, "stamp", 0) or 0) > 0,
            float(getattr(p, "other_charges", 0) or 0) > 0,
            float(getattr(p, "amount_due", 0) or 0) > 0,
          ])
        )

        if has_saved_charges:
            duty          = float(getattr(p, "duty", 0) or 0)
            scf           = float(getattr(p, "scf", 0) or 0)
            envl          = float(getattr(p, "envl", 0) or 0)
            caf           = float(getattr(p, "caf", 0) or 0)
            gct           = float(getattr(p, "gct", 0) or 0)
            stamp         = float(getattr(p, "stamp", 0) or 0)

            freight = float(
                getattr(
                    p,
                    "freight_fee",
                    getattr(p, "freight", 0),
                )
                or 0
            )

            handling = float(
                getattr(
                    p,
                    "handling_fee",
                    getattr(
                        p,
                        "storage_fee",
                        getattr(p, "handling", 0),
                    ),
                )
                or 0
            )

            other_charges = float(getattr(p, "other_charges", 0) or 0)
            bad_address_fee = float(getattr(p, "bad_address_fee", 0) or 0) 

            if is_subscription_covered:
                # Subscription always removes freight and handling.
                freight = 0.0
                handling = 0.0

                # Customs are also removed when value is US$100 or less.
                if val <= 100:
                    duty = 0.0
                    scf = 0.0
                    envl = 0.0
                    caf = 0.0
                    gct = 0.0
                    stamp = 0.0

                if (
                    bool(
                        getattr(p, "epc", False)
                        or getattr(
                            p,
                            "bad_address",
                            False,
                        )
                    )
                    and bad_address_fee <= 0
                ):
                    bad_address_fee = 500.0

                grand_total = (
                    duty
                    + scf
                    + envl
                    + caf
                    + gct
                    + stamp
                    + other_charges
                    + bad_address_fee
                )

                # Remove any old discount from when the
                # subscription was previously exhausted.
                if hasattr(p, "discount_due"):
                    p.discount_due = 0.0

                if hasattr(p, "freight_fee"):
                    p.freight_fee = 0.0

                if hasattr(p, "freight"):
                    p.freight = 0.0

                if hasattr(p, "handling_fee"):
                    p.handling_fee = 0.0

                if hasattr(p, "storage_fee"):
                    p.storage_fee = 0.0

                if hasattr(p, "handling"):
                    p.handling = 0.0

                if hasattr(p, "freight_total"):
                    p.freight_total = (
                        other_charges
                        + bad_address_fee
                    )

            else:
                grand_total = float(
                    getattr(p, "amount_due", 0)
                    or getattr(p, "grand_total", 0)
                    or 0
                )

            p.amount_due = grand_total

            if hasattr(p, "grand_total"):
                p.grand_total = grand_total

            p.invoice_id = inv.id

            # build a ch-like dict for view_lines (so your render below still works)
            ch = {
                "duty": duty,
                "scf": scf,
                "envl": envl,
                "caf": caf,
                "gct": gct,
                "stamp": stamp,
                "freight": freight,
                "handling": handling,
                "other_charges": other_charges,
                "bad_address_fee": bad_address_fee,
                "grand_total": grand_total,
                "customs_total": float(getattr(p, "customs_total", 0) or 0),
                "freight_total": float(getattr(p, "freight_total", 0) or 0),
                "discount_due": float(getattr(p, "discount_due", 0) or 0),
            }

            # ✅ still link package to invoice (do not change charges)
            p.invoice_id = inv.id

        else:
            # calculate full breakdown (only when nothing saved)
            ch = calculate_charges(desc, val, wt)

            duty          = float(ch.get("duty", 0) or 0)
            scf           = float(ch.get("scf", 0) or 0)
            envl          = float(ch.get("envl", 0) or 0)
            caf           = float(ch.get("caf", 0) or 0)
            gct           = float(ch.get("gct", 0) or 0)
            stamp         = float(ch.get("stamp", 0) or 0)
            freight       = float(ch.get("freight", 0) or 0)
            handling      = float(ch.get("handling", 0) or 0)
            bad_address_fee = float(getattr(p, "bad_address_fee", 0) or 0)
            other_charges = float(ch.get("other_charges", 0) or 0)
            grand_total   = float(ch.get("grand_total", 0) or 0)

            # --- persist breakdown onto the Package row ---
            p.category      = desc
            p.value         = val
            p.weight        = wt
            p.duty          = duty
            p.scf           = scf
            p.envl          = envl
            p.caf           = caf
            p.gct           = gct
            p.stamp         = stamp
            p.customs_total = float(ch.get("customs_total", 0) or 0)

            # map freight / handling to whichever columns exist
            if hasattr(p, "freight_fee"):
                p.freight_fee = freight
            else:
                p.freight = freight

            if hasattr(p, "storage_fee"):
                p.storage_fee = handling
            else:
                p.handling = handling

            p.other_charges = other_charges
            p.bad_address_fee = bad_address_fee
            p.amount_due    = grand_total
            p.invoice_id    = inv.id

        # aggregate invoice totals (works for both branches)
        totals["duty"]          += duty
        totals["scf"]           += scf
        totals["envl"]          += envl
        totals["caf"]           += caf
        totals["gct"]           += gct
        totals["stamp"]         += stamp
        totals["freight"]       += freight
        totals["handling"]      += handling
        totals["other_charges"] += other_charges
        totals["bad_address"]   += bad_address_fee
        totals["grand_total"]   += grand_total

        view_lines.append({
            "house_awb":   p.house_awb,
            "description": desc,
            "weight":      wt,
            "value_usd":   val,
            "bad_address_fee": bad_address_fee,
            **ch,
            "subscription_applied": getattr(p, "subscription_applied", False),
            "subscription_result": getattr(p, "subscription_result", None),
            "subscription_covered": (
                getattr(p, "subscription_applied", False)
                and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
            ),
            "customs_only_due_to_subscription": (
                getattr(p, "subscription_applied", False)
                and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
                and float(getattr(p, "customs_total", 0) or 0) > 0
            ),
        })

    # finalize invoice from totals
    inv.total_duty     = totals["duty"]
    inv.total_scf      = totals["scf"]
    inv.total_envl     = totals["envl"]
    inv.total_caf      = totals["caf"]
    inv.total_gct      = totals["gct"]
    inv.total_stamp    = totals["stamp"]
    inv.total_freight  = totals["freight"]
    inv.total_handling = totals["handling"]
    if hasattr(inv, "total_bad_address"):
        inv.total_bad_address = totals["bad_address"]
    inv.grand_total    = totals["grand_total"]
    inv.amount_due     = totals["grand_total"]
    inv.subtotal       = totals["grand_total"]

    db.session.commit()

    invoice_dict = {
        "id":            inv.id,
        "user_id":       user.id,
        "number":        inv.invoice_number,
        "date":          inv.date_submitted,
        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "subtotal":      totals["grand_total"],
        "total_due":     totals["grand_total"],
        "packages": [{
            "house_awb":      x["house_awb"],
            "description":    x["description"],
            "weight":         x["weight"],
            "value":          x["value_usd"],
            "freight":        x.get("freight", 0),
            "storage":        x.get("handling", 0),
            "duty":           x.get("duty", 0),
            "scf":            x.get("scf", 0),
            "envl":           x.get("envl", 0),
            "caf":            x.get("caf", 0),
            "gct":            x.get("gct", 0),
            "other_charges":  x.get("other_charges", 0),
            "bad_address_fee": x.get("bad_address_fee", 0),
            "discount_due":   x.get("discount_due", 0),
            "subscription_applied": x.get("subscription_applied", False),
            "subscription_result": x.get("subscription_result", None),

            "subscription_covered": x.get("subscription_covered", False),
            "customs_only_due_to_subscription": x.get("customs_only_due_to_subscription", False),
        } for x in view_lines],
    }

    flash(f"Invoice {invoice_number} generated successfully!", "success")
    return render_template("admin/invoice_view.html", invoice=invoice_dict)


@admin_bp.route(
    "/invoice/create/<int:package_id>",
    methods=["GET", "POST"],
)
@admin_required
def invoice_create(package_id):
    package = Package.query.get_or_404(package_id)
    calculation = None

    from app.utils.unassigned import is_pkg_unassigned

    if is_pkg_unassigned(package):
        flash(
            "Cannot create an invoice for an UNASSIGNED "
            "package. Assign it to a customer first.",
            "danger",
        )
        return redirect(
            request.referrer
            or url_for("admin.dashboard")
        )

    if getattr(package, "invoice_id", None):
        flash(
            "This package is already attached to an invoice.",
            "warning",
        )
        return redirect(
            url_for(
                "admin.view_invoice",
                invoice_id=package.invoice_id,
            )
        )

    if request.method == "POST":
        try:
            category = (
                request.form.get("category")
                or getattr(package, "category", None)
                or package.description
                or "Miscellaneous"
            ).strip()

            invoice_usd = float(
                request.form.get(
                    "invoice_usd",
                    getattr(
                        package,
                        "declared_value",
                        None,
                    )
                    or package.value
                    or 0,
                )
            )

            new_weight = float(
                request.form.get(
                    "weight",
                    package.weight or 0,
                )
            )

            other_charges = float(
                request.form.get(
                    "other_charges",
                    getattr(
                        package,
                        "other_charges",
                        0,
                    )
                    or 0,
                )
            )

            if invoice_usd < 0:
                raise ValueError(
                    "Declared value cannot be negative."
                )

            if new_weight < 0:
                raise ValueError(
                    "Weight cannot be negative."
                )

            if other_charges < 0:
                raise ValueError(
                    "Other charges cannot be negative."
                )

            old_weight = float(
                package.weight or 0
            )

            package.category = category
            package.value = invoice_usd
            package.weight = new_weight
            package.other_charges = other_charges

            if hasattr(package, "declared_value"):
                package.declared_value = invoice_usd

            # A weight change may change eligibility or allowance.
            if abs(new_weight - old_weight) > 0.0001:
                subscription_result = (
                    reconcile_subscription_usage(
                        package
                    )
                )

                if subscription_result not in (
                    "subscription_applied",
                    "already_applied",
                ):
                    clear_package_subscription(
                        package,
                        result=(
                            subscription_result
                            or "no_subscription"
                        ),
                    )

            subscription_covered = bool(
                getattr(
                    package,
                    "subscription_applied",
                    False,
                )
                and (
                    getattr(
                        package,
                        "subscription_result",
                        "",
                    )
                    or ""
                )
                == "subscription_applied"
            )

            calculation = calculate_charges(
                category,
                invoice_usd,
                new_weight,
            ) or {}

            def charge(*keys):
                for key in keys:
                    value = calculation.get(key)

                    if value not in (None, ""):
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            continue

                return 0.0

            duty = charge("duty", "duty_amount")
            scf = charge("scf", "scf_amount")
            envl = charge("envl", "envl_amount")
            caf = charge("caf", "caf_amount")
            gct = charge("gct", "gct_amount")
            stamp = charge("stamp", "stamp_amount")

            customs_total = charge(
                "customs_total",
                "customs",
                "customs_amount",
                "customs_total_amount",
            )

            freight = charge(
                "freight",
                "freight_fee",
                "freight_amount",
            )

            handling = charge(
                "handling",
                "handling_fee",
                "storage_fee",
                "handling_amount",
            )

            has_bad_address = bool(
                getattr(package, "epc", False)
                or getattr(
                    package,
                    "bad_address",
                    False,
                )
            )

            bad_address_fee = (
                500.0 if has_bad_address else 0.0
            )

            subscription_discount = 0.0

            if subscription_covered:
                # Active allowance removes freight/handling.
                freight = 0.0
                handling = 0.0

                # Value of US$100 or less also removes customs.
                if invoice_usd <= 100:
                    duty = 0.0
                    scf = 0.0
                    envl = 0.0
                    caf = 0.0
                    gct = 0.0
                    stamp = 0.0
                    customs_total = 0.0
                else:
                    # Ensure the total agrees with the saved
                    # customs components.
                    customs_total = (
                        duty
                        + scf
                        + envl
                        + caf
                        + gct
                        + stamp
                    )

            else:
                # An exhausted subscription receives 5% off
                # freight and handling only, when eligible.
                discount_percent = float(
                    get_subscription_discount_percent(
                        package
                    )
                    or 0
                )

                if discount_percent > 0:
                    subscription_discount = round(
                        (
                            freight
                            + handling
                        )
                        * (
                            discount_percent
                            / 100
                        ),
                        2,
                    )

            grand_total = max(
                customs_total
                + freight
                + handling
                + other_charges
                + bad_address_fee
                - subscription_discount,
                0.0,
            )

            package.duty = duty
            package.scf = scf
            package.envl = envl
            package.caf = caf
            package.gct = gct
            package.stamp = stamp
            package.customs_total = customs_total

            if hasattr(package, "freight_fee"):
                package.freight_fee = freight

            if hasattr(package, "freight"):
                package.freight = freight

            if hasattr(package, "handling_fee"):
                package.handling_fee = handling

            if hasattr(package, "storage_fee"):
                package.storage_fee = handling

            if hasattr(package, "handling"):
                package.handling = handling

            if hasattr(package, "freight_total"):
                package.freight_total = (
                    freight
                    + handling
                    + other_charges
                    + bad_address_fee
                )

            if hasattr(package, "bad_address_fee"):
                package.bad_address_fee = (
                    bad_address_fee
                )

            if hasattr(package, "discount_due"):
                package.discount_due = (
                    subscription_discount
                )

            if hasattr(package, "grand_total"):
                package.grand_total = grand_total

            package.amount_due = grand_total

            now = datetime.utcnow()

            invoice = Invoice(
                user_id=package.user_id,
                invoice_number=(
                    f"INV-{package.user_id}-"
                    f"{now.strftime('%Y%m%d%H%M%S')}"
                ),
                date_submitted=now,
                date_issued=now,
                created_at=now,
                status="unpaid",
                subtotal=grand_total,
                grand_total=grand_total,
                amount_due=grand_total,
            )

            db.session.add(invoice)
            db.session.flush()

            package.invoice_id = invoice.id

            db.session.commit()

            flash(
                "Invoice created successfully.",
                "success",
            )

            return redirect(
                url_for(
                    "admin.view_invoice",
                    invoice_id=invoice.id,
                )
            )

        except (TypeError, ValueError) as error:
            db.session.rollback()
            flash(str(error), "danger")

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "Creating an invoice for package %s failed",
                package_id,
            )

            flash(
                f"Invoice could not be created: {error}",
                "danger",
            )

    return render_template(
        "admin/create_invoice.html",
        package=package,
        categories=CATEGORIES.keys(),
        result=calculation,
    )

@admin_bp.route('/invoices/user/<int:user_id>', methods=['GET'], endpoint='view_customer_invoice')
@admin_required
def view_customer_invoice(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin.dashboard'))

    now_utc = datetime.now(timezone.utc)

    pkgs = (
        Package.query
        .filter(
            Package.user_id == user.id,
            Package.invoice_id.is_(None)
        )
        .order_by(asc(getattr(Package, "date_received", Package.created_at)))
        .all()
    )

    from app.utils.unassigned import is_pkg_unassigned

    bad = [p for p in pkgs if is_pkg_unassigned(p)]
    if bad:
        flash(f"Cannot view/generate proforma: {len(bad)} package(s) are UNASSIGNED. Assign them first.", "danger")
        return redirect(url_for("admin.dashboard"))


    items = []
    totals = dict(
        duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0,
        freight=0, handling=0, other_charges=0, bad_address=0, grand_total=0,
    )

    for p in pkgs:
        desc = p.description or "Miscellaneous"
        wt   = float(getattr(p, "weight", 0) or 0)
        val  = float(getattr(p, "value", 0) or 0)

        # ✅ FIX: define tracking safely
        tracking = (
            getattr(p, "tracking_number", None)
            or getattr(p, "tracking", None)
            or getattr(p, "tracking_no", None)
            or ""
        )

        # charges already stored
        duty          = float(getattr(p, "duty", 0) or 0)
        scf           = float(getattr(p, "scf", 0) or 0)
        envl          = float(getattr(p, "envl", 0) or 0)
        caf           = float(getattr(p, "caf", 0) or 0)
        gct           = float(getattr(p, "gct", 0) or 0)
        stamp         = float(getattr(p, "stamp", 0) or 0)

        # freight/handling can live in different columns depending on your model
        # ✅ Subscription-covered packages should not show freight
        is_subscription_covered = (
            bool(getattr(p, "subscription_applied", False))
            and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
        )

        value_usd = float(getattr(p, "value", 0) or 0)

        if is_subscription_covered:
            freight = 0.0
            handling = 0.0
        else:
            freight = float(getattr(p, "freight_fee", getattr(p, "freight", 0)) or 0)
            handling = float(getattr(p, "storage_fee", getattr(p, "handling", 0)) or 0)

        other_charges = float(getattr(p, "other_charges", 0) or 0)
        bad_address_fee = float(getattr(p, "bad_address_fee", 0) or 0)
        grand_total   = float(getattr(p, "amount_due", 0) or getattr(p, "grand_total", 0) or 0)

        items.append({
            "id": p.id,
            "house_awb": p.house_awb,
            "description": desc,
            "tracking_number": tracking,     # ✅ REQUIRED by your template
            "weight": wt,
            "value_usd": val,

            "freight": freight,
            "storage": handling,

            # ✅ aliases your breakdown modal/template uses
            "freight_fee": freight,
            "storage_fee": handling,

            "duty": duty,
            "scf": scf,
            "envl": envl,
            "caf": caf,
            "gct": gct,
            "stamp": stamp,

            "other_charges": other_charges,
            "bad_address_fee": bad_address_fee,
            "discount_due": float(getattr(p, "discount_due", 0) or 0),

            "amount_due": grand_total,       # ✅ REQUIRED by your template
        })

        totals["duty"]          += duty
        totals["scf"]           += scf
        totals["envl"]          += envl
        totals["caf"]           += caf
        totals["gct"]           += gct
        totals["stamp"]         += stamp
        totals["freight"]       += freight
        totals["handling"]      += handling
        totals["other_charges"] += other_charges
        totals["bad_address"]   += bad_address_fee
        totals["grand_total"]   += grand_total

    subtotal = float(totals["grand_total"] or 0.0)
    discount_total = float(totals.get("discount_total", 0.0) or 0.0)
    payments_total = 0.0
    total_due = max(subtotal - discount_total - payments_total, 0.0)

    invoice_dict = {
        "id": int(user.id),
        "number": f"PROFORMA-{user.id}",

        "date": now_utc,

    # ✅ add these so the same template works for real invoices AND proforma
        "date_issued": now_utc,
        "date_submitted": now_utc,
        "created_at": now_utc,

        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "branch": "Main Branch",
        "staff": getattr(current_user, "full_name", "FAFL ADMIN"),

        # Optional but used in template
        "credit_available": float(getattr(user, "wallet_balance", 0) or 0),

        "subtotal": subtotal,
        "discount_total": discount_total,
        "payments_total": payments_total,
        "total_due": total_due,

        # ✅ IMPORTANT: pass FULL package keys the template expects
        "packages": [
            {
                "id": i["id"],
                "house_awb": i["house_awb"],
                "description": i["description"],
                "tracking_number": i["tracking_number"],   # ✅
                "weight": i["weight"],
                "value": i["value_usd"],

                "freight": i.get("freight", 0),
                "storage": i.get("storage", 0),

                # ✅ for AWB modal (your template uses these names)
                "freight_fee": i.get("freight_fee", i.get("freight", 0)),
                "storage_fee": i.get("storage_fee", i.get("storage", 0)),

                "duty": i["duty"],
                "scf": i["scf"],
                "envl": i["envl"],
                "caf": i["caf"],
                "gct": i["gct"],
                "stamp": i["stamp"],

                "other_charges": i["other_charges"],
                "bad_address_fee": i.get("bad_address_fee", 0),
                "discount_due": i["discount_due"],

                "amount_due": i.get("amount_due", 0),
            }
            for i in items
        ],
    }

    return render_template(
        "admin/invoices/_invoice_inline.html",
        invoice=invoice_dict,
        USD_TO_JMD=USD_TO_JMD,
        proforma_user_id=user.id,
    )


@admin_bp.route('/invoice/mark_paid', methods=['POST'])
@admin_required
def mark_invoice_paid():
    try:
        invoice_id = int(request.form.get('invoice_id') or 0)
        amount = round(
            float(request.form.get('payment_amount') or 0),
            2
        )
        method = (
            request.form.get('payment_type') or "Cash"
        ).strip()
        authorized_by = (
            request.form.get('authorized_by') or "Admin"
        ).strip()

        inv = Invoice.query.get_or_404(invoice_id)

        old_status = inv.status or "unpaid"
        old_amount_due = round(
            float(inv.amount_due or 0),
            2
        )

        # Calculate the live balance using existing payments.
        subtotal, discount_total, payments_total, total_due = (
            fetch_invoice_totals_pg(inv.id)
        )

        total_due = round(
            max(float(total_due or 0), 0.0),
            2
        )
        payments_total = round(
            float(payments_total or 0),
            2
        )

        # ---------------------------------------------------------
        # The invoice is already fully paid.
        # Close/synchronize it without creating another payment.
        # ---------------------------------------------------------
        if total_due == 0:
            now_utc = datetime.now(timezone.utc)

            inv.amount_due = 0.00
            inv.status = "paid"

            if not inv.date_paid:
                inv.date_paid = now_utc

            if old_status != "paid":
                lock_delivered_packages_for_invoice(
                    inv.id,
                    reason="Invoice fully paid",
                    actor_admin_id=current_user.id,
                )

            db.session.add(AuditLog(
                module="Finance",
                action="Invoice Status Synchronized",
                admin_id=current_user.id,
                user_id=inv.user_id,
                entity_type="Invoice",
                entity_id=inv.id,
                reason="Existing payments cover invoice",
                description=(
                    f"Invoice "
                    f"{inv.invoice_number or ('#' + str(inv.id))} "
                    f"was closed without creating another payment because "
                    f"existing completed payments already covered the balance. "
                    f"Authorized by: {authorized_by}."
                ),
                old_value=(
                    f"Status: {old_status}; "
                    f"Amount Due: JMD {old_amount_due:,.2f}"
                ),
                new_value=(
                    f"Status: paid; "
                    f"Amount Due: JMD 0.00; "
                    f"Total Paid: JMD {payments_total:,.2f}"
                ),
            ))

            db.session.commit()

            return jsonify({
                "success": True,
                "invoice_id": inv.id,
                "status": inv.status,
                "amount_due": 0.00,
                "paid_sum": payments_total,
                "payment_date": (
                    inv.date_paid.strftime('%Y-%m-%d %H:%M:%S')
                    if inv.date_paid
                    else now_utc.strftime('%Y-%m-%d %H:%M:%S')
                ),
                "payment_type": "Existing payment",
                "amount": 0.00,
                "authorized_by": authorized_by,
                "payment_created": False,
                "message": (
                    "Invoice closed successfully. "
                    "No additional payment was created."
                ),
            })

        # ---------------------------------------------------------
        # A balance remains, so a real payment is required.
        # ---------------------------------------------------------
        if amount <= 0:
            return jsonify({
                "success": False,
                "error": (
                    f"Enter a payment amount greater than 0. "
                    f"Balance due: ${total_due:,.2f}."
                ),
            }), 400

        # Prevent accidental overpayment.
        if amount > total_due:
            return jsonify({
                "success": False,
                "error": (
                    f"Payment cannot exceed the outstanding balance of "
                    f"${total_due:,.2f}."
                ),
            }), 400

        notes = (
            f"Authorized by: {authorized_by}"
            if authorized_by
            else None
        )

        payment = Payment(
            invoice_id=inv.id,
            user_id=inv.user_id,
            method=method,
            amount_jmd=amount,
            notes=notes,
            transaction_type="invoice_payment",
            status="completed",
            source="admin",
            authorized_by_admin_id=current_user.id,
            created_at=datetime.now(timezone.utc),
        )

        db.session.add(payment)
        db.session.flush()

        # Recalculate after saving the new payment.
        subtotal, discount_total, payments_total, total_due = (
            fetch_invoice_totals_pg(inv.id)
        )

        payments_total = round(
            float(payments_total or 0),
            2
        )
        total_due = round(
            max(float(total_due or 0), 0.0),
            2
        )

        inv.amount_due = total_due

        if total_due == 0:
            inv.status = "paid"
            inv.date_paid = datetime.now(timezone.utc)

            if old_status != "paid":
                lock_delivered_packages_for_invoice(
                    inv.id,
                    reason="Invoice fully paid",
                    actor_admin_id=current_user.id,
                )

        elif payments_total > 0:
            inv.status = "partial"
            inv.date_paid = None

        else:
            inv.status = "unpaid"
            inv.date_paid = None

        db.session.add(AuditLog(
            module="Finance",
            action="Invoice Payment Recorded",
            admin_id=current_user.id,
            user_id=inv.user_id,
            entity_type="Invoice",
            entity_id=inv.id,
            reason="Invoice payment",
            description=(
                f"Payment of JMD {amount:,.2f} recorded on invoice "
                f"{inv.invoice_number or ('#' + str(inv.id))}. "
                f"Method: {method}. "
                f"Authorized by: {authorized_by}. "
                f"Invoice status changed from "
                f"{old_status} to {inv.status}."
            ),
            old_value=(
                f"Status: {old_status}; "
                f"Amount Due: JMD {old_amount_due:,.2f}"
            ),
            new_value=(
                f"Status: {inv.status}; "
                f"Amount Due: JMD "
                f"{float(inv.amount_due or 0):,.2f}; "
                f"Total Paid: JMD {payments_total:,.2f}"
            ),
        ))

        db.session.commit()

        return jsonify({
            "success": True,
            "invoice_id": inv.id,
            "status": inv.status,
            "amount_due": float(inv.amount_due or 0),
            "paid_sum": payments_total,
            "payment_date": payment.created_at.strftime(
                '%Y-%m-%d %H:%M:%S'
            ),
            "payment_type": method,
            "amount": amount,
            "authorized_by": authorized_by,
            "payment_created": True,
            "message": "Payment recorded successfully.",
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to record invoice payment"
        )

        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

@admin_bp.route("/invoice/cart_summary", methods=["POST"])
@admin_required
def invoice_cart_summary():
    try:
        data = request.get_json(silent=True) or {}
        user_id = int(data.get("user_id") or 0)
        invoice_ids = data.get("invoice_ids") or []

        ids = []
        for x in invoice_ids:
            try:
                ids.append(int(x))
            except Exception:
                pass
        ids = list(dict.fromkeys(ids))

        if not user_id or not ids:
            return jsonify({"success": False, "error": "No invoices selected."}), 400

        # Load invoices and compute due/paid/owed
        items = []
        for inv_id in ids:
            inv = Invoice.query.get(inv_id)
            if not inv:
                continue

            # ✅ safety: only invoices that belong to this user
            ok = False
            if hasattr(inv, "user_id") and inv.user_id == user_id:
                ok = True
            if hasattr(inv, "customer_id") and inv.customer_id == user_id:
                ok = True

            if not ok:
                continue

            subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)

            label = getattr(inv, "invoice_number", None) or f"INV{inv.id:05d}"

            # due = subtotal - discounts
            due = max(float(subtotal) - float(discount_total), 0.0)
            paid = float(payments_total or 0.0)
            owed = float(total_due or 0.0)

            items.append({
                "id": inv.id,
                "label": label,
                "due": round(due, 2),
                "paid": round(paid, 2),
                "owed": round(owed, 2),
            })

        return jsonify({"success": True, "items": items})

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

@admin_bp.route("/invoice/bulk_payment", methods=["POST"])
@admin_required
def bulk_invoice_payment():
    try:
        data = request.get_json(silent=True) or {}

        user_id = int(data.get("user_id") or 0)
        invoice_ids = data.get("invoice_ids") or []
        amount = float(data.get("amount") or 0)
        method = (data.get("payment_type") or "Cash").strip()
        authorized_by = (data.get("authorized_by") or "").strip()
        allocation = (data.get("allocation") or "oldest_first").strip()

        if not user_id or not invoice_ids:
            return jsonify({"success": False, "error": "No invoices selected."}), 400
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be greater than 0."}), 400
        if not authorized_by:
            return jsonify({"success": False, "error": "Authorized By is required."}), 400

        ids = []
        for x in invoice_ids:
            try:
                ids.append(int(x))
            except Exception:
                pass
        ids = list(dict.fromkeys(ids))

        invoices = []
        for inv_id in ids:
            inv = Invoice.query.get(inv_id)
            if not inv:
                continue

            ok = False
            if hasattr(inv, "user_id") and inv.user_id == user_id:
                ok = True
            if hasattr(inv, "customer_id") and inv.customer_id == user_id:
                ok = True

            if ok:
                invoices.append(inv)

        if not invoices:
            return jsonify({"success": False, "error": "No valid invoices found for this user."}), 400

        inv_rows = []
        old_state = {}

        for inv in invoices:
            subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)
            owed = float(total_due or 0.0)

            old_state[inv.id] = {
                "status": inv.status or "unpaid",
                "amount_due": float(getattr(inv, "amount_due", 0) or owed or 0),
                "paid_sum": float(payments_total or 0),
                "owed": owed,
                "invoice_number": inv.invoice_number or f"Invoice #{inv.id}",
            }

            inv_rows.append((inv, owed))

        def inv_date(inv):
            return (
                getattr(inv, "date", None)
                or getattr(inv, "date_submitted", None)
                or getattr(inv, "created_at", None)
                or getattr(inv, "id", 0)
            )

        if allocation == "newest_first":
            inv_rows.sort(key=lambda t: inv_date(t[0]), reverse=True)
        elif allocation == "highest_owed_first":
            inv_rows.sort(key=lambda t: t[1], reverse=True)
        else:
            inv_rows.sort(key=lambda t: inv_date(t[0]))

        remaining = float(amount)
        created = []
        now = datetime.now(timezone.utc)

        for inv, owed in inv_rows:
            if remaining <= 0:
                break
            if owed <= 0:
                continue

            pay_amt = min(remaining, owed)
            notes = f"Authorized by: {authorized_by}" if authorized_by else None

            p = Payment(
                invoice_id=inv.id,
                user_id=getattr(inv, "user_id", user_id) or user_id,
                method=method,
                amount_jmd=float(pay_amt),
                notes=notes,
                transaction_type="invoice_payment",
                status="completed",
                source="admin",
                authorized_by_admin_id=current_user.id,
                created_at=now,
            )

            db.session.add(p)

            created.append((inv.id, float(pay_amt)))
            remaining -= float(pay_amt)

        updated = []

        for inv, _ in inv_rows:
            subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)

            prev_status = (inv.status or "").lower()
            inv.amount_due = float(total_due)

            if inv.amount_due <= 0:
                inv.status = "paid"
                inv.date_paid = now

                if prev_status != "paid":
                    lock_delivered_packages_for_invoice(
                        inv.id,
                        reason="Invoice fully paid",
                        actor_admin_id=current_user.id
                    )

            elif float(payments_total or 0) > 0:
                inv.status = "partial"
                inv.date_paid = None

            else:
                inv.status = "unpaid"
                inv.date_paid = None

            applied_to_invoice = 0.0
            for created_inv_id, created_amount in created:
                if created_inv_id == inv.id:
                    applied_to_invoice += float(created_amount or 0)

            if applied_to_invoice > 0:
                old = old_state.get(inv.id, {})

                db.session.add(AuditLog(
                    module="Finance",
                    action="Bulk Invoice Payment Applied",
                    admin_id=current_user.id,
                    user_id=getattr(inv, "user_id", user_id) or user_id,
                    entity_type="Invoice",
                    entity_id=inv.id,
                    reason="Bulk invoice payment",
                    description=(
                        f"Bulk payment of JMD {applied_to_invoice:,.2f} applied to "
                        f"{inv.invoice_number or ('Invoice #' + str(inv.id))}. "
                        f"Method: {method}. Authorized by: {authorized_by}. "
                        f"Allocation: {allocation}."
                    ),
                    old_value=(
                        f"Status: {old.get('status', 'unpaid')}; "
                        f"Amount Due: JMD {float(old.get('amount_due', 0)):,.2f}; "
                        f"Paid Sum: JMD {float(old.get('paid_sum', 0)):,.2f}"
                    ),
                    new_value=(
                        f"Status: {inv.status}; "
                        f"Amount Due: JMD {float(inv.amount_due or 0):,.2f}; "
                        f"Total Paid: JMD {float(payments_total or 0):,.2f}"
                    ),
                ))

            updated.append({
                "invoice_id": inv.id,
                "amount_due": float(inv.amount_due),
                "status": inv.status,
                "paid_sum": float(payments_total or 0),
            })

        db.session.commit()

        return jsonify({
            "success": True,
            "applied_total": float(amount - remaining),
            "unused_amount": float(max(remaining, 0.0)),
            "created_payments": created,
            "updated": updated,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

@admin_bp.route('/generate-pdf-invoice/<int:user_id>')
@admin_required
def generate_pdf_invoice(user_id):
    user = User.query.get_or_404(user_id)

    # last 20 invoices for this user
    invoices = (Invoice.query
                .filter_by(user_id=user.id)
                .order_by(Invoice.id.desc())
                .limit(20).all())

    items, total = [], 0.0

    for inv in invoices:
        amount = float(
            (getattr(inv, "grand_total", None) if getattr(inv, "grand_total", None) is not None
             else getattr(inv, "amount_due", 0)) or 0
        )

        label = getattr(inv, "invoice_number", None) or f"INV{int(inv.id):05d}"
        desc  = getattr(inv, "description", None) or "Invoice"

        items.append({
            "invoice_id": label,
            "description": f"{desc} ({getattr(inv, 'status', 'unpaid')})",
            "total": round(amount, 2)
        })
        total += amount

    today = datetime.utcnow().strftime('%B %d, %Y')

    html = render_template(
        "invoice.html",
        full_name=getattr(user, "full_name", ""),
        registration_number=getattr(user, "registration_number", ""),
        date=today,
        items=items,
        grand_total=round(total, 2)
    )

    pdf = HTML(string=html).write_pdf()
    resp = make_response(pdf)
    resp.headers['Content-Type'] = 'application/pdf'

    rn = getattr(user, "registration_number", user.id)
    resp.headers['Content-Disposition'] = f'inline; filename=invoice_{rn}.pdf'
    return resp



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

    created = 0
    for uid in user_ids:
        now = datetime.utcnow()

        inv = Invoice(
            user_id=int(uid),
            invoice_number = _generate_invoice_number() if "_generate_invoice_number" in globals() else f"INV-{now.strftime('%Y%m%d%H%M%S')}",
            status="pending",                 # keep consistent lowercase if you want
            date_submitted=now,
            date_issued=now,                  # ✅ THIS FIXES THE HEADER DATE
            created_at=now,                   # ✅ if your model expects it
            amount=0,
            grand_total=0,
            amount_due=0,
        )
        db.session.add(inv)
        created += 1
    db.session.commit()

    flash(f"✅ {created} invoices successfully generated!", "success")
    return redirect(url_for('logistics.shipment_log', shipment_id=shipment_id))



@admin_bp.route(
    "/invoices/<int:invoice_id>/pdf",
    methods=["GET"],
)
@admin_required
def invoice_pdf(invoice_id):
    invoice = Invoice.query.get_or_404(
        invoice_id
    )

    # This shared builder uses saved subscription-protected
    # package charges.
    invoice_dict = _build_invoice_view_dict(
        invoice
    )

    (
        subtotal,
        discount_total,
        payments_total,
        total_due,
    ) = fetch_invoice_totals_pg(invoice_id)

    invoice_status = (
        invoice.status or ""
    ).strip().lower()

    if invoice_status == "paid":
        total_due = 0.0

    invoice_dict.update(
        {
            "id": invoice.id,
            "number": (
                invoice.invoice_number
                or f"INV{invoice.id:05d}"
            ),
            "invoice_number": (
                invoice.invoice_number
                or f"INV{invoice.id:05d}"
            ),
            "date": (
                invoice.date_issued
                or invoice.date_submitted
                or invoice.created_at
                or datetime.utcnow()
            ),
            "date_issued": (
                invoice.date_issued
                or invoice.date_submitted
                or invoice.created_at
                or datetime.utcnow()
            ),
            "subtotal": float(
                subtotal or 0
            ),
            "grand_total": float(
                subtotal or 0
            ),
            "discount_total": float(
                discount_total or 0
            ),
            "payments_total": float(
                payments_total or 0
            ),
            "total_due": float(
                total_due or 0
            ),
            "amount_due": float(
                total_due or 0
            ),
        }
    )

    try:
        relative_path = generate_invoice_pdf(
            invoice_dict
        )

    except Exception as error:
        current_app.logger.exception(
            "Generating PDF for invoice %s failed",
            invoice_id,
        )

        flash(
            f"Invoice PDF could not be generated: {error}",
            "danger",
        )

        return redirect(
            url_for(
                "admin.view_invoice",
                invoice_id=invoice.id,
            )
        )

    return redirect(
        url_for(
            "static",
            filename=relative_path,
        )
    )


# ---------- VIEW (Image 1 style) ----------
@admin_bp.route(
    "/invoices/<int:invoice_id>",
    methods=["GET"],
    endpoint="view_invoice",
)
@admin_required
def view_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(
        invoice_id
    )

    user = getattr(invoice, "user", None)

    packages = []

    package_rows = (
        Package.query
        .filter_by(invoice_id=invoice_id)
        .order_by(Package.created_at.asc())
        .all()
    )

    for package in package_rows:
        description = (
            getattr(package, "category", None)
            or package.description
            or "Miscellaneous"
        )

        weight = float(
            getattr(package, "weight", 0)
            or 0
        )

        declared_value = float(
            getattr(
                package,
                "declared_value",
                None,
            )
            or getattr(package, "value", None)
            or getattr(package, "value_usd", None)
            or getattr(
                package,
                "invoice_value",
                None,
            )
            or 0
        )

        freight = float(
            getattr(
                package,
                "freight_fee",
                getattr(package, "freight", 0),
            )
            or 0
        )

        handling = float(
            getattr(
                package,
                "handling_fee",
                getattr(
                    package,
                    "storage_fee",
                    getattr(package, "handling", 0),
                ),
            )
            or 0
        )

        saved_amount_due = getattr(
            package,
            "amount_due",
            None,
        )

        if saved_amount_due is not None:
            package_due = float(
                saved_amount_due or 0
            )
        else:
            package_due = float(
                getattr(
                    package,
                    "grand_total",
                    0,
                )
                or 0
            )

        subscription_result = (
            getattr(
                package,
                "subscription_result",
                None,
            )
        )

        subscription_covered = bool(
            getattr(
                package,
                "subscription_applied",
                False,
            )
            and (
                subscription_result or ""
            )
            == "subscription_applied"
        )

        customs_total = float(
            getattr(
                package,
                "customs_total",
                0,
            )
            or 0
        )

        packages.append(
            {
                "id": package.id,
                "house_awb": (
                    package.house_awb or ""
                ),
                "description": description,
                "tracking_number": (
                    getattr(
                        package,
                        "tracking_number",
                        "",
                    )
                    or ""
                ),
                "weight": weight,
                "value": declared_value,
                "value_usd": declared_value,

                "freight": freight,
                "freight_fee": freight,

                "handling": handling,
                "handling_fee": handling,
                "storage": handling,
                "storage_fee": handling,

                "duty": float(
                    getattr(package, "duty", 0)
                    or 0
                ),
                "scf": float(
                    getattr(package, "scf", 0)
                    or 0
                ),
                "envl": float(
                    getattr(package, "envl", 0)
                    or 0
                ),
                "caf": float(
                    getattr(package, "caf", 0)
                    or 0
                ),
                "gct": float(
                    getattr(package, "gct", 0)
                    or 0
                ),
                "stamp": float(
                    getattr(package, "stamp", 0)
                    or 0
                ),

                "customs_total": customs_total,

                "other_charges": float(
                    getattr(
                        package,
                        "other_charges",
                        0,
                    )
                    or 0
                ),

                "bad_address_fee": float(
                    getattr(
                        package,
                        "bad_address_fee",
                        0,
                    )
                    or 0
                ),

                "discount_due": float(
                    getattr(
                        package,
                        "discount_due",
                        0,
                    )
                    or 0
                ),

                "amount_due": package_due,

                "grand_total": float(
                    getattr(
                        package,
                        "grand_total",
                        package_due,
                    )
                    or 0
                ),

                "subscription_applied": bool(
                    getattr(
                        package,
                        "subscription_applied",
                        False,
                    )
                ),

                "subscription_result": (
                    subscription_result
                ),

                "subscription_covered": (
                    subscription_covered
                ),

                "customs_only_due_to_subscription": bool(
                    subscription_covered
                    and customs_total > 0
                ),
            }
        )

    # -------------------------------------------------
    # Shop For Me invoices may have no Package records.
    # Keep shop_total here.
    # -------------------------------------------------
    if (
        not packages
        and (
            invoice.invoice_number or ""
        ).startswith("SHOP-")
    ):
        shop_total = float(
            invoice.grand_total
            or invoice.amount_due
            or 0
        )

        packages.append(
            {
                "id": None,
                "house_awb": (
                    invoice.invoice_number
                ),
                "description": (
                    invoice.description
                    or "Shop For Me Quote"
                ),
                "tracking_number": "",
                "weight": 0,
                "value": 0,
                "value_usd": 0,

                "freight": 0,
                "freight_fee": 0,
                "handling": 0,
                "handling_fee": 0,
                "storage": 0,
                "storage_fee": 0,

                "duty": 0,
                "scf": 0,
                "envl": 0,
                "caf": 0,
                "gct": 0,
                "stamp": 0,
                "customs_total": 0,

                "other_charges": 0,
                "bad_address_fee": 0,
                "discount_due": 0,

                "amount_due": shop_total,
                "grand_total": shop_total,

                "subscription_applied": False,
                "subscription_result": None,
                "subscription_covered": False,
                "customs_only_due_to_subscription": False,
            }
        )

    preview_subtotal = sum(
        float(
            package.get(
                "amount_due",
                0,
            )
            or 0
        )
        for package in packages
    )

    (
        database_subtotal,
        database_discount_total,
        database_payments_total,
        database_total_due,
    ) = fetch_invoice_totals_pg(
        invoice_id
    )

    preview_discount_total = float(
        getattr(
            invoice,
            "discount_total",
            None,
        )
        if getattr(
            invoice,
            "discount_total",
            None,
        )
        not in (None, "")
        else (
            database_discount_total
            or 0
        )
    )

    preview_payments_total = float(
        database_payments_total or 0
    )

    invoice_status = (
        invoice.status or ""
    ).strip().lower()

    if invoice_status == "paid":
        preview_balance_due = 0.0
    else:
        preview_balance_due = max(
            preview_subtotal
            - preview_discount_total
            - preview_payments_total,
            0.0,
        )

    invoice_number = (
        invoice.invoice_number
        or f"INV{invoice.id:05d}"
    )

    invoice_date = (
        invoice.date_issued
        or invoice.date_submitted
        or invoice.created_at
        or datetime.utcnow()
    )

    invoice_dict = {
        "id": invoice.id,
        "user_id": invoice.user_id,
        "user": user,

        "invoice_number": invoice_number,
        "number": invoice_number,

        "grand_total": float(
            invoice.grand_total
            or preview_subtotal
            or database_subtotal
            or 0
        ),

        "date": invoice_date,
        "date_issued": invoice_date,

        "customer_code": (
            getattr(
                user,
                "registration_number",
                "",
            )
            if user
            else ""
        ),

        "customer_name": (
            getattr(user, "full_name", "")
            if user
            else ""
        ),

        "subtotal": float(
            database_subtotal
            or preview_subtotal
            or 0
        ),

        "discount_total": (
            preview_discount_total
        ),

        "payments_total": (
            preview_payments_total
        ),

        "total_due": (
            preview_balance_due
        ),

        "amount_due": (
            preview_balance_due
        ),

        "description": (
            getattr(
                invoice,
                "description",
                "",
            )
            or ""
        ),

        "packages": packages,

        "preview_subtotal": float(
            preview_subtotal
        ),

        "preview_discount_total": float(
            preview_discount_total
        ),

        "preview_payments_total": float(
            preview_payments_total
        ),

        "preview_total_due": float(
            preview_balance_due
        ),
    }

    # -------------------------------------------------
    # Authorized signers
    # -------------------------------------------------
    authorized_signers = []

    try:
        from app.models import Settings

        settings = db.session.get(Settings, 1)

        if (
            settings
            and getattr(
                settings,
                "authorized_signers",
                None,
            )
        ):
            authorized_signers = [
                signer.strip()
                for signer in (
                    settings.authorized_signers
                    or ""
                ).split(",")
                if signer.strip()
            ]

    except Exception:
        current_app.logger.exception(
            "Loading authorized invoice signers failed"
        )
        authorized_signers = []

    if not authorized_signers:
        try:
            authorized_signers = [
                user_row.full_name
                or user_row.email
                for user_row in (
                    User.query
                    .filter_by(is_admin=True)
                    .order_by(
                        User.full_name.asc()
                    )
                    .all()
                )
            ]

        except Exception:
            authorized_signers = []

    if not authorized_signers:
        authorized_signers = [
            getattr(
                current_user,
                "full_name",
                "Admin",
            )
            or "Admin"
        ]

    is_inline = bool(
        request.headers.get(
            "X-Requested-With"
        )
        == "XMLHttpRequest"
        or request.args.get("inline") == "1"
    )

    template_name = (
        "admin/invoices/_invoice_inline.html"
        if is_inline
        else "admin/invoice_view.html"
    )

    return render_template(
        template_name,
        invoice=invoice_dict,
        USD_TO_JMD=USD_TO_JMD,
        authorized_signers=authorized_signers,
    )  


# ---------- BREAKDOWN (Lightning icon) ----------
@admin_bp.route("/invoice/breakdown/<int:package_id>", methods=["GET"])
@admin_required
def invoice_breakdown(package_id):
    p = Package.query.get_or_404(package_id)

    desc = (p.description or getattr(p, "category", None) or "Miscellaneous")
    weight = float(getattr(p, "weight", 0) or 0)
    value = float(getattr(p, "value", 0) or getattr(p, "value_usd", 0) or 0)

    is_subscription_covered = (
        bool(getattr(p, "subscription_applied", False))
        and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
    )

    if is_subscription_covered:
        freight_val = 0.0
        handling_val = 0.0
        bad_address_val = 500.0 if bool(getattr(p, "epc", False) or getattr(p, "bad_address", False)) else 0.0
        other_val = float(getattr(p, "other_charges", 0) or 0)

        if value <= 100:
            duty_val = 0.0
            gct_val = 0.0
            scf_val = 0.0
            envl_val = 0.0
            caf_val = 0.0
            stamp_val = 0.0
        else:
            duty_val = float(getattr(p, "duty", 0) or 0)
            gct_val = float(getattr(p, "gct", 0) or 0)
            scf_val = float(getattr(p, "scf", 0) or 0)
            envl_val = float(getattr(p, "envl", 0) or 0)
            caf_val = float(getattr(p, "caf", 0) or 0)
            stamp_val = float(getattr(p, "stamp", 0) or 0)

        customs_total_val = duty_val + gct_val + scf_val + envl_val + caf_val + stamp_val
        freight_total_val = (
            bad_address_val
            + other_val
        )

    else:
        ch = calculate_charges(desc, value, weight)

        freight_db = float(getattr(p, "freight_fee", getattr(p, "freight", 0)) or 0)
        handling_db = float(
            getattr(p, "handling_fee", getattr(p, "storage_fee", getattr(p, "handling", 0))) or 0
        )
        # Bad address/EPC fee
        is_epc = bool(getattr(p, "epc", False))
        bad_address_db = float(getattr(p, "bad_address_fee", 0) or 0)

        if is_epc and bad_address_db <= 0:
            bad_address_db = 500.0
        other_db = float(getattr(p, "other_charges", 0) or 0)

        freight_val = freight_db if freight_db > 0 else float(ch.get("freight", 0) or 0)
        handling_val = handling_db if handling_db > 0 else float(ch.get("handling", 0) or 0)
        bad_address_val = bad_address_db
        other_val = other_db if other_db > 0 else float(ch.get("other_charges", 0) or 0)

        duty_val = float(getattr(p, "duty", 0) or ch.get("duty", 0) or 0)
        gct_val = float(getattr(p, "gct", 0) or ch.get("gct", 0) or 0)
        scf_val = float(getattr(p, "scf", 0) or ch.get("scf", 0) or 0)
        envl_val = float(getattr(p, "envl", 0) or ch.get("envl", 0) or 0)
        caf_val = float(getattr(p, "caf", 0) or ch.get("caf", 0) or 0)
        stamp_val = float(getattr(p, "stamp", 0) or ch.get("stamp", 0) or 0)

        customs_total_val = float(ch.get("customs_total", 0) or 0)
        freight_total_val = freight_val + handling_val + bad_address_val + other_val

    discount_due_val = float(
        getattr(p, "discount_due", 0)
        or 0
    )

    if is_subscription_covered:
        discount_due_val = 0.0

    # The exhausted subscription discount applies only
    # to freight and handling.
    discount_due_val = min(
        max(discount_due_val, 0.0),
        freight_val + handling_val,
    )

    freight_total_val = max(
        freight_val
        + handling_val
        + bad_address_val
        + other_val
        - discount_due_val,
        0.0,
    )

    grand_total_val = max(
        freight_val
        + handling_val
        + bad_address_val
        + other_val
        + duty_val
        + gct_val
        + scf_val
        + envl_val
        + caf_val
        + stamp_val
        - discount_due_val,
        0.0,
    )

    payload = {
        "duty": duty_val,
        "gct": gct_val,
        "freight": freight_val,
        "handling": handling_val,
        "scf": scf_val,
        "envl": envl_val,
        "caf": caf_val,
        "stamp": stamp_val,
        "bad_address": bool(getattr(p, "bad_address", False) or getattr(p, "epc", False) or bad_address_val > 0),
        "bad_address_fee": bad_address_val,
        "other_charges": other_val,
        "discount_due": discount_due_val,
        "grand_total": grand_total_val,
        "customs_total": customs_total_val,
        "freight_total": freight_total_val,
        "category": desc,
        "weight": weight,
        "value": value,
        "subscription_applied": bool(getattr(p, "subscription_applied", False)),
        "subscription_result": getattr(p, "subscription_result", None),
    }

    return jsonify(payload)

@admin_bp.route(
    "/invoice/<int:invoice_id>/delete",
    methods=["POST"],
)
@admin_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    # Save these values before deleting the invoice.
    user_id = invoice.user_id
    invoice_number = (
        invoice.invoice_number
        or f"Invoice #{invoice.id}"
    )

    # -------------------------------------------------
    # Preserve the return page and filters
    # -------------------------------------------------
    tab = (
        request.form.get("tab")
        or "invoices"
    ).strip() or "invoices"

    inv_page = request.form.get(
        "inv_page",
        1,
        type=int,
    )

    inv_per_page = request.form.get(
        "inv_per_page",
        10,
        type=int,
    )

    inv_from = (
        request.form.get("inv_from")
        or ""
    ).strip()

    inv_to = (
        request.form.get("inv_to")
        or ""
    ).strip()

    def back():
        kwargs = {
            "id": user_id,
            "tab": tab,
            "inv_page": inv_page,
            "inv_per_page": inv_per_page,
        }

        if inv_from:
            kwargs["inv_from"] = inv_from

        if inv_to:
            kwargs["inv_to"] = inv_to

        return redirect(
            url_for(
                "accounts_profiles.view_user",
                **kwargs,
            )
        )

    def error_response(message, status_code=400):
        if (
            request.headers.get(
                "X-Requested-With"
            )
            == "XMLHttpRequest"
        ):
            return jsonify(
                success=False,
                error=message,
            ), status_code

        flash(message, "danger")
        return back()

    invoice_status = (
        invoice.status
        or ""
    ).strip().lower()

    # -------------------------------------------------
    # Never delete an invoice marked paid
    # -------------------------------------------------
    if invoice_status == "paid":
        return error_response(
            "This invoice is paid and cannot be deleted."
        )

    # -------------------------------------------------
    # Never delete an invoice with completed payments
    # This also protects partially paid invoices.
    # -------------------------------------------------
    completed_payments = (
        Payment.query
        .filter(
            Payment.invoice_id == invoice.id,
            func.lower(
                func.coalesce(
                    Payment.status,
                    "",
                )
            ).in_(
                [
                    "completed",
                    "settled",
                ]
            ),
        )
        .all()
    )

    if completed_payments:
        completed_total = sum(
            float(
                getattr(
                    payment,
                    "amount_jmd",
                    0,
                )
                or 0
            )
            for payment in completed_payments
        )

        return error_response(
            (
                "This invoice has completed payments "
                f"totalling JMD {completed_total:,.2f} "
                "and cannot be deleted."
            )
        )

    try:
        # -------------------------------------------------
        # Reverse and detach pending payment attempts
        # -------------------------------------------------
        pending_payments = (
            Payment.query
            .filter(
                Payment.invoice_id == invoice.id,
                func.lower(
                    func.coalesce(
                        Payment.status,
                        "",
                    )
                )
                == "pending",
            )
            .all()
        )

        for payment in pending_payments:
            payment.status = "reversed"
            payment.invoice_id = None

            old_notes = (
                getattr(payment, "notes", None)
                or ""
            ).strip()

            cancellation_note = (
                f"Pending payment reversed because "
                f"{invoice_number} was deleted before payment."
            )

            if old_notes:
                new_notes = (
                    f"{old_notes} | "
                    f"{cancellation_note}"
                )
            else:
                new_notes = cancellation_note

            # Payment.notes is limited to 255 characters.
            payment.notes = new_notes[:255]

        # Detach any failed/reversed records that still point
        # to the invoice so the audit record is preserved.
        noncompleted_payments = (
            Payment.query
            .filter(
                Payment.invoice_id == invoice.id,
                func.lower(
                    func.coalesce(
                        Payment.status,
                        "",
                    )
                ).notin_(
                    [
                        "completed",
                        "settled",
                    ]
                ),
            )
            .all()
        )

        for payment in noncompleted_payments:
            payment.invoice_id = None

        # -------------------------------------------------
        # Detach packages so they can be invoiced again
        # -------------------------------------------------
        packages = (
            Package.query
            .filter(
                Package.invoice_id == invoice.id
            )
            .all()
        )

        package_count = len(packages)

        for package in packages:
            package.invoice_id = None

            # An unpaid invoice must not leave the package
            # financially or operationally locked.
            if hasattr(package, "pricing_locked"):
                package.pricing_locked = False

            if hasattr(package, "pricing_locked_at"):
                package.pricing_locked_at = None

            if hasattr(package, "pricing_locked_by"):
                package.pricing_locked_by = None

            if (
                hasattr(package, "is_locked")
                and (
                    getattr(
                        package,
                        "locked_reason",
                        "",
                    )
                    or ""
                )
                .strip()
                .lower()
                in (
                    "invoice paid",
                    "invoice fully paid",
                    "pricing locked",
                )
            ):
                package.is_locked = False

                if hasattr(package, "locked_reason"):
                    package.locked_reason = None

                if hasattr(package, "locked_at"):
                    package.locked_at = None

        db.session.delete(invoice)
        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Deleting unpaid invoice %s failed",
            invoice_id,
        )

        return error_response(
            (
                "The invoice could not be deleted: "
                f"{error}"
            ),
            500,
        )

    message = (
        f"{invoice_number} was deleted. "
        f"{package_count} package(s) can now be "
        "included in a new invoice."
    )

    if (
        request.headers.get(
            "X-Requested-With"
        )
        == "XMLHttpRequest"
    ):
        return jsonify(
            success=True,
            message=message,
            packages_released=package_count,
        )

    flash(message, "success")
    return back()


@admin_bp.route("/invoice/add-payment/<int:invoice_id>", methods=["POST"])
@admin_required
def add_payment(invoice_id):
    inv = (
        Invoice.query
        .filter(Invoice.id == invoice_id)
        .with_for_update()
        .first_or_404()
    )

    # Read form fields
    try:
        amount = round(
            float(request.form.get("amount_jmd", 0) or 0),
            2
        )
    except (TypeError, ValueError):
        amount = 0.00

    method        = (request.form.get("method") or "Cash").strip()
    authorized_by = (request.form.get("authorized_by") or "").strip()
    reference     = (request.form.get("reference") or "").strip()
    extra_notes   = (request.form.get("notes") or "").strip()

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if amount <= 0:
        msg = "Payment amount must be greater than 0."
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)
    current_due = round(
        max(float(total_due or 0), 0.0),
        2
    )

    if current_due <= 0:
        msg = "This invoice is already fully paid."
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    if amount > current_due:
        msg = f"Payment cannot exceed balance due of JMD {current_due:,.2f}."
        if is_ajax:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    # Build notes safely
    notes_parts = []
    if extra_notes:
        notes_parts.append(extra_notes)
    if authorized_by:
        notes_parts.append(f"Authorised by: {authorized_by}")
    notes = "\n".join(notes_parts) if notes_parts else None

    payment_kwargs = {
        "invoice_id": inv.id,
        "user_id": inv.user_id,
        "transaction_type": "invoice_payment",
        "status": "completed",
        "source": "admin",
        "authorized_by_admin_id": current_user.id,
    }

    # amount field name
    if hasattr(Payment, "amount_jmd"):
        payment_kwargs["amount_jmd"] = amount
    else:
        payment_kwargs["amount"] = amount

    # method field name
    if hasattr(Payment, "method"):
        payment_kwargs["method"] = method
    else:
        payment_kwargs["payment_type"] = method

    # optional scalar fields only
    if hasattr(Payment, "reference"):
        payment_kwargs["reference"] = reference or None

    if hasattr(Payment, "notes"):
        payment_kwargs["notes"] = notes

    if hasattr(Payment, "payment_date"):
        payment_kwargs["payment_date"] = datetime.now(timezone.utc)

    if hasattr(Payment, "bill_number"):
        payment_kwargs["bill_number"] = (
            f"BILL-{inv.id}-"
            f"{to_jamaica(datetime.now(timezone.utc)).strftime('%Y%m%d%H%M%S')}"
        )

    # ---------------------------------
    # Prevent duplicate payment submit
    # ---------------------------------
    recent_payment = (
        Payment.query
        .filter(
            Payment.invoice_id == inv.id,
            Payment.amount_jmd == amount,
            Payment.method == method
        )
        .order_by(Payment.created_at.desc())
        .first()
    )

    if recent_payment:
        recent_created_at = recent_payment.created_at

        # Database timestamps may be returned without timezone information.
        if recent_created_at.tzinfo is None:
            recent_created_at = recent_created_at.replace(
                tzinfo=timezone.utc
            )

        time_diff = (
            datetime.now(timezone.utc) - recent_created_at
        ).total_seconds()

        # If the same payment was added in the last 10 seconds, block it
        if time_diff < 10:
            msg = "Duplicate payment detected. Please refresh."

            if is_ajax:
                return jsonify({"ok": False, "error": msg}), 400

            flash(msg, "warning")
            return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    p = Payment(**payment_kwargs)
    db.session.add(p)
    db.session.flush()

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)
    new_due = round(
        max(float(total_due or 0), 0.0),
        2
    )
    base_total = round(
        max(float(subtotal or 0) - float(discount_total or 0), 0.0),
        2
    )
    paid_sum = round(
        float(payments_total or 0),
        2
    )

    inv.amount_due = new_due

    previous_status = inv.status
    if new_due <= 0:
        inv.status = "paid"
        if hasattr(inv, "date_paid"):
            inv.date_paid = datetime.now(timezone.utc)

        shop_request = PurchaseRequest.query.filter_by(invoice_id=inv.id).first()

        if shop_request:
            shop_request.status = "paid"

        if previous_status != "paid":
            lock_delivered_packages_for_invoice(
                inv.id,
                reason="Invoice fully paid",
                actor_admin_id=current_user.id,
            )

    elif 0 < new_due < base_total:
        inv.status = "partial"

        if hasattr(inv, "date_paid"):
            inv.date_paid = None

    else:
        inv.status = "unpaid"
        if hasattr(inv, "date_paid"):
            inv.date_paid = None
    db.session.commit()
    if is_ajax:
        return jsonify({
            "ok": True,
            "invoice_id": inv.id,
            "status": inv.status,
            "paid_sum": float(paid_sum),
            "amount_due": float(inv.amount_due),
        })

    flash(f"Payment of {amount:,.2f} JMD recorded for {inv.invoice_number}.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=inv.id))


@admin_bp.route("/invoice/add-discount/<int:invoice_id>", methods=["POST"])
@admin_required
def add_discount(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)

    previous_status = inv.status or "unpaid"
    old_grand_total = float(inv.grand_total or inv.amount or 0)
    old_amount_due = float(inv.amount_due or 0)

    amount = float(request.form.get("amount_jmd", 0) or 0)

    if amount <= 0:
        flash("Discount amount must be greater than 0.", "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    base_total_before = float(inv.grand_total or inv.amount or 0)
    base_total_after = max(base_total_before - amount, 0.0)

    inv.grand_total = base_total_after
    inv.amount = base_total_after

    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount

    paid_sum = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    new_due = max(base_total_after - paid_sum, 0.0)
    inv.amount_due = new_due

    if new_due <= 0:
        inv.status = "paid"
        inv.date_paid = datetime.utcnow()

        if previous_status != "paid":
            mark_invoice_packages_delivered(inv.id)
            for pkg in inv.packages:
                pkg.status = "delivered"

    elif 0 < new_due < base_total_after:
        inv.status = "partial"

    else:
        inv.status = "unpaid"

    db.session.add(AuditLog(
        module="Finance",
        action="Invoice Discount Added",
        admin_id=current_user.id,
        user_id=inv.user_id,
        entity_type="Invoice",
        entity_id=inv.id,
        reason="Manual invoice discount",
        description=(
            f"Discount of JMD {amount:,.2f} added to invoice "
            f"{inv.invoice_number or ('#' + str(inv.id))}. "
            f"Status changed from {previous_status} to {inv.status}."
        ),
        old_value=(
            f"Status: {previous_status}; "
            f"Grand Total: JMD {old_grand_total:,.2f}; "
            f"Amount Due: JMD {old_amount_due:,.2f}"
        ),
        new_value=(
            f"Status: {inv.status}; "
            f"Grand Total: JMD {float(inv.grand_total or 0):,.2f}; "
            f"Amount Due: JMD {float(inv.amount_due or 0):,.2f}; "
            f"Discount: JMD {amount:,.2f}"
        ),
    ))

    db.session.commit()

    flash("Discount added.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))


@admin_bp.route(
    "/proforma-invoice-modal/<int:invoice_id>",
    methods=["GET"],
    endpoint="proforma_invoice_modal",
)
@admin_required
def proforma_invoice_modal(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    user = inv.user

    pkgs = (
        Package.query
        .filter_by(invoice_id=invoice_id)
        .order_by(Package.created_at.asc())
        .all()
    )

    from app.models import Settings
    settings = Settings.query.get(1)

    effective_usd_to_jmd = (getattr(settings, "usd_to_jmd", None) or USD_TO_JMD)

    raw_logo = (settings.logo_path if settings and settings.logo_path else "logo.png") or "logo.png"
    raw_logo = raw_logo.lstrip("/")
    if raw_logo.lower().startswith("static/"):
        raw_logo = raw_logo[7:]

    logo_data_uri = _static_image_data_uri(raw_logo)
    logo_url = url_for(
        "static",
        filename=raw_logo,
        _external=True,
        _scheme="https",
    )

    items = []
    subtotal = 0.0

    for p in pkgs:
        desc = p.description or getattr(p, "category", "Miscellaneous") or "Miscellaneous"
        wt_raw = float(p.weight or 0)
        wt = math.ceil(wt_raw) if wt_raw > 0 else 0
        val = float(getattr(p, "value", 0) or getattr(p, "value_usd", 0) or 0)

        is_subscription_covered = (
            bool(getattr(p, "subscription_applied", False))
            and (getattr(p, "subscription_result", "") or "") == "subscription_applied"
        )

        if is_subscription_covered:
            freight = 0.0
            handling = 0.0
            other = float(getattr(p, "other_charges", 0) or 0)
            bad_address_fee = float(getattr(p, "bad_address_fee", 0) or 0)

            if (
                bool(getattr(p, "epc", False))
                or bool(getattr(p, "bad_address", False))
            ) and bad_address_fee <= 0:
                bad_address_fee = 500.0

            if val <= 100:
                duty = gct = scf = envl = caf = stamp = 0.0
            else:
                duty = float(getattr(p, "duty", 0) or 0)
                gct = float(getattr(p, "gct", 0) or 0)
                scf = float(getattr(p, "scf", 0) or 0)
                envl = float(getattr(p, "envl", 0) or 0)
                caf = float(getattr(p, "caf", 0) or 0)
                stamp = float(getattr(p, "stamp", 0) or 0)

        else:
            freight = float(getattr(p, "freight_fee", getattr(p, "freight", 0)) or 0)
            handling = float(
                getattr(
                    p,
                    "handling_fee",
                    getattr(
                        p,
                        "storage_fee",
                        getattr(p, "handling", 0),
                    ),
                )
                or 0
            )
            duty = float(getattr(p, "duty", 0) or 0)
            gct = float(getattr(p, "gct", 0) or 0)
            scf = float(getattr(p, "scf", 0) or 0)
            envl = float(getattr(p, "envl", 0) or 0)
            caf = float(getattr(p, "caf", 0) or 0)
            stamp = float(getattr(p, "stamp", 0) or 0)
            other = float(getattr(p, "other_charges", 0) or 0)
            bad_address_fee = float(getattr(p, "bad_address_fee", 0) or 0)

            if (
                bool(getattr(p, "epc", False))
                or bool(getattr(p, "bad_address", False))
            ) and bad_address_fee <= 0:
                bad_address_fee = 500.0

        discount_due = float(
            getattr(p, "discount_due", 0)
            or 0
        )

        # Covered packages do not use the exhausted-plan discount.
        if is_subscription_covered:
            discount_due = 0.0

        # The exhausted-plan discount may never exceed
        # freight plus handling.
        discount_due = min(
            max(discount_due, 0.0),
            freight + handling,
        )

        total_jmd = max(
            freight
            + handling
            + bad_address_fee
            + other
            + duty
            + gct
            + scf
            + envl
            + caf
            + stamp
            - discount_due,
            0.0,
        )

        subtotal += total_jmd

        items.append({
            "house_awb": p.house_awb,
            "description": desc,
            "weight": wt,
            "value": val,
            "freight": freight,
            "handling": handling,
            "storage": handling,
            "freight_fee": freight,
            "storage_fee": handling,
            "bad_address_fee": bad_address_fee,
            "duty": duty,
            "gct": gct,
            "scf": scf,
            "envl": envl,
            "caf": caf,
            "stamp": stamp,
            "other_charges": other,
            "discount_due": discount_due,
            "amount_due": total_jmd,
            "subscription_applied": bool(getattr(p, "subscription_applied", False)),
            "subscription_result": getattr(p, "subscription_result", None),
            "subscription_covered": is_subscription_covered,
            "customs_only_due_to_subscription": (
                is_subscription_covered and val > 100 and total_jmd > 0
            ),
        })

    live_subtotal = float(subtotal or 0.0)

    _db_subtotal, discount_total, payments_total, _db_total_due = fetch_invoice_totals_pg(invoice_id)

    if (
        inv.status or ""
    ).strip().lower() == "paid":
        balance_due = 0.0
    else:
        balance_due = max(
            live_subtotal
            - float(discount_total or 0)
            - float(payments_total or 0),
            0.0,
        )

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number or f"PROFORMA-{inv.id}",
        "date": inv.date_issued or inv.date_submitted or datetime.utcnow(),
        "customer_code": getattr(user, "registration_number", "") or "",
        "customer_name": getattr(user, "full_name", "") or "",
        "branch": "Main Branch",
        "staff": getattr(current_user, "full_name", "FAFL ADMIN"),
        "subtotal": live_subtotal,
        "discount_total": float(
            getattr(inv, "discount_total", None)
            if getattr(inv, "discount_total", None) not in (None, "")
            else (discount_total or 0.0)
        ),
        "payments_total": float(payments_total or 0.0),
        "total_due": balance_due,
        "notes": getattr(inv, "description", "") or "",
        "packages": items,
    }

    shop_request = PurchaseRequest.query.filter_by(invoice_id=inv.id).first()

    if shop_request:
        item_total_usd = float(shop_request.quoted_item_price_usd or 0)
        service_fee_jmd = float(shop_request.quoted_service_fee_jmd or 0)
        usd_rate = float(getattr(settings, "usd_to_jmd", 162) or 162)

        item_total_jmd = item_total_usd * usd_rate
        total_due_now = item_total_jmd + service_fee_jmd

        invoice_dict.update({
            "item_total_usd": item_total_usd,
            "item_total_jmd": item_total_jmd,
            "service_fee_jmd": service_fee_jmd,
            "total_due_now": total_due_now,
            "balance_due": balance_due,
            "usd_rate": usd_rate,
        })

        return render_template(
            "admin/invoices/_shop_for_me_invoice_core.html",
            invoice=invoice_dict,
            settings=settings,
            USD_TO_JMD=effective_usd_to_jmd,
            logo_data_uri=logo_data_uri,
            logo_url=logo_url,
        )

    return render_template(
        "admin/invoices/_invoice_core.html",
        invoice=invoice_dict,
        settings=settings,
        USD_TO_JMD=effective_usd_to_jmd,
        logo_data_uri=logo_data_uri,
        logo_url=logo_url,
    )

@admin_bp.route("/invoice/save/<int:invoice_id>", methods=["POST"])
@admin_required
def save_invoice_notes(invoice_id):
    notes = request.form.get("notes", "")
    inv = Invoice.query.get_or_404(invoice_id)
    if hasattr(inv, "notes"):
        inv.notes = notes
    else:
        inv.description = notes
    db.session.commit()
    flash("Invoice saved.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

@admin_bp.route('/invoices/<int:invoice_id>/inline', methods=['GET'], endpoint='invoice_inline')
@admin_required
def invoice_inline(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    user = inv.user if hasattr(inv, "user") else None

    is_shop_invoice = (inv.invoice_number or "").startswith("SHOP-")

    packages = [{
        "id": p.id,
        "house_awb": p.house_awb,
        "description": p.description,
        "merchant": getattr(p, "merchant", None),
        "weight": float(p.weight or 0),
        "value_usd": float(getattr(p, "value", 0) or 0),
        "amount_due": float(getattr(p, "amount_due", 0) or 0),
    } for p in Package.query.filter_by(invoice_id=invoice_id).all()]

    if is_shop_invoice and not packages:
        shop_total = float(inv.grand_total or inv.amount_due or 0)

        packages.append({
            "id": None,
            "house_awb": inv.invoice_number,
            "description": inv.description or "Shop For Me Quote",
            "merchant": "Shop For Me",
            "weight": 0,
            "value_usd": 0,
            "amount_due": shop_total,
        })

        subtotal = shop_total
        discount_total = float(getattr(inv, "discount_total", 0) or 0)

        pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
        payments_total = (
            db.session.query(func.coalesce(func.sum(pay_col), 0.0))
            .filter(Payment.invoice_id == inv.id)
            .scalar()
            or 0.0
        )

        if (inv.status or "").lower() == "paid":
            total_due = 0.0
        else:
            total_due = max(subtotal - discount_total - float(payments_total or 0), 0.0)

    else:
        subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(invoice_id)

        if (inv.status or "").lower() == "paid":
            total_due = 0.0

    invoice_dict = {
        "id": inv.id,
        "user_id": inv.user_id,

        # ✅ Send both keys so template always finds correct invoice number
        "invoice_number": inv.invoice_number,
        "number": inv.invoice_number,

        "date": inv.date_submitted or inv.created_at or datetime.utcnow(),
        "date_issued": inv.date_issued or inv.date_submitted or inv.created_at or datetime.utcnow(),

        "customer_code": getattr(user, "registration_number", "") if user else "",
        "customer_name": getattr(user, "full_name", "") if user else "",

        "subtotal": float(subtotal or 0),
        "grand_total": float(inv.grand_total or subtotal or 0),
        "discount_total": float(discount_total or 0),
        "payments_total": float(payments_total or 0),
        "total_due": float(total_due or 0),
        "amount_due": float(total_due or 0),

        # ✅ These are what _invoice_inline.html uses
        "preview_subtotal": float(subtotal or 0),
        "preview_discount_total": float(discount_total or 0),
        "preview_payments_total": float(payments_total or 0),
        "preview_total_due": float(total_due or 0),

        "packages": packages,
        "description": getattr(inv, "description", "") or "",
    }

    return render_template(
        "admin/invoices/_invoice_inline.html",
        invoice=invoice_dict,
        USD_TO_JMD=USD_TO_JMD,
    )

@admin_bp.route(
    "/invoice/<int:invoice_id>/email-proforma",
    methods=["POST"],
)
@login_required
@admin_required
def email_proforma_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    # -------------------------------------------------
    # Customer email
    # -------------------------------------------------
    user = getattr(invoice, "user", None)

    customer_email = (
        getattr(user, "email", None)
        or getattr(invoice, "customer_email", None)
    )

    if not customer_email:
        return jsonify(
            ok=False,
            error=(
                "Customer email not found for this invoice."
            ),
        ), 400

    # -------------------------------------------------
    # Packages attached to this invoice
    # -------------------------------------------------
    packages = (
        Package.query
        .filter_by(invoice_id=invoice_id)
        .order_by(Package.created_at.asc())
        .all()
    )

    if not packages:
        return jsonify(
            ok=False,
            error=(
                "This invoice does not have any packages."
            ),
        ), 400

    from app.models import Settings

    settings = Settings.query.get(1)

    effective_usd_to_jmd = (
        getattr(settings, "usd_to_jmd", None)
        or USD_TO_JMD
    )

    logo_path = (
        settings.logo_path
        if settings
        and getattr(settings, "logo_path", None)
        else "logo.png"
    )

    logo_url = url_for(
        "static",
        filename=logo_path,
        _external=True,
    )

    items = []
    live_subtotal = 0.0

    # -------------------------------------------------
    # Build invoice using protected saved package prices
    # -------------------------------------------------
    for package in packages:
        description = (
            getattr(package, "category", None)
            or package.description
            or "Miscellaneous"
        )

        weight = math.ceil(
            float(
                getattr(package, "weight", 0)
                or 0
            )
        )

        value = float(
            getattr(
                package,
                "declared_value",
                None,
            )
            or getattr(package, "value", 0)
            or getattr(package, "value_usd", 0)
            or 0
        )

        subscription_covered = bool(
            getattr(
                package,
                "subscription_applied",
                False,
            )
            and (
                getattr(
                    package,
                    "subscription_result",
                    "",
                )
                or ""
            )
            == "subscription_applied"
        )

        # ---------------------------------------------
        # Saved customs charges
        # ---------------------------------------------
        duty = float(
            getattr(package, "duty", 0)
            or 0
        )

        gct = float(
            getattr(package, "gct", 0)
            or 0
        )

        scf = float(
            getattr(package, "scf", 0)
            or 0
        )

        envl = float(
            getattr(package, "envl", 0)
            or 0
        )

        caf = float(
            getattr(package, "caf", 0)
            or 0
        )

        stamp = float(
            getattr(package, "stamp", 0)
            or 0
        )

        # ---------------------------------------------
        # Saved freight and handling charges
        # ---------------------------------------------
        freight = float(
            getattr(
                package,
                "freight_fee",
                getattr(package, "freight", 0),
            )
            or 0
        )

        handling = float(
            getattr(
                package,
                "handling_fee",
                getattr(
                    package,
                    "storage_fee",
                    getattr(package, "handling", 0),
                ),
            )
            or 0
        )

        other_charges = float(
            getattr(
                package,
                "other_charges",
                0,
            )
            or 0
        )

        bad_address_fee = float(
            getattr(
                package,
                "bad_address_fee",
                0,
            )
            or 0
        )

        # EPC/bad-address fee still applies.
        has_bad_address = bool(
            getattr(package, "epc", False)
            or getattr(
                package,
                "bad_address",
                False,
            )
        )

        if has_bad_address and bad_address_fee <= 0:
            bad_address_fee = 500.0

        discount_due = float(
            getattr(
                package,
                "discount_due",
                0,
            )
            or 0
        )

        # ---------------------------------------------
        # Enforce subscription pricing
        # ---------------------------------------------
        if subscription_covered:
            # Covered packages never pay freight/handling.
            freight = 0.0
            handling = 0.0

            # A previous exhausted-plan discount must not remain.
            discount_due = 0.0

            # US$100 or less also receives no customs charges.
            if value <= 100:
                duty = 0.0
                gct = 0.0
                scf = 0.0
                envl = 0.0
                caf = 0.0
                stamp = 0.0

        customs_total = (
            duty
            + gct
            + scf
            + envl
            + caf
            + stamp
        )

        # Discount_due contains the exhausted-plan discount.
        # It applies only to freight and handling.
        maximum_discount = (
            freight
            + handling
        )

        discount_due = min(
            max(discount_due, 0.0),
            maximum_discount,
        )

        line_total = max(
            customs_total
            + freight
            + handling
            + other_charges
            + bad_address_fee
            - discount_due,
            0.0,
        )

        live_subtotal += line_total

        items.append(
            {
                "id": package.id,
                "house_awb": (
                    package.house_awb or ""
                ),
                "tracking_number": (
                    getattr(
                        package,
                        "tracking_number",
                        "",
                    )
                    or ""
                ),
                "description": description,
                "weight": weight,
                "value": value,
                "value_usd": value,

                "freight": freight,
                "freight_fee": freight,

                "handling": handling,
                "storage": handling,
                "handling_fee": handling,
                "storage_fee": handling,

                "duty": duty,
                "gct": gct,
                "scf": scf,
                "envl": envl,
                "caf": caf,
                "stamp": stamp,

                "customs_total": customs_total,
                "bad_address": has_bad_address,
                "bad_address_fee": bad_address_fee,
                "other_charges": other_charges,
                "discount_due": discount_due,

                "grand_total": line_total,
                "amount_due": line_total,

                "subscription_applied": bool(
                    getattr(
                        package,
                        "subscription_applied",
                        False,
                    )
                ),
                "subscription_result": getattr(
                    package,
                    "subscription_result",
                    None,
                ),
                "subscription_covered": (
                    subscription_covered
                ),
                "customs_only_due_to_subscription": (
                    subscription_covered
                    and value > 100
                    and customs_total > 0
                ),
            }
        )

    # -------------------------------------------------
    # Invoice discounts, payments and balance
    # -------------------------------------------------
    (
        _database_subtotal,
        discount_total,
        payments_total,
        _database_total_due,
    ) = fetch_invoice_totals_pg(invoice_id)

    discount_total = float(
        discount_total or 0
    )

    payments_total = float(
        payments_total or 0
    )

    invoice_status = (
        invoice.status or ""
    ).strip().lower()

    if invoice_status == "paid":
        balance_due = 0.0
    else:
        balance_due = max(
            live_subtotal
            - discount_total
            - payments_total,
            0.0,
        )

    invoice_number = (
        invoice.invoice_number
        or f"INV{invoice.id:05d}"
    )

    invoice_dict = {
        "id": invoice.id,
        "user_id": invoice.user_id,
        "number": invoice_number,
        "invoice_number": invoice_number,

        "date": (
            invoice.date_issued
            or invoice.date_submitted
            or invoice.created_at
            or datetime.utcnow()
        ),

        "date_issued": (
            invoice.date_issued
            or invoice.date_submitted
            or invoice.created_at
            or datetime.utcnow()
        ),

        "customer_code": (
            getattr(
                user,
                "registration_number",
                "",
            )
            if user
            else ""
        ),

        "customer_name": (
            getattr(user, "full_name", "")
            if user
            else ""
        ),

        "branch": "Main Branch",
        "staff": getattr(
            current_user,
            "full_name",
            "FAFL ADMIN",
        ),

        "subtotal": live_subtotal,
        "grand_total": live_subtotal,
        "discount_total": discount_total,
        "payments_total": payments_total,
        "total_due": balance_due,
        "amount_due": balance_due,

        "notes": (
            getattr(invoice, "description", "")
            or ""
        ),

        "packages": items,
    }

    # -------------------------------------------------
    # Render invoice HTML
    # -------------------------------------------------
    try:
        html = render_template(
            "admin/invoices/_invoice_core.html",
            invoice=invoice_dict,
            settings=settings,
            USD_TO_JMD=effective_usd_to_jmd,
            logo_url=logo_url,
        )
    except Exception as error:
        current_app.logger.exception(
            "Rendering proforma invoice %s failed",
            invoice_id,
        )

        return jsonify(
            ok=False,
            error=(
                "The invoice could not be rendered: "
                f"{error}"
            ),
        ), 500

    # -------------------------------------------------
    # Generate PDF
    # -------------------------------------------------
    try:
        pdf_bytes = HTML(
            string=html,
            base_url=request.url_root,
        ).write_pdf()

    except Exception as error:
        current_app.logger.exception(
            "PDF generation failed for invoice %s",
            invoice_id,
        )

        return jsonify(
            ok=False,
            error=(
                "The invoice PDF could not be generated: "
                f"{error}"
            ),
        ), 500

    filename = (
        f"Proforma_{invoice_number}.pdf"
    )

    # -------------------------------------------------
    # Email content
    # -------------------------------------------------
    full_name = (
        invoice_dict["customer_name"]
        or "Customer"
    )

    subject = (
        f"Proforma Invoice {invoice_number} "
        "- Foreign A Foot Logistics"
    )

    plain_body = (
        f"Hi {full_name},\n\n"
        f"Please find your Proforma Invoice "
        f"{invoice_number} attached.\n\n"
        f"Balance Due: JMD {balance_due:,.2f}\n\n"
        f"If you have any questions, simply reply "
        f"to this email.\n\n"
        f"— Foreign A Foot Logistics\n"
        f"(876) 560-7764\n"
    )

    html_body = f"""
    <div style="font-family:Arial,sans-serif; line-height:1.6;">
      <p>Hi <b>{full_name}</b>,</p>

      <p>
        Your <b>Proforma Invoice {invoice_number}</b>
        is attached.
      </p>

      <p>
        <b>Balance Due:</b>
        JMD {balance_due:,.2f}
      </p>

      <p>
        If you have any questions, simply reply
        to this email.
      </p>

      <p style="margin-top:16px;">
        — Foreign A Foot Logistics<br>
        (876) 560-7764
      </p>
    </div>
    """

    # -------------------------------------------------
    # Send email
    # -------------------------------------------------
    try:
        email_sent = email_utils.send_email(
            to_email=customer_email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            attachments=[
                (
                    pdf_bytes,
                    filename,
                    "application/pdf",
                )
            ],
            recipient_user_id=invoice.user_id,
        )

        if email_sent is False:
            return jsonify(
                ok=False,
                error=(
                    "The email service did not confirm "
                    "that the invoice was sent."
                ),
            ), 500

    except Exception as error:
        current_app.logger.exception(
            "Emailing proforma invoice %s failed",
            invoice_id,
        )

        return jsonify(
            ok=False,
            error=(
                "The invoice email could not be sent: "
                f"{error}"
            ),
        ), 500

    return jsonify(
        ok=True,
        sent_to=customer_email,
        filename=filename,
        balance_due=balance_due,
    )

@admin_bp.route("/invoice/receipt/<int:invoice_id>")
@admin_required
def invoice_receipt(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    user = inv.user if hasattr(inv, "user") else None

    rows = Package.query.filter_by(invoice_id=invoice_id).all()

    def _money(n):
        try: return f"{float(n):,.2f}"
        except Exception: return "0.00"

    table_data = [["#", "Tracking", "House AWB", "Description", "Weight (lb)", "Item (USD)", "Freight", "Other", "Amount Due"]]
    for i, r in enumerate(rows, start=1):
        table_data.append([
            i,
            getattr(r, "tracking_number", getattr(r, "tracking", None)) or "—",
            r.house_awb or "—",
            r.description or "—",
            f"{float(r.weight or 0):.2f}",
            f"{float(getattr(r, 'value', 0) or 0):.2f}",
            _money(getattr(r, "freight_fee", getattr(r, "freight", 0)) or 0),
            _money(getattr(r, "other_charges", 0) or 0),
            _money(getattr(r, "amount_due", 0) or 0),
        ])

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(invoice_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    flow = []
    flow.append(Paragraph("<b>FOREIGN A FOOT LOGISTICS</b>", styles['Title']))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("12 Port Lane<br/>Kingston, JM<br/>(876) 955-0123<br/>accounts@foreignafoot.com", styles['Normal']))
    flow.append(Spacer(1, 10))

    meta = [
        ["Invoice #", inv.invoice_number or f"INV-{invoice_id}"],
        ["Date", (inv.date_submitted or datetime.utcnow()).strftime("%b %d, %Y")],
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
        f"{getattr(user, 'full_name', '')}<br/>{getattr(user, 'registration_number', '')}<br/>{getattr(user, 'email', '')}<br/>{getattr(user, 'mobile', '')}",
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
        ["Total Due", _money(total_due)],
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
                     download_name=f"receipt_{inv.invoice_number or invoice_id}.pdf",
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

    form = WalletUpdateForm(obj=wallet)
    if form.validate_on_submit():
        old_balance = wallet.ewallet_balance
        new_balance = form.ewallet_balance.data
        wallet.ewallet_balance = new_balance

        diff = (new_balance or 0) - (old_balance or 0)
        if diff != 0:
            db.session.add(WalletTransaction(
                user_id=user.id, amount=diff,
                description=form.description.data or f"Manual wallet update by admin: {diff:+.2f}",
                type='adjustment'
            ))
        db.session.commit()
        return jsonify({'success': True, 'new_balance': wallet.ewallet_balance})

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
            return redirect(url_for('admin.admin_update_wallet'))

        try:
            amount = float(amount_str)
        except ValueError:
            flash("Invalid amount value.", "danger")
            return redirect(url_for('admin.admin_update_wallet'))

        user = User.query.get(user_id)
        if not user:
            flash("User not found.", "danger")
            return redirect(url_for('admin.admin_update_wallet'))

        update_wallet_balance(user.id, amount, description)
        flash(f"Wallet updated successfully for user ID {user.id}.", "success")
        return redirect(url_for('admin.user_profile', id=user.id))

    users = User.query.order_by(User.full_name.asc()).all()
    return render_template('admin_update_wallet.html', users=users)


# ---------- ADMIN PROFILE ----------

@admin_bp.route('/profile', methods=['GET', 'POST'])
@admin_required
def admin_profile():
    form = AdminProfileForm(obj=current_user)

    if form.validate_on_submit():
        current_user.full_name = (form.name.data or "").strip()
        current_user.email = (form.email.data or "").strip()

        pw = (getattr(form, "password", None).data or "").strip() if hasattr(form, "password") else ""
        if pw:
            current_user.password = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt())  # ✅ bytes

        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for('admin.admin_profile'))

    return render_template('admin/admin_profile.html', form=form, admin=current_user)


@admin_bp.app_context_processor
def inject_admin_badges():
    # defaults
    unread_broadcast_count = 0
    unread_messages_count = 0

    try:
        if current_user.is_authenticated:
            # Broadcast notifications unread (for admin view)
            unread_broadcast_count = db.session.scalar(
                sa.select(func.count()).select_from(Notification).where(
                    Notification.is_broadcast.is_(True),
                    Notification.is_read.is_(False),
                )
            ) or 0

            # Admin unread messages (where admin is the recipient)
            unread_messages_count = db.session.scalar(
                sa.select(func.count()).select_from(Message).where(
                    Message.recipient_id == current_user.id,
                    Message.is_read.is_(False)
                )
            ) or 0

    except Exception as e:
        db.session.rollback()
        # optional logging
        # current_app.logger.warning("inject_admin_badges failed: %s", e)
        unread_broadcast_count = 0
        unread_messages_count = 0

    return dict(
        unread_broadcast_count=int(unread_broadcast_count),
        unread_messages_count=int(unread_messages_count),
    )

