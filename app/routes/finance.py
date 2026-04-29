import os
import uuid
import base64
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import current_app, abort
from flask import send_file
from io import BytesIO
from urllib.parse import quote

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, abort, current_app, make_response
from datetime import datetime, date, timedelta, timezone
from calendar import monthrange
from weasyprint import HTML

from flask_login import current_user
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email

from sqlalchemy import func, or_
import sqlalchemy as sa

from app.forms import LoginForm, ExpenseForm
from app.routes.admin_auth_routes import admin_required
from app.calculator_data import USD_TO_JMD

from app.extensions import db
from app.models import Invoice, Expense, User, ExpenseAuditLog, Package, Payment, EmployeePayroll, PayrollRun, PayrollItem
import cloudinary
import cloudinary.uploader

from app.utils.invoice_totals import fetch_invoice_totals_pg, mark_invoice_packages_delivered
from app.utils.email_utils import send_email, EMAIL_FROM, EMAIL_ADDRESS


finance_bp = Blueprint('finance', __name__, url_prefix='/finance')

def _cloudinary_ready():
    return all([
        current_app.config.get("CLOUDINARY_CLOUD_NAME"),
        current_app.config.get("CLOUDINARY_API_KEY"),
        current_app.config.get("CLOUDINARY_API_SECRET"),
    ])

def _init_cloudinary_from_config():
    cloudinary.config(
        cloud_name=current_app.config["CLOUDINARY_CLOUD_NAME"],
        api_key=current_app.config["CLOUDINARY_API_KEY"],
        api_secret=current_app.config["CLOUDINARY_API_SECRET"],
        secure=True,
    )

