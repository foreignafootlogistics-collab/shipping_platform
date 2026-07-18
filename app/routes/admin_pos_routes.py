import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from app.utils.time import to_jamaica
from decimal import Decimal

from flask import Blueprint, render_template, request, jsonify, url_for, redirect, flash
from sqlalchemy import or_, func

from app.extensions import db
from flask_login import current_user
from app.models import User, Package, Invoice, Payment, POSCloseout, AuditLog
from app.routes.admin_auth_routes import admin_required
from app.utils.invoice_totals import fetch_invoice_totals_pg

from app.utils.email_utils import send_email, EMAIL_FROM, EMAIL_ADDRESS

admin_pos_bp = Blueprint("admin_pos", __name__, url_prefix="/admin/pos")

JAMAICA_TZ = ZoneInfo("America/Jamaica")

def _to_decimal(value, default="0.00"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)

def _create_or_update_pending_pos_payment(
    *,
    invoice,
    user_id,
    amount_due,
    notes="",
    created_at=None,
):
    """
    Create or update the pending POS payment placeholder for an invoice.

    Pending records are not included in invoice payment totals because
    fetch_invoice_totals_pg() counts completed payments only.
    """

    amount_due = Decimal(str(amount_due or 0)).quantize(
        Decimal("0.01")
    )

    if amount_due <= Decimal("0.00"):
        return None

    now_utc = created_at or datetime.now(timezone.utc)
    now_jamaica = to_jamaica(now_utc)

    pending_payment = (
        Payment.query
        .filter(
            Payment.invoice_id == invoice.id,
            Payment.source == "pos",
            Payment.transaction_type == "invoice_payment",
            func.lower(Payment.status) == "pending",
        )
        .order_by(Payment.created_at.desc())
        .first()
    )

    pending_note = (
        f"POS transfer pending. "
        f"Package released on "
        f"{now_jamaica.strftime('%Y-%m-%d %I:%M %p')} "
        f"Jamaica time."
    )

    if notes:
        pending_note = f"{pending_note}\n{notes.strip()}"

    if pending_payment:
        pending_payment.amount_jmd = float(amount_due)
        pending_payment.method = "Transfer Pending"
        pending_payment.status = "pending"
        pending_payment.notes = pending_note
        pending_payment.authorized_by_admin_id = current_user.id

        return pending_payment

    pending_payment = Payment(
        user_id=user_id,
        invoice_id=invoice.id,
        method="Transfer Pending",
        amount_jmd=float(amount_due),
        transaction_type="invoice_payment",
        status="pending",
        notes=pending_note,
        source="pos",
        authorized_by_admin_id=current_user.id,
        created_at=now_utc,
    )

    db.session.add(pending_payment)
    db.session.flush()

    return pending_payment


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
            Package.is_locked.is_(False),
        )
        .order_by(Package.created_at.asc())
        .all()
    )

    rows = []
    total = Decimal("0.00")

    # Group visible packages by invoice.
    invoice_groups = {}
    uninvoiced_packages = []

    for package in packages:
        if package.invoice_id:
            invoice_groups.setdefault(
                package.invoice_id,
                []
            ).append(package)
        else:
            uninvoiced_packages.append(package)

    package_balances = {}

    # Calculate and allocate each invoice's live outstanding balance.
    for invoice_id, invoice_packages in invoice_groups.items():
        invoice = Invoice.query.get(invoice_id)

        if not invoice:
            for package in invoice_packages:
                package_balances[package.id] = Decimal("0.00")
            continue

        subtotal, discount_total, payments_total, total_due = (
            fetch_invoice_totals_pg(invoice.id)
        )

        invoice_balance = Decimal(
            str(max(float(total_due or 0), 0.0))
        ).quantize(Decimal("0.01"))

        package_charges = {
            package.id: _package_charge_amount(package).quantize(
                Decimal("0.01")
            )
            for package in invoice_packages
        }

        group_charge_total = sum(
            package_charges.values(),
            Decimal("0.00"),
        )

        allocated = Decimal("0.00")

        for index, package in enumerate(invoice_packages):
            is_last = index == len(invoice_packages) - 1

            if is_last:
                package_balance = invoice_balance - allocated

            elif group_charge_total > Decimal("0.00"):
                package_balance = (
                    invoice_balance
                    * package_charges[package.id]
                    / group_charge_total
                ).quantize(Decimal("0.01"))

            else:
                package_balance = Decimal("0.00")

            if package_balance < Decimal("0.00"):
                package_balance = Decimal("0.00")

            package_balances[package.id] = package_balance
            allocated += package_balance

    # Uninvoiced packages still use their original package charge.
    for package in uninvoiced_packages:
        package_balances[package.id] = (
            _package_charge_amount(package)
            .quantize(Decimal("0.01"))
        )

    # Build the response sent to the POS screen.
    for package in packages:
        amount_due = package_balances.get(
            package.id,
            Decimal("0.00"),
        )

        total += amount_due

        invoice_number = ""

        if package.invoice_id and package.invoice:
            invoice_number = (
                package.invoice.invoice_number or ""
            )

        rows.append({
            "id": package.id,
            "tracking_number": package.tracking_number or "",
            "house_awb": package.house_awb or "",
            "description": (
                package.description
                or package.category
                or ""
            ),
            "weight": str(package.weight or ""),
            "status": package.status or "",
            "amount_due": str(amount_due),
            "invoice_id": package.invoice_id,
            "invoice_number": invoice_number,
        })

    return jsonify({
        "customer": {
            "id": user.id,
            "name": (
                (user.full_name or "").strip()
                or user.email
                or "Customer"
            ),
            "email": user.email or "",
            "registration_number": (
                user.registration_number or ""
            ),
        },
        "packages": rows,
        "total": str(total.quantize(Decimal("0.01"))),
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
        subtotal, discount_total, payments_total, total_due = (
            fetch_invoice_totals_pg(invoice.id)
        )

        live_balance = round(
            max(float(total_due or 0), 0.0),
            2,
        )

        invoice.amount_due = live_balance

        if live_balance > 0:
            db.session.rollback()

            return jsonify({
                "ok": False,
                "error": (
                    f"Invoice {invoice.invoice_number} still has a "
                    f"balance of JMD {live_balance:,.2f}. "
                    f"Take payment before delivery."
                ),
            }), 400

    else:
        if float(charge) > 0:
            return jsonify({
                "ok": False,
                "error": "This package is not attached to a paid invoice yet."
            }), 400

    now_utc = datetime.now(timezone.utc)

    package.status = "Delivered"
    package.is_locked = True
    package.delivery_scan_status = "scanned"
    package.delivery_scanned_at = now_utc
    package.delivery_scanned_by_id = current_user.id
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

    discount_type = (data.get("discount_type") or "none").strip().lower()

    try:
        discount_amount = Decimal(str(data.get("discount_amount") or 0))
    except Exception:
        discount_amount = Decimal("0.00")

    if discount_type not in {"none", "fixed", "percent"}:
        discount_type = "none"

    if discount_amount < Decimal("0.00"):
        discount_amount = Decimal("0.00")

    if not user_id:
        return jsonify({"ok": False, "error": "Customer is required."}), 400

    if not package_ids:
        return jsonify({"ok": False, "error": "Select at least one package."}), 400

    if payment_method not in {"cash", "card", "transfer", "transfer_pending"}:
        return jsonify({"ok": False, "error": "Invalid payment method."}), 400

    is_pending_transfer = payment_method == "transfer_pending"

    user = User.query.get_or_404(user_id)

    package_ids = [int(x) for x in package_ids if str(x).isdigit()]

    packages = (
        Package.query
        .filter(
            Package.user_id == user.id,
            Package.id.in_(package_ids)
        )
        .with_for_update()
        .order_by(Package.created_at.asc())
        .all()
    )

    if not packages:
        return jsonify({"ok": False, "error": "No valid packages found."}), 400

    invalid = []
    subtotal = Decimal("0.00")

    for p in packages:
        if p.status != "Ready for Pick Up":
            invalid.append(f"Package {p.id} is not ready for checkout.")
            continue

        if getattr(p, "is_locked", False):
            invalid.append(f"Package {p.id} is already locked.")
            continue

        subtotal += _package_charge_amount(p)

    if invalid:
        return jsonify({"ok": False, "error": " ".join(invalid)}), 400

    discount = Decimal("0.00")

    if discount_type == "fixed":
        discount = discount_amount
    elif discount_type == "percent":
        discount = (subtotal * discount_amount) / Decimal("100")

    if discount > subtotal:
        discount = subtotal

    final_total = subtotal - discount

    discount_note = ""
    if discount > 0:
        discount_note = (
            f"POS discount applied: {discount_type} "
            f"{discount_amount} | Discount JMD {discount:.2f} | "
            f"Subtotal JMD {subtotal:.2f} | Final Total JMD {final_total:.2f}"
        )

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
        pending_payment_ids = []

        collected_total = Decimal("0.00")
        pending_total = Decimal("0.00")

        total_before_discount = subtotal if subtotal > 0 else Decimal("1.00")

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

            group_discount = Decimal("0.00")
            if discount > 0:
                group_discount = (group_total / total_before_discount) * discount

            group_final_total = group_total - group_discount

            if group_final_total < Decimal("0.00"):
                group_final_total = Decimal("0.00")

            invoice.subtotal_before_discount = group_total
            invoice.discount_type = discount_type if group_discount > 0 else "none"
            invoice.discount_amount = (
                discount_amount if group_discount > 0 else Decimal("0.00")
            )
            invoice.discount_total = group_discount
            invoice.grand_total = float(group_final_total)
            invoice.amount = float(group_final_total)

            # Check payments already recorded against this invoice.
            existing_paid_raw = (
                db.session.query(
                    func.coalesce(func.sum(Payment.amount_jmd), 0)
                )
                .filter(
                    Payment.invoice_id == invoice.id,
                    func.lower(Payment.status) == "completed",
                )
                .scalar()
            )

            existing_paid = Decimal(str(existing_paid_raw or 0)).quantize(
                Decimal("0.01")
            )

            invoice_total = group_final_total.quantize(Decimal("0.01"))

            remaining_due = (invoice_total - existing_paid).quantize(
                Decimal("0.01")
            )

            if remaining_due < Decimal("0.00"):
                remaining_due = Decimal("0.00")

            invoice.amount_due = float(remaining_due)

            # Invoice was already fully paid before entering POS.
            if remaining_due == Decimal("0.00"):
                invoice.status = "paid"

                if not invoice.date_paid:
                    invoice.date_paid = now_utc

            # Collect only the actual outstanding balance.
            elif not is_pending_transfer:
                payment_notes = notes or "POS payment"

                if discount_note:
                    payment_notes = f"{payment_notes}\n{discount_note}"

                payment = Payment(
                    user_id=user.id,
                    invoice_id=invoice.id,
                    method=payment_method.title(),
                    amount_jmd=float(remaining_due),
                    transaction_type="invoice_payment",
                    status="completed",
                    notes=payment_notes,
                    source="pos",
                    authorized_by_admin_id=current_user.id,
                    created_at=now_utc,
                )

                db.session.add(payment)
                db.session.flush()
                created_payment_ids.append(payment.id)
                collected_total += remaining_due

                if discount_note:
                    invoice.description = (
                        (invoice.description or "") +
                        f"\n{discount_note}"
                    ).strip()

                invoice.amount_due = 0.00
                invoice.status = "paid"
                invoice.date_paid = now_utc

            # Transfer has not yet been confirmed.
            else:
                invoice.status = (
                    "partial"
                    if existing_paid > Decimal("0.00")
                    else "unpaid"
                )
                invoice.date_paid = None
                invoice.amount_due = float(remaining_due)

                pending_note = notes or ""

                if discount_note:
                    pending_note = (
                        f"{pending_note}\n{discount_note}"
                    ).strip()

                pending_payment = (
                    _create_or_update_pending_pos_payment(
                        invoice=invoice,
                        user_id=user.id,
                        amount_due=remaining_due,
                        notes=pending_note,
                        created_at=now_utc,
                    )
                )

                if pending_payment:
                    pending_payment_ids.append(
                        pending_payment.id
                    )

                pending_total += remaining_due

                invoice.description = (
                    (invoice.description or "")
                    + (
                        "\nPOS release pending transfer. "
                        f"Outstanding balance: "
                        f"JMD {remaining_due:,.2f}."
                    )
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

            new_discount = Decimal("0.00")
            if discount > 0:
                new_discount = (new_total / total_before_discount) * discount

            new_final_total = new_total - new_discount

            if new_final_total < Decimal("0.00"):
                new_final_total = Decimal("0.00")

            invoice_description = notes or f"POS checkout for {len(uninvoiced_packages)} package(s)"
            if discount_note:
                invoice_description = f"{invoice_description}\n{discount_note}"

            pos_invoice = Invoice(
                user_id=user.id,
                invoice_number=(
                    f"POS-"
                    f"{datetime.now(JAMAICA_TZ).strftime('%Y%m%d%H%M%S%f')}"
                ),
                description=invoice_description,
                total_weight=float(new_weight),
                amount=float(new_final_total),
                amount_due=float(new_final_total) if is_pending_transfer else 0.0,
                grand_total=float(new_final_total),

                subtotal_before_discount=new_total,
                discount_type=discount_type if new_discount > 0 else "none",
                discount_amount=discount_amount if new_discount > 0 else Decimal("0.00"),
                discount_total=new_discount,

                date_issued=now_utc,
                date_paid=None if is_pending_transfer else now_utc,
                created_at=now_utc,
                status="unpaid" if is_pending_transfer else "paid"
            )

            db.session.add(pos_invoice)
            db.session.flush()

            checkout_invoice_ids.append(pos_invoice.id)

            if not is_pending_transfer:
                payment_notes = notes or "POS payment"

                if discount_note:
                    payment_notes = (
                        f"{payment_notes}\n{discount_note}"
                    )

                pos_payment = Payment(
                    user_id=user.id,
                    invoice_id=pos_invoice.id,
                    method=payment_method.title(),
                    amount_jmd=float(new_final_total),
                    transaction_type="invoice_payment",
                    status="completed",
                    notes=payment_notes,
                    source="pos",
                    authorized_by_admin_id=current_user.id,
                    created_at=now_utc,
                )

                db.session.add(pos_payment)
                db.session.flush()

                created_payment_ids.append(pos_payment.id)
                collected_total += new_final_total

            else:
                pending_payment = (
                    _create_or_update_pending_pos_payment(
                        invoice=pos_invoice,
                        user_id=user.id,
                        amount_due=new_final_total,
                        notes=notes,
                        created_at=now_utc,
                    )
                )

                if pending_payment:
                    pending_payment_ids.append(
                        pending_payment.id
                    )

                pending_total += new_final_total

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

        display_total = (
            pending_total
            if is_pending_transfer
            else collected_total
        )

        return jsonify({
            "ok": True,
            "message": message,
            "invoice_ids": checkout_invoice_ids,
            "payment_ids": created_payment_ids,
            "pending_payment_ids": pending_payment_ids,
            "subtotal": str(
                subtotal.quantize(Decimal("0.01"))
            ),
            "discount": str(
                discount.quantize(Decimal("0.01"))
            ),
            "total": str(
                collected_total.quantize(Decimal("0.01"))
            ),
            "amount_collected": str(
                collected_total.quantize(Decimal("0.01"))
            ),
            "amount_pending": str(
                pending_total.quantize(Decimal("0.01"))
            ),
            "payment_pending": is_pending_transfer,
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "ok": False,
            "error": f"Checkout failed: {str(e)}"
        }), 500

@admin_pos_bp.route(
    "/pending-payments",
    methods=["GET"],
)
@admin_required
def pending_payments():
    search = (
        request.args.get("q") or ""
    ).strip().lower()

    pending_records = (
        Payment.query
        .filter(
            Payment.source == "pos",
            Payment.transaction_type == "invoice_payment",
            func.lower(Payment.status) == "pending",
        )
        .order_by(Payment.created_at.asc())
        .limit(1000)
        .all()
    )

    rows = []
    stale_records_changed = False

    for pending_payment in pending_records:
        invoice = pending_payment.invoice

        if not invoice:
            pending_payment.status = "cancelled"
            stale_records_changed = True
            continue

        (
            subtotal,
            discount_total,
            payments_total,
            total_due,
        ) = fetch_invoice_totals_pg(invoice.id)

        live_balance = Decimal(
            str(max(float(total_due or 0), 0.0))
        ).quantize(Decimal("0.01"))

        # Automatically close stale pending placeholders.
        if live_balance <= Decimal("0.00"):
            pending_payment.amount_jmd = 0.00
            pending_payment.status = "settled"
            stale_records_changed = True
            continue

        # Keep the placeholder synchronized with the live balance.
        if round(
            float(pending_payment.amount_jmd or 0),
            2,
        ) != float(live_balance):
            pending_payment.amount_jmd = float(
                live_balance
            )
            stale_records_changed = True

        user = invoice.user or pending_payment.user

        packages = (
            Package.query
            .filter(Package.invoice_id == invoice.id)
            .order_by(Package.created_at.asc())
            .all()
        )

        tracking_values = []

        for package in packages:
            if package.tracking_number:
                tracking_values.append(
                    package.tracking_number
                )

            if package.house_awb:
                tracking_values.append(
                    package.house_awb
                )

        customer_name = (
            (user.full_name or "").strip()
            if user
            else ""
        ) or (
            user.email
            if user
            else ""
        ) or "Customer"

        registration_number = (
            user.registration_number
            if user
            else ""
        ) or ""

        email = (
            user.email
            if user
            else ""
        ) or ""

        mobile = (
            user.mobile
            if user
            else ""
        ) or ""

        invoice_number = (
            invoice.invoice_number
            or f"Invoice #{invoice.id}"
        )

        search_text = " ".join([
            customer_name,
            registration_number,
            email,
            mobile,
            invoice_number,
            *tracking_values,
        ]).lower()

        if search and search not in search_text:
            continue

        released_at = (
            to_jamaica(pending_payment.created_at)
            if pending_payment.created_at
            else None
        )

        rows.append({
            "pending_payment_id": pending_payment.id,
            "invoice_id": invoice.id,
            "invoice_number": invoice_number,
            "customer_name": customer_name,
            "registration_number": registration_number,
            "email": email,
            "mobile": mobile,
            "tracking": tracking_values,
            "subtotal": round(
                float(subtotal or 0),
                2,
            ),
            "discount_total": round(
                float(discount_total or 0),
                2,
            ),
            "payments_total": round(
                float(payments_total or 0),
                2,
            ),
            "amount_due": float(live_balance),
            "released_at": released_at,
            "notes": pending_payment.notes or "",
        })

    if stale_records_changed:
        db.session.commit()

    total_pending = round(
        sum(
            float(row["amount_due"] or 0)
            for row in rows
        ),
        2,
    )

    return render_template(
        "admin/pos/pending_payments.html",
        rows=rows,
        search=search,
        total_pending=total_pending,
    )


@admin_pos_bp.route(
    "/pending-payments/<int:payment_id>/collect",
    methods=["POST"],
)
@admin_required
def collect_pending_payment(payment_id):
    pending_payment = (
        Payment.query
        .filter(
            Payment.id == payment_id,
            Payment.source == "pos",
            Payment.transaction_type == "invoice_payment",
            func.lower(Payment.status) == "pending",
        )
        .with_for_update(of=Payment)
        .first_or_404()
    )

    invoice = (
        Invoice.query
        .filter(Invoice.id == pending_payment.invoice_id)
        .with_for_update(of=Invoice)
        .first_or_404()
    )

    try:
        amount = Decimal(
            str(request.form.get("amount") or 0)
        ).quantize(Decimal("0.01"))
    except Exception:
        amount = Decimal("0.00")

    method = (
        request.form.get("method") or ""
    ).strip().lower()

    reference = (
        request.form.get("reference") or ""
    ).strip()

    notes = (
        request.form.get("notes") or ""
    ).strip()

    allowed_methods = {
        "cash": "Cash",
        "card": "Card",
        "transfer": "Transfer",
    }

    if method not in allowed_methods:
        flash(
            "Select Cash, Card or Transfer.",
            "danger",
        )

        return redirect(
            url_for("admin_pos.pending_payments")
        )

    if amount <= Decimal("0.00"):
        flash(
            "Payment amount must be greater than zero.",
            "danger",
        )

        return redirect(
            url_for("admin_pos.pending_payments")
        )

    (
        subtotal,
        discount_total,
        payments_total,
        total_due,
    ) = fetch_invoice_totals_pg(invoice.id)

    live_balance = Decimal(
        str(max(float(total_due or 0), 0.0))
    ).quantize(Decimal("0.01"))

    if live_balance <= Decimal("0.00"):
        pending_payment.amount_jmd = 0.00
        pending_payment.status = "settled"

        invoice.amount_due = 0.00
        invoice.status = "paid"

        if not invoice.date_paid:
            invoice.date_paid = datetime.now(
                timezone.utc
            )

        db.session.commit()

        flash(
            "This invoice was already fully paid. "
            "The pending record was closed.",
            "info",
        )

        return redirect(
            url_for("admin_pos.pending_payments")
        )

    if amount > live_balance:
        flash(
            (
                f"Payment cannot exceed the balance of "
                f"JMD {live_balance:,.2f}."
            ),
            "danger",
        )

        return redirect(
            url_for("admin_pos.pending_payments")
        )

    now_utc = datetime.now(timezone.utc)
    now_jamaica = to_jamaica(now_utc)

    payment_notes = (
        f"POS collection against pending payment "
        f"#{pending_payment.id}. "
        f"Collected on "
        f"{now_jamaica.strftime('%Y-%m-%d %I:%M %p')} "
        f"Jamaica time."
    )

    if notes:
        payment_notes = (
            f"{payment_notes}\n{notes}"
        )

    completed_payment = Payment(
        user_id=invoice.user_id,
        invoice_id=invoice.id,
        method=allowed_methods[method],
        amount_jmd=float(amount),
        transaction_type="invoice_payment",
        status="completed",
        reference=reference or None,
        notes=payment_notes,
        source="pos",
        authorized_by_admin_id=current_user.id,
        created_at=now_utc,
    )

    db.session.add(completed_payment)
    db.session.flush()

    remaining_balance = (
        live_balance - amount
    ).quantize(Decimal("0.01"))

    old_pending_amount = Decimal(
        str(pending_payment.amount_jmd or 0)
    ).quantize(Decimal("0.01"))

    pending_payment.amount_jmd = float(
        remaining_balance
    )

    settlement_note = (
        f"JMD {amount:,.2f} collected by "
        f"admin ID {current_user.id} on "
        f"{now_jamaica.strftime('%Y-%m-%d %I:%M %p')} "
        f"Jamaica time."
    )

    pending_payment.notes = (
        f"{pending_payment.notes or ''}\n"
        f"{settlement_note}"
    ).strip()

    clean_description = re.sub(
        (
            r"\s*POS release pending transfer\.\s*"
            r"Outstanding balance:\s*"
            r"JMD\s*[\d,]+(?:\.\d{1,2})?\."
        ),
        "",
        invoice.description or "",
        flags=re.IGNORECASE,
    ).strip()

    if remaining_balance <= Decimal("0.00"):
        pending_payment.amount_jmd = 0.00
        pending_payment.status = "settled"

        invoice.amount_due = 0.00
        invoice.status = "paid"
        invoice.date_paid = now_utc

        invoice.description = (
            f"{clean_description}\n"
            f"POS pending balance settled on "
            f"{now_jamaica.strftime('%Y-%m-%d %I:%M %p')} "
            f"Jamaica time."
        ).strip()

    else:
        pending_payment.status = "pending"

        invoice.amount_due = float(
            remaining_balance
        )
        invoice.status = "partial"
        invoice.date_paid = None

        invoice.description = (
            f"{clean_description}\n"
            f"POS release pending transfer. "
            f"Outstanding balance: "
            f"JMD {remaining_balance:,.2f}."
        ).strip()

    db.session.add(AuditLog(
        module="POS",
        action="Pending POS Payment Collected",
        admin_id=current_user.id,
        user_id=invoice.user_id,
        entity_type="Invoice",
        entity_id=invoice.id,
        reason="Pending POS balance collection",
        description=(
            f"Collected JMD {amount:,.2f} by "
            f"{allowed_methods[method]} against "
            f"{invoice.invoice_number or ('Invoice #' + str(invoice.id))}. "
            f"Remaining balance: "
            f"JMD {remaining_balance:,.2f}."
        ),
        old_value=(
            f"Pending Payment ID: {pending_payment.id}; "
            f"Pending Amount: "
            f"JMD {old_pending_amount:,.2f}"
        ),
        new_value=(
            f"Completed Payment ID: "
            f"{completed_payment.id}; "
            f"Invoice Status: {invoice.status}; "
            f"Remaining Balance: "
            f"JMD {remaining_balance:,.2f}"
        ),
    ))

    db.session.commit()

    if remaining_balance <= Decimal("0.00"):
        flash(
            (
                f"Payment of JMD {amount:,.2f} recorded. "
                f"Invoice "
                f"{invoice.invoice_number} is now paid."
            ),
            "success",
        )
    else:
        flash(
            (
                f"Partial payment of "
                f"JMD {amount:,.2f} recorded. "
                f"Balance remaining: "
                f"JMD {remaining_balance:,.2f}."
            ),
            "success",
        )

    return redirect(
        url_for("admin_pos.pending_payments")
    )

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
    selected_date_str = request.args.get("date")
    if selected_date_str:
        business_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
    else:
        business_date = datetime.now(JAMAICA_TZ).date()

    start_local = datetime.combine(
        business_date,
        datetime.min.time(),
        tzinfo=JAMAICA_TZ
    )
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
        "gross_total": Decimal("0.00"),
        "discount": Decimal("0.00"),
        "total": Decimal("0.00"),
    }

    rows = []

    for p in payments:
        amount = Decimal(str(p.amount_jmd or 0))
        method = (p.method or "").strip().lower()

        invoice_discount = Decimal("0.00")

        if p.invoice:
            invoice_discount = Decimal(
                str(p.invoice.discount_total or 0)
            ).quantize(Decimal("0.01"))

        # Gross for this POS transaction—not the invoice's historical total.
        invoice_gross = (
            amount + invoice_discount
        ).quantize(Decimal("0.01"))

        if method == "cash":
            summary["cash"] += amount
        elif method == "card":
            summary["card"] += amount
        elif method in {"transfer", "bank", "bank transfer"}:
            summary["transfer"] += amount

        summary["gross_total"] += invoice_gross
        summary["discount"] += invoice_discount
        summary["total"] += amount

        rows.append({
            "time": to_jamaica(p.created_at),
            "customer": p.user.full_name if p.user else "",
            "invoice": p.invoice.invoice_number if p.invoice else "",
            "method": p.method,
            "gross": invoice_gross,
            "discount": invoice_discount,
            "amount": amount,
        })

    closeout = POSCloseout.query.filter_by(
        business_date=business_date
    ).first()

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
        closeout.expected_discount = summary["discount"]
        closeout.expected_total = summary["total"]

        closeout.actual_cash = actual_cash
        closeout.cash_difference = cash_difference
        closeout.notes = notes
        closeout.closed_by_admin_id = current_user.id
        closeout.closed_at = datetime.now(timezone.utc)

        db.session.flush()

        db.session.add(AuditLog(
            module="POS",
            action="POS Closeout",
            admin_id=current_user.id,
            user_id=None,
            entity_type="POSCloseout",
            entity_id=closeout.id,
            reason="Daily register closeout",
            description=(
                f"POS closeout completed for {business_date}. "
                f"Expected Cash: JMD {float(summary['cash']):,.2f}. "
                f"Actual Cash: JMD {float(actual_cash):,.2f}. "
                f"Cash Difference: JMD {float(cash_difference):,.2f}. "
                f"Card: JMD {float(summary['card']):,.2f}. "
                f"Transfer: JMD {float(summary['transfer']):,.2f}. "
                f"Discount: JMD {float(summary['discount']):,.2f}. "
                f"Total: JMD {float(summary['total']):,.2f}."
            ),
            old_value="Register open / not closed",
            new_value=(
                f"Closed By Admin ID: {current_user.id}; "
                f"Business Date: {business_date}; "
                f"Expected Total: JMD {float(summary['total']):,.2f}; "
                f"Actual Cash: JMD {float(actual_cash):,.2f}"
            ),
        ))

        db.session.commit()
        flash("POS register closed successfully.", "success")

        return redirect(
            url_for(
                "admin_pos.daily_sales",
                date=business_date.strftime("%Y-%m-%d")
            )
        )

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