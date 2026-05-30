import re
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, render_template, request, jsonify, url_for, redirect
from sqlalchemy import or_, func

from app.extensions import db
from flask_login import current_user
from app.models import User, Package, Invoice, Payment, POSCloseout
from app.routes.admin_auth_routes import admin_required

from app.utils.email_utils import send_email, EMAIL_FROM, EMAIL_ADDRESS

admin_pos_bp = Blueprint("admin_pos", __name__, url_prefix="/admin/pos")

def _to_decimal(value, default="0.00"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _package_charge_amount(pkg):
    total = getattr(pkg, "grand_total", None)
    if total not in (None, "", 0, "0"):
        return _to_decimal(total)

    amount_due = getattr(pkg, "amount_due", None)
    return _to_decimal(amount_due)


def _normalize_scan_value(value):
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def _find_ready_package_by_scan(scan_value):
    normalized = _normalize_scan_value(scan_value)
    if not normalized:
        return None, "Empty scan value."

    base_query = Package.query.filter(
        Package.status == "Ready for Pick Up",
        Package.is_locked.is_(False),
    )

    # 1. exact match first
    exact_matches = (
        base_query
        .filter(
            or_(
                func.upper(func.replace(Package.tracking_number, " ", "")) == normalized,
                func.upper(func.replace(Package.house_awb, " ", "")) == normalized,
            )
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(exact_matches) > 1:
        return None, "Multiple ready packages matched exactly. Type more characters."

    # 2. ends-with match
    ends_with_matches = (
        base_query
        .filter(
            or_(
                func.upper(func.replace(Package.tracking_number, " ", "")).like(f"%{normalized}"),
                func.upper(func.replace(Package.house_awb, " ", "")).like(f"%{normalized}"),
            )
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    if len(ends_with_matches) == 1:
        return ends_with_matches[0], None
    if len(ends_with_matches) > 1:
        return None, "Multiple ready packages matched. Type more characters."

    # 3. contains match only if unique
    contains_matches = (
        base_query
        .filter(
            or_(
                func.upper(func.replace(Package.tracking_number, " ", "")).like(f"%{normalized}%"),
                func.upper(func.replace(Package.house_awb, " ", "")).like(f"%{normalized}%"),
            )
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    if len(contains_matches) == 1:
        return contains_matches[0], None
    if len(contains_matches) > 1:
        return None, "Multiple ready packages matched. Type more characters."

    return None, "No ready package matched that tracking number / House AWB."


@admin_pos_bp.route("/", methods=["GET"])
@admin_required
def index():
    return render_template("admin/pos/index.html")


@admin_pos_bp.route("/search-customers", methods=["GET"])
@admin_required
def search_customers():
    q = (request.args.get("q") or "").strip()

    if not q:
        return jsonify([])

    users = (
        User.query
        .filter(
            or_(
                User.full_name.ilike(f"%{q}%"),
                User.email.ilike(f"%{q}%"),
                User.mobile.ilike(f"%{q}%"),
                User.registration_number.ilike(f"%{q}%"),
            )
        )
        .order_by(User.full_name.asc(), User.email.asc())
        .limit(20)
        .all()
    )

    rows = []
    for u in users:
        rows.append({
            "id": u.id,
            "name": (u.full_name or "").strip() or u.email or "Customer",
            "email": u.email or "",
            "registration_number": u.registration_number or "",
            "phone": u.mobile or "",
        })

    return jsonify(rows)


@admin_pos_bp.route("/customer/<int:user_id>/packages", methods=["GET"])
@admin_required
def customer_packages(user_id):
    user = User.query.get_or_404(user_id)

    packages = (
        Package.query
        .filter(
            Package.user_id == user.id,
            Package.status == "Ready for Pick Up",
            Package.is_locked.is_(False)
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    rows = []
    total = Decimal("0.00")

    for p in packages:
        charge = _package_charge_amount(p)
        total += charge

        invoice_number = ""
        if p.invoice_id and p.invoice:
            invoice_number = p.invoice.invoice_number or ""

        rows.append({
            "id": p.id,
            "tracking_number": p.tracking_number or "",
            "house_awb": p.house_awb or "",
            "description": p.description or p.category or "",
            "weight": str(p.weight or ""),
            "status": p.status or "",
            "amount_due": str(charge),
            "invoice_id": p.invoice_id,
            "invoice_number": invoice_number,
        })

    return jsonify({
        "customer": {
            "id": user.id,
            "name": (user.full_name or "").strip() or user.email or "Customer",
            "email": user.email or "",
            "registration_number": user.registration_number or "",
        },
        "packages": rows,
        "total": str(total)
    })


@admin_pos_bp.route("/scan-lookup", methods=["GET"])
@admin_required
def scan_lookup():
    q = request.args.get("q") or ""
    package, error = _find_ready_package_by_scan(q)

    if error:
        return jsonify({"ok": False, "error": error}), 404

    user = package.user
    if not user:
        return jsonify({"ok": False, "error": "Matched package has no customer attached."}), 400

    return jsonify({
        "ok": True,
        "customer": {
            "id": user.id,
            "name": (user.full_name or "").strip() or user.email or "Customer",
            "email": user.email or "",
            "registration_number": user.registration_number or "",
        },
        "package": {
            "id": package.id,
            "tracking_number": package.tracking_number or "",
            "house_awb": package.house_awb or "",
        }
    })


@admin_pos_bp.route("/scan-deliver", methods=["POST"])
@admin_required
def scan_deliver():
    data = request.get_json(silent=True) or {}
    scan_value = data.get("scan_value") or ""

    package, error = _find_ready_package_by_scan(scan_value)
    if error:
        return jsonify({"ok": False, "error": error}), 404

    invoice = package.invoice
    charge = _package_charge_amount(package)

    if invoice:
        if float(invoice.amount_due or 0) > 0:
            return jsonify({
                "ok": False,
                "error": f"Invoice {invoice.invoice_number} still has a balance. Take payment before delivery."
            }), 400
    else:
        if float(charge) > 0:
            return jsonify({
                "ok": False,
                "error": "This package is not attached to a paid invoice yet."
            }), 400

    package.status = "Delivered"
    package.is_locked = True
    db.session.commit()

    user = package.user

    return jsonify({
        "ok": True,
        "message": f"Package {package.tracking_number or package.house_awb or package.id} marked Delivered.",
        "customer": {
            "id": user.id if user else None,
            "name": ((user.full_name or "").strip() if user else "") or (user.email if user else "") or "Customer",
        },
        "package": {
            "id": package.id,
            "tracking_number": package.tracking_number or "",
            "house_awb": package.house_awb or "",
        }
    })


@admin_pos_bp.route("/checkout", methods=["POST"])
@admin_required
def checkout():
    data = request.get_json(silent=True) or {}

    user_id = data.get("user_id")
    package_ids = data.get("package_ids") or []
    payment_method = (data.get("payment_method") or "").strip().lower()
    notes = (data.get("notes") or "").strip()

    if not user_id:
        return jsonify({"ok": False, "error": "Customer is required."}), 400

    if not package_ids:
        return jsonify({"ok": False, "error": "Select at least one package."}), 400

    if payment_method not in {"cash", "card", "transfer", "transfer_pending"}:
        return jsonify({"ok": False, "error": "Invalid payment method."}), 400

    is_pending_transfer = payment_method == "transfer_pending"

    user = User.query.get_or_404(user_id)

    packages = (
        Package.query
        .filter(
            Package.user_id == user.id,
            Package.id.in_(package_ids)
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    if not packages:
        return jsonify({"ok": False, "error": "No valid packages found."}), 400

    invalid = []
    total = Decimal("0.00")

    for p in packages:
        if p.status != "Ready for Pick Up":
            invalid.append(f"Package {p.id} is not ready for checkout.")
            continue

        if getattr(p, "is_locked", False):
            invalid.append(f"Package {p.id} is already locked.")
            continue

        total += _package_charge_amount(p)

    if invalid:
        return jsonify({"ok": False, "error": " ".join(invalid)}), 400

    try:
        now_utc = datetime.now(timezone.utc)

        existing_invoice_groups = {}
        uninvoiced_packages = []

        for p in packages:
            if p.invoice_id:
                existing_invoice_groups.setdefault(p.invoice_id, []).append(p)
            else:
                uninvoiced_packages.append(p)

        checkout_invoice_ids = []
        created_payment_ids = []

        for invoice_id, pkg_list in existing_invoice_groups.items():
            invoice = Invoice.query.get(invoice_id)

            if not invoice:
                db.session.rollback()
                return jsonify({
                    "ok": False,
                    "error": f"Invoice {invoice_id} not found."
                }), 400

            group_total = Decimal("0.00")

            for p in pkg_list:
                group_total += _package_charge_amount(p)

            if not is_pending_transfer:
                payment = Payment(
                    user_id=user.id,
                    invoice_id=invoice.id,
                    method=payment_method.title(),
                    amount_jmd=float(group_total),
                    transaction_type="invoice_payment",
                    status="completed",
                    notes=notes or "POS payment",
                    source="pos"
                )

                db.session.add(payment)
                db.session.flush()
                created_payment_ids.append(payment.id)

                current_due = Decimal(str(invoice.amount_due or 0))
                new_due = current_due - group_total

                if new_due < Decimal("0.00"):
                    new_due = Decimal("0.00")

                invoice.amount_due = float(new_due)

                if new_due == Decimal("0.00"):
                    invoice.status = "paid"
                    invoice.date_paid = now_utc
                else:
                    invoice.status = "unpaid"

            else:
                invoice.status = "unpaid"

                if notes:
                    invoice.description = (
                        (invoice.description or "") +
                        f"\nPOS release pending transfer: {notes}"
                    ).strip()

            checkout_invoice_ids.append(invoice.id)

            for p in pkg_list:
                p.status = "Delivered"
                p.is_locked = True

                p.delivery_scan_status = "scanned"
                p.delivery_scanned_at = now_utc
                p.delivery_scanned_by_id = current_user.id

        if uninvoiced_packages:
            new_total = Decimal("0.00")
            new_weight = Decimal("0.00")

            for p in uninvoiced_packages:
                new_total += _package_charge_amount(p)

                try:
                    new_weight += Decimal(str(p.weight or 0))
                except Exception:
                    pass

            pos_invoice = Invoice(
                user_id=user.id,
                invoice_number=f"POS-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                description=notes or f"POS checkout for {len(uninvoiced_packages)} package(s)",
                total_weight=float(new_weight),
                amount=float(new_total),
                amount_due=float(new_total) if is_pending_transfer else 0.0,
                grand_total=float(new_total),
                date_issued=now_utc,
                date_paid=None if is_pending_transfer else now_utc,
                created_at=now_utc,
                status="unpaid" if is_pending_transfer else "paid"
            )

            db.session.add(pos_invoice)
            db.session.flush()

            checkout_invoice_ids.append(pos_invoice.id)

            if not is_pending_transfer:
                pos_payment = Payment(
                    user_id=user.id,
                    invoice_id=pos_invoice.id,
                    method=payment_method.title(),
                    amount_jmd=float(new_total),
                    transaction_type="invoice_payment",
                    status="completed",
                    notes=notes or "POS payment",
                    source="pos"
                )

                db.session.add(pos_payment)
                db.session.flush()
                created_payment_ids.append(pos_payment.id)

            for p in uninvoiced_packages:
                p.invoice_id = pos_invoice.id
                p.status = "Delivered"
                p.is_locked = True

                p.delivery_scan_status = "scanned"
                p.delivery_scanned_at = now_utc
                p.delivery_scanned_by_id = current_user.id

        db.session.commit()

        message = (
            "Package(s) released. Transfer is pending and balance remains outstanding."
            if is_pending_transfer
            else "Checkout completed successfully."
        )

        return jsonify({
            "ok": True,
            "message": message,
            "invoice_ids": checkout_invoice_ids,
            "payment_ids": created_payment_ids,
            "total": str(total),
            "payment_pending": is_pending_transfer,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "ok": False,
            "error": f"Checkout failed: {str(e)}"
        }), 500


@admin_pos_bp.route("/invoice/<int:invoice_id>/receipt", methods=["GET"])
@admin_required
def invoice_receipt(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    return render_template("admin/pos/receipt_print.html", invoice=inv)


@admin_pos_bp.route("/invoice/<int:invoice_id>/email-receipt", methods=["POST"])
@admin_required
def email_receipt(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    user = inv.user

    if not user or not user.email:
        return jsonify({
            "ok": False,
            "error": "Customer does not have an email address."
        }), 400

    try:
        customer_name = ((user.full_name or "").strip() or user.email or "Customer")

        subject = f"Receipt for Invoice {inv.invoice_number}"

        plain_body = f"""
Hi {customer_name},

Your payment has been received.

Invoice Number: {inv.invoice_number}
Total: JMD {float(inv.grand_total or inv.amount or 0):,.2f}
Balance: JMD {float(inv.amount_due or 0):,.2f}
Status: {(inv.status or "").title()}

Thank you for shipping with Foreign A Foot Logistics Limited.
""".strip()

        html_body = f"""
<p>Hi {customer_name},</p>

<p>Your payment has been received.</p>

<p>
<b>Invoice Number:</b> {inv.invoice_number}<br>
<b>Total:</b> JMD {float(inv.grand_total or inv.amount or 0):,.2f}<br>
<b>Balance:</b> JMD {float(inv.amount_due or 0):,.2f}<br>
<b>Status:</b> {(inv.status or "").title()}
</p>

<p>Thank you for shipping with Foreign A Foot Logistics Limited.</p>
""".strip()

        ok = send_email(
            to_email=user.email,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
            recipient_user_id=user.id,
        )

        if not ok:
            return jsonify({
                "ok": False,
                "error": "Email failed to send."
            }), 500

        return jsonify({
            "ok": True,
            "message": f"Receipt emailed to {user.email}."
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"Failed to email receipt: {str(e)}"
        }), 500

@admin_pos_bp.route("/daily-sales", methods=["GET", "POST"])
@admin_required
def daily_sales():
    from datetime import datetime, timedelta, date
    from zoneinfo import ZoneInfo

    jamaica_tz = ZoneInfo("America/Jamaica")

    selected_date_str = request.args.get("date")
    if selected_date_str:
        business_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    else:
        business_date = datetime.now(jamaica_tz).date()

    start_local = datetime.combine(business_date, datetime.min.time(), tzinfo=jamaica_tz)
    end_local = start_local + timedelta(days=1)

    # Your DB timestamps are stored naive UTC, so convert local day to UTC naive range
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

    payments = (
        Payment.query
        .filter(
            Payment.created_at >= start_utc,
            Payment.created_at < end_utc,
            Payment.status == "completed",
            Payment.transaction_type == "invoice_payment",
            Payment.source == "pos"
        )
        .order_by(Payment.created_at.desc())
        .all()
    )

    summary = {
        "cash": Decimal("0.00"),
        "card": Decimal("0.00"),
        "transfer": Decimal("0.00"),
        "total": Decimal("0.00")
    }

    rows = []

    for p in payments:
        amount = Decimal(str(p.amount_jmd or 0))
        method = (p.method or "").strip().lower()

        if method == "cash":
            summary["cash"] += amount
        elif method == "card":
            summary["card"] += amount
        elif method in {"transfer", "bank", "bank transfer"}:
            summary["transfer"] += amount

        summary["total"] += amount

        rows.append({
            "time": p.created_at,
            "customer": p.user.full_name if p.user else "",
            "invoice": p.invoice.invoice_number if p.invoice else "",
            "method": p.method,
            "amount": amount
        })

    closeout = POSCloseout.query.filter_by(business_date=business_date).first()

    if request.method == "POST":
        actual_cash = Decimal(str(request.form.get("actual_cash") or "0"))
        notes = (request.form.get("notes") or "").strip()

        cash_difference = actual_cash - summary["cash"]

        if not closeout:
            closeout = POSCloseout(business_date=business_date)
            db.session.add(closeout)

        closeout.expected_cash = summary["cash"]
        closeout.expected_card = summary["card"]
        closeout.expected_transfer = summary["transfer"]
        closeout.expected_total = summary["total"]
        closeout.actual_cash = actual_cash
        closeout.cash_difference = cash_difference
        closeout.notes = notes
        closeout.closed_by_admin_id = current_user.id
        closeout.closed_at = datetime.now(timezone.utc)

        db.session.commit()
        flash("POS register closed successfully.", "success")

        return redirect(url_for("admin_pos.daily_sales", date=business_date.strftime("%Y-%m-%d")))

    return render_template(
        "admin/pos/daily_sales.html",
        summary=summary,
        rows=rows,
        business_date=business_date,
        closeout=closeout
    )

@admin_pos_bp.route("/closeouts", methods=["GET"])
@admin_required
def closeouts():
    closeouts = (
        POSCloseout.query
        .order_by(POSCloseout.business_date.desc())
        .limit(100)
        .all()
    )

    return render_template(
        "admin/pos/closeouts.html",
        closeouts=closeouts
    )