def _upload_expense_attachment_to_cloudinary(file_storage):
    """
    Accepts: PDF, JPG, JPEG, PNG
    Uploads:
      - PDF => resource_type='raw'
      - Images => resource_type='image'
    Returns: (original_name, secure_url, public_id, mime)
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None, None, None

    filename = secure_filename(file_storage.filename)
    ext = (Path(filename).suffix or "").lower()

    allowed = {".pdf", ".jpg", ".jpeg", ".png"}
    if ext not in allowed:
        raise ValueError("Only PDF, JPG, JPEG, PNG files are allowed.")

    if not _cloudinary_ready():
        raise ValueError("Cloudinary env vars missing. Set CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.")

    _init_cloudinary_from_config()

    resource_type = "raw" if ext == ".pdf" else "image"

    res = cloudinary.uploader.upload(
        file_storage,
        resource_type=resource_type,
        folder="fafl/expenses",
        public_id=f"expense_{uuid.uuid4().hex}",
        use_filename=False,
        unique_filename=False,
    )

    if ext == ".pdf":
        mime = "application/pdf"
    elif ext == ".png":
        mime = "image/png"
    else:
        mime = "image/jpeg"

    return (
        filename,
        res.get("secure_url"),
        res.get("public_id"),
        mime,
    )

def _delete_cloudinary_asset(public_id: str, mime: str | None = None):
    if not public_id or not _cloudinary_ready():
        return
    _init_cloudinary_from_config()

    resource_type = "image" if (mime or "").lower().startswith("image/") else "raw"
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
    except Exception:
        pass


def _month_bounds(ym: str):
    y, m = map(int, ym.split('-'))
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    return start.isoformat(), end.isoformat()


# Small helpers for normalized invoice expressions
def _invoice_paid_amount_expr():
    # Prefer grand_total, then amount, then amount_due
    return func.coalesce(
        Invoice.grand_total,
        Invoice.amount,
        Invoice.amount_due,
        0.0
    )


def _invoice_due_amount_expr():
    # Prefer grand_total, then amount_due, then amount
    return func.coalesce(
        Invoice.grand_total,
        Invoice.amount_due,
        Invoice.amount,
        0.0
    )

def _invoice_issued_date_expr():
    # COALESCE(i.date_issued, i.date_submitted, i.created_at)
    return func.coalesce(Invoice.date_issued, Invoice.date_submitted, Invoice.created_at)


def _invoice_paid_date_expr():
    # COALESCE(i.date_paid, i.created_at)
    return func.coalesce(Invoice.date_paid, Invoice.created_at)

def _ensure_expense_upload_folder():
    folder = current_app.config.get("EXPENSE_UPLOAD_FOLDER")
    if not folder:
        # default to instance folder (safe, not publicly served)
        folder = os.path.join(current_app.instance_path, "expense_uploads")
        current_app.config["EXPENSE_UPLOAD_FOLDER"] = folder
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_expense_pdf(file_storage):
    """
    Saves a PDF to disk and returns:
      (original_name, stored_name, mime)
    """
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None, None

    filename = secure_filename(file_storage.filename)
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are allowed.")

    folder = _ensure_expense_upload_folder()
    stored = f"expense_{uuid.uuid4().hex}.pdf"
    path = os.path.join(folder, stored)

    file_storage.save(path)

    mime = file_storage.mimetype or "application/pdf"
    return filename, stored, mime


def _log_expense_action(action: str, expense, request_obj):
    """
    Writes an audit log snapshot for CREATED/UPDATED/DELETED.
    """
    from app.models import ExpenseAuditLog  # avoid circular import

    actor_id = getattr(current_user, "id", None)
    actor_email = getattr(current_user, "email", None)
    actor_role = getattr(current_user, "role", None)

    log = ExpenseAuditLog(
        expense_id=getattr(expense, "id", None),
        action=action,
        actor_id=actor_id,
        actor_email=actor_email,
        actor_role=actor_role,
        expense_date=getattr(expense, "date", None),
        expense_category=getattr(expense, "category", None),
        expense_amount=float(getattr(expense, "amount", 0.0) or 0.0),
        expense_description=getattr(expense, "description", None),
        expense_attachment_name=getattr(expense, "attachment_name", None),
        expense_attachment_stored=getattr(expense, "attachment_url", None),
        ip_address=(request_obj.headers.get("X-Forwarded-For", request_obj.remote_addr) or "")[:64],
        user_agent=(request_obj.headers.get("User-Agent", "") or "")[:255],
    )
    db.session.add(log)

def _money(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0

def _refund_expense_total(start_date=None, end_date=None):
    """
    Sum completed refund payments so they can be treated as finance expenses.
    Includes:
      - package_refund
      - delivery_refund
    """
    q = (
        db.session.query(func.coalesce(func.sum(Payment.amount_jmd), 0.0))
        .filter(Payment.transaction_type.in_(["package_refund", "delivery_refund"]))
        .filter(Payment.status == "completed")
    )

    if start_date:
        q = q.filter(func.date(Payment.created_at) >= start_date)

    if end_date:
        q = q.filter(func.date(Payment.created_at) <= end_date)

    return float(q.scalar() or 0.0)

def _get_unpaid_user_rows(search=None, date_from=None, date_to=None):
    # sum payments per invoice
    pay_sum = (
        db.session.query(
            Payment.invoice_id.label("inv_id"),
            func.coalesce(func.sum(Payment.amount_jmd), 0).label("paid_jmd"),
        )
        .group_by(Payment.invoice_id)
        .subquery()
    )

    # owed = (grand_total OR amount_due OR amount) - payments
    billed_expr = func.coalesce(Invoice.grand_total, Invoice.amount_due, Invoice.amount, 0.0)
    owed_expr = func.greatest(billed_expr - func.coalesce(pay_sum.c.paid_jmd, 0), 0)

    inv_q = (
        db.session.query(
            Invoice.user_id.label("user_id"),
            func.count(Invoice.id).label("unpaid_count"),
            func.coalesce(func.sum(owed_expr), 0).label("unpaid_total"),
        )
        .outerjoin(pay_sum, pay_sum.c.inv_id == Invoice.id)
        .filter(func.lower(Invoice.status).in_(("pending", "unpaid", "issued")))
        .group_by(Invoice.user_id)
    )

    # optional date filter (uses invoice created_at; change if you prefer date_issued)
    if date_from:
        inv_q = inv_q.filter(func.date(Invoice.created_at) >= date_from)
    if date_to:
        inv_q = inv_q.filter(func.date(Invoice.created_at) <= date_to)

    inv_sub = inv_q.subquery()

    q = (
        db.session.query(
            User.full_name,
            User.registration_number,
            User.email,
            User.mobile,
            inv_sub.c.unpaid_count,
            inv_sub.c.unpaid_total,
        )
        .join(inv_sub, inv_sub.c.user_id == User.id)
    )

    if search:
        like = f"%{search.strip()}%"
        q = q.filter(
            or_(
                User.full_name.ilike(like),
                User.email.ilike(like),
                User.registration_number.ilike(like),
            )
        )

    rows = []
    for r in q.order_by(User.full_name.asc()).all():
        rows.append({
            "name": r.full_name,
            "reg": r.registration_number,
            "email": r.email,
            "mobile": r.mobile,
            "unpaid_count": int(r.unpaid_count or 0),
            "unpaid_total": _money(r.unpaid_total),
        })

    grand_total = sum(x["unpaid_total"] for x in rows)
    total_customers_with_balance = len([x for x in rows if x["unpaid_total"] > 0])

    return rows, grand_total, total_customers_with_balance



def _send_unpaid_report_email(pdf_bytes: bytes, generated_at: str):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    import smtplib

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pwd  = os.getenv("SMTP_PASS", "")
    from_email = os.getenv("FROM_EMAIL", user)

    to_list = [x.strip() for x in (os.getenv("FINANCE_REPORT_EMAILS", "") or "").split(",") if x.strip()]
    if not to_list:
        current_app.logger.warning("FINANCE_REPORT_EMAILS not set; skipping report email.")
        return

    msg = MIMEMultipart()
    msg["Subject"] = f"FAFL Weekly Unpaid Invoices Report ({generated_at})"
    msg["From"] = from_email
    msg["To"] = ", ".join(to_list)

    msg.attach(MIMEText("Attached is the latest Unpaid Invoices Report.\n\n- FAFL System", "plain"))

    attach = MIMEApplication(pdf_bytes, _subtype="pdf")
    attach.add_header("Content-Disposition", "attachment", filename="unpaid_users_report.pdf")
    msg.attach(attach)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        if user and pwd:
            s.login(user, pwd)
        s.sendmail(from_email, to_list, msg.as_string())


def _static_image_data_uri(filename: str) -> str | None:
    """
    Returns a data: URI for a static image so WeasyPrint can render it reliably.
    """
    try:
        static_dir = Path(current_app.root_path) / "static"
        path = static_dir / filename
        if not path.exists():
            return None

        ext = path.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else None
        if not mime:
            return None

        b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


# ---------------------- FINANCE DASHBOARD ---------------------- #
@finance_bp.route('/dashboard')
@admin_required(roles=['finance'])
def finance_dashboard():
    # ---- Period ----
    ym = request.args.get('month')          # 'YYYY-MM'
    start = request.args.get('start')       # 'YYYY-MM-DD'
    end = request.args.get('end')           # 'YYYY-MM-DD'

    if ym and not (start or end):
        start, end = _month_bounds(ym)
    elif not (start and end):
        now_ym = datetime.now().strftime('%Y-%m')
        start, end = _month_bounds(now_ym)

    # Convert to date objects for filters
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    issued_date_expr = _invoice_issued_date_expr()
    paid_date_expr = _invoice_paid_date_expr()
    amt_paid_expr = _invoice_paid_amount_expr()
    amt_due_expr = _invoice_due_amount_expr()

    open_statuses = ['pending', 'issued', 'unpaid', 'partial']

    # ---- KPIs ----

    # Total paid in period
    total_paid = (
        db.session.query(
            func.coalesce(
                func.sum(amt_paid_expr),
                0.0
            )
        )
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(start_date, end_date))
        .scalar() or 0.0
    )

   
    manual_expenses = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
        .filter(func.date(Expense.date).between(start_date, end_date))
        .scalar() or 0.0
    )

    refund_expenses = _refund_expense_total(start_date, end_date)

    total_expenses = float(manual_expenses or 0.0) + float(refund_expenses or 0.0)

    # Receivables for the period
    total_amount_due = (
        db.session.query(
            func.coalesce(
                func.sum(amt_due_expr),
                0.0
            )
        )
        .filter(amt_due_expr > 0)
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .scalar() or 0.0
    )

    # All-time outstanding
    total_amount_due_all = (
        db.session.query(
            func.coalesce(
                func.sum(amt_due_expr),
                0.0
            )
        )
        .filter(amt_due_expr > 0)
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .scalar() or 0.0
    )

    net = total_paid - total_expenses

    # ---- Charts ----

    # Paid trend (daily)
    paid_trend_rows = (
        db.session.query(
            func.date(paid_date_expr).label('d'),
            func.coalesce(func.sum(amt_paid_expr), 0.0).label('total'),
        )
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(start_date, end_date))
        .group_by(func.date(paid_date_expr))
        .order_by(func.date(paid_date_expr))
        .all()
    )
    paid_labels = [
        (r.d.isoformat() if isinstance(r.d, (datetime, date)) else str(r.d))
        for r in paid_trend_rows
    ]
    paid_values = [float(r.total or 0) for r in paid_trend_rows]

    # Expense mix by category
    expense_mix_rows = (
        db.session.query(
            Expense.category.label('category'),
            func.coalesce(func.sum(Expense.amount), 0.0).label('total'),
        )
        .filter(func.date(Expense.date).between(start_date, end_date))
        .group_by(Expense.category)
        .order_by(func.coalesce(func.sum(Expense.amount), 0.0).desc())
        .all()
    )

    exp_map = {str(r.category): float(r.total or 0) for r in expense_mix_rows}

    refund_mix_rows = (
        db.session.query(
            Payment.transaction_type.label("category"),
            func.coalesce(func.sum(Payment.amount_jmd), 0.0).label("total"),
        )
        .filter(Payment.transaction_type.in_(["package_refund", "delivery_refund"]))
        .filter(Payment.status == "completed")
        .filter(func.date(Payment.created_at).between(start_date, end_date))
        .group_by(Payment.transaction_type)
        .all()
    )

    for r in refund_mix_rows:
        if r.category == "package_refund":
            label = "Package Refund"
        elif r.category == "delivery_refund":
            label = "Delivery Refund"
        else:
            label = "Refund"

        exp_map[label] = exp_map.get(label, 0.0) + float(r.total or 0)

    exp_labels = [r.category for r in expense_mix_rows]
    exp_values = [float(r.total or 0) for r in expense_mix_rows]

    # A/R aging – do buckets in Python for portability
    open_invoices = (
        db.session.query(Invoice)
        .filter(amt_due_expr > 0)
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .all()
    )
    today = date.today()
    aging = {'0-30': 0.0, '31-60': 0.0, '61-90': 0.0, '91+': 0.0}

    for inv in open_invoices:
        base_dt = inv.date_issued or inv.date_submitted or inv.created_at
        if not base_dt:
            continue
        base_date = base_dt.date() if isinstance(base_dt, datetime) else base_dt
        days = (today - base_date).days
        # Prefer outstanding if present, otherwise fall back to full billed
        due_amt = float(inv.amount_due or inv.grand_total or inv.amount or 0.0)

        if days <= 30:
            aging['0-30'] += due_amt
        elif 31 <= days <= 60:
            aging['31-60'] += due_amt
        elif 61 <= days <= 90:
            aging['61-90'] += due_amt
        else:
            aging['91+'] += due_amt

    # Top customers (paid)
    top_customers_rows = (
        db.session.query(
            User.full_name.label('customer'),
            func.coalesce(func.sum(amt_paid_expr), 0.0).label('total'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(start_date, end_date))
        .group_by(User.id, User.full_name)
        .order_by(func.coalesce(func.sum(amt_paid_expr), 0.0).desc())
        .limit(5)
        .all()
    )
    top_customers = [
        {'customer': r.customer, 'total': float(r.total or 0)} for r in top_customers_rows
    ]

    # Tables: paid & due
    paid_rows_raw = (
        db.session.query(
            Invoice.id.label('invoice_id'),
            Invoice.invoice_number,
            User.full_name.label('customer'),
            amt_paid_expr.label('amount'),
            func.date(paid_date_expr).label('date_paid'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(start_date, end_date))
        .order_by(func.date(paid_date_expr).desc())
        .all()
    )
    paid_rows = [
        {
            'invoice_id': r.invoice_id,
            'invoice_number': r.invoice_number,
            'customer': r.customer,
            'amount': float(r.amount or 0),
            'date_paid': r.date_paid,
        }
        for r in paid_rows_raw
    ]

    due_rows_raw = (
        db.session.query(
            Invoice.id.label('invoice_id'),
            Invoice.invoice_number,
            User.full_name.label('customer'),
            amt_due_expr.label('amount_due'),
            func.date(issued_date_expr).label('date_issued'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(amt_due_expr > 0)
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .order_by(func.date(issued_date_expr).desc())
        .all()
    )
    due_rows = [
        {
            'invoice_id': r.invoice_id,
            'invoice_number': r.invoice_number,
            'customer': r.customer,
            'amount_due': float(r.amount_due or 0),
            'date_issued': r.date_issued,
        }
        for r in due_rows_raw
    ]

    user_role = getattr(current_user, 'role', 'Admin')
    return render_template(
        'admin/finance/finance_dashboard.html',
        start=start,
        end=end,
        total_paid=total_paid,
        total_expenses=total_expenses,
        total_amount_due=total_amount_due,
        total_amount_due_all=total_amount_due_all,
        net=net,
        paid_labels=paid_labels,
        paid_values=paid_values,
        exp_labels=exp_labels,
        exp_values=exp_values,
        aging=aging,
        top_customers=top_customers,
        paid_rows=paid_rows,
        due_rows=due_rows,
        usd_to_jmd=USD_TO_JMD,
        user_role=user_role,
        manual_expenses=manual_expenses,
        refund_expenses=refund_expenses,
    )


# ---------------------- UNPAID INVOICES ---------------------- #
@finance_bp.route('/unpaid_invoices')
@admin_required(roles=['finance'])
def unpaid_invoices():
    start = (request.args.get('start') or '').strip()
    end = (request.args.get('end') or '').strip()
    q = (request.args.get('q') or '').strip()

    # ✅ include pending again
    status = (request.args.get('status') or 'issued,unpaid,pending,partial').strip().lower()
    min_due_raw = (request.args.get('min_due') or '').strip()
    max_due_raw = (request.args.get('max_due') or '').strip()

    # ✅ status list from dropdown (now includes pending)
    allowed = {'issued', 'unpaid', 'pending', 'partial'}
    status_list = [s for s in (t.strip() for t in status.split(',')) if s in allowed]
    if not status_list:
        status_list = ['issued', 'unpaid', 'pending', 'partial']

    # ✅ stable string so dropdown stays selected
    if set(status_list) == {"issued", "unpaid", "pending", "partial"}:
        status_selected = "issued,unpaid,pending,partial"
    elif set(status_list) == {"issued", "unpaid"}:
        status_selected = "issued,unpaid"
    else:
        status_selected = status_list[0]

    # parse min/max safely
    min_due = None
    max_due = None
    try:
        if min_due_raw != '':
            min_due = float(min_due_raw)
    except ValueError:
        min_due = None
    try:
        if max_due_raw != '':
            max_due = float(max_due_raw)
    except ValueError:
        max_due = None

    issued_date_expr = _invoice_issued_date_expr()
    amt_due_expr = func.coalesce(
        Invoice.amount_due,
        Invoice.grand_total,
        Invoice.amount,
        0.0
    )

    query = (
        db.session.query(
            Invoice.id.label('invoice_id'),
            Invoice.invoice_number,
            User.full_name.label('customer'),
            User.registration_number,
            Invoice.status,
            amt_due_expr.label('amount_due'),
            func.date(issued_date_expr).label('date_issued'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(amt_due_expr > 0)
        .filter(func.lower(Invoice.status).in_(status_list))
    )

    # ✅ IMPORTANT: only apply date filter if user actually chose it
    if start and end:
        start_date = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
        query = query.filter(func.date(issued_date_expr).between(start_date, end_date))
    else:
        # so template inputs don't look broken
        start = ''
        end = ''

    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(func.coalesce(User.full_name, '')).like(like),
                func.lower(func.coalesce(User.registration_number, '')).like(like),
                func.lower(func.coalesce(Invoice.invoice_number, '')).like(like),
            )
        )

    if min_due is not None:
        query = query.filter(amt_due_expr >= min_due)
    if max_due is not None:
        query = query.filter(amt_due_expr <= max_due)

    invoices_raw = query.order_by(func.date(issued_date_expr).desc()).all()

    invoices = [
        {
            'invoice_id': r.invoice_id,
            'invoice_number': r.invoice_number,
            'customer': r.customer,
            'registration_number': r.registration_number,
            'status': r.status,
            'amount_due': float(r.amount_due or 0),
            'date_issued': r.date_issued,
        }
        for r in invoices_raw
    ]

    total_due = sum(r['amount_due'] for r in invoices)

    # ✅ counts for all outstanding by status
    status_counts_rows = (
        db.session.query(func.lower(Invoice.status).label('s'), func.count(Invoice.id).label('cnt'))
        .filter(amt_due_expr > 0)
        .group_by(func.lower(Invoice.status))
        .all()
    )
    status_counts = {r.s: r.cnt for r in status_counts_rows}

    return render_template(
        'admin/finance/unpaid_invoices.html',
        invoices=invoices,
        total_due=total_due,
        start=start,
        end=end,
        q=q,
        status_selected=status_selected,
        min_due=min_due_raw,
        max_due=max_due_raw,
        current_date=date.today(),
        status_counts=status_counts,
    )


@finance_bp.route("/reports/unpaid-users.pdf", methods=["GET"])
@admin_required(roles=["finance"])
def unpaid_users_pdf():
    """
    - If accessed by a logged-in finance user: works normally.
    - If accessed by cron: allow only when token matches REPORT_CRON_TOKEN.
    """
    token = (request.args.get("token") or "").strip()
    cron_token = (os.getenv("REPORT_CRON_TOKEN") or "").strip()

    # ✅ If token matches, allow without login
    if not (cron_token and token == cron_token):
        # otherwise require finance login
        # (this reuse avoids duplicating the whole route)
        return admin_required(roles=["finance"])(lambda: _render_unpaid_users_pdf())()

    return _render_unpaid_users_pdf(send_email=(request.args.get("send") == "1"))


def _render_unpaid_users_pdf(send_email: bool = False):
    search = (request.args.get("search") or "").strip() or None
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None

    rows, grand_total, total_customers_with_balance = _get_unpaid_user_rows(search, date_from, date_to)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Settings (logo + rates)
    from app.models import Settings
    settings = Settings.query.get(1)

    effective_usd_to_jmd = (getattr(settings, "usd_to_jmd", None) or USD_TO_JMD)

    # ---- logo url (robust) ----
    raw_logo = (settings.logo_path if settings and settings.logo_path else "logo.png") or "logo.png"
    # normalize: remove leading /, and remove leading "static/"
    raw_logo = raw_logo.lstrip("/")
    if raw_logo.lower().startswith("static/"):
        raw_logo = raw_logo[7:]

    # Try embed first (best for WeasyPrint)
    logo_data_uri = _static_image_data_uri(raw_logo)

    # Fallback to URL (browser works, but WeasyPrint sometimes can't fetch it)
    logo_url = url_for("static", filename=raw_logo, _external=True, _scheme="https")

    html = render_template(
        "admin/finance/unpaid_users_report.html",
        rows=rows,
        grand_total=grand_total,
        total_customers_with_balance=total_customers_with_balance,
        search=search,
        date_from=date_from,
        date_to=date_to,
        generated_at=generated_at,
        logo_url=logo_url,
        logo_data_uri=logo_data_uri,
    )

    pdf = HTML(string=html, base_url=request.url_root).write_pdf()

    if send_email:
        _send_unpaid_report_email(pdf_bytes=pdf, generated_at=generated_at)

    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = 'inline; filename="unpaid_users_report.pdf"'
    return resp



@finance_bp.route("/unpaid_invoices/mark_paid_bulk", methods=["POST"])
@admin_required(roles=["finance"])
def bulk_mark_invoices_paid():
    """
    Bulk mark invoices paid:
    - creates a Payment record for each invoice (for remaining balance)
    - sets invoice.status='paid', invoice.amount_due=0, invoice.date_paid=now (if date_paid exists)
    - sets Package.amount_due=0 and status='delivered' for packages on that invoice
    """

    # ✅ payment method from dropdown
    payment_method = (request.form.get("payment_method") or "Bulk").strip()

    # ✅ support both invoice_ids[] and invoice_ids
    raw_ids = request.form.getlist("invoice_ids[]") or request.form.getlist("invoice_ids")
    invoice_ids = []
    for x in raw_ids:
        try:
            invoice_ids.append(int(x))
        except Exception:
            pass

    if not invoice_ids:
        flash("Select at least one invoice.", "warning")
        return redirect(url_for("finance.unpaid_invoices"))

    now = datetime.now(timezone.utc)
    actor_name = (
        getattr(current_user, "full_name", None)
        or getattr(current_user, "email", None)
        or "Finance"
    )

    # ✅ lock invoice rows (avoid double-paying)
    invoices = (
        db.session.query(Invoice)
        .filter(Invoice.id.in_(invoice_ids))
        .with_for_update()
        .all()
    )

    changed = 0
    skipped = 0

    try:
        for inv in invoices:
            # ✅ compute remaining balance (includes discounts + prior payments)
            # IMPORTANT: make sure fetch_invoice_totals_pg is imported/defined in finance.py
            subtotal, discount_total, payments_total, total_due = fetch_invoice_totals_pg(inv.id)
            balance = float(total_due or 0.0)

            # already paid / nothing due
            if balance <= 0:
                inv.amount_due = 0.0
                inv.status = "paid"
                if hasattr(inv, "date_paid"):
                    inv.date_paid = inv.date_paid or now

                Package.query.filter_by(invoice_id=inv.id).update(
                    {"amount_due": 0.0, "status": "delivered"},
                    synchronize_session=False
                )
                skipped += 1
                continue

            # ✅ Create Payment for remaining balance (matches your Payment model)
            p = Payment(
                invoice_id=inv.id,
                user_id=inv.user_id,
                method=payment_method,  # ✅ FROM DROPDOWN
                amount_jmd=balance,
                reference=f"BULK-{now.strftime('%Y%m%d%H%M%S')}-{inv.id}",
                notes=f"Bulk marked paid by {actor_name}",
                created_at=now,
            )
            db.session.add(p)
            db.session.flush()

            # ✅ recompute after inserting payment
            _s, _d, _p, new_due = fetch_invoice_totals_pg(inv.id)
            inv.amount_due = float(new_due or 0.0)

            if inv.amount_due <= 0:
                inv.amount_due = 0.0
                inv.status = "paid"
                if hasattr(inv, "date_paid"):
                    inv.date_paid = now

                Package.query.filter_by(invoice_id=inv.id).update(
                    {"amount_due": 0.0, "status": "delivered"},
                    synchronize_session=False
                )
            else:
                inv.status = "partial"

            changed += 1

        db.session.commit()
        flash(f"Bulk update complete. Paid: {changed}, Already paid/zero due: {skipped}", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Bulk mark paid failed: {e}", "danger")

    return redirect(url_for("finance.unpaid_invoices"))


# ---------------------- MONTHLY EXPENSES ---------------------- #
@finance_bp.route('/monthly_expenses', methods=['GET', 'POST'])
@admin_required(roles=['finance'])
def monthly_expenses():
    form = ExpenseForm()

    if request.method == "POST" and form.validate_on_submit():
        try:
            attachment_name = None
            attachment_url = None
            attachment_public_id = None
            attachment_mime = None
            uploaded_at = None

            # ✅ Allow PDF / JPG / JPEG / PNG
            file_obj = request.files.get("attachment")
            if file_obj and file_obj.filename:
                attachment_name, attachment_url, attachment_public_id, attachment_mime = (
                    _upload_expense_attachment_to_cloudinary(file_obj)
                )
                uploaded_at = datetime.utcnow()

            new_expense = Expense(
                date=form.date.data,
                category=form.category.data,
                amount=float(form.amount.data),
                description=form.description.data or '',

                # Cloudinary attachment fields
                attachment_name=attachment_name,
                attachment_url=attachment_url,
                attachment_public_id=attachment_public_id,
                attachment_mime=attachment_mime,
                attachment_uploaded_at=uploaded_at,
            )

            db.session.add(new_expense)
            db.session.flush()

            # ✅ audit log
            _log_expense_action("CREATED", new_expense, request)

            db.session.commit()
            flash('Expense added successfully.', 'success')
            return redirect(url_for('finance.monthly_expenses'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error adding expense: {e}', 'danger')

    expenses_q = Expense.query.order_by(Expense.date.desc(), Expense.id.desc()).all()
    expenses = []
    for e in expenses_q:
        expenses.append({
            'id': f"expense-{e.id}",
            'row_type': 'expense',
            'date': e.date,
            'category': e.category,
            'amount': float(e.amount or 0),
            'description': e.description,
            'attachment_name': getattr(e, "attachment_name", None),
            'attachment_url': getattr(e, "attachment_url", None),
            'attachment_mime': getattr(e, "attachment_mime", None),
            'has_attachment': bool(getattr(e, "attachment_url", None)),
        })

    # -----------------------------
    # Refund payments as expenses
    # -----------------------------
    refund_rows = (
        Payment.query
        .filter(Payment.transaction_type.in_(["package_refund", "delivery_refund"]))
        .filter(Payment.status == "completed")
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .all()
    )

    for p in refund_rows:
        if p.transaction_type == "package_refund":
            category = "Package Refund"
        elif p.transaction_type == "delivery_refund":
            category = "Delivery Refund"
        else:
            category = "Refund"

        desc_bits = []

        if getattr(p, "reference", None):
            desc_bits.append(f"Ref: {p.reference}")

        if getattr(p, "notes", None):
            desc_bits.append(p.notes)

        expenses.append({
            'id': f"refund-{p.id}",
            'row_type': 'refund',
            'date': (p.created_at.date() if p.created_at else None),
            'category': category,
            'amount': float(p.amount_jmd or 0),
            'description': " | ".join(desc_bits) if desc_bits else "Refund payment",
            'attachment_name': None,
            'attachment_url': None,
            'attachment_mime': None,
            'has_attachment': False,
        })

    # newest first across both manual expenses + refunds
    expenses.sort(
        key=lambda x: (x.get("date") or date.min, str(x.get("id") or "")),
        reverse=True
    )

    total_expenses = sum(float(e.get('amount') or 0) for e in expenses) if expenses else 0.0

    return render_template(
        'admin/finance/monthly_expenses.html',
        form=form,
        expenses=expenses,
        total_expenses=total_expenses,
    )


# ---------------------- OPEN ATTACHMENT (Cloudinary redirect) ---------------------- #
@finance_bp.route('/expenses/<int:expense_id>/attachment')
@admin_required(roles=['finance'])
def download_expense_attachment(expense_id):
    expense = db.session.get(Expense, expense_id)
    if not expense or not getattr(expense, "attachment_url", None):
        abort(404)

    # ✅ Redirect straight to Cloudinary
    return redirect(expense.attachment_url)


# ---------------------- DELETE EXPENSE (Cloudinary delete) ---------------------- #
@finance_bp.route('/expenses/delete/<int:expense_id>', methods=['POST'])
@admin_required(roles=['finance'])
def delete_expense(expense_id):
    try:
        expense = db.session.get(Expense, expense_id)
        if not expense:
            flash("Expense not found.", "danger")
            return redirect(url_for('finance.monthly_expenses'))

        # ✅ audit log BEFORE delete
        _log_expense_action("DELETED", expense, request)

        # ✅ delete Cloudinary asset
        public_id = getattr(expense, "attachment_public_id", None)
        mime = getattr(expense, "attachment_mime", None)
        if public_id:
            _delete_cloudinary_asset(public_id, mime)

        db.session.delete(expense)
        db.session.commit()
        flash("Expense deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting expense: {e}", "danger")

    return redirect(url_for('finance.monthly_expenses'))

# ---------------------- ADD EXPENSE ---------------------- #
@finance_bp.route('/expenses/add', methods=['GET', 'POST'])
@admin_required
def add_expense():
    return redirect(url_for('finance.monthly_expenses'))


# ---------------------- VIEW EXPENSES ---------------------- #
@finance_bp.route('/expenses')
@admin_required
def view_expenses():
    return redirect(url_for('finance.monthly_expenses'))

# ---------------------- EDIT EXPENSE ---------------------- #
@finance_bp.route('/expenses/edit/<int:expense_id>', methods=['GET', 'POST'])
@admin_required(roles=['finance'])
def edit_expense(expense_id):
    expense = db.session.get(Expense, expense_id)
    if not expense:
        flash("Expense not found.", "danger")
        return redirect(url_for('finance.monthly_expenses'))

    form = ExpenseForm(obj=expense)

    if request.method == 'GET':
        form.date.data = expense.date
        form.category.data = expense.category
        form.amount.data = expense.amount
        form.description.data = expense.description

    if form.validate_on_submit():
        try:
            expense.date = form.date.data
            expense.category = form.category.data
            expense.amount = float(form.amount.data)
            expense.description = form.description.data or ''

            # ✅ Optional: replace attachment
            file_obj = request.files.get("attachment")
            if file_obj and file_obj.filename:
                # delete old cloudinary file first
                old_public_id = getattr(expense, "attachment_public_id", None)
                old_mime = getattr(expense, "attachment_mime", None)
                if old_public_id:
                    _delete_cloudinary_asset(old_public_id, old_mime)

                attachment_name, attachment_url, attachment_public_id, attachment_mime = (
                    _upload_expense_attachment_to_cloudinary(file_obj)
                )

                expense.attachment_name = attachment_name
                expense.attachment_url = attachment_url
                expense.attachment_public_id = attachment_public_id
                expense.attachment_mime = attachment_mime
                expense.attachment_uploaded_at = datetime.utcnow()

            db.session.flush()

            # ✅ audit log
            _log_expense_action("UPDATED", expense, request)

            db.session.commit()
            flash("Expense updated successfully.", "success")
            return redirect(url_for('finance.monthly_expenses'))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating expense: {e}", "danger")

    return render_template(
        'admin/finance/edit_expense.html',
        form=form,
        expense=expense
    )

@finance_bp.route("/expense_audit_logs")
@admin_required(roles=["finance"])
def expense_audit_logs():
    # Filters
    start = request.args.get("start", "").strip()   # YYYY-MM-DD
    end = request.args.get("end", "").strip()       # YYYY-MM-DD
    q = request.args.get("q", "").strip()
    action = request.args.get("action", "").strip().upper()  # CREATED/UPDATED/DELETED

    query = ExpenseAuditLog.query

    # date range filter (created_at)
    if start:
        try:
            start_dt = datetime.fromisoformat(start)
            query = query.filter(ExpenseAuditLog.created_at >= start_dt)
        except Exception:
            pass

    if end:
        try:
            # include whole end day
            end_dt = datetime.fromisoformat(end) + timedelta(days=1)
            query = query.filter(ExpenseAuditLog.created_at < end_dt)
        except Exception:
            pass

    # action filter
    if action in ("CREATED", "UPDATED", "DELETED"):
        query = query.filter(ExpenseAuditLog.action == action)

    # text search (actor, category, description, expense_id)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(func.coalesce(ExpenseAuditLog.actor_email, "")).like(like),
                func.lower(func.coalesce(ExpenseAuditLog.actor_role, "")).like(like),
                func.lower(func.coalesce(ExpenseAuditLog.expense_category, "")).like(like),
                func.lower(func.coalesce(ExpenseAuditLog.expense_description, "")).like(like),
                func.cast(ExpenseAuditLog.expense_id, db.String).like(f"%{q}%"),
            )
        )

    logs = query.order_by(ExpenseAuditLog.created_at.desc()).limit(500).all()

    return render_template(
        "admin/finance/expense_audit_logs.html",
        logs=logs,
        start=start,
        end=end,
        q=q,
        action_selected=action,
    )

# ---------------------- MONTHLY INCOME ---------------------- #
@finance_bp.route('/monthly-income')
@admin_required(roles=['finance'])
def monthly_income():
    today = date.today()
    default_start = date(today.year, today.month, 1)
    default_end = date(today.year, today.month, monthrange(today.year, today.month)[1])

    start = (request.args.get("start") or default_start.isoformat()).strip()
    end = (request.args.get("end") or default_end.isoformat()).strip()
    q = (request.args.get("q") or "").strip()
    method = (request.args.get("method") or "").strip()

    try:
        start_date = datetime.fromisoformat(start).date()
    except Exception:
        start_date = default_start
        start = default_start.isoformat()

    try:
        end_date = datetime.fromisoformat(end).date()
    except Exception:
        end_date = default_end
        end = default_end.isoformat()

    amt_due_expr = func.coalesce(
        Invoice.amount_due,
        Invoice.grand_total,
        Invoice.amount,
        0.0
    )
    issued_date_expr = _invoice_issued_date_expr()
    open_statuses = ['pending', 'issued', 'unpaid', 'partial']

    payments_query = (
        db.session.query(
            Payment.id.label("payment_id"),
            Payment.created_at.label("date_paid"),
            Payment.amount_jmd.label("amount"),
            Payment.method,
            Payment.reference,
            Payment.notes,
            Payment.status,
            Payment.transaction_type,
            Invoice.id.label("invoice_id"),
            Invoice.invoice_number,
            Invoice.status.label("invoice_status"),
            Invoice.amount_due.label("invoice_amount_due"),
            User.full_name.label("customer_name"),
            User.registration_number,
        )
        .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
        .outerjoin(User, User.id == Payment.user_id)
        .filter(func.date(Payment.created_at).between(start_date, end_date))
        .filter(func.lower(func.coalesce(Payment.status, "completed")) == "completed")
    )

    if q:
        like = f"%{q.lower()}%"
        payments_query = payments_query.filter(
            or_(
                func.lower(func.coalesce(User.full_name, "")).like(like),
                func.lower(func.coalesce(User.registration_number, "")).like(like),
                func.lower(func.coalesce(Invoice.invoice_number, "")).like(like),
                func.lower(func.coalesce(Payment.reference, "")).like(like),
                func.lower(func.coalesce(Payment.notes, "")).like(like),
            )
        )

    if method:
        payments_query = payments_query.filter(
            func.lower(func.coalesce(Payment.method, "")) == method.lower()
        )

    payments_raw = payments_query.order_by(Payment.created_at.desc()).all()

    incomes = []
    for r in payments_raw:
        incomes.append({
            "payment_id": r.payment_id,
            "invoice_id": r.invoice_id,
            "invoice_number": r.invoice_number or "—",
            "invoice_status": r.invoice_status or "—",
            "invoice_amount_due": float(r.invoice_amount_due or 0),
            "customer_name": r.customer_name or "—",
            "registration_number": r.registration_number or "—",
            "amount": float(r.amount or 0),
            "date_paid": r.date_paid,
            "method": r.method or "—",
            "reference": r.reference or "—",
            "notes": r.notes or "",
            "status": r.status or "completed",
            "transaction_type": r.transaction_type or "invoice_payment",
        })

    total_income = sum(r["amount"] for r in incomes)

    methods = [
        x[0] for x in (
            db.session.query(Payment.method)
            .filter(Payment.method.isnot(None))
            .distinct()
            .order_by(Payment.method.asc())
            .all()
        )
        if x[0]
    ]

    daily_paid_raw = (
        db.session.query(
            func.date(Payment.created_at).label("d"),
            func.coalesce(func.sum(Payment.amount_jmd), 0.0).label("total"),
        )
        .filter(func.date(Payment.created_at).between(start_date, end_date))
        .filter(func.lower(func.coalesce(Payment.status, "completed")) == "completed")
        .group_by(func.date(Payment.created_at))
        .order_by(func.date(Payment.created_at))
        .all()
    )

    chart_labels = [
        r.d.isoformat() if isinstance(r.d, date) else str(r.d)
        for r in daily_paid_raw
    ]
    chart_values = [float(r.total or 0) for r in daily_paid_raw]

    due_rows_query = (
        db.session.query(
            Invoice.id.label("invoice_id"),
            Invoice.invoice_number,
            Invoice.status,
            Invoice.user_id,
            User.full_name.label("customer_name"),
            User.email.label("customer_email"),
            User.registration_number.label("registration_number"),
            User.mobile.label("customer_mobile"),
            amt_due_expr.label("amount_due"),
            func.date(issued_date_expr).label("date_issued"),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(amt_due_expr > 0)
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .filter(func.lower(Invoice.status).in_(open_statuses))
    )

    if q:
        like = f"%{q.lower()}%"
        due_rows_query = due_rows_query.filter(
            or_(
                func.lower(func.coalesce(User.full_name, "")).like(like),
                func.lower(func.coalesce(User.registration_number, "")).like(like),
                func.lower(func.coalesce(User.email, "")).like(like),
                func.lower(func.coalesce(Invoice.invoice_number, "")).like(like),
            )
        )

    due_rows_raw = (
        due_rows_query
        .order_by(func.date(issued_date_expr).desc())
        .all()
    )

    due_rows = []
    today_date = date.today()

    for r in due_rows_raw:
        issued = r.date_issued
        age_days = 0

        if issued:
            issued_date = issued if isinstance(issued, date) else issued.date()
            age_days = (today_date - issued_date).days

        due_rows.append({
            "invoice_id": r.invoice_id,
            "invoice_number": r.invoice_number or f"INV{r.invoice_id:05d}",
            "customer_name": r.customer_name,
            "customer_email": r.customer_email,
            "customer_mobile": "".join(ch for ch in str(r.customer_mobile or "") if ch.isdigit()),
            "registration_number": r.registration_number,
            "amount_due": float(r.amount_due or 0),
            "whatsapp_url": (
                "https://wa.me/"
                + "".join(ch for ch in str(r.customer_mobile or "") if ch.isdigit())
                + "?text="
                + quote(
                    f"Hi {r.customer_name or 'Customer'}, this is a payment reminder from Foreign A Foot Logistics. "
                    f"Invoice {r.invoice_number or f'INV{r.invoice_id:05d}'} has a remaining balance of "
                    f"JMD {float(r.amount_due or 0):,.2f}. Please settle as soon as possible. Thank you."
                )
                if r.customer_mobile else None
            ),
            "date_issued": r.date_issued,
            "status": r.status or "unpaid",
            "age_days": age_days,
        })

    total_amount_due = sum(r["amount_due"] for r in due_rows)

    daily_due_query = (
        db.session.query(
            func.date(issued_date_expr).label("d"),
            func.coalesce(func.sum(amt_due_expr), 0.0).label("total"),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(amt_due_expr > 0)
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .filter(func.lower(Invoice.status).in_(open_statuses))
    )

    if q:
        like = f"%{q.lower()}%"
        daily_due_query = daily_due_query.filter(
            or_(
                func.lower(func.coalesce(User.full_name, "")).like(like),
                func.lower(func.coalesce(User.registration_number, "")).like(like),
                func.lower(func.coalesce(User.email, "")).like(like),
                func.lower(func.coalesce(Invoice.invoice_number, "")).like(like),
            )
        )

    daily_due_raw = (
        daily_due_query
        .group_by(func.date(issued_date_expr))
        .order_by(func.date(issued_date_expr))
        .all()
    )

    due_labels = [
        r.d.isoformat() if isinstance(r.d, date) else str(r.d)
        for r in daily_due_raw
    ]
    due_values = [float(r.total or 0) for r in daily_due_raw]

    return render_template(
        "admin/finance/monthly_income.html",
        incomes=incomes,
        total_income=total_income,
        chart_labels=chart_labels,
        chart_values=chart_values,
        due_rows=due_rows,
        total_amount_due=total_amount_due,
        due_labels=due_labels,
        due_values=due_values,
        start=start,
        end=end,
        q=q,
        method_selected=method,
        methods=methods,
    )



@finance_bp.route("/invoices/send-reminders-bulk", methods=["POST"])
@admin_required(roles=["finance"])
def send_invoice_reminders_bulk():
    raw_ids = request.form.getlist("invoice_ids")
    invoice_ids = []

    for x in raw_ids:
        try:
            invoice_ids.append(int(x))
        except Exception:
            pass

    invoice_ids = list(dict.fromkeys(invoice_ids))

    if not invoice_ids:
        flash("Select at least one invoice to send reminders.", "warning")
        return redirect(request.referrer or url_for("finance.monthly_income"))

    invoices = (
        Invoice.query
        .filter(Invoice.id.in_(invoice_ids))
        .order_by(Invoice.user_id.asc(), Invoice.date_issued.asc(), Invoice.id.asc())
        .all()
    )

    grouped = {}

    for inv in invoices:
        user = User.query.get(inv.user_id)

        if not user:
            continue

        if user.id not in grouped:
            grouped[user.id] = {
                "user": user,
                "invoices": [],
                "total_due": 0.0,
            }

        amount_due = float(inv.amount_due or inv.grand_total or inv.amount or 0)
        invoice_number = inv.invoice_number or f"INV{inv.id:05d}"

        grouped[user.id]["invoices"].append({
            "invoice_number": invoice_number,
            "amount_due": amount_due,
        })
        grouped[user.id]["total_due"] += amount_due

    sent = 0
    failed = 0
    skipped = 0

    for group in grouped.values():
        user = group["user"]

        if not user.email:
            skipped += 1
            continue

        invoice_lines_plain = "\n".join(
            f"- {x['invoice_number']}: JMD {x['amount_due']:,.2f}"
            for x in group["invoices"]
        )

        invoice_lines_html = "".join(
            f"<li><strong>{x['invoice_number']}</strong>: JMD {x['amount_due']:,.2f}</li>"
            for x in group["invoices"]
        )

        total_due = float(group["total_due"] or 0)

        subject = "Payment Reminder - Outstanding Invoice Balance"

        plain_body = f"""
