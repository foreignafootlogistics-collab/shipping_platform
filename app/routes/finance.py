from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
from datetime import datetime, date
from calendar import monthrange
import sqlite3
import os
from flask_login import current_user
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email
from app.forms import LoginForm, ExpenseForm
from app.routes.admin_auth_routes import admin_required

from app.calculator_data import USD_TO_JMD

finance_bp = Blueprint('finance', __name__, url_prefix='/finance')


def _month_bounds(ym: str):
    y, m = map(int, ym.split('-'))
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    return start.isoformat(), end.isoformat()

@finance_bp.route('/dashboard')
@admin_required
def finance_dashboard():
    # ---- Period ----
    ym    = request.args.get('month')          # 'YYYY-MM'
    start = request.args.get('start')          # 'YYYY-MM-DD'
    end   = request.args.get('end')            # 'YYYY-MM-DD'
    if ym and not (start or end):
        start, end = _month_bounds(ym)
    elif not (start and end):
        now_ym = datetime.now().strftime('%Y-%m')
        start, end = _month_bounds(now_ym)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ---- Helpers (date/amount normalization) ----
    issued_sql = "DATE(COALESCE(i.date_issued, i.date_submitted, i.created_at))"
    paid_sql   = "DATE(COALESCE(i.date_paid, i.created_at))"  # fallback if date_paid missing
    amt_paid_sql = "COALESCE(i.amount, i.grand_total, i.amount_due, 0)"
    amt_due_sql  = "COALESCE(i.amount_due, i.grand_total, i.amount, 0)"


    # ---- KPIs ----
    # Paid in period (normalize amount & date)
    c.execute(f"""
      SELECT IFNULL(SUM({amt_paid_sql}),0) AS total
      FROM invoices i
      WHERE LOWER(i.status)='paid'
        AND {paid_sql} BETWEEN ? AND ?
    """, (start, end))
    total_paid = float(c.fetchone()['total'] or 0)

    # Expenses in period
    c.execute("""
      SELECT IFNULL(SUM(e.amount),0) AS total
      FROM expenses e
      WHERE DATE(e.date) BETWEEN ? AND ?
    """, (start, end))
    total_expenses = float(c.fetchone()['total'] or 0)

    # Receivables (same slice as Receivables page)
    open_statuses = ('pending','issued','unpaid')
    placeholders  = ",".join("?" for _ in open_statuses)

    c.execute(f"""
      SELECT IFNULL(SUM({amt_due_sql}),0) AS total
      FROM invoices i
      WHERE {amt_due_sql} > 0
        AND LOWER(i.status) IN ({placeholders})
        AND {issued_sql} BETWEEN ? AND ?
    """, (*open_statuses, start, end))
    total_amount_due = float(c.fetchone()['total'] or 0)

    # Optional: all-time outstanding for a sanity label/badge
    c.execute(f"""
      SELECT IFNULL(SUM({amt_due_sql}),0) AS total
      FROM invoices i
      WHERE {amt_due_sql} > 0
        AND LOWER(i.status) IN ({placeholders})
    """, open_statuses)
    total_amount_due_all = float(c.fetchone()['total'] or 0)

    net = total_paid - total_expenses

    # ---- Charts (normalize amount & dates consistently) ----
    c.execute(f"""
      SELECT {paid_sql} AS d, SUM({amt_paid_sql}) AS total
      FROM invoices i
      WHERE LOWER(i.status)='paid' AND {paid_sql} BETWEEN ? AND ?
      GROUP BY d ORDER BY d
    """, (start, end))
    paid_trend  = c.fetchall()
    paid_labels = [r['d'] for r in paid_trend]
    paid_values = [r['total'] for r in paid_trend]

    c.execute("""
      SELECT e.category, SUM(e.amount) AS total
      FROM expenses e
      WHERE DATE(e.date) BETWEEN ? AND ?
      GROUP BY e.category ORDER BY total DESC
    """, (start, end))
    expense_mix = c.fetchall()
    exp_labels  = [r['category'] for r in expense_mix]
    exp_values  = [r['total'] for r in expense_mix]

    # A/R aging (based on normalized issued date and normalized due amount)
    c.execute(f"""
      SELECT
        CASE
          WHEN CAST((julianday('now') - julianday({issued_sql})) AS INTEGER) <= 30 THEN '0-30'
          WHEN CAST((julianday('now') - julianday({issued_sql})) AS INTEGER) BETWEEN 31 AND 60 THEN '31-60'
          WHEN CAST((julianday('now') - julianday({issued_sql})) AS INTEGER) BETWEEN 61 AND 90 THEN '61-90'
          ELSE '91+'
        END AS bucket,
        SUM({amt_due_sql}) AS total
      FROM invoices i
      WHERE {amt_due_sql} > 0
        AND LOWER(i.status) IN ({placeholders})
        AND {issued_sql} BETWEEN ? AND ?
      GROUP BY bucket
    """, (*open_statuses, start, end))
    aging_rows = {r['bucket']: r['total'] for r in c.fetchall()}
    aging = {
      '0-30': aging_rows.get('0-30', 0),
      '31-60': aging_rows.get('31-60', 0),
      '61-90': aging_rows.get('61-90', 0),
      '91+':   aging_rows.get('91+', 0)
    }

    # Top customers (paid)
    c.execute(f"""
      SELECT u.full_name AS customer, SUM({amt_paid_sql}) AS total
      FROM invoices i JOIN users u ON u.id=i.user_id
      WHERE LOWER(i.status)='paid' AND {paid_sql} BETWEEN ? AND ?
      GROUP BY u.id ORDER BY total DESC LIMIT 5
    """, (start, end))
    top_customers = c.fetchall()

    # Tables
    c.execute(f"""
      SELECT i.id AS invoice_id,
             i.invoice_number,
             u.full_name AS customer,
             {amt_paid_sql} AS amount,
             {paid_sql} AS date_paid
      FROM invoices i JOIN users u ON u.id=i.user_id
      WHERE LOWER(i.status)='paid' AND {paid_sql} BETWEEN ? AND ?
      ORDER BY {paid_sql} DESC
    """, (start, end))
    paid_rows = c.fetchall()

    c.execute(f"""
      SELECT i.id AS invoice_id,
             i.invoice_number,
             u.full_name AS customer,
             {amt_due_sql} AS amount_due,
             {issued_sql} AS date_issued
      FROM invoices i JOIN users u ON u.id=i.user_id
      WHERE {amt_due_sql} > 0
        AND LOWER(i.status) IN ({placeholders})
        AND {issued_sql} BETWEEN ? AND ?
      ORDER BY date_issued DESC
    """, (*open_statuses, start, end))
    due_rows = c.fetchall()


    conn.close()

    user_role = getattr(current_user, 'role', 'Admin')
    return render_template(
        'admin/finance/finance_dashboard.html',
        start=start, end=end,
        total_paid=total_paid,
        total_expenses=total_expenses,
        total_amount_due=total_amount_due,         # JMD base
        total_amount_due_all=total_amount_due_all, # optional
        net=net,
        paid_labels=paid_labels, paid_values=paid_values,
        exp_labels=exp_labels,   exp_values=exp_values,
        aging=aging,
        top_customers=top_customers,
        paid_rows=paid_rows,
        due_rows=due_rows,
        usd_to_jmd=USD_TO_JMD,
        user_role=user_role
    )


