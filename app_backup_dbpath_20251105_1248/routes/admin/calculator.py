from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from app.calculator_data import calculate_charges, CATEGORIES, get_freight
from app.forms import AdminCalculatorForm

admin_calculator_bp = Blueprint(
    "admin_calculator", __name__, url_prefix="/admin/calculator"
)

@admin_calculator_bp.route("/", methods=["GET", "POST"])
@login_required
def admin_calculator():
    form = AdminCalculatorForm()

    # Handle AJAX request
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        try:
            category = request.json.get("category")
            invoice_usd = float(request.json.get("invoice_usd", 0))
            weight = float(request.json.get("weight", 0))

            result = calculate_charges(category, invoice_usd, weight)
            return jsonify(result)  # must return a dict with required fields

        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # Render the modal template with the form
    return render_template(
        "admin/admin_calculator_modal.html",
        admin_calculator_form=form,  # ✅ pass the form with the expected name
        categories=CATEGORIES.keys()
    )