Hi {user.full_name or 'Customer'},

This is a friendly reminder from Foreign A Foot Logistics.

The following invoice(s) have outstanding balances:

{invoice_lines_plain}

Total outstanding: JMD {total_due:,.2f}

Please make payment at your earliest convenience.

Thank you,
Foreign A Foot Logistics Limited
""".strip()

        html_body = f"""
<p>Hi {user.full_name or 'Customer'},</p>

<p>This is a friendly reminder from <strong>Foreign A Foot Logistics</strong>.</p>

<p>The following invoice(s) have outstanding balances:</p>

<ul>
  {invoice_lines_html}
</ul>

<h3>Total outstanding: JMD {total_due:,.2f}</h3>

<p>Please make payment at your earliest convenience.</p>

<p>Thank you,<br>Foreign A Foot Logistics Limited</p>
""".strip()

        ok = send_email(
            to_email=user.email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            recipient_user_id=user.id,
        )

        if ok:
            sent += 1
        else:
            failed += 1

    if sent:
        flash(f"Sent {sent} grouped reminder email(s).", "success")

    if failed:
        flash(f"{failed} grouped reminder email(s) failed.", "danger")

    if skipped:
        flash(f"Skipped {skipped} customer(s) because email address was missing.", "warning")

    return redirect(request.referrer or url_for("finance.monthly_income"))


# ---------------------- MONTHLY PROFIT/LOSS ---------------------- #


@finance_bp.route('/monthly_profit_loss')
@admin_required(roles=['finance'])
def monthly_profit_loss():
    today = date.today()
    current_month_key = today.strftime('%Y-%m')

    amt_paid_expr = _invoice_paid_amount_expr()
    paid_date_expr = _invoice_paid_date_expr()

    # Get all paid invoices with a paid date
    paid_rows = (
        db.session.query(
            amt_paid_expr.label('amount'),
            func.date(paid_date_expr).label('date_paid'),
        )
        .filter(func.lower(Invoice.status) == 'paid')
        .all()
    )

    # Get all expenses
    expense_rows = (
        db.session.query(Expense.amount.label('amount'), Expense.date.label('date'))
        .all()
    )

    # Build last 6 months keys (oldest -> newest)
    month_keys = []
    y, m = today.year, today.month
    for i in range(5, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12
            yy -= 1
        month_keys.append(f"{yy:04d}-{mm:02d}")

    # Aggregate by YYYY-MM
    monthly_data = {k: {'income': 0.0, 'expenses': 0.0} for k in month_keys}

    for r in paid_rows:
        if not r.date_paid:
            continue
        d = r.date_paid if isinstance(r.date_paid, date) else r.date_paid.date()
        key = d.strftime('%Y-%m')
        if key in monthly_data:
            monthly_data[key]['income'] += float(r.amount or 0)

    for r in expense_rows:
        if not r.date:
            continue
        d = r.date if isinstance(r.date, date) else r.date.date()
        key = d.strftime('%Y-%m')
        if key in monthly_data:
            monthly_data[key]['expenses'] += float(r.amount or 0)

    refund_rows = (
        db.session.query(
            Payment.amount_jmd.label("amount"),
            Payment.created_at.label("date"),
            Payment.transaction_type.label("transaction_type"),
        )
        .filter(Payment.transaction_type.in_(["package_refund", "delivery_refund"]))
        .filter(Payment.status == "completed")
        .all()
    )

    for r in refund_rows:
        if not r.date:
            continue
        d = r.date if isinstance(r.date, date) else r.date.date()
        key = d.strftime('%Y-%m')
        if key in monthly_data:
            monthly_data[key]['expenses'] += float(r.amount or 0)

    summary = []
    for key in month_keys:
        income = monthly_data[key]['income']
        expenses = monthly_data[key]['expenses']
        summary.append({
            'month': key,
            'income': income,
            'expenses': expenses,
            'profit': income - expenses,
        })

    # Current month totals (still correct)
    current = monthly_data.get(current_month_key, {'income': 0.0, 'expenses': 0.0})
    total_income = current['income']
    total_expenses = current['expenses']

    current_manual_expenses = 0.0
    for r in expense_rows:
        if not r.date:
            continue
        d = r.date if isinstance(r.date, date) else r.date.date()
        if d.strftime('%Y-%m') == current_month_key:
            current_manual_expenses += float(r.amount or 0)

    current_refund_expenses = _refund_expense_total(
        date(today.year, today.month, 1),
        date(today.year, today.month, monthrange(today.year, today.month)[1])
    )
    net_profit = total_income - total_expenses

    return render_template(
        'admin/finance/monthly_profit_loss.html',
        total_income=total_income,
        total_expenses=total_expenses,
        net_profit=net_profit,
        summary=summary,
        current_manual_expenses=current_manual_expenses,
        current_refund_expenses=current_refund_expenses,
    )

def _build_customer_statement_pdf(user_id, start="", end=""):
    user = User.query.get_or_404(user_id)

    start = (start or "").strip()
    end = (end or "").strip()

    start_date = None
    end_date = None

    try:
        if start:
            start_date = datetime.fromisoformat(start).date()
    except Exception:
        start_date = None

    try:
        if end:
            end_date = datetime.fromisoformat(end).date()
    except Exception:
        end_date = None

    invoice_date_expr = _invoice_issued_date_expr()

    invoice_q = db.session.query(Invoice).filter(Invoice.user_id == user.id)

    payment_q = (
        db.session.query(Payment)
        .filter(Payment.user_id == user.id)
        .filter(Payment.status == "completed")
    )

    if start_date:
        invoice_q = invoice_q.filter(func.date(invoice_date_expr) >= start_date)
        payment_q = payment_q.filter(func.date(Payment.created_at) >= start_date)

    if end_date:
        invoice_q = invoice_q.filter(func.date(invoice_date_expr) <= end_date)
        payment_q = payment_q.filter(func.date(Payment.created_at) <= end_date)

    invoices = invoice_q.all()
    payments = payment_q.all()

    rows = []

    for inv in invoices:
        inv_date = inv.date_issued or inv.date_submitted or inv.created_at
        invoice_total = float(inv.grand_total or inv.amount or inv.amount_due or 0)

        rows.append({
            "date": inv_date,
            "type": "Invoice",
            "reference": inv.invoice_number or f"INV{inv.id:05d}",
            "debit": invoice_total,
            "credit": 0.0,
        })

    for p in payments:
        rows.append({
            "date": p.created_at,
            "type": "Payment",
            "reference": p.reference or f"TX-{p.id}",
            "debit": 0.0,
            "credit": float(p.amount_jmd or 0),
        })

    rows.sort(key=lambda x: x["date"] or datetime.min)

    balance = 0.0
    total_invoiced = 0.0
    total_paid = 0.0

    for row in rows:
        total_invoiced += row["debit"]
        total_paid += row["credit"]

        balance += row["debit"] - row["credit"]
        if balance < 0:
            balance = 0

        row["balance"] = balance

    html = render_template(
        "admin/finance/customer_statement_pdf.html",
        user=user,
        rows=rows,
        total_invoiced=total_invoiced,
        total_paid=total_paid,
        balance=balance,
        start=start,
        end=end,
        generated_at=datetime.now(),
    )

    pdf = HTML(string=html, base_url=request.url_root).write_pdf()
    filename = f"statement_{user.registration_number or user.id}.pdf"

    return user, pdf, filename, balance

@finance_bp.route("/customer/<int:user_id>/statement.pdf")
@admin_required(roles=["finance"])
def customer_statement_pdf(user_id):
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()

    user, pdf, filename, balance = _build_customer_statement_pdf(user_id, start, end)

    return send_file(
        BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )

@finance_bp.route("/customer/<int:user_id>/statement/email", methods=["POST"])
@admin_required(roles=["finance"])
def email_customer_statement_pdf(user_id):
    user = User.query.get_or_404(user_id)

    if not user.email:
        flash("Customer does not have an email address.", "danger")
        return redirect(request.referrer or url_for("accounts_profiles.view_user", id=user_id, tab="payments"))

    start = (request.form.get("start") or "").strip()
    end = (request.form.get("end") or "").strip()

    user, pdf, filename, balance = _build_customer_statement_pdf(user_id, start, end)

    subject = "Your Foreign A Foot Logistics Customer Statement"

    period_text = "All time"
    if start or end:
        period_text = f"{start or 'Beginning'} to {end or 'Today'}"

    plain_body = f"""
