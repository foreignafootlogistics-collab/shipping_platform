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
        .all()
    )

    print("POS CHECKOUT DB PACKAGE COUNT:", len(packages))
    for p in packages:
        print("POS CHECKOUT PKG:", p.id, p.status, p.is_locked, p.user_id)

    if not packages:
        print("POS CHECKOUT FAIL: no valid packages found")
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

    print("POS CHECKOUT INVALID:", invalid)
    print("POS CHECKOUT TOTAL:", total)

    if invalid:
        print("POS CHECKOUT FAIL: invalid packages")
        return jsonify({"ok": False, "error": " ".join(invalid)}), 400

    try:
        invoice = Invoice(
            user_id=user.id,
            invoice_number=f"POS-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            amount=float(total),
            amount_due=0,
            grand_total=float(total),
            status="paid",
            date_issued=datetime.now(timezone.utc),
            date_paid=datetime.now(timezone.utc),
            description=notes or "POS checkout"
        )
        db.session.add(invoice)
        db.session.flush()

        payment = Payment(
            user_id=user.id,
            invoice_id=invoice.id,
            method=payment_method,
            amount_jmd=float(total),
            transaction_type="invoice_payment",
            status="completed",
            notes=notes or "POS payment",
            source="admin"
        )
        db.session.add(payment)

        for p in packages:
            p.status = "Delivered"
            p.is_locked = True
            
        db.session.commit()
        print("POS CHECKOUT SUCCESS:", invoice.id, payment.id)

        return jsonify({
            "ok": True,
            "message": "Checkout completed successfully.",
            "invoice_id": invoice.id,
            "payment_id": payment.id,
            "total": str(total),
        })

    except Exception as e:
        db.session.rollback()
        print("POS CHECKOUT EXCEPTION:", str(e))
        return jsonify({"ok": False, "error": f"Checkout failed: {str(e)}"}), 500