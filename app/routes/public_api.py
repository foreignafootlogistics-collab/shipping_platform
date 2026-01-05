from flask import Blueprint, jsonify, request
from app.extensions import db, csrf
from app.models import Settings, AdminRate
from app.calculator_data import calculate_charges, CATEGORIES


public_api_bp = Blueprint("public_api", __name__, url_prefix="/api")

def get_settings():
    s = db.session.get(Settings, 1)
    return s

@public_api_bp.get("/categories")
@csrf.exempt
def categories():
    return jsonify(ok=True, categories=list(CATEGORIES.keys()))

@public_api_bp.post("/estimate")
@csrf.exempt
def estimate():
    data = request.get_json(silent=True) or {}
    category = data.get("category")
    invoice_usd = float(data.get("invoice_usd") or 0)
    weight = float(data.get("weight") or 0)

    result = calculate_charges(category, invoice_usd, weight)
    return jsonify(ok=True, result=result)

@public_api_bp.get("/rates")
@csrf.exempt
def rates():
    # Example: expose settings + AdminRate freight table for your website rates page
    s = get_settings()
    brackets = AdminRate.query.order_by(AdminRate.max_weight.asc()).all()

    return jsonify(
        ok=True,
        settings={
            "usd_to_jmd": getattr(s, "usd_to_jmd", 165) if s else 165,
            "base_rate": getattr(s, "base_rate", 0) if s else 0,
            "handling_fee": getattr(s, "handling_fee", 0) if s else 0,
            "min_billable_weight": getattr(s, "min_billable_weight", 1) if s else 1,
            "per_lb_above_100_jmd": getattr(s, "per_lb_above_100_jmd", 500) if s else 500,
        },
        freight_brackets=[
            {"max_weight": r.max_weight, "rate_jmd": float(r.rate_jmd or r.rate or 0)}
            for r in brackets
        ],
    )

