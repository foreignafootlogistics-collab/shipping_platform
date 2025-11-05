from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from app.calculator_data import calculate_charges, CATEGORIES, get_freight
from flask_wtf.csrf import validate_csrf, CSRFError
from wtforms import ValidationError

calculator_bp = Blueprint('calculator', __name__)

@calculator_bp.route("/calculate", methods=["POST"], strict_slashes=False)
@login_required
def calculator_ajax():
    """
    AJAX endpoint to calculate customs charges based on category, invoice, and weight.
    Accepts CSRF token from JSON payload.
    """
    from app.models import CalculatorLog, db  # Lazy import to prevent circular import

    # Read JSON payload
    data = request.get_json()
    if not data:
        return jsonify({"error": "No input data provided"}), 400

    # Validate CSRF token manually
    csrf_token = data.get("csrf_token")
    try:
        validate_csrf(csrf_token)
    except CSRFError as e:
        return jsonify({"error": f"CSRF validation failed: {str(e)}"}), 400

    # Extract inputs
    category = data.get("category")
    invoice_usd = data.get("invoice_usd")
    weight = data.get("weight")

    # ----- Manual validation -----
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

    # ----- Perform calculation -----
    try:
        result = calculate_charges(category, invoice_usd, weight)

        # Log the calculation
        calc_log = CalculatorLog(
            user_id=current_user.id,
            category=data['category'],
            value_usd=data['invoice_usd'],  # ✅ correct
            duty_amount=result['duty'],
            scf_amount=result['scf'],
            envl_amount=result['envl'],
            caf_amount=result['caf'],
            gct_amount=result['gct'],
            total_amount=result['grand_total']
        )

        db.session.add(calc_log)
        db.session.commit()

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Calculation failed: {str(e)}"}), 500