Hi {user.full_name or 'Customer'},

Please find attached your customer statement for the period: {period_text}.

Current balance due: JMD {float(balance or 0):,.2f}

Thank you,
Foreign A Foot Logistics Limited
""".strip()

    html_body = f"""
<p>Hi {user.full_name or 'Customer'},</p>

<p>Please find attached your customer statement for the period:</p>

<p><strong>{period_text}</strong></p>

<p><strong>Current balance due:</strong> JMD {float(balance or 0):,.2f}</p>

<p>Thank you,<br>Foreign A Foot Logistics Limited</p>
""".strip()

    ok = send_email(
        to_email=user.email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=[(pdf, filename, "application/pdf")],
        recipient_user_id=user.id,
    )

    if ok:
        flash(f"Statement emailed to {user.email}.", "success")
    else:
        flash("Failed to email statement.", "danger")

    return redirect(request.referrer or url_for("accounts_profiles.view_user", id=user_id, tab="payments"))


@finance_bp.route("/payroll", methods=["GET"])
@admin_required(roles=["finance"])
def payroll_dashboard():
    employees = EmployeePayroll.query.filter_by(is_active=True).all()
    runs = PayrollRun.query.order_by(PayrollRun.created_at.desc()).limit(20).all()

    return render_template(
        "admin/finance/payroll.html",
        employees=employees,
        runs=runs
    )

@finance_bp.route("/payroll/create", methods=["POST"])
@admin_required(roles=["finance"])
def create_payroll():
    start = request.form.get("start")
    end = request.form.get("end")

    if not start or not end:
        flash("Start and end date required", "danger")
        return redirect(url_for("finance.payroll_dashboard"))

    period_start = datetime.fromisoformat(start).date()
    period_end = datetime.fromisoformat(end).date()

    existing_run = PayrollRun.query.filter_by(
        period_start=period_start,
        period_end=period_end,
        status="draft"
    ).first()

    if existing_run:
        flash("A draft payroll already exists for this period. Open it instead of creating another one.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=existing_run.id))

    employees = EmployeePayroll.query.filter_by(is_active=True).all()

    if not employees:
        flash("No active payroll employees found. Add employees before creating payroll.", "warning")
        return redirect(url_for("finance.payroll_dashboard"))

    run = PayrollRun(
        period_start=period_start,
        period_end=period_end
    )
    db.session.add(run)
    db.session.flush()

    total = 0

    for emp in employees:
        if emp.pay_type == "salary":
            gross = float(emp.base_salary or 0)
        else:
            gross = 0

        item = PayrollItem(
            payroll_run_id=run.id,
            user_id=emp.user_id,
            gross_pay=gross,
            deductions=0,
            net_pay=gross
        )

        total += gross
        db.session.add(item)

    run.total_gross = total
    run.total_net = total

    db.session.commit()

    flash("Payroll created successfully.", "success")
    return redirect(url_for("finance.payroll_detail", run_id=run.id))


@finance_bp.route("/payroll/<int:run_id>/delete", methods=["POST"])
@admin_required(roles=["finance"])
def delete_payroll_run(run_id):
    run = PayrollRun.query.get_or_404(run_id)

    if run.status == "paid":
        flash("Paid payroll runs cannot be deleted.", "danger")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    PayrollItem.query.filter_by(payroll_run_id=run.id).delete()
    db.session.delete(run)
    db.session.commit()

    flash("Draft payroll run deleted successfully.", "success")
    return redirect(url_for("finance.payroll_dashboard"))

@finance_bp.route("/payroll/delete-selected", methods=["POST"])
@admin_required(roles=["finance"])
def delete_selected_payroll_runs():
    raw_ids = request.form.getlist("run_ids")
    run_ids = []

    for x in raw_ids:
        try:
            run_ids.append(int(x))
        except Exception:
            pass

    if not run_ids:
        flash("Select at least one draft payroll run to delete.", "warning")
        return redirect(url_for("finance.payroll_dashboard"))

    runs = PayrollRun.query.filter(PayrollRun.id.in_(run_ids)).all()

    deleted = 0
    skipped = 0

    for run in runs:
        if run.status == "paid":
            skipped += 1
            continue

        PayrollItem.query.filter_by(payroll_run_id=run.id).delete()
        db.session.delete(run)
        deleted += 1

    db.session.commit()

    if deleted:
        flash(f"Deleted {deleted} draft payroll run(s).", "success")

    if skipped:
        flash(f"Skipped {skipped} paid payroll run(s). Paid payroll cannot be deleted.", "warning")

    return redirect(url_for("finance.payroll_dashboard"))


@finance_bp.route("/payroll/employees/add", methods=["GET", "POST"])
@admin_required(roles=["finance"])
def add_payroll_employee():
    if request.method == "POST":
        user_id = request.form.get("user_id")
        pay_type = (request.form.get("pay_type") or "salary").strip().lower()

        if not user_id:
            flash("Please select an employee.", "danger")
            return redirect(url_for("finance.add_payroll_employee"))

        try:
            base_salary = float(request.form.get("base_salary") or 0)
        except Exception:
            base_salary = 0

        try:
            hourly_rate = float(request.form.get("hourly_rate") or 0)
        except Exception:
            hourly_rate = 0

        if pay_type not in {"salary", "hourly"}:
            flash("Invalid pay type.", "danger")
            return redirect(url_for("finance.add_payroll_employee"))

        if pay_type == "salary" and base_salary <= 0:
            flash("Salary must be greater than 0.", "danger")
            return redirect(url_for("finance.add_payroll_employee"))

        if pay_type == "hourly" and hourly_rate <= 0:
            flash("Hourly rate must be greater than 0.", "danger")
            return redirect(url_for("finance.add_payroll_employee"))

        existing = EmployeePayroll.query.filter_by(user_id=int(user_id)).first()
        if existing:
            flash("This user is already in payroll.", "warning")
            return redirect(url_for("finance.payroll_dashboard"))

        emp = EmployeePayroll(
            user_id=int(user_id),
            pay_type=pay_type,
            base_salary=base_salary if pay_type == "salary" else 0,
            hourly_rate=hourly_rate if pay_type == "hourly" else 0,
            is_active=True
        )

        db.session.add(emp)
        db.session.commit()

        flash("Employee added to payroll.", "success")
        return redirect(url_for("finance.payroll_dashboard"))

    existing_ids = [x[0] for x in db.session.query(EmployeePayroll.user_id).all()]

    users = (
        User.query
        .filter(~User.id.in_(existing_ids) if existing_ids else True)
        .filter(User.role.in_(["admin", "finance", "operations", "accounts_manager"]))
        .order_by(User.full_name.asc())
        .all()
    )

    return render_template(
        "admin/finance/add_payroll_employee.html",
        users=users
    )

@finance_bp.route("/payroll/<int:run_id>")
@admin_required(roles=["finance"])
def payroll_detail(run_id):
    run = PayrollRun.query.get_or_404(run_id)

    items = (
        PayrollItem.query
        .filter_by(payroll_run_id=run.id)
        .all()
    )

    return render_template(
        "admin/finance/payroll_detail.html",
        run=run,
        items=items
    )

@finance_bp.route("/payroll/payslip/<int:item_id>")
@admin_required(roles=["finance"])
def view_payslip(item_id):
    item = PayrollItem.query.get_or_404(item_id)
    run = PayrollRun.query.get(item.payroll_run_id)

    return render_template(
        "admin/finance/payslip.html",
        item=item,
        run=run
    )

@finance_bp.route("/payroll/<int:run_id>/mark-paid", methods=["POST"])
@admin_required(roles=["finance"])
def mark_payroll_paid(run_id):
    run = PayrollRun.query.get_or_404(run_id)

    if run.status == "paid":
        flash("Payroll is already marked as paid.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    run.status = "paid"
    run.paid_at = datetime.now(timezone.utc)
    run.paid_by_admin_id = current_user.id

    expense = Expense(
        date=date.today(),
        category="Payroll",
        amount=float(run.total_net or 0),
        description=f"Payroll paid for period {run.period_start} to {run.period_end}"
    )
    db.session.add(expense)

    db.session.commit()

    flash("Payroll marked as paid.", "success")
    return redirect(url_for("finance.payroll_detail", run_id=run.id))


@finance_bp.route("/payroll/payslip/<int:item_id>/pdf")
@admin_required(roles=["finance"])
def payslip_pdf(item_id):
    item = PayrollItem.query.get_or_404(item_id)
    run = PayrollRun.query.get_or_404(item.payroll_run_id)

    html = render_template(
        "admin/finance/payslip.html",
        item=item,
        run=run,
        pdf_mode=True
    )

    pdf = HTML(string=html, base_url=request.url_root).write_pdf()

    filename = f"payslip_{item.user.full_name if item.user else item.id}_{run.period_start}_{run.period_end}.pdf"
    filename = secure_filename(filename)

    return send_file(
        BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


@finance_bp.route("/payroll/payslip/<int:item_id>/email", methods=["POST"])
@admin_required(roles=["finance"])
def email_payslip(item_id):
    item = PayrollItem.query.get_or_404(item_id)
    run = PayrollRun.query.get_or_404(item.payroll_run_id)

    user = item.user
    if not user or not user.email:
        flash("Employee does not have an email address.", "danger")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    html = render_template(
        "admin/finance/payslip.html",
        item=item,
        run=run,
        pdf_mode=True
    )

    pdf = HTML(string=html, base_url=request.url_root).write_pdf()

    employee_name = user.full_name or user.email
    subject = f"Payslip for {run.period_start} to {run.period_end}"

    plain_body = f"""
