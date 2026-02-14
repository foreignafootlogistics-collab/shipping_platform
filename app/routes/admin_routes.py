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
from app.utils.invoice_totals import fetch_invoice_totals_pg, mark_invoice_packages_delivered


import sqlalchemy as sa
from sqlalchemy import func, extract, asc
from app.extensions import db
from app.models import (
    User, Wallet, Message, ScheduledDelivery, WalletTransaction, Package, Invoice, Notification, Payment, RateBracket, Discount, shipment_packages, Prealert, ShipmentLog    
)
from app.routes.admin_auth_routes import admin_required


admin_bp = Blueprint(
    'admin', __name__,
    url_prefix='/admin',
    template_folder='templates/admin'
)

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

def allowed_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS

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
@admin_required(roles=['superadmin'])   # 🔒 only superadmin can create other admins
def register_admin():
    form = AdminRegisterForm()

    if form.validate_on_submit():
        full_name = (form.full_name.data or "").strip()
        email = (form.email.data or "").strip()
        password = (form.password.data or "").strip()

        # 🔹 role coming from the <select name="role"> in your HTML
        #    e.g. "admin", "finance", "operations", "accounts_manager"
        role = (request.form.get("role") or "admin").strip()

        # 1) Guard: existing email
        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return render_template('admin/register_admin.html', form=form)

        # 2) See if this is the very first admin-type user in the system
        admin_roles = ["admin", "superadmin", "finance", "operations", "accounts_manager"]

        conds = [
            User.is_superadmin.is_(True),
            User.role.in_(admin_roles),
        ]

        # Some older builds use is_admin boolean
        if hasattr(User, "is_admin"):
            conds.append(User.is_admin.is_(True))

        has_any_admin = User.query.filter(sa.or_(*conds)).first() is not None

        # Default flags
        is_admin = True
        is_superadmin = False

        # If there are NO admins at all yet, force owner superadmin
        if not has_any_admin:
            role = "superadmin"
            is_superadmin = True

        # Optional registration number for first admin
        registration_number = "FAFL10000" if not has_any_admin else None

        # Hash password with bcrypt (store as bytes in LargeBinary column)
        hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

        # Create the user
        u = User(
            full_name=full_name,
            email=email,
            password=hashed_pw,              # bytes (LargeBinary)
            role=role,
            created_at=datetime.now(timezone.utc),    # if your column is string, it'll still store fine
            registration_number=registration_number,
            is_admin=is_admin if hasattr(User, "is_admin") else True,
            is_superadmin=is_superadmin,
        )

        db.session.add(u)
        db.session.commit()

        flash(
            f"Admin account for {full_name} created successfully "
            f"({role}{' / SUPERADMIN' if is_superadmin else ''}).",
            "success"
        )
        return redirect(url_for('admin.dashboard'))

    # GET or failed validation
    return render_template('admin/register_admin.html', form=form)

@admin_bp.route('/manage-admins')
@admin_required(roles=['superadmin'])
def manage_admins():
    """List all admin-type users so superadmin can edit them."""
    admins = User.query.filter_by(is_admin=True).all()
    return render_template('admin/manage_admins.html', admins=admins)

@admin_bp.route('/admins/<int:user_id>/update-role', methods=['POST'])
@admin_required(roles=['superadmin'])  # only superadmin can change roles
def update_admin_role(user_id):
    from app.extensions import db
    from app.models import User

    new_role = (request.form.get("role") or "").strip().lower()

    admin = User.query.get_or_404(user_id)

    # Update role field
    admin.role = new_role

    # Keep is_admin / is_superadmin flags in sync
    if new_role == "superadmin":
        admin.is_superadmin = True
        admin.is_admin = True
    elif new_role in ("admin", "finance", "operations", "accounts_manager"):
        admin.is_superadmin = False
        admin.is_admin = True
    else:
        # fallback – not really expected for this screen
        admin.is_superadmin = False
        admin.is_admin = False

    db.session.commit()
    flash("Admin role updated successfully.", "success")
    return redirect(url_for('admin.manage_admins'))