@finance_bp.route('/unpaid_invoices')
@admin_required
def unpaid_invoices():
    start   = request.args.get('start')
    end     = request.args.get('end')
    q       = request.args.get('q', '').strip()
    # include pending by default
    status  = request.args.get('status', 'issued,unpaid,pending')
    min_due = request.args.get('min_due')
    max_due = request.args.get('max_due')

    # fallback to current month
    if not (start and end):
        from datetime import date
        from calendar import monthrange
        today = date.today()
        start = date(today.year, today.month, 1).isoformat()
        end   = date(today.year, today.month, monthrange(today.year, today.month)[1]).isoformat()

    status_list = [s for s in (t.strip().lower() for t in status.split(',')) if s in ('issued','unpaid','pending')]
    if not status_list:
        status_list = ['issued','unpaid','pending']

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ---- NORMALIZED FIELDS (key change) ----
    issued_sql   = "DATE(COALESCE(i.date_issued, i.date_submitted, i.created_at))"
    amt_due_sql  = "COALESCE(i.amount_due, i.grand_total, i.amount, 0)"

    # ---- WHERE + PARAMS (key change) ----
    where = [
        f"{amt_due_sql} > 0",
        f"{issued_sql} BETWEEN ? AND ?",
        f"LOWER(i.status) IN ({','.join(['?']*len(status_list))})"
    ]
    params = [start, end, *status_list]

    if q:
        where.append("(LOWER(u.full_name) LIKE ? OR LOWER(u.registration_number) LIKE ?)")
        like = f"%{q.lower()}%"
        params += [like, like]

    # Use normalized amount for numeric filters too (so NULL amount_due doesn't hide rows)
    if min_due:
        where.append(f"{amt_due_sql} >= ?"); params.append(float(min_due))
    if max_due:
        where.append(f"{amt_due_sql} <= ?"); params.append(float(max_due))

    # ---- SELECT (key change) ----
    sql = f"""
      SELECT i.id AS invoice_id,
             i.invoice_number,
             u.full_name AS customer,
             u.registration_number,
             i.status,
             {amt_due_sql} AS amount_due,
             {issued_sql}  AS date_issued
      FROM invoices i
      JOIN users u ON u.id = i.user_id
      WHERE {' AND '.join(where)}
      ORDER BY {issued_sql} DESC
    """
    c.execute(sql, params)
    invoices = c.fetchall()

    # total uses normalized column already exposed as 'amount_due'
    total_due = sum((r['amount_due'] or 0) for r in invoices)

    c.execute("""
      SELECT LOWER(status) s, COUNT(*) cnt
      FROM invoices
      WHERE amount_due > 0
      GROUP BY LOWER(status)
    """)
    status_counts = {row['s']: row['cnt'] for row in c.fetchall()}
    conn.close()

    return render_template(
        'admin/finance/unpaid_invoices.html',
        invoices=invoices,
        total_due=total_due,
        start=start, end=end, q=q,
        status_selected=','.join(status_list),
        min_due=min_due or '',
        max_due=max_due or '',
        status_counts=status_counts
    )

