# app/calculator.py
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from flask_wtf.csrf import validate_csrf, CSRFError

from app.calculator_data import calculate_charges, CATEGORIES

calculator_bp = Blueprint("calculator", __name__, url_prefix="/calculator")


@calculator_bp.route("/calculate", methods=["POST"], strict_slashes=False)
@login_required
def calculator_ajax():
    """
    Global calculator endpoint used by BOTH admin + customer + shipment modal.
    Expects JSON:
      { csrf_token, category, invoice_usd, weight }
    Returns:
      calculate_charges() result dict
    """
    # Lazy import to prevent circular import
    from app.models import CalculatorLog
    from app.extensions import db

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "No input data provided"}), 400

    # CSRF
    csrf_token = data.get("csrf_token")
    try:
        validate_csrf(csrf_token)
    except CSRFError as e:
        return jsonify({"error": f"CSRF validation failed: {str(e)}"}), 400

    # Inputs
    category = (data.get("category") or "").strip()
    invoice_usd = data.get("invoice_usd")
    weight = data.get("weight")

    errors = []

    if category not in CATEGORIES:
        errors.append(f"Invalid category: {category}")

    try:
        invoice_usd = float(invoice_usd)
        if invoice_usd < 0:
            errors.append("Invoice USD must be non-negative")
    except (TypeError, ValueError):
        errors.append("Invoice USD must be a number")

    try:
        weight = float(weight)
        if weight <= 0:
            errors.append("Weight must be greater than zero")
    except (TypeError, ValueError):
        errors.append("Weight must be a number")

    if errors:
        return jsonify({"error": errors}), 400

    try:
        result = calculate_charges(category, invoice_usd, weight)

        # Optional log (safe)
        try:
            calc_log = CalculatorLog(
                user_id=current_user.id,
                category=category,
                value_usd=invoice_usd,
                duty_amount=float(result.get("duty") or 0),
                scf_amount=float(result.get("scf") or 0),
                envl_amount=float(result.get("envl") or 0),
                caf_amount=float(result.get("caf") or 0),
                gct_amount=float(result.get("gct") or 0),
                total_amount=float(result.get("grand_total") or 0),
            )
            db.session.add(calc_log)
            db.session.commit()
        except Exception:
            db.session.rollback()

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": f"Calculation failed: {str(e)}"}), 500
