from flask import Blueprint, jsonify, request
from flask_cors import CORS
from app.calculator_data import CATEGORIES, categories as CATEGORY_LIST, calculate_charges, USD_TO_JMD

api_bp = Blueprint("api", __name__, url_prefix="/api")
CORS(api_bp, resources={r"/*": {"origins": "*"}})

@api_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "status": "up"})

@api_bp.route("/categories", methods=["GET"])
def get_categories():
    return jsonify({"ok": True, "categories": CATEGORY_LIST})

@api_bp.route("/estimate", methods=["POST"])
def estimate():
    data = request.get_json(silent=True) or {}
    category    = (data.get("category") or "").strip()
    invoice_usd = float(data.get("invoice_usd") or 0)
    weight      = float(data.get("weight") or 0)
    result = calculate_charges(category=category, invoice_usd=invoice_usd, weight=weight)
    return jsonify({
        "ok": True,
        "inputs": {"category": category, "invoice_usd": invoice_usd, "weight": weight, "usd_to_jmd": USD_TO_JMD},
        "result": result
    })
