from flask import Blueprint, request, jsonify
from app.calculator_data import calculate_charges, categories

public_api_bp = Blueprint("public_api", __name__)

@public_api_bp.get("/api/categories")
def api_categories():
    return jsonify({
        "ok": True,
        "categories": categories
    }), 200


@public_api_bp.post("/api/estimate")
def api_estimate():
    data = request.get_json(silent=True) or {}

    category = data.get("category", "Other")
    invoice_usd = data.get("invoice_usd", 0)
    weight = data.get("weight", 0)

    try:
        invoice_usd = float(invoice_usd or 0)
        weight = float(weight or 0)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid numbers"}), 400

    if weight <= 0:
        return jsonify({"ok": False, "error": "Weight must be greater than 0"}), 400

    try:
        result = calculate_charges(category, invoice_usd, weight)
        return jsonify({"ok": True, "result": result}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