@finance_bp.route('/monthly_expenses', methods=['GET', 'POST'])
@admin_required
def monthly_expenses():
    
    form = ExpenseForm()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if form.validate_on_submit():
        try:
            date = form.date.data.strftime('%Y-%m-%d')
            category = form.category.data
            amount = float(form.amount.data)
            description = form.description.data or ''

            c.execute(
                "INSERT INTO expenses (date, category, amount, description) VALUES (?, ?, ?, ?)",
                (date, category, amount, description)
            )
            conn.commit()
            flash('Expense added successfully.', 'success')
            return redirect(url_for('finance.monthly_expenses'))
        except Exception as e:
            flash(f'Error adding expense: {e}', 'danger')

    c.execute("SELECT date, category, amount, description FROM expenses ORDER BY date DESC")
    expenses = c.fetchall()
    total_expenses = sum([row['amount'] for row in expenses]) if expenses else 0
    conn.close()

    return render_template('admin/finance/monthly_expenses.html',
                           form=form, expenses=expenses, total_expenses=total_expenses)


# ---------------------- ADD EXPENSE ---------------------- #
@finance_bp.route('/expenses/add', methods=['GET', 'POST'])
@admin_required
def add_expense():
    
    form = ExpenseForm()
    if form.validate_on_submit():
        try:
            date = form.date.data.strftime('%Y-%m-%d')
            category = form.category.data
            amount = float(form.amount.data)
            description = form.description.data or ''

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "INSERT INTO expenses (date, category, amount, description) VALUES (?, ?, ?, ?)",
                (date, category, amount, description)
            )
            conn.commit()
            conn.close()
            flash('Expense added successfully.', 'success')
            return redirect(url_for('finance.view_expenses'))
        except Exception as e:
            flash(f'Error adding expense: {e}', 'danger')

    return render_template('admin/finance/add_expense.html', form=form)