Hi {employee_name},

Please find attached your payslip for the period {run.period_start} to {run.period_end}.

Net Pay: JMD {float(item.net_pay or 0):,.2f}

Thank you,
Foreign A Foot Logistics Limited
""".strip()

    html_body = f"""
<p>Hi {employee_name},</p>

<p>Please find attached your payslip for the period <strong>{run.period_start}</strong> to <strong>{run.period_end}</strong>.</p>

<p><strong>Net Pay:</strong> JMD {float(item.net_pay or 0):,.2f}</p>

<p>Thank you,<br>Foreign A Foot Logistics Limited</p>
""".strip()

    filename = f"payslip_{employee_name}_{run.period_start}_{run.period_end}.pdf"
    filename = secure_filename(filename)

    ok = send_email(
        to_email=user.email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=[(pdf, filename, "application/pdf")],
        recipient_user_id=user.id,
    )

    if ok:
        flash(f"Payslip emailed to {user.email}.", "success")
    else:
        flash("Failed to email payslip.", "danger")

    return redirect(url_for("finance.payroll_detail", run_id=run.id))

@finance_bp.route("/payroll/<int:run_id>/export")
@admin_required(roles=["finance"])
def export_payroll(run_id):
    import csv
    from io import StringIO

    run = PayrollRun.query.get_or_404(run_id)
    items = PayrollItem.query.filter_by(payroll_run_id=run.id).all()

    output = StringIO()
    writer = csv.writer(output)

    writer.writerow(["Employee", "Gross", "Allowance", "Overtime", "Bonus", "NIS", "Tax", "Other Deductions", "Total Deductions", "Net Pay"])

    for item in items:
        writer.writerow([
            item.user.full_name if item.user else "",
            float(item.gross_pay or 0),
            float(getattr(item, "allowance", 0) or 0),
            float(getattr(item, "overtime", 0) or 0),
            float(getattr(item, "bonus", 0) or 0),
            float(getattr(item, "nis", 0) or 0),
            float(getattr(item, "tax", 0) or 0),
            float(getattr(item, "other_deductions", 0) or 0),
            float(item.deductions or 0),
            float(item.net_pay or 0),
        ])

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=payroll_{run.period_start}_{run.period_end}.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@finance_bp.route("/payroll/item/<int:item_id>/update", methods=["POST"])
@admin_required(roles=["finance"])
def update_payroll_item(item_id):
    item = PayrollItem.query.get_or_404(item_id)
    run = PayrollRun.query.get_or_404(item.payroll_run_id)

    if run.status == "paid":
        flash("Cannot edit payroll after it has been marked paid.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    def money(name):
        try:
            return float(request.form.get(name) or 0)
        except Exception:
            return 0

    base_gross = money("gross_pay")
    allowance = money("allowance")
    overtime = money("overtime")
    bonus = money("bonus")

    nis = money("nis")
    nht = money("nht")
    education_tax = money("education_tax")
    tax = money("tax")
    other_deductions = money("other_deductions")

    gross_total = base_gross + allowance + overtime + bonus
    deductions = nis + nht + education_tax + tax + other_deductions
    net_pay = gross_total - deductions

    if net_pay < 0:
        flash("Deductions cannot be greater than total earnings.", "danger")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    item.gross_pay = round(gross_total, 2)
    item.allowance = round(allowance, 2)
    item.overtime = round(overtime, 2)
    item.bonus = round(bonus, 2)

    item.nis = round(nis, 2)
    item.nht = round(nht, 2)
    item.education_tax = round(education_tax, 2)
    item.tax = round(tax, 2)
    item.other_deductions = round(other_deductions, 2)

    item.deductions = round(deductions, 2)
    item.net_pay = round(net_pay, 2)

    items = PayrollItem.query.filter_by(payroll_run_id=run.id).all()
    run.total_gross = sum(float(i.gross_pay or 0) for i in items)
    run.total_net = sum(float(i.net_pay or 0) for i in items)

    db.session.commit()

    flash("Payroll item updated successfully.", "success")
    return redirect(url_for("finance.payroll_detail", run_id=run.id))


@finance_bp.route("/payroll/<int:run_id>/bulk-calc", methods=["POST"])
@admin_required(roles=["finance"])
def bulk_calculate_payroll(run_id):
    run = PayrollRun.query.get_or_404(run_id)

    if run.status == "paid":
        flash("Cannot calculate payroll after it has been marked paid.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    item_ids = [int(x) for x in request.form.getlist("item_ids") if str(x).isdigit()]

    if not item_ids:
        flash("Select at least one employee.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=run.id))

    # 2026 effective tax-free amount from JIS: 1,876,614 annual
    MONTHLY_TAX_FREE = 1876614 / 12

    for item in PayrollItem.query.filter(PayrollItem.id.in_(item_ids)).all():
        gross = float(item.gross_pay or 0)

        nis = gross * 0.03
        nht = gross * 0.02
        education_tax = gross * 0.0225

        taxable_income = max(0, gross - MONTHLY_TAX_FREE)
        tax = taxable_income * 0.25

        other_deductions = float(item.other_deductions or 0)

        total_deductions = nis + nht + education_tax + tax + other_deductions

        item.nis = round(nis, 2)
        item.nht = round(nht, 2)
        item.education_tax = round(education_tax, 2)
        item.tax = round(tax, 2)
        item.deductions = round(total_deductions, 2)
        item.net_pay = round(gross - total_deductions, 2)

    items = PayrollItem.query.filter_by(payroll_run_id=run.id).all()
    run.total_gross = sum(float(i.gross_pay or 0) for i in items)
    run.total_net = sum(float(i.net_pay or 0) for i in items)

    db.session.commit()

    flash("Payroll calculated for selected employees. You can still edit deductions manually if needed.", "success")
    return redirect(url_for("finance.payroll_detail", run_id=run.id))


@finance_bp.route("/payroll/<int:run_id>/bulk-email", methods=["POST"])
@admin_required(roles=["finance"])
def bulk_email_payslips(run_id):
    run = PayrollRun.query.get_or_404(run_id)

    item_ids = request.form.getlist("item_ids")

    if not item_ids:
        flash("Select at least one employee.", "warning")
        return redirect(url_for("finance.payroll_detail", run_id=run_id))

    sent = 0
    failed = 0

    for item in PayrollItem.query.filter(PayrollItem.id.in_(item_ids)).all():
        user = item.user

        if not user or not user.email:
            failed += 1
            continue

        html = render_template(
            "admin/finance/payslip.html",
            item=item,
            run=run,
            pdf_mode=True
        )

        pdf = HTML(string=html, base_url=request.url_root).write_pdf()

        subject = f"Payslip for {run.period_start} to {run.period_end}"

        plain_body = f"""
