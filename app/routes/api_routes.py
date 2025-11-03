# app/routes/api_routes.py
from flask import Blueprint, request, jsonify
from flask_cors import CORS

# import your existing calculator data/functions
from app.calculator_data import (
    calculate_charges,  # def calculate_charges(category, invoice_usd, weight)
    CATEGORIES,         # dict
    categories as CATEGORY_LIST,  # list(CATEGORIES.keys())
    USD_TO_JMD,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")
CORS(api_bp, resources={r"/*": {"origins": "*"}})  # relax for now; tighten later if desired

@api_bp.route("/categories", methods=["GET"])
def get_categories():
    # send exact strings your calculator expects
    return jsonify({"ok": True, "categories": CATEGORY_LIST})

@api_bp.route("/estimate", methods=["POST"])
def estimate():
    """
    Request JSON:
    {
      "category": "Clothing & Footwear",
      "invoice_usd": 120.0,
      "weight": 3.5
    }
    """
    data = request.get_json(silent=True) or {}
    try:
        category    = (data.get("category") or "").strip()
        invoice_usd = float(data.get("invoice_usd") or 0)
        weight      = float(data.get("weight") or 0)

        # call your official function (returns JMD numbers and totals)
        result = calculate_charges(category=category, invoice_usd=invoice_usd, weight=weight)

        # Return exactly your keys so the frontend can render the full breakdown
        # Your result has: base_jmd, duty, scf, envl, caf, gct, stamp, customs_total, freight, handling, freight_total, grand_total
        return jsonify({
            "ok": True,
            "inputs": {"category": category, "invoice_usd": invoice_usd, "weight": weight, "usd_to_jmd": USD_TO_JMD},
            "result": result
        }), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
