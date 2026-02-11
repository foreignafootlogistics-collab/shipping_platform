import os
import uuid
from werkzeug.utils import secure_filename
from flask import current_app, abort

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file, abort, current_app
from datetime import datetime, date, timedelta
from calendar import monthrange

from flask_login import current_user
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email

from sqlalchemy import func, or_

from app.forms import LoginForm, ExpenseForm
from app.routes.admin_auth_routes import admin_required
from app.calculator_data import USD_TO_JMD

from app.extensions import db
from app.models import Invoice, Expense, User, ExpenseAuditLog
import cloudinary
import cloudinary.uploader



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

def _upload_expense_pdf_to_cloudinary(file_storage):
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None, None, None

    filename = secure_filename(file_storage.filename)
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are allowed.")

    if not _cloudinary_ready():
        raise ValueError("Cloudinary env vars missing. Set CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET.")

    _init_cloudinary_from_config()

    res = cloudinary.uploader.upload(
        file_storage,
        resource_type="raw",      # ✅ required for PDFs
        folder="fafl/expenses",
        public_id=f"expense_{uuid.uuid4().hex}",
        use_filename=False,
        unique_filename=False,
    )

    return (
        filename,
        res.get("secure_url"),
        res.get("public_id"),
        file_storage.mimetype or "application/pdf",
    )

def _delete_cloudinary_raw(public_id: str):
    if not public_id or not _cloudinary_ready():
        return
    _init_cloudinary_from_config()
    try:
        cloudinary.uploader.destroy(public_id, resource_type="raw")
    except Exception:
        pass


# -----------------------------
# Audit log helper (yours)
# -----------------------------
def _log_expense_action(action: str, expense, request_obj):
    # keep your existing implementation
    from app.models import ExpenseAuditLog

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

        # update these to match your new Expense fields:
        expense_attachment_name=getattr(expense, "attachment_name", None),
        expense_attachment_stored=getattr(expense, "attachment_public_id", None),

        ip_address=(request_obj.headers.get("X-Forwarded-For", request_obj.remote_addr) or "")[:64],
        user_agent=(request_obj.headers.get("User-Agent", "") or "")[:255],
    )
    db.session.add(log)

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
        expense_attachment_stored=getattr(expense, "attachment_stored", None),
        ip_address=(request_obj.headers.get("X-Forwarded-For", request_obj.remote_addr) or "")[:64],
        user_agent=(request_obj.headers.get("User-Agent", "") or "")[:255],
    )
    db.session.add(log)


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

    open_statuses = ['pending', 'issued', 'unpaid']

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

    # Total expenses in period
    total_expenses = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
        .filter(func.date(Expense.date).between(start_date, end_date))
        .scalar() or 0.0
    )

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
    )


# ---------------------- UNPAID INVOICES ---------------------- #
@finance_bp.route('/unpaid_invoices')
@admin_required(roles=['finance'])
def unpaid_invoices():
    start = (request.args.get('start') or '').strip()
    end = (request.args.get('end') or '').strip()
    q = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or 'issued,unpaid').strip().lower()  # ✅ default matches UI
    min_due_raw = (request.args.get('min_due') or '').strip()
    max_due_raw = (request.args.get('max_due') or '').strip()

    # fallback to current month ONLY if missing
    if not (start and end):
        today = date.today()
        start = date(today.year, today.month, 1).isoformat()
        end = date(today.year, today.month, monthrange(today.year, today.month)[1]).isoformat()

    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()

    # ✅ status list from dropdown
    allowed = {'issued', 'unpaid'}  # keep it aligned with your UI
    status_list = [s for s in (t.strip() for t in status.split(',')) if s in allowed]
    if not status_list:
        status_list = ['issued', 'unpaid']

    # ✅ stable string that matches <option value="">
    if set(status_list) == {"issued", "unpaid"}:
        status_selected = "issued,unpaid"
    else:
        status_selected = status_list[0]  # issued OR unpaid


    # parse amounts safely
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
    amt_due_expr = _invoice_due_amount_expr()

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
        .filter(func.date(issued_date_expr).between(start_date, end_date))
        .filter(func.lower(Invoice.status).in_(status_list))
    )

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

    # status counts (same statuses)
    status_counts_rows = (
        db.session.query(
            func.lower(Invoice.status).label('s'),
            func.count(Invoice.id).label('cnt'),
        )
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
        status_counts=status_counts,
    )