Hi {user.full_name},

Please find attached your payslip.

Net Pay: JMD {float(item.net_pay or 0):,.2f}

Foreign A Foot Logistics
""".strip()

        ok = send_email(
            to_email=user.email,
            subject=subject,
            plain_body=plain_body,
            html_body=html,
            attachments=[(pdf, f"payslip_{item.id}.pdf", "application/pdf")],
            recipient_user_id=user.id,
        )

        if ok:
            sent += 1
        else:
            failed += 1

    flash(f"{sent} payslips sent, {failed} failed.", "success" if sent else "warning")

    return redirect(url_for("finance.payroll_detail", run_id=run_id))

@finance_bp.route("/monthly-income/daily-sales")
@admin_required(roles=["finance"])
def daily_sales_report():
    today = date.today()
    default_start = date(today.year, today.month, 1)
    default_end = date(today.year, today.month, monthrange(today.year, today.month)[1])

    start = (request.args.get("start") or default_start.isoformat()).strip()
    end = (request.args.get("end") or default_end.isoformat()).strip()

    try:
        start_date = datetime.fromisoformat(start).date()
    except Exception:
        start_date = default_start
        start = default_start.isoformat()

    try:
        end_date = datetime.fromisoformat(end).date()
    except Exception:
        end_date = default_end
        end = default_end.isoformat()

    rows_raw = (
        db.session.query(
            func.date(Payment.created_at).label("sale_date"),
            Payment.method.label("method"),
            func.count(Payment.id).label("payment_count"),
            func.coalesce(func.sum(Payment.amount_jmd), 0.0).label("total_sales"),
        )
        .filter(func.date(Payment.created_at).between(start_date, end_date))
        .filter(func.lower(func.coalesce(Payment.status, "completed")) == "completed")
        .group_by(
            func.date(Payment.created_at),
            Payment.method
        )
        .order_by(func.date(Payment.created_at).desc())
        .all()
    )

    by_day = {}

    for r in rows_raw:
        d = r.sale_date
        method = (r.method or "Other").strip()
        amount = float(r.total_sales or 0)
        count = int(r.payment_count or 0)

        if d not in by_day:
            by_day[d] = {
                "sale_date": d,
                "payment_count": 0,
                "cash": 0.0,
                "card": 0.0,
                "bank_transfer": 0.0,
                "wallet": 0.0,
                "other": 0.0,
                "total_sales": 0.0,
            }

        method_l = method.lower()

        if method_l == "cash":
            by_day[d]["cash"] += amount
        elif method_l == "card":
            by_day[d]["card"] += amount
        elif method_l in ["bank transfer", "bank", "transfer"]:
            by_day[d]["bank_transfer"] += amount
        elif method_l == "wallet":
            by_day[d]["wallet"] += amount
        else:
            by_day[d]["other"] += amount

        by_day[d]["payment_count"] += count
        by_day[d]["total_sales"] += amount

    rows = list(by_day.values())
    rows.sort(key=lambda x: x["sale_date"], reverse=True)

    total_sales = sum(r["total_sales"] for r in rows)
    total_cash = sum(r["cash"] for r in rows)
    total_card = sum(r["card"] for r in rows)
    total_bank = sum(r["bank_transfer"] for r in rows)
    total_wallet = sum(r["wallet"] for r in rows)
    total_other = sum(r["other"] for r in rows)
    total_payments = sum(r["payment_count"] for r in rows)

    return render_template(
        "admin/finance/daily_sales_report.html",
        rows=rows,
        total_sales=total_sales,
        total_cash=total_cash,
        total_card=total_card,
        total_bank=total_bank,
        total_wallet=total_wallet,
        total_other=total_other,
        total_payments=total_payments,
        start=start,
        end=end,
    )

@finance_bp.route("/monthly-income/daily-sales/<sale_date>")
@admin_required(roles=["finance"])
def daily_sales_detail(sale_date):
    try:
        selected_date = datetime.fromisoformat(sale_date).date()
    except Exception:
        flash("Invalid sales date.", "danger")
        return redirect(url_for("finance.daily_sales_report"))

    rows_raw = (
        db.session.query(
            Payment.id.label("payment_id"),
            Payment.created_at.label("created_at"),
            Payment.amount_jmd.label("amount"),
            Payment.method,
            Payment.reference,
            Payment.notes,
            Payment.transaction_type,
            Invoice.id.label("invoice_id"),
            Invoice.invoice_number,
            User.full_name.label("customer_name"),
            User.registration_number.label("registration_number"),
        )
        .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
        .outerjoin(User, User.id == Payment.user_id)
        .filter(func.date(Payment.created_at) == selected_date)
        .filter(func.lower(func.coalesce(Payment.status, "completed")) == "completed")
        .order_by(Payment.created_at.asc())
        .all()
    )

    rows = []
    total_sales = 0.0

    for r in rows_raw:
        amount = float(r.amount or 0)
        total_sales += amount

        rows.append({
            "payment_id": r.payment_id,
            "created_at": r.created_at,
            "amount": amount,
            "method": r.method or "Other",
            "reference": r.reference or "—",
            "notes": r.notes or "",
            "transaction_type": r.transaction_type or "invoice_payment",
            "invoice_id": r.invoice_id,
            "invoice_number": r.invoice_number or "—",
            "customer_name": r.customer_name or "—",
            "registration_number": r.registration_number or "—",
        })

    return render_template(
        "admin/finance/daily_sales_detail.html",
        rows=rows,
        selected_date=selected_date,
        total_sales=total_sales,
    )