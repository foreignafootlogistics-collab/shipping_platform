from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, make_response, jsonify, send_file, abort
)
from flask_login import login_required, current_user, login_user, logout_user
from werkzeug.utils import secure_filename

import os, io, math, smtplib
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from calendar import monthrange
from collections import OrderedDict

import openpyxl
from weasyprint import HTML
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email
from app.forms import (
    LoginForm, SendMessageForm, AdminLoginForm, BulkMessageForm, UploadPackageForm,
    SingleRateForm, BulkRateForm, MiniRateForm, AdminProfileForm, AdminRegisterForm,
    ExpenseForm, WalletUpdateForm, AdminCalculatorForm, PackageBulkActionForm, InvoiceForm, InvoiceItemForm

)

from app.utils import email_utils
from app.utils.wallet import update_wallet, update_wallet_balance
from app.utils.invoice_utils import generate_invoice
from app.utils.rates import get_rate_for_weight
from app.utils.invoice_pdf import generate_invoice_pdf
from app.calculator import calculate_charges
from app.calculator_data import CATEGORIES, USD_TO_JMD

import sqlalchemy as sa
from sqlalchemy import func, extract
from app.extensions import db
from app.models import (
    User, Wallet, Message, ScheduledDelivery, WalletTransaction, Package, Invoice, Notification, Payment, RateBracket    
)

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

def _fetch_invoice_totals_pg(invoice_id: int):
    """Compute totals using ORM instead of SQLite."""
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return 0.0, 0.0, 0.0, 0.0

    # subtotal = sum of package.amount_due (or fallback to invoice.subtotal/grand_total)
    pkg_sum = db.session.scalar(
        sa.select(func.coalesce(func.sum(Package.amount_due), 0.0)).where(Package.invoice_id == invoice_id)
    ) or 0.0
    subtotal = pkg_sum or float(getattr(inv, "subtotal", 0.0) or getattr(inv, "grand_total", 0.0) or 0.0)

    discount_total = float(getattr(inv, "discount_total", 0.0) or 0.0)

    # Payments (if Payment model exists)
    payments_total = 0.0
    try:
        from app.models import Payment
        payments_total = db.session.scalar(
            sa.select(func.coalesce(func.sum(Payment.amount), 0.0)).where(Payment.invoice_id == invoice_id)
        ) or 0.0
    except Exception:
        payments_total = 0.0

    total_due = max(subtotal - discount_total - payments_total, 0.0)
    return float(subtotal), float(discount_total), float(payments_total), float(total_due)


@admin_bp.route('/register-admin', methods=['GET', 'POST'])
@admin_required
def register_admin():
    form = AdminRegisterForm()
    if form.validate_on_submit():
        import bcrypt
        full_name = form.full_name.data.strip()
        email = form.email.data.strip()
        password = form.password.data.strip()

        # guard: existing email?
        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return render_template('admin/register_admin.html', form=form)

        # first admin gets FAFL10000 (optional)
        first_admin = not User.query.filter_by(role='admin').first()
        registration_number = "FAFL10000" if first_admin else None

        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode()

        u = User(
            full_name=full_name,
            email=email,
            password=hashed_pw,
            role="admin",
            created_at=datetime.utcnow(),
            registration_number=registration_number
        )
        # if your model has is_admin:
        if hasattr(u, "is_admin"):
            u.is_admin = True

        db.session.add(u)
        db.session.commit()
        flash(f"Admin account for {full_name} created successfully!", "success")
        return redirect(url_for('admin.dashboard'))
    return render_template('admin/register_admin.html', form=form)

