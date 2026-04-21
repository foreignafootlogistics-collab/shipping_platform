from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import current_user
from sqlalchemy import or_

from app.extensions import db
from app.models import User, Package, Invoice, Payment
from app.routes.admin_auth_routes import admin_required

admin_pos_bp = Blueprint("admin_pos", __name__, url_prefix="/admin/pos")


def _to_decimal(value, default="0.00"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _package_charge_amount(pkg):
    """
    Prefer grand_total if present, otherwise amount_due.
    Adjust this if your system uses a different final field.
    """
    total = getattr(pkg, "grand_total", None)
    if total not in (None, "", 0, "0"):
        return _to_decimal(total)

    amount_due = getattr(pkg, "amount_due", None)
    return _to_decimal(amount_due)


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

    all_user_packages = (
        Package.query
        .filter(Package.user_id == user.id)
        .order_by(Package.created_at.asc())
        .all()
    )

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

    print("ALL USER PACKAGES:", len(all_user_packages))
    for p in all_user_packages:
        print("ALL PKG:", p.id, p.status, p.is_locked, p.user_id)

    print("POS PACKAGE LOAD USER:", user_id)
    print("POS PACKAGE COUNT:", len(packages))

    for p in packages:
        print("PKG:", p.id, p.status, p.is_locked, p.user_id)

    rows = []
    total = Decimal("0.00")

    for p in packages:
        charge = _package_charge_amount(p)
        total += charge

        rows.append({
            "id": p.id,
            "tracking_number": p.tracking_number or "",
            "house_awb": p.house_awb or "",
            "description": p.description or p.category or "",
            "weight": str(p.weight or ""),
            "status": p.status or "",
            "amount_due": str(charge),
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

@admin_pos_bp.route("/checkout", methods=["POST"])
@admin_required
def checkout():
    data = request.get_json(silent=True) or {}
    print("POS CHECKOUT RAW DATA:", data)

    user_id = data.get("user_id")
    package_ids = data.get("package_ids") or []
    payment_method = (data.get("payment_method") or "").strip().lower()
    notes = (data.get("notes") or "").strip()

    print("POS CHECKOUT USER ID:", user_id)
    print("POS CHECKOUT PACKAGE IDS:", package_ids)
    print("POS CHECKOUT PAYMENT METHOD:", payment_method)

    if not user_id:
        print("POS CHECKOUT FAIL: missing user_id")
        return jsonify({"ok": False, "error": "Customer is required."}), 400

    if not package_ids:
        print("POS CHECKOUT FAIL: no package_ids")
        return jsonify({"ok": False, "error": "Select at least one package."}), 400

    if payment_method not in {"cash", "card", "transfer"}:
        print("POS CHECKOUT FAIL: invalid payment_method")
        return jsonify({"ok": False, "error": "Invalid payment method."}), 400

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

    print("POS CHECKOUT DB PACKAGE COUNT:", len(packages))
    for p in packages:
        print(
            "POS CHECKOUT PKG:",
            p.id,
            p.status,
            p.is_locked,
            p.user_id,
            "invoice_id=",
            p.invoice_id
        )

    if not packages:
        print("POS CHECKOUT FAIL: no valid packages found")
        return jsonify({"ok": False, "error": "No valid packages found."}), 400

    # -----------------------------
    # Validate selected packages
    # -----------------------------
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

    print("POS CHECKOUT INVALID:", invalid)
    print("POS CHECKOUT TOTAL:", total)

    if invalid:
        print("POS CHECKOUT FAIL: invalid packages")
        return jsonify({"ok": False, "error": " ".join(invalid)}), 400

    try:
        now_utc = datetime.now(timezone.utc)

        # --------------------------------------------
        # Split into:
        # 1) already invoiced packages
        # 2) packages with no invoice yet
        # --------------------------------------------
        existing_invoice_groups = {}
        uninvoiced_packages = []

        for p in packages:
            if p.invoice_id:
                existing_invoice_groups.setdefault(p.invoice_id, []).append(p)
            else:
                uninvoiced_packages.append(p)

        created_invoice_ids = []
        created_payment_ids = []

        # --------------------------------------------
        # A. Pay EXISTING invoices
        # --------------------------------------------
        for invoice_id, pkg_list in existing_invoice_groups.items():
            invoice = Invoice.query.get(invoice_id)
            if not invoice:
                db.session.rollback()
                return jsonify({
                    "ok": False,
                    "error": f"Invoice {invoice_id} not found for selected package(s)."
                }), 400

            group_total = Decimal("0.00")
            for p in pkg_list:
                group_total += _package_charge_amount(p)

            print("POS EXISTING INVOICE:", invoice.id, invoice.invoice_number, "GROUP TOTAL:", group_total)

            payment = Payment(
                user_id=user.id,
                invoice_id=invoice.id,
                method=payment_method.title(),
                amount_jmd=float(group_total),
                transaction_type="invoice_payment",
                status="completed",
                notes=notes or "POS payment",
                source="admin"
            )
            db.session.add(payment)
            db.session.flush()
            created_payment_ids.append(payment.id)

            # Reduce balance on the existing invoice
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

            # Mark selected packages as delivered/locked
            for p in pkg_list:
                p.status = "Delivered"
                p.is_locked = True

        # --------------------------------------------
        # B. Create POS invoice ONLY for uninvoiced packages
        # --------------------------------------------
        if uninvoiced_packages:
            new_invoice_total = Decimal("0.00")
            new_invoice_weight = Decimal("0.00")

            for p in uninvoiced_packages:
                new_invoice_total += _package_charge_amount(p)
                try:
                    new_invoice_weight += Decimal(str(p.weight or 0))
                except Exception:
                    pass

            pos_invoice = Invoice(
                user_id=user.id,
                invoice_number=f"POS-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                description=notes or f"POS checkout for {len(uninvoiced_packages)} package(s)",
                total_weight=float(new_invoice_weight),
                amount=float(new_invoice_total),
                amount_due=0.0,
                grand_total=float(new_invoice_total),
                date_issued=now_utc,
                date_paid=now_utc,
                created_at=now_utc,
                status="paid"
            )
            db.session.add(pos_invoice)
            db.session.flush()
            created_invoice_ids.append(pos_invoice.id)

            pos_payment = Payment(
                user_id=user.id,
                invoice_id=pos_invoice.id,
                method=payment_method.title(),
                amount_jmd=float(new_invoice_total),
                transaction_type="invoice_payment",
                status="completed",
                notes=notes or "POS payment",
                source="admin"
            )
            db.session.add(pos_payment)
            db.session.flush()
            created_payment_ids.append(pos_payment.id)

            for p in uninvoiced_packages:
                p.invoice_id = pos_invoice.id
                p.status = "Delivered"
                p.is_locked = True

        db.session.commit()

        print("POS CHECKOUT SUCCESS")
        print("CREATED INVOICE IDS:", created_invoice_ids)
        print("CREATED PAYMENT IDS:", created_payment_ids)

        return jsonify({
            "ok": True,
            "message": "Checkout completed successfully.",
            "invoice_ids": created_invoice_ids,
            "payment_ids": created_payment_ids,
            "total": str(total),
        })

    except Exception as e:
        db.session.rollback()
        print("POS CHECKOUT EXCEPTION:", str(e))
        return jsonify({
            "ok": False,
            "error": f"Checkout failed: {str(e)}"
        }), 500