# ---------------------- VIEW EXPENSES ---------------------- #
@finance_bp.route('/expenses')
@admin_required
def view_expenses():
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT date, category, amount, description FROM expenses ORDER BY date DESC")
    expenses = c.fetchall()
    conn.close()

    return render_template('admin/finance/view_expenses.html', expenses=expenses)


# ---------------------- EDIT EXPENSE ---------------------- #
@finance_bp.route('/expenses/edit/<int:expense_id>', methods=['GET', 'POST'])
@admin_required
def edit_expense(expense_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
    expense = c.fetchone()

    if not expense:
        flash("Expense not found.", "danger")
        return redirect(url_for('finance.view_expenses'))

    form = ExpenseForm()
    if request.method == 'GET':
        form.amount.data = expense['amount']
        form.category.data = expense['category']
        form.description.data = expense['description']
        form.date.data = datetime.strptime(expense['date'], '%Y-%m-%d')

    if form.validate_on_submit():
        try:
            c.execute("""
                UPDATE expenses
                SET date = ?, category = ?, amount = ?, description = ?
                WHERE id = ?
            """, (
                form.date.data.strftime('%Y-%m-%d'),
                form.category.data,
                float(form.amount.data),
                form.description.data,
                expense_id
            ))
            conn.commit()
            flash("Expense updated successfully.", "success")
            return redirect(url_for('finance.view_expenses'))
        except Exception as e:
            flash(f"Error updating expense: {e}", "danger")

    conn.close()
    return render_template('admin/finance/edit_expense.html', form=form, expense=expense)


# ---------------------- DELETE EXPENSE ---------------------- #
@finance_bp.route('/expenses/delete/<int:expense_id>')
@admin_required
def delete_expense(expense_id):
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        conn.commit()
        conn.close()
        flash("Expense deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting expense: {e}", "danger")

    return redirect(url_for('finance.view_expenses'))


# ---------------------- MONTHLY INCOME ---------------------- #
@finance_bp.route('/monthly-income')
@admin_required
def monthly_income():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ---- Normalized expressions (handle schema variance) ----
    amt_paid_sql = "COALESCE(i.amount, i.grand_total, i.amount_due, 0)"
    paid_date_sql = "COALESCE(i.date_paid, i.created_at)"  # fallback if date_paid is null

    amt_due_sql = "COALESCE(i.amount_due, i.grand_total, i.amount, 0)"
    issued_date_sql = "COALESCE(i.date_issued, i.date_submitted, i.created_at)"
    open_statuses = ("pending", "issued", "unpaid")

    # ---- PAID this month (table + total) ----
    c.execute(f"""
      SELECT
        COALESCE(i.invoice_number, printf('INV%05d', i.id)) AS invoice_number,
        u.full_name AS customer_name,
        {amt_paid_sql} AS amount,
        DATE({paid_date_sql}) AS date_paid
      FROM invoices i
      JOIN users u ON i.user_id = u.id
      WHERE LOWER(i.status)='paid'
        AND strftime('%Y-%m', {paid_date_sql}) = strftime('%Y-%m','now')
      ORDER BY date_paid DESC
    """)
    incomes = c.fetchall()
    total_income = float(sum(r['amount'] or 0 for r in incomes)) if incomes else 0.0

    # ---- Paid chart (daily totals for current month) ----
    c.execute(f"""
      SELECT strftime('%d', {paid_date_sql}) AS day, SUM({amt_paid_sql}) AS total
      FROM invoices i
      WHERE LOWER(i.status)='paid'
        AND strftime('%Y-%m', {paid_date_sql}) = strftime('%Y-%m','now')
      GROUP BY day
      ORDER BY day
    """)
    daily_paid = c.fetchall()
    chart_labels = [r['day'] for r in daily_paid]
    chart_values = [float(r['total'] or 0) for r in daily_paid]

    # ---- AMOUNT DUE issued this month (open statuses) ----
    c.execute(f"""
      SELECT
        COALESCE(i.invoice_number, printf('INV%05d', i.id)) AS invoice_number,
        u.full_name AS customer_name,
        {amt_due_sql} AS amount_due,
        DATE({issued_date_sql}) AS date_issued
      FROM invoices i
      JOIN users u ON i.user_id = u.id
      WHERE {amt_due_sql} > 0
        AND strftime('%Y-%m', {issued_date_sql}) = strftime('%Y-%m','now')
        AND LOWER(i.status) IN ('pending','issued','unpaid')
      ORDER BY date_issued DESC
    """)
    due_rows = c.fetchall()
    total_amount_due = float(sum(r['amount_due'] or 0 for r in due_rows)) if due_rows else 0.0

    # ---- Issued (amount due) chart (daily totals for current month) ----
    c.execute(f"""
      SELECT strftime('%d', {issued_date_sql}) AS day, SUM({amt_due_sql}) AS total
      FROM invoices i
      WHERE {amt_due_sql} > 0
        AND strftime('%Y-%m', {issued_date_sql}) = strftime('%Y-%m','now')
        AND LOWER(i.status) IN ('pending','issued','unpaid')
      GROUP BY day
      ORDER BY day
    """)
    daily_due = c.fetchall()
    due_labels = [r['day'] for r in daily_due]
    due_values = [float(r['total'] or 0) for r in daily_due]

    conn.close()

    return render_template(
        'admin/finance/monthly_income.html',
        incomes=incomes,
        total_income=total_income,
        chart_labels=chart_labels,
        chart_values=chart_values,
        due_rows=due_rows,
        total_amount_due=total_amount_due,
        due_labels=due_labels,
        due_values=due_values
    )

# ---------------------- MONTHLY PROFIT/LOSS ---------------------- #
@finance_bp.route('/monthly_profit_loss')
@admin_required
def monthly_profit_loss():
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    current_month = datetime.now().strftime('%Y-%m')

    c.execute("""
        SELECT IFNULL(SUM(amount), 0) AS total_income
        FROM invoices
        WHERE status = 'paid' AND strftime('%Y-%m', date_paid) = ?
    """, (current_month,))
    total_income = c.fetchone()['total_income']

    c.execute("""
        SELECT IFNULL(SUM(amount), 0) AS total_expenses
        FROM expenses
        WHERE strftime('%Y-%m', date) = ?
    """, (current_month,))
    total_expenses = c.fetchone()['total_expenses']

    net_profit = total_income - total_expenses

    c.execute("""
        SELECT
            month,
            IFNULL(SUM(income), 0) AS income,
            IFNULL(SUM(expenses), 0) AS expenses,
            IFNULL(SUM(income), 0) - IFNULL(SUM(expenses), 0) AS profit
        FROM (
            SELECT strftime('%Y-%m', date_paid) AS month, amount AS income, 0 AS expenses
            FROM invoices
            WHERE status = 'paid' AND date_paid >= date('now', '-6 months')
            UNION ALL
            SELECT strftime('%Y-%m', date) AS month, 0 AS income, amount AS expenses
            FROM expenses
            WHERE date >= date('now', '-6 months')
        )
        GROUP BY month
        ORDER BY month
    """)
    summary = c.fetchall()
    conn.close()

    return render_template('admin/finance/monthly_profit_loss.html',
                           total_income=total_income,
                           total_expenses=total_expenses,
                           net_profit=net_profit,
                           summary=summary)

