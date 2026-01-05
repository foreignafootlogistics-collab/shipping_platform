from flask import Blueprint, jsonify, request
from app.extensions import db, csrf
from app.models import Settings, AdminRate
from app.calculator_data import calculate_charges, CATEGORIES

public_api_bp = Blueprint("public_api", __name__, url_prefix="/public-api")

def _get_settings_row():
    s = db.session.get(Settings, 1)
    if not s:
        s = Settings(id=1)
        db.session.add(s)
        db.session.commit()
    return s

@public_api_bp.get("/categories")
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
def rates():
    settings = _get_settings_row()

    brackets = AdminRate.query.order_by(AdminRate.max_weight.asc()).all()

    out = []
    for b in brackets:
        rate_jmd = float(b.rate or 0)
        per_lb = (rate_jmd / float(b.max_weight or 1)) if b.max_weight else 0
        out.append({
            "max_weight_lbs": int(b.max_weight),
            "rate_jmd": rate_jmd,
            "per_lb_jmd": round(per_lb, 2),
        })

    return jsonify({
        "ok": True,
        "settings": {
            "currency_code": settings.currency_code,
            "currency_symbol": settings.currency_symbol,
            "usd_to_jmd": float(settings.usd_to_jmd or 0),
            "handling_fee": float(settings.handling_fee or 0),
        },
        "brackets": out,
    })