# ---------- Admin Dashboard ---------- 
# ---------- Admin Dashboard ----------
@admin_bp.route('/dashboard')
@admin_required() 
def dashboard():
    from app.forms import AdminCalculatorForm  # keep this import here

    admin_calculator_form = AdminCalculatorForm()

    # ---- Top summary cards ----
    total_users = db.session.scalar(sa.select(func.count()).select_from(User)) or 0
    total_packages = db.session.scalar(sa.select(func.count()).select_from(Package)) or 0
    pending_invoices = db.session.scalar(
        sa.select(func.count())
        .select_from(Invoice)
        .where(Invoice.status.in_(('pending', 'unpaid', 'issued')))
    ) or 0

    # ---- Scheduled Deliveries (with optional date filters) ----
    start_date_str = request.args.get("start_date", "")
    end_date_str   = request.args.get("end_date", "")

    deliveries_q = sa.select(ScheduledDelivery).order_by(
        ScheduledDelivery.scheduled_date.desc(),
        ScheduledDelivery.id.desc()
    )

    try:
        if start_date_str:
            sd = datetime.fromisoformat(start_date_str).date()
            deliveries_q = deliveries_q.where(ScheduledDelivery.scheduled_date >= sd)
        if end_date_str:
            ed = datetime.fromisoformat(end_date_str).date()
            deliveries_q = deliveries_q.where(ScheduledDelivery.scheduled_date <= ed)
    except Exception:
        # ignore bad date filters
        pass

    deliveries = db.session.execute(deliveries_q.limit(10)).scalars().all()

    # ==============================
    #  Helper: parse dates in Python
    # ==============================
    def _parse_any_dt(value):
        """
        Try to parse value into a datetime.
        Handles datetime, date, or string in a few common formats.
        Returns datetime or None.
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)

        s = str(value).strip()
        if not s:
            return None

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%m/%d/%Y",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None

    # ==============================
    #  Build monthly stats in Python
    # ==============================
    today = date.today()
    current_year = today.year
    current_month = today.month
    window_start_90d = today - timedelta(days=90)

    month_map = {
        1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr',
        5: 'May', 6: 'Jun', 7: 'Jul', 8: 'Aug',
        9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
    }

    # Start with 0 for each month
    user_data_dict = {m: 0 for m in month_map.keys()}
    pkg_data_dict  = {m: 0 for m in month_map.keys()}

    # ---------- Users: use date_registered first, fallback created_at ----------
    users = User.query.all()
    today_new_users = 0

    for u in users:
        dt = _parse_any_dt(getattr(u, "date_registered", None)) \
             or _parse_any_dt(getattr(u, "created_at", None))
        if not dt:
            continue

        d = dt.date()

        if d == today:
            today_new_users += 1

        if dt.year == current_year and 1 <= dt.month <= 12:
            user_data_dict[dt.month] += 1

    # ---------- Packages: use date_received first, fallback created_at ----------
    packages = Package.query.all()
    today_new_packages = 0
    active_customer_ids_90d = set()

    for p in packages:
        dt = _parse_any_dt(getattr(p, "date_received", None)) \
             or _parse_any_dt(getattr(p, "created_at", None))
        if not dt:
            continue

        d = dt.date()

        if d == today:
            today_new_packages += 1

        if dt.year == current_year and 1 <= dt.month <= 12:
            pkg_data_dict[dt.month] += 1

        if d >= window_start_90d and p.user_id:
            active_customer_ids_90d.add(p.user_id)

    # This month totals for the text beside charts
    this_month_new_users = user_data_dict.get(current_month, 0)
    this_month_new_packages = pkg_data_dict.get(current_month, 0)

    # Active customers in last 90 days
    active_customers_90d = len(active_customer_ids_90d)

    # ---- normalize counts so template always gets integers ----
    today_new_users         = int(today_new_users or 0)
    today_new_packages      = int(today_new_packages or 0)
    this_month_new_users    = int(this_month_new_users or 0)
    this_month_new_packages = int(this_month_new_packages or 0)
    active_customers_90d    = int(active_customers_90d or 0)
    
    return render_template(
        'admin/admin_dashboard.html',
        total_users=total_users,
        total_packages=total_packages,
        pending_invoices=pending_invoices,
        deliveries=deliveries,

        # chart data
        user_labels=[month_map[m] for m in month_map],
        user_data=[user_data_dict[m] for m in month_map],
        pkg_labels=[month_map[m] for m in month_map],
        pkg_data=[pkg_data_dict[m] for m in month_map],

        # filters for scheduled deliveries
        start_date=start_date_str,
        end_date=end_date_str,

        # live stats
        today_new_users=today_new_users,
        today_new_packages=today_new_packages,
        this_month_new_users=this_month_new_users,
        this_month_new_packages=this_month_new_packages,
        active_customers_90d=active_customers_90d,

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

    # recipients for bulk send
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

        recipients = User.query.filter(User.id.in_(ids)).all()
        now = datetime.now(timezone.utc)

        for u in recipients:
            db.session.add(Message(
                sender_id=current_user.id,
                recipient_id=u.id,
                subject=subject,
                body=body,
                thread_key=None,  # ✅ ALWAYS None (no threads)
                is_read=False,
                created_at=now,
            ))

            # email notify
            if u.email:
                send_bulk_message_email(
                    to_email=u.email,
                    full_name=u.full_name,
                    subject=subject,
                    message_body=body,
                    recipient_user_id=u.id,
                )

        db.session.commit()
        flash(f"Message + Email sent to {len(recipients)} customer(s).", "success")
        return redirect(url_for("admin.messages", box="sent"))

    # ---- Mailbox controls (Gmail-like) ----
    box = (request.args.get("box") or "inbox").lower()   # inbox | sent | all
    q = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread") == "1"
    include_archived = request.args.get("archived") == "1"

    page = request.args.get("page", type=int) or 1
    per_page = request.args.get("per_page", type=int) or 20
    per_page = max(10, min(per_page, 200))

    base = Message.query

    # mailbox filter (this is the BIG Gmail feel)
    if box == "sent":
        base = base.filter(Message.sender_id == current_user.id)
    elif box == "all":
        base = base.filter(sa.or_(
            Message.sender_id == current_user.id,
            Message.recipient_id == current_user.id
        ))
    else:  # inbox default
        base = base.filter(Message.recipient_id == current_user.id)

    # hide deleted for THIS admin
    base = base.filter(sa.and_(
        sa.or_(Message.sender_id != current_user.id, Message.deleted_by_sender.is_(False)),
        sa.or_(Message.recipient_id != current_user.id, Message.deleted_by_recipient.is_(False)),
    ))

    # hide archived unless explicitly included
    if not include_archived:
        base = base.filter(sa.and_(
            sa.or_(Message.sender_id != current_user.id, Message.archived_by_sender.is_(False)),
            sa.or_(Message.recipient_id != current_user.id, Message.archived_by_recipient.is_(False)),
        ))

    # unread only makes sense for inbox; if they click it in other boxes, it’ll just return none
    if unread_only:
        base = base.filter(
            Message.recipient_id == current_user.id,
            Message.is_read.is_(False)
        )

    # Search: subject/body + other user's name/email
    if q:
        # join "other user" safely depending on mailbox
        # We'll just filter message content first (fast/simple)
        base = base.filter(sa.or_(
            Message.subject.ilike(f"%{q}%"),
            Message.body.ilike(f"%{q}%"),
        ))

    base = base.order_by(Message.created_at.desc())

    pagination = base.paginate(page=page, per_page=per_page, error_out=False)
    messages_list = pagination.items

    # for display: figure out "other user" + label from/to like Gmail
    rows = []
    for m in messages_list:
        is_sent = (m.sender_id == current_user.id)
        other_id = m.recipient_id if is_sent else m.sender_id
        other = User.query.get(other_id)
        rows.append({
            "m": m,
            "other": other,
            "is_sent": is_sent,
        })

    return render_template(
        "admin/messages.html",
        form=form,
        rows=rows,
        pagination=pagination,
        box=box,
        q=q,
        unread_only=unread_only,
        include_archived=include_archived,
        per_page=per_page,
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
        return redirect(url_for("admin.message_detail", message_id=message_id))

    subject = (request.form.get("subject") or "").strip() or f"Re: {original.subject}"

    # reply goes to the other party
    recipient_id = original.sender_id if original.sender_id != current_user.id else original.recipient_id

    msg = Message(
        sender_id=current_user.id,
        recipient_id=recipient_id,
        subject=subject,
        body=body,
        thread_key=None,  # ✅ ALWAYS None
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(msg)
    db.session.commit()

    # Email notification to customer
    other = User.query.get(recipient_id)
    if other and other.email:
        preview = (body[:120] + "…") if len(body) > 120 else body
        send_new_message_email(other.email, other.full_name, subject, preview, other.id)

    flash("Reply sent.", "success")
    return redirect(url_for("admin.message_detail", message_id=msg.id))



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
    return redirect(request.referrer or url_for("admin.messages"))


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
    return redirect(request.referrer or url_for("admin.messages"))


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
        return redirect(url_for("admin.message_detail", message_id=message_id))

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

    ok = send_email(
        to_email=to_email,
        subject=email_subject,
        plain_body=forwarded_plain,
        html_body=forwarded_html_body_only,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,  # optional but good
        recipient_user_id=None,               # forwarding should NOT log into messages
    )

    if ok:
        flash("Message forwarded successfully.", "success")
    else:
        flash("Forward failed. Please try again.", "danger")

    return redirect(url_for("admin.message_detail", message_id=message_id))


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
        box=(request.args.get("box") or default_box),
        q=(request.args.get("q") or None),
        unread=(request.args.get("unread") or None),
        archived=(request.args.get("archived") or None),
        per_page=(request.args.get("per_page") or None),
        page=(request.args.get("page") or None),
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
        if m.recipient_id == current_user.id:
            m.archived_by_recipient = False

        changed += 1

    db.session.commit()
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
    packages = (Package.query
                .filter_by(user_id=user.id, invoice_id=None)
                .order_by(Package.created_at.asc())
                .all())
    if not packages:
        flash("No packages available to invoice.", "warning")
        return redirect(url_for('admin.dashboard'))

    if request.method != 'POST':
        return render_template("admin/invoice_confirm.html",
                               user=user, packages=packages)

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
        freight=0, handling=0, other_charges=0, grand_total=0,
    )
    view_lines = []

    for p in packages:
        desc = p.description or "Miscellaneous"
        wt   = float(p.weight or 0)
        val  = float(getattr(p, "value", 0) or 0)

        # calculate full breakdown
        ch = calculate_charges(desc, val, wt)

        duty          = float(ch.get("duty", 0) or 0)
        scf           = float(ch.get("scf", 0) or 0)
        envl          = float(ch.get("envl", 0) or 0)
        caf           = float(ch.get("caf", 0) or 0)
        gct           = float(ch.get("gct", 0) or 0)
        stamp         = float(ch.get("stamp", 0) or 0)
        freight       = float(ch.get("freight", 0) or 0)
        handling      = float(ch.get("handling", 0) or 0)
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
        p.amount_due    = grand_total      # what we already relied on
        p.invoice_id    = inv.id

        # aggregate invoice totals
        totals["duty"]          += duty
        totals["scf"]           += scf
        totals["envl"]          += envl
        totals["caf"]           += caf
        totals["gct"]           += gct
        totals["stamp"]         += stamp
        totals["freight"]       += freight
        totals["handling"]      += handling
        totals["other_charges"] += other_charges
        totals["grand_total"]   += grand_total

        view_lines.append({
            "house_awb":  p.house_awb,
            "description": desc,
            "weight":      wt,
            "value_usd":   val,
            **ch,
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
    inv.grand_total    = totals["grand_total"]
    inv.amount_due     = totals["grand_total"]
    inv.subtotal       = totals["grand_total"]

    db.session.commit()

    invoice_dict = {
        "id":           inv.id,
        "user_id":      user.id,
        "number":       inv.invoice_number,
        "date":         inv.date_submitted,
        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "subtotal":     totals["grand_total"],
        "total_due":    totals["grand_total"],
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
            "discount_due":   x.get("discount_due", 0),
        } for x in view_lines],
    }
    flash(f"Invoice {invoice_number} generated successfully!", "success")
    return render_template("admin/invoice_view.html", invoice=invoice_dict)


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

@admin_bp.route('/invoices/user/<int:user_id>', methods=['GET'], endpoint='view_customer_invoice')
@admin_required
def view_customer_invoice(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('admin.dashboard'))

    pkgs = (
        Package.query
        .filter(
            Package.user_id == user.id,
            Package.invoice_id.is_(None)
        )
        .order_by(asc(getattr(Package, "date_received", Package.created_at)))
        .all()
    )

    items = []
    totals = dict(
        duty=0, scf=0, envl=0, caf=0, gct=0,
        stamp=0, freight=0, handling=0, other_charges=0,
        grand_total=0
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
        freight  = float(getattr(p, "freight_fee", getattr(p, "freight", 0)) or 0)
        handling = float(getattr(p, "storage_fee", getattr(p, "handling", 0)) or 0)

        other_charges = float(getattr(p, "other_charges", 0) or 0)
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
        totals["grand_total"]   += grand_total

    invoice_dict = {
        "id": int(user.id),
        "number": f"PROFORMA-{user.id}",
        "date": datetime.utcnow(),
        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "branch": "Main Branch",
        "staff": getattr(current_user, "full_name", "FAFL ADMIN"),

        # Optional but used in template
        "credit_available": float(getattr(user, "wallet_balance", 0) or 0),

        subtotal = float(totals["grand_total"] or 0.0)

        # Proforma has no payments yet, but keep structure consistent
        discount_total = float(totals.get("discount_total", 0.0) or 0.0)
        payments_total = 0.0

        total_due = max(subtotal - discount_total - payments_total, 0.0)

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
        amount = float(request.form.get('payment_amount') or 0)
        method = (request.form.get('payment_type') or "Cash").strip()
        authorized_by = (request.form.get('authorized_by') or "Admin").strip()

        inv = Invoice.query.get_or_404(invoice_id)

        if amount <= 0:
            return jsonify({"success": False, "error": "Payment amount must be greater than 0."}), 400

        # ✅ Create Payment using ONLY columns that exist in your model
        notes = f"Authorized by: {authorized_by}" if authorized_by else None

        payment = Payment(
            invoice_id=inv.id,
            user_id=inv.user_id,
            method=method,
            amount_jmd=amount,
            notes=notes,
            created_at=datetime.now(timezone.utc),
        )
        db.session.add(payment)
        db.session.flush()

        # ✅ Recompute using your shared totals function (includes discounts + all payments)
        subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)

        inv.amount_due = float(total_due)
        prev_status = inv.status

        if inv.amount_due <= 0:
            inv.status = "paid"
            inv.date_paid = datetime.utcnow()
            if prev_status != "paid":
                mark_invoice_packages_delivered(inv.id)
        elif float(payments_total) > 0:
            inv.status = "partial"
            inv.date_paid = None
        else:
            inv.status = "unpaid"
            inv.date_paid = None

        db.session.commit()

        return jsonify({
            "success": True,
            "invoice_id": inv.id,
            "status": inv.status,
            "amount_due": float(inv.amount_due),
            "paid_sum": float(payments_total),
            "payment_date": payment.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "payment_type": method,
            "amount": amount,
            "authorized_by": authorized_by,
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@admin_bp.route('/generate-pdf-invoice/<int:user_id>')
@admin_required
def generate_pdf_invoice(user_id):
    user = User.query.get_or_404(user_id)

    # last 10 invoices for this user
    invoices = (Invoice.query
                .filter_by(user_id=user.id)
                .order_by(Invoice.id.desc())
                .limit(10).all())

    items, total = [], 0.0
    for inv in invoices:
        amount = float(getattr(inv, "grand_total", getattr(inv, "amount_due", 0)) or 0)
        items.append({
            "description": f"{getattr(inv, 'description', 'Invoice')} ({inv.status})",
            "weight": "—",
            "rate": "—",
            "total": round(amount, 2)
        })
        total += amount

    today = datetime.utcnow().strftime('%B %d, %Y')
    html = render_template("invoice.html",
                           full_name=getattr(user, "full_name", ""),
                           registration_number=getattr(user, "registration_number", ""),
                           date=today,
                           items=items,
                           grand_total=round(total, 2))
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
    inv = Invoice.query.get_or_404(invoice_id)
    user = inv.user if hasattr(inv, "user") else None

    # packages for the table (LIVE calc like proforma: ceil weight)
    packages = []
    rows = Package.query.filter_by(invoice_id=invoice_id).order_by(Package.created_at.asc()).all()

    for p in rows:
        desc = p.description or getattr(p, "category", "Miscellaneous") or "Miscellaneous"

        wt_raw = float(getattr(p, "weight", 0) or 0)
        wt = int(math.ceil(wt_raw))  # ✅ round up like proforma

        val = float(
            getattr(p, "value", None)
            or getattr(p, "value_usd", None)
            or getattr(p, "invoice_value", None)
            or 0
        )

        ch = calculate_charges(desc, val, wt)
        due = float(ch.get("grand_total", 0) or 0)

        packages.append({
            "id": p.id,
            "house_awb": p.house_awb,
            "description": desc,
            "merchant": getattr(p, "merchant", None),
            "weight": wt,          # show rounded weight
            "value_usd": val,
            "amount_due": due,     # ✅ matches proforma
        })


    preview_subtotal = sum(float(x.get("amount_due", 0) or 0) for x in packages)

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(invoice_id)

    preview_payments_total = float(payments_total or 0.0)
    preview_discount_total = float(discount_total or 0.0)

    preview_balance_due = max(
        float(preview_subtotal or 0)
        - float(discount_total or 0)
        - float(payments_total or 0),
        0.0
    )
        
    

    # Make a dict that still feels like the ORM object in Jinja
    invoice_dict = {
        "id": inv.id,
        "user_id": inv.user_id,
        "user": user,                         # so invoice.user.* works
        "invoice_number": inv.invoice_number, # so invoice.invoice_number works
        "grand_total": float(inv.grand_total or subtotal or 0),
        "date_issued": (
            inv.date_issued
            or inv.date_submitted
            or inv.created_at
            or datetime.utcnow()
        ),
        "customer_code": getattr(user, "registration_number", "") if user else "",
        "customer_name": getattr(user, "full_name", "") if user else "",
        "subtotal": subtotal,
        "discount_total": discount_total,
        "payments_total": payments_total,
        "total_due": total_due,
        "description": getattr(inv, "description", "") or "",
        "amount_due": float(getattr(inv, "amount_due", total_due) or 0.0),
        "packages": packages,
        "preview_subtotal": float(preview_subtotal),
        "preview_discount_total": float(discount_total or 0),
        "preview_payments_total": float(payments_total or 0),
        "preview_total_due": float(preview_balance_due),

    }

    # ---------- authorised signers ----------
    authorized_signers: list[str] = []
    try:
        from app.models import Settings, User
        settings = Settings.query.get(1)
        if settings and getattr(settings, "authorized_signers", None):
            authorized_signers = [
                s.strip() for s in settings.authorized_signers.split(",") if s.strip()
            ]
    except Exception:
        authorized_signers = []

    if not authorized_signers:
        try:
            from app.models import User
            authorized_signers = [
                (u.full_name or u.email)
                for u in User.query.filter_by(is_admin=True)
                                  .order_by(User.full_name.asc())
                                  .all()
            ]
        except Exception:
            authorized_signers = []

    if not authorized_signers:
        authorized_signers = [getattr(current_user, "full_name", "Admin")]

    is_inline = (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.args.get('inline') == '1'
    )
    tpl = "admin/invoices/_invoice_inline.html" if is_inline else "admin/invoice_view.html"

    return render_template(
        tpl,
        invoice=invoice_dict,
        USD_TO_JMD=USD_TO_JMD,
        authorized_signers=authorized_signers,
    )
  

# ---------- BREAKDOWN (Lightning icon) ----------
@admin_bp.route("/invoice/breakdown/<int:package_id>", methods=["GET"])
@admin_required
def invoice_breakdown(package_id):
    p = Package.query.get_or_404(package_id)

    desc   = (p.description or getattr(p, "category", None) or "Miscellaneous")
    weight = float(getattr(p, "weight", 0) or 0)
    value  = float(getattr(p, "value", 0) or getattr(p, "value_usd", 0) or 0)

    ch = calculate_charges(desc, value, weight)

    payload = {
        # core breakdown
        "duty":          float(ch.get("duty", 0) or 0),
        "gct":           float(ch.get("gct", 0) or 0),
        "scf":           float(ch.get("scf", 0) or 0),
        "envl":          float(ch.get("envl", 0) or 0),
        "caf":           float(ch.get("caf", 0) or 0),
        "stamp":         float(ch.get("stamp", 0) or 0),

        # totals
        "customs_total": float(ch.get("customs_total", 0) or 0),
        "freight":       float(ch.get("freight", 0) or 0),
        "handling":      float(ch.get("handling", 0) or 0),
        "freight_total": float(ch.get("freight_total", 0) or 0),

        # IMPORTANT: name must match your modal field
        "other_charges": float(ch.get("other_charges", 0) or 0),

        # grand total (JMD)
        "grand_total":   float(ch.get("grand_total", 0) or 0),

        # also return these so the modal can prefill top fields
        "category": desc,
        "weight": weight,
        "value": value,
    }

    return jsonify(payload)


@admin_bp.route("/invoice/<int:invoice_id>/delete", methods=["POST"])
@admin_required
def delete_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)

    # pull "return context" from the form (Option B)
    tab          = (request.form.get("tab") or "invoices").strip() or "invoices"
    inv_page     = request.form.get("inv_page", 1, type=int)
    inv_per_page = request.form.get("inv_per_page", 10, type=int)
    inv_from     = (request.form.get("inv_from") or "").strip()
    inv_to       = (request.form.get("inv_to") or "").strip()

    def back():
        kwargs = {
            "id": inv.user_id,
            "tab": tab,
            "inv_page": inv_page,
            "inv_per_page": inv_per_page,
        }
        if inv_from:
            kwargs["inv_from"] = inv_from
        if inv_to:
            kwargs["inv_to"] = inv_to
        return redirect(url_for("accounts_profiles.view_user", **kwargs))

    # If any payments exist, block delete (safer)
    pay_count = Payment.query.filter(Payment.invoice_id == inv.id).count()
    if pay_count > 0:
        msg = f"Cannot delete invoice: {pay_count} payment(s) are linked to it."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(success=False, error=msg), 400
        flash(msg, "danger")
        return back()

    # Unlink packages from this invoice (avoid FK errors / keep packages)
    Package.query.filter(Package.invoice_id == inv.id).update({"invoice_id": None})

    db.session.delete(inv)
    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        # still return JSON, but now your client-side reload can keep tab via URL if you want
        return jsonify(success=True)

    flash("Invoice deleted.", "success")
    return back()


@admin_bp.route("/invoice/add-payment/<int:invoice_id>", methods=["POST"])
@admin_required
def add_payment(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)

    # Read form fields
    try:
        amount = float(request.form.get("amount_jmd", 0) or 0)
    except Exception:
        amount = 0

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

    # Build notes safely
    notes_parts = []
    if extra_notes:
        notes_parts.append(extra_notes)
    if authorized_by:
        notes_parts.append(f"Authorised by: {authorized_by}")
    notes = "\n".join(notes_parts) if notes_parts else None

    # ✅ Build kwargs depending on your Payment columns
    payment_kwargs = {
        "invoice_id": inv.id,
        "user_id": inv.user_id,
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

    # optional fields if they exist
    if hasattr(Payment, "reference"):
        payment_kwargs["reference"] = reference or None

    if hasattr(Payment, "notes"):
        payment_kwargs["notes"] = notes

    if hasattr(Payment, "authorized_by"):
        payment_kwargs["authorized_by"] = authorized_by or "Admin"

    if hasattr(Payment, "payment_date"):
        payment_kwargs["payment_date"] = datetime.utcnow()

    if hasattr(Payment, "bill_number"):
        payment_kwargs["bill_number"] = f"BILL-{inv.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    p = Payment(**payment_kwargs)
    db.session.add(p)
    db.session.flush()

    # ✅ Sum payments using the correct column
    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
    paid_sum = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)
    inv.amount_due = float(total_due)
    new_due = inv.amount_due
    base_total = float(subtotal or 0)
    paid_sum = float(payments_total or 0)


    previous_status = inv.status

    if new_due <= 0:
        inv.status = "paid"
        if hasattr(inv, "date_paid"):
            inv.date_paid = datetime.utcnow()

        if previous_status != "paid":
            mark_invoice_packages_delivered(inv.id)

    elif 0 < new_due < base_total:
        inv.status = "partial"
    else:
        inv.status = "unpaid"

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
    previous_status = inv.status

    amount = float(request.form.get("amount_jmd", 0) or 0)
    if amount <= 0:
        flash("Discount amount must be greater than 0.", "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    # First apply discount to base total
    base_total_before = float(inv.grand_total or inv.amount or 0)
    base_total_after  = max(base_total_before - amount, 0.0)

    inv.grand_total = base_total_after
    inv.amount      = base_total_after  # keep legacy in sync

    # Now recompute amount_due based on new base total & existing payments
    paid_sum = (
        db.session.query(func.coalesce(func.sum(Payment.amount_jmd), 0.0))
        .filter(Payment.invoice_id == inv.id)
        .scalar()
        or 0.0
    )

    new_due = max(base_total_after - paid_sum, 0.0)
    inv.amount_due = new_due

    # Status
    if new_due <= 0:
        inv.status    = "paid"
        inv.date_paid = datetime.utcnow()

        if previous_status != "paid":
            mark_invoice_packages_delivered(inv.id)
            for pkg in inv.packages:
                pkg.status = "delivered" 
    elif 0 < new_due < base_total_after:
        inv.status = "partial"
    else:
        inv.status = "unpaid"

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

    pkgs = (Package.query
            .filter_by(invoice_id=invoice_id)
            .order_by(Package.created_at.asc())
            .all())

    # Settings (logo + rates)
    from app.models import Settings
    settings = Settings.query.get(1)

    effective_usd_to_jmd = (getattr(settings, "usd_to_jmd", None) or USD_TO_JMD)

    # ---- logo url (robust) ----
    logo_url = url_for(
        "static",
        filename=(settings.logo_path if settings and settings.logo_path else "logo.png"),
        _external=True
    )

    items = []
    subtotal = 0.0

    for p in pkgs:
        desc = p.description or getattr(p, "category", "Miscellaneous") or "Miscellaneous"
        wt_raw = float(p.weight or 0)
        wt = math.ceil(wt_raw)
        val  = float(getattr(p, "value", 0) or getattr(p, "value_usd", 0) or 0)

        # ✅ Calculate charges live (same as lightning breakdown)
        ch = calculate_charges(desc, val, wt)

        freight   = float(ch.get("freight", 0) or 0)
        handling  = float(ch.get("handling", 0) or 0)
        duty      = float(ch.get("duty", 0) or 0)
        gct       = float(ch.get("gct", 0) or 0)
        scf       = float(ch.get("scf", 0) or 0)
        envl      = float(ch.get("envl", 0) or 0)
        caf       = float(ch.get("caf", 0) or 0)
        stamp     = float(ch.get("stamp", 0) or 0)
        other     = float(ch.get("other_charges", 0) or 0)
        total_jmd = float(ch.get("grand_total", 0) or 0)

        subtotal += total_jmd

        items.append({
            "house_awb": p.house_awb,
            "description": desc,
            "weight": wt,
            "value": val,
            "freight": freight,
            "handling": handling,
            "storage": handling,          # optional alias
            "duty": duty,
            "gct": gct,
            "scf": scf,
            "envl": envl,
            "caf": caf,
            "stamp": stamp,
            "other_charges": other,
            "discount_due": 0.0,
        })

    # ✅ Keep the LIVE subtotal you calculated above (do NOT overwrite it)
    live_subtotal = float(subtotal or 0.0)

    # Pull discounts/payments from DB (ignore DB subtotal)
    _db_subtotal, discount_total, payments_total, _db_total_due = fetch_invoice_totals_pg(invoice_id)

    # ✅ Compute balance due using LIVE subtotal
    balance_due = max(live_subtotal - float(discount_total or 0) - float(payments_total or 0), 0.0)



    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number or f"PROFORMA-{inv.id}",
        "date": inv.date_issued or inv.date_submitted or datetime.utcnow(),
        "customer_code": getattr(user, "registration_number", "") or "",
        "customer_name": getattr(user, "full_name", "") or "",
        "branch": "Main Branch",
        "staff": getattr(current_user, "full_name", "FAFL ADMIN"),

        "subtotal": live_subtotal,
        "discount_total": float(discount_total or 0.0),
        "payments_total": float(payments_total or 0.0),
        "total_due": balance_due,
        "notes": getattr(inv, "description", "") or "",
        "packages": items,
    }


    return render_template(
        "admin/invoices/_invoice_core.html",
        invoice=invoice_dict,
        settings=settings,
        USD_TO_JMD=effective_usd_to_jmd,
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

    packages = [{
        "id": p.id,
        "house_awb": p.house_awb,
        "description": p.description,
        "merchant": getattr(p, "merchant", None),
        "weight": float(p.weight or 0),
        "value_usd": float(getattr(p, "value", 0) or 0),
        "amount_due": float(getattr(p, "amount_due", 0) or 0),
    } for p in Package.query.filter_by(invoice_id=invoice_id).all()]

    subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(invoice_id)

    invoice_dict = {
        "id": inv.id,
        "user_id": inv.user_id,
        "number": inv.invoice_number,
        "date": inv.date_submitted or inv.created_at or datetime.utcnow(),
        "customer_code": getattr(user, "registration_number", "") if user else "",
        "customer_name": getattr(user, "full_name", "") if user else "",
        "subtotal": subtotal,
        "discount_total": discount_total,
        "payments_total": payments_total,
        "total_due": total_due,
        "packages": packages,
        "description": getattr(inv, "description", "") or "",
    }
    return render_template("admin/invoices/_invoice_inline.html",
                           invoice=invoice_dict, USD_TO_JMD=USD_TO_JMD)

@admin_bp.route("/invoice/<int:invoice_id>/email-proforma", methods=["POST"])
@login_required
@admin_required
def email_proforma_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)

    # -----------------------------
    # 1) Customer email
    # -----------------------------
    customer_email = None
    if inv.user and getattr(inv.user, "email", None):
        customer_email = inv.user.email
    else:
        customer_email = getattr(inv, "customer_email", None)

    if not customer_email:
        return jsonify(ok=False, error="Customer email not found for this invoice."), 400

    user_obj = inv.user

    # -----------------------------
    # 2) Build SAME proforma payload as modal
    # -----------------------------
    pkgs = (
        Package.query
        .filter_by(invoice_id=invoice_id)
        .order_by(Package.created_at.asc())
        .all()
    )

    from app.models import Settings
    settings = Settings.query.get(1)
    effective_usd_to_jmd = getattr(settings, "usd_to_jmd", None) or USD_TO_JMD

    logo_url = url_for(
        "static",
        filename=(settings.logo_path if settings and settings.logo_path else "logo.png"),
        _external=True
    )

    items = []
    subtotal = 0.0

    for p in pkgs:
        desc = p.description or getattr(p, "category", "Miscellaneous") or "Miscellaneous"
        wt = math.ceil(float(getattr(p, "weight", 0) or 0))
        val = float(getattr(p, "value", 0) or getattr(p, "value_usd", 0) or 0)

        ch = calculate_charges(desc, val, wt)
        line_total = float(ch.get("grand_total", 0) or 0)
        subtotal += line_total

        items.append({
            "house_awb": p.house_awb,
            "description": desc,
            "weight": wt,
            "value": val,
            "freight": float(ch.get("freight", 0) or 0),
            "handling": float(ch.get("handling", 0) or 0),
            "storage": float(ch.get("handling", 0) or 0),
            "duty": float(ch.get("duty", 0) or 0),
            "gct": float(ch.get("gct", 0) or 0),
            "scf": float(ch.get("scf", 0) or 0),
            "envl": float(ch.get("envl", 0) or 0),
            "caf": float(ch.get("caf", 0) or 0),
            "stamp": float(ch.get("stamp", 0) or 0),
            "other_charges": float(ch.get("other_charges", 0) or 0),
            "discount_due": 0.0,
        })

    live_subtotal = float(subtotal or 0.0)
    _db_subtotal, discount_total, payments_total, _db_total_due = fetch_invoice_totals_pg(invoice_id)
    balance_due = max(live_subtotal - discount_total - payments_total, 0.0)

    invoice_number = inv.invoice_number or f"INV{inv.id:05d}"

    invoice_dict = {
        "id": inv.id,
        "number": invoice_number,
        "date": inv.date_issued or inv.date_submitted or datetime.utcnow(),
        "customer_code": getattr(user_obj, "registration_number", ""),
        "customer_name": getattr(user_obj, "full_name", ""),
        "branch": "Main Branch",
        "staff": getattr(current_user, "full_name", "FAFL ADMIN"),
        "subtotal": live_subtotal,
        "discount_total": float(discount_total or 0),
        "payments_total": float(payments_total or 0),
        "total_due": balance_due,
        "notes": getattr(inv, "description", "") or "",
        "packages": items,
    }

    # -----------------------------
    # 3) Render invoice HTML
    # -----------------------------
    html = render_template(
        "admin/invoices/_invoice_core.html",
        invoice=invoice_dict,
        settings=settings,
        USD_TO_JMD=effective_usd_to_jmd,
        logo_url=logo_url,
    )

    # -----------------------------
    # 4) Generate PDF
    # -----------------------------
    try:
        pdf_bytes = HTML(
            string=html,
            base_url=request.url_root
        ).write_pdf()
    except Exception as e:
        current_app.logger.exception("PDF generation failed")
        return jsonify(ok=False, error=str(e)), 500

    filename = f"Proforma_{invoice_number}.pdf"

    # -----------------------------
    # 5) Email content
    # -----------------------------
    subject = f"Proforma Invoice {invoice_number} - Foreign A Foot Logistics"
    full_name = invoice_dict["customer_name"] or "Customer"

    plain_body = (
        f"Hi {full_name},\n\n"
        f"Please find your Proforma Invoice {invoice_number} attached.\n\n"
        f"Balance Due: JMD {balance_due:,.2f}\n\n"
        f"If you have any questions, simply reply to this email.\n\n"
        f"— Foreign A Foot Logistics\n"
        f"(876) 560-7764\n"
    )

    html_body = f"""
    <div style="font-family:Arial,sans-serif; line-height:1.6;">
      <p>Hi <b>{full_name}</b>,</p>
      <p>Your <b>Proforma Invoice {invoice_number}</b> is attached.</p>
      <p><b>Balance Due:</b> JMD {balance_due:,.2f}</p>
      <p>If you have any questions, simply reply to this email.</p>
      <p style="margin-top:16px;">
        — Foreign A Foot Logistics<br>
        (876) 560-7764
      </p>
    </div>
    """

    # -----------------------------
    # 6) Send via email_utils ✅
    # -----------------------------
    email_utils.send_email(
        to_email=customer_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=[(pdf_bytes, filename, "application/pdf")],
        recipient_user_id=inv.user_id,
    )

    return jsonify(ok=True, sent_to=customer_email, filename=filename)



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