# ---------- Admin Dashboard ----------
# Admin dashboard (Postgres version)
@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    
    admin_calculator_form = AdminCalculatorForm()

    # ---- Cards ----
    total_users = db.session.scalar(sa.select(func.count()).select_from(User)) or 0
    total_packages = db.session.scalar(sa.select(func.count()).select_from(Package)) or 0
    pending_invoices = db.session.scalar(
        sa.select(func.count()).select_from(Invoice).where(
            Invoice.status.in_(('pending', 'unpaid', 'issued'))
        )
    ) or 0

    # Optional date filters for scheduled deliveries (your template has a filter form)
    start_date_str = request.args.get("start_date", "")
    end_date_str   = request.args.get("end_date", "")

    deliveries_q = sa.select(ScheduledDelivery).order_by(ScheduledDelivery.scheduled_date.desc())

    # Apply date filters if provided (assumes scheduled_date is a Date or DateTime)
    try:
        if start_date_str:
            sd = datetime.fromisoformat(start_date_str).date()
            deliveries_q = deliveries_q.where(ScheduledDelivery.scheduled_date >= sd)
        if end_date_str:
            ed = datetime.fromisoformat(end_date_str).date()
            deliveries_q = deliveries_q.where(ScheduledDelivery.scheduled_date <= ed)
    except Exception:
        # If parsing fails, we just ignore filters
        pass

    deliveries = db.session.execute(deliveries_q.limit(10)).scalars().all()

    # ---- Charts: Monthly users & monthly packages (current year) ----
    current_year = date.today().year
    month_map = {
        1: 'Jan', 2: 'Feb', 3: 'Mar', 4: 'Apr',
        5: 'May', 6: 'Jun', 7: 'Jul', 8: 'Aug',
        9: 'Sep', 10: 'Oct', 11: 'Nov', 12: 'Dec'
    }

    # Build zero-filled dicts 1..12
    user_data_dict = {m: 0 for m in month_map.keys()}
    pkg_data_dict  = {m: 0 for m in month_map.keys()}

    # Coalesce dates safely (cast to timestamp so Postgres COALESCE types match)
    # Users: prefer date_registered, fallback created_at
    user_coalesced_ts = func.coalesce(
        sa.cast(User.date_registered, sa.DateTime()),
        sa.cast(User.created_at,     sa.DateTime())
    )

    # Packages: prefer date_received, fallback created_at
    pkg_coalesced_ts = func.coalesce(
        sa.cast(Package.date_received, sa.DateTime()),
        sa.cast(Package.created_at,    sa.DateTime())
    )

    # --- Users per month (current year) ---
    user_rows = db.session.execute(
        sa.select(
            extract('month', user_coalesced_ts).label('m'),
            func.count(User.id).label('cnt'),
        )
        .where(extract('year', user_coalesced_ts) == current_year)
        .group_by(sa.text('m'))
        .order_by(sa.text('m'))
    ).all()

    for m_num, cnt in user_rows:
        # m_num may come as Decimal/float; normalize to int 1..12
        m = int(m_num)
        if 1 <= m <= 12:
            user_data_dict[m] = int(cnt or 0)

    # --- Packages per month (current year) ---
    pkg_rows = db.session.execute(
        sa.select(
            extract('month', pkg_coalesced_ts).label('m'),
            func.count(Package.id).label('cnt'),
        )
        .where(extract('year', pkg_coalesced_ts) == current_year)
        .group_by(sa.text('m'))
        .order_by(sa.text('m'))
    ).all()

    for m_num, cnt in pkg_rows:
        m = int(m_num)
        if 1 <= m <= 12:
            pkg_data_dict[m] = int(cnt or 0)

    return render_template(
        'admin/admin_dashboard.html',
        total_users=total_users,
        total_packages=total_packages,
        pending_invoices=pending_invoices,
        deliveries=deliveries,

        # keep your original labels/data shape
        user_labels=[month_map[m] for m in month_map],
        user_data=[user_data_dict[m] for m in month_map],
        pkg_labels=[month_map[m] for m in month_map],
        pkg_data=[pkg_data_dict[m] for m in month_map],

        # pass filters back to template (your filter form binds to these)
        start_date=start_date_str,
        end_date=end_date_str,

        admin_calculator_form=admin_calculator_form
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
        return render_template('admin/rates/edit_rate.html', form=form, rate_id=rate_id)

    return render_template('admin/rates/edit_rate.html', form=form, rate_id=rate_id)



# ----- Admin Inbox / Sent + Bulk Messaging -----
@admin_bp.route("/messages", methods=["GET", "POST"])
@admin_required
def view_messages():
    admin_id = current_user.id  # <- use ORM user
    form = SendMessageForm()

    form.recipient_ids.choices = [
        (u.id, f"{u.full_name} ({u.email})")
        for u in User.query.order_by(User.full_name).all()
    ]

    if form.validate_on_submit():
        subject = form.subject.data.strip()
        body = form.body.data.strip()
        selected_user_ids = form.recipient_ids.data

        if not selected_user_ids:
            flash("Please select at least one recipient.", "danger")
            return redirect(url_for("admin.view_messages"))

        msgs = []
        for uid in selected_user_ids:
            msgs.append(Message(
                sender_id=admin_id,
                recipient_id=uid,
                subject=subject,
                body=body,
                created_at=datetime.utcnow()
            ))
        db.session.add_all(msgs)
        db.session.commit()
        flash(f"Message sent to {len(selected_user_ids)} user(s)!", "success")
        return redirect(url_for("admin.view_messages"))

    inbox = Message.query.filter_by(recipient_id=admin_id).order_by(Message.created_at.desc()).all()
    for msg in inbox:
        sender = User.query.get(msg.sender_id)
        msg.sender_label = "Admin" if (sender and getattr(sender, "is_admin", False)) else "Customer"

    sent = Message.query.filter_by(sender_id=admin_id).order_by(Message.created_at.desc()).all()
    return render_template("admin/messages.html", form=form, inbox=inbox, sent=sent)


@admin_bp.route("/messages/mark_read/<int:msg_id>", methods=["POST"])
@admin_required
def mark_message_read(msg_id):
    admin_id = current_user.id
    msg = Message.query.get_or_404(msg_id)
    if msg.recipient_id != admin_id:
        flash("Not authorized.", "danger")
        return redirect(url_for("admin.view_messages"))

    msg.is_read = True
    db.session.commit()
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
    user = User.query.get_or_404(user_id)
    packages = Package.query.filter_by(user_id=user.id, invoice_id=None)\
                            .order_by(Package.created_at.asc()).all()
    if not packages:
        flash("No packages available to invoice.", "warning")
        return redirect(url_for('admin.dashboard'))

    if request.method != 'POST':
        return render_template("admin/invoice_confirm.html", user=user, packages=packages)

    # create invoice shell
    invoice_number = f"INV-{user.id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    inv = Invoice(
        user_id=user.id,
        invoice_number=invoice_number,
        date_submitted=datetime.utcnow(),
        date_issued=datetime.utcnow(),
        status="unpaid",
        amount_due=0
    )
    db.session.add(inv)
    db.session.flush()  # get inv.id

    totals = dict(duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0, freight=0, handling=0, grand_total=0)
    view_lines = []
    for p in packages:
        desc = p.description or "Miscellaneous"
        wt   = float(p.weight or 0)
        val  = float(getattr(p, "value", 0) or 0)
        ch   = calculate_charges(desc, val, wt)

        # link to invoice
        p.amount_due = ch["grand_total"]
        p.invoice_id = inv.id

        # aggregate
        for k in totals:
            totals[k] += float(ch.get(k, 0) or 0)

        view_lines.append({
            "house_awb":  p.house_awb,
            "description": desc,
            "weight":      wt,
            "value_usd":   val,
            **ch
        })

    # finalize invoice
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
    inv.subtotal       = totals["grand_total"]  # optional

    db.session.commit()

    invoice_dict = {
        "id": inv.id,
        "number": inv.invoice_number,
        "date": inv.date_submitted,
        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "subtotal": totals["grand_total"],
        "total_due": totals["grand_total"],
        "packages": [{
            "house_awb": x["house_awb"],
            "description": x["description"],
            "weight": x["weight"],
            "value": x["value_usd"],
            "freight": x.get("freight", 0),
            "storage": x.get("handling", 0),
            "duty": x.get("duty", 0),
            "scf": x.get("scf", 0),
            "envl": x.get("envl", 0),
            "caf": x.get("caf", 0),
            "gct": x.get("gct", 0),
            "other_charges": x.get("other_charges", 0),
            "discount_due": x.get("discount_due", 0),
        } for x in view_lines]
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



@admin_bp.route('/invoice/mark_paid', methods=['POST'])
@admin_required
def mark_invoice_paid():
    try:
        from app.models import Payment  # if not already imported
        invoice_id = int(request.form.get('invoice_id'))
        amount = float(request.form.get('payment_amount'))
        payment_type = request.form.get('payment_type')
        authorized_by = request.form.get('authorized_by')

        inv = Invoice.query.get_or_404(invoice_id)
        inv.status = 'paid'

        payment = Payment(
            bill_number=f'BILL-{inv.id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}',
            payment_date=datetime.utcnow(),
            payment_type=payment_type,
            amount=amount,
            authorized_by=authorized_by,
            invoice_id=inv.id,
            invoice_path=None
        )
        db.session.add(payment)
        db.session.commit()

        return jsonify({
            'success': True,
            'invoice_id': inv.id,
            'bill_number': payment.bill_number,
            'payment_date': payment.payment_date.strftime('%Y-%m-%d %H:%M:%S'),
            'payment_type': payment.payment_type,
            'amount': payment.amount,
            'authorized_by': payment.authorized_by,
            'invoice_path': None
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

@admin_bp.route('/proforma-invoice/<int:user_id>')
@admin_required
def proforma_invoice(user_id):
    user = User.query.get_or_404(user_id)
    pkgs = Package.query.filter_by(user_id=user_id, invoice_id=None).order_by(Package.created_at.asc()).all()

    items = []
    totals = dict(duty=0, scf=0, envl=0, caf=0, gct=0, stamp=0, freight=0, handling=0, grand_total=0)
    for p in pkgs:
        desc = p.description or "Miscellaneous"
        wt   = float(p.weight or 0)
        val  = float(getattr(p, "value", 0) or 0)
        ch   = calculate_charges(desc, val, wt)

        items.append({
            "house_awb": p.house_awb,
            "description": desc,
            "weight": wt,
            "value_usd": val,
            **ch
        })
        for k in totals:
            totals[k] += float(ch.get(k, 0) or 0)

    invoice_dict = {
        "id": None,
        "number": f"PROFORMA-{user.id}",
        "date": datetime.utcnow(),
        "customer_code": getattr(user, "registration_number", ""),
        "customer_name": getattr(user, "full_name", ""),
        "subtotal": totals["grand_total"],
        "total_due": totals["grand_total"],
        "packages": [{
            "house_awb": i["house_awb"],
            "description": i["description"],
            "weight": i["weight"],
            "value": i["value_usd"],
            "freight": i.get("freight", 0),
            "storage": i.get("storage", 0),
            "duty": i.get("duty", 0),
            "scf": i.get("scf", 0),
            "envl": i.get("envl", 0),
            "caf": i.get("caf", 0),
            "gct": i.get("gct", 0),
            "other_charges": i.get("other_charges", 0),
            "discount_due": i.get("discount_due", 0),
        } for i in items]
    }
    return render_template("admin/invoices/_invoice_inline.html",
                           invoice=invoice_dict, USD_TO_JMD=USD_TO_JMD)

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
        inv = Invoice(user_id=int(uid), date_submitted=datetime.utcnow(), status='Pending')
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

    packages = []
    for p in Package.query.filter_by(invoice_id=invoice_id).all():
        packages.append({
            "id": p.id,
            "house_awb": p.house_awb,
            "description": p.description,
            "merchant": getattr(p, "merchant", None),
            "weight": float(p.weight or 0),
            "value_usd": float(getattr(p, "value", 0) or 0),
            "amount_due": float(getattr(p, "amount_due", 0) or 0),
        })

    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_pg(invoice_id)

    invoice_dict = {
        "id": inv.id,
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
    p = Package.query.get_or_404(package_id)
    desc   = p.description or getattr(p, "category", "Miscellaneous")
    weight = float(p.weight or 0)
    value  = float(getattr(p, "value", 0) or 0)

    ch = calculate_charges(desc, value, weight)
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

    inv = Invoice.query.get_or_404(invoice_id)
    p = Payment(
        bill_number=f'BILL-{inv.id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}',
        payment_date=datetime.utcnow(),
        payment_type=payment_type,
        amount=amount,
        authorized_by=authorized_by,
        invoice_id=inv.id,
        invoice_path=None
    )
    db.session.add(p)
    db.session.commit()
    flash("Payment recorded.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))


@admin_bp.route("/invoice/add-discount/<int:invoice_id>", methods=["POST"])
@admin_required
def add_discount(invoice_id):
    amount = float(request.form.get("amount_jmd", 0))
    if amount <= 0:
        flash("Discount amount must be greater than 0.", "warning")
        return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

    inv = Invoice.query.get_or_404(invoice_id)
    cur = float(getattr(inv, "discount_total", 0) or 0)
    inv.discount_total = cur + amount
    db.session.commit()

    flash("Discount added.", "success")
    return redirect(url_for("admin.view_invoice", invoice_id=invoice_id))

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

    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_pg(invoice_id)

    invoice_dict = {
        "id": inv.id,
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

    subtotal, discount_total, payments_total, total_due = _fetch_invoice_totals_pg(invoice_id)

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
        current_user.full_name = form.name.data.strip()
        current_user.email = form.email.data.strip()
        pw = form.password.data.strip() if hasattr(form, 'password') else ''
        if pw:
            import bcrypt
            current_user.password = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for('admin.admin_profile'))

    return render_template('admin/admin_profile.html', form=form, admin=current_user)