# ---------------------- MONTHLY EXPENSES ---------------------- #
@finance_bp.route('/monthly_expenses', methods=['GET', 'POST'])
@admin_required(roles=['finance'])
def monthly_expenses():
    form = ExpenseForm()

    if form.validate_on_submit():
        try:
            attachment_name = None
            attachment_url = None
            attachment_public_id = None
            attachment_mime = None
            uploaded_at = None

            # ✅ Cloudinary upload (PDF only)
            file_obj = request.files.get("attachment")
            if file_obj and file_obj.filename:
                attachment_name, attachment_url, attachment_public_id, attachment_mime = (
                    _upload_expense_pdf_to_cloudinary(file_obj)
                )
                uploaded_at = datetime.utcnow()

            new_expense = Expense(
                date=form.date.data,
                category=form.category.data,
                amount=float(form.amount.data),
                description=form.description.data or '',

                # ✅ Cloudinary fields (make sure these exist in Expense model)
                attachment_name=attachment_name,
                attachment_url=attachment_url,
                attachment_public_id=attachment_public_id,
                attachment_mime=attachment_mime,
                attachment_uploaded_at=uploaded_at,
            )

            db.session.add(new_expense)
            db.session.flush()  # so new_expense.id exists for logging

            # ✅ audit log (created)
            _log_expense_action("CREATED", new_expense, request)

            db.session.commit()
            flash('Expense added successfully.', 'success')
            return redirect(url_for('finance.monthly_expenses'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error adding expense: {e}', 'danger')

    expenses_q = Expense.query.order_by(Expense.date.desc()).all()
    expenses = []
    for e in expenses_q:
        expenses.append({
            'id': e.id,
            'date': e.date,
            'category': e.category,
            'amount': float(e.amount or 0),
            'description': e.description,
            'attachment_name': e.attachment_name,
            'has_attachment': bool(getattr(e, "attachment_url", None)),  # ✅ Cloudinary
        })

    total_expenses = sum(e['amount'] for e in expenses) if expenses else 0.0

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

        # ✅ Delete from Cloudinary (if present)
        public_id = getattr(expense, "attachment_public_id", None)
        if public_id:
            _delete_cloudinary_raw(public_id)

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
    form = ExpenseForm()
    if form.validate_on_submit():
        try:
            new_expense = Expense(
                date=form.date.data,
                category=form.category.data,
                amount=float(form.amount.data),
                description=form.description.data or '',
            )
            db.session.add(new_expense)
            db.session.commit()
            flash('Expense added successfully.', 'success')
            return redirect(url_for('finance.view_expenses'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding expense: {e}', 'danger')

    return render_template('admin/finance/add_expense.html', form=form)


# ---------------------- VIEW EXPENSES ---------------------- #
@finance_bp.route('/expenses')
@admin_required
def view_expenses():
    expenses_q = Expense.query.order_by(Expense.date.desc()).all()
    expenses = [
        {
            'id': e.id,
            'date': e.date,
            'category': e.category,
            'amount': float(e.amount or 0),
            'description': e.description,
        }
        for e in expenses_q
    ]
    return render_template('admin/finance/view_expenses.html', expenses=expenses)


# ---------------------- EDIT EXPENSE ---------------------- #
@finance_bp.route('/expenses/edit/<int:expense_id>', methods=['GET', 'POST'])
@admin_required
def edit_expense(expense_id):
    expense = db.session.get(Expense, expense_id)
    if not expense:
        flash("Expense not found.", "danger")
        return redirect(url_for('finance.view_expenses'))

    form = ExpenseForm()

    if request.method == 'GET':
        form.amount.data = expense.amount
        form.category.data = expense.category
        form.description.data = expense.description
        form.date.data = expense.date

    if form.validate_on_submit():
        try:
            expense.date = form.date.data
            expense.category = form.category.data
            expense.amount = float(form.amount.data)
            expense.description = form.description.data or ''
            db.session.commit()
            flash("Expense updated successfully.", "success")
            return redirect(url_for('finance.view_expenses'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating expense: {e}", "danger")

    return render_template('admin/finance/edit_expense.html', form=form, expense=expense)


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
    # Current month bounds
    today = date.today()
    month_start = date(today.year, today.month, 1)
    month_end = date(today.year, today.month, monthrange(today.year, today.month)[1])

    amt_paid_expr = _invoice_paid_amount_expr()
    paid_date_expr = _invoice_paid_date_expr()
    amt_due_expr = _invoice_due_amount_expr()
    issued_date_expr = _invoice_issued_date_expr()
    open_statuses = ['pending', 'issued', 'unpaid']

    # Paid this month (table)
    incomes_raw = (
        db.session.query(
            Invoice.id,
            Invoice.invoice_number,
            User.full_name.label('customer_name'),
            amt_paid_expr.label('amount'),
            func.date(paid_date_expr).label('date_paid'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(month_start, month_end))
        .order_by(func.date(paid_date_expr).desc())
        .all()
    )
    incomes = []
    for r in incomes_raw:
        inv_number = r.invoice_number or f"INV{r.id:05d}"
        incomes.append({
            'invoice_number': inv_number,
            'customer_name': r.customer_name,
            'amount': float(r.amount or 0),
            'date_paid': r.date_paid,
        })
    total_income = sum(r['amount'] for r in incomes) if incomes else 0.0

    # Paid chart (daily)
    daily_paid_raw = (
        db.session.query(
            func.extract('day', func.date(paid_date_expr)).label('day'),
            func.coalesce(func.sum(amt_paid_expr), 0.0).label('total'),
        )
        .filter(func.lower(Invoice.status) == 'paid')
        .filter(func.date(paid_date_expr).between(month_start, month_end))
        .group_by(func.extract('day', func.date(paid_date_expr)))
        .order_by(func.extract('day', func.date(paid_date_expr)))
        .all()
    )
    chart_labels = [f"{int(r.day):02d}" for r in daily_paid_raw]
    chart_values = [float(r.total or 0) for r in daily_paid_raw]

    # Amount due issued this month (open statuses)
    due_rows_raw = (
        db.session.query(
            Invoice.id,
            Invoice.invoice_number,
            User.full_name.label('customer_name'),
            amt_due_expr.label('amount_due'),
            func.date(issued_date_expr).label('date_issued'),
        )
        .join(User, User.id == Invoice.user_id)
        .filter(amt_due_expr > 0)
        .filter(func.date(issued_date_expr).between(month_start, month_end))
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .order_by(func.date(issued_date_expr).desc())
        .all()
    )
    due_rows = []
    for r in due_rows_raw:
        inv_number = r.invoice_number or f"INV{r.id:05d}"
        due_rows.append({
            'invoice_number': inv_number,
            'customer_name': r.customer_name,
            'amount_due': float(r.amount_due or 0),
            'date_issued': r.date_issued,
        })
    total_amount_due = sum(r['amount_due'] for r in due_rows) if due_rows else 0.0

    # Issued chart (daily)
    daily_due_raw = (
        db.session.query(
            func.extract('day', func.date(issued_date_expr)).label('day'),
            func.coalesce(func.sum(amt_due_expr), 0.0).label('total'),
        )
        .filter(amt_due_expr > 0)
        .filter(func.date(issued_date_expr).between(month_start, month_end))
        .filter(func.lower(Invoice.status).in_(open_statuses))
        .group_by(func.extract('day', func.date(issued_date_expr)))
        .order_by(func.extract('day', func.date(issued_date_expr)))
        .all()
    )
    due_labels = [f"{int(r.day):02d}" for r in daily_due_raw]
    due_values = [float(r.total or 0) for r in daily_due_raw]

    return render_template(
        'admin/finance/monthly_income.html',
        incomes=incomes,
        total_income=total_income,
        chart_labels=chart_labels,
        chart_values=chart_values,
        due_rows=due_rows,
        total_amount_due=total_amount_due,
        due_labels=due_labels,
        due_values=due_values,
    )


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
    net_profit = total_income - total_expenses

    return render_template(
        'admin/finance/monthly_profit_loss.html',
        total_income=total_income,
        total_expenses=total_expenses,
        net_profit=net_profit,
        summary=summary,
    )
