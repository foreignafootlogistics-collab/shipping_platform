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
    s = _get_settings_row()

    brackets = (
        AdminRate.query
        .order_by(AdminRate.max_weight.asc())
        .all()
    )

    bracket_rows = []
    for b in brackets:
        bracket_rows.append({
            "max_weight_lbs": int(b.max_weight),
            "rate_jmd": float(b.rate or 0),
        })


    result = {
        "currency_code": s.currency_code or "JMD",
        "currency_symbol": s.currency_symbol or "$",

        # Freight tab (Settings)
        "special_below_1lb_jmd": float(s.special_below_1lb_jmd or 0),
        "per_0_1lb_below_1lb_jmd": float(s.per_0_1lb_below_1lb_jmd or 0),
        "min_billable_weight": int(s.min_billable_weight or 1),
        "per_lb_above_100_jmd": float(s.per_lb_above_100_jmd or 0),
        "handling_fee_jmd": float(s.handling_fee or 0),
        "handling_above_100_jmd": float(s.handling_above_100_jmd or 0),
        "weight_round_method": s.weight_round_method or "round_up",

        # Customs tab (optional to display on website)
        "customs_enabled": bool(s.customs_enabled),
        "customs_exchange_rate": float(s.customs_exchange_rate or 0),
        "diminis_point_usd": float(s.diminis_point_usd or 0),
        "default_duty_rate": float(s.default_duty_rate or 0),
        "insurance_rate": float(s.insurance_rate or 0),
        "scf_rate": float(s.scf_rate or 0),
        "envl_rate": float(s.envl_rate or 0),
        "stamp_duty_jmd": float(s.stamp_duty_jmd or 0),
        "gct_25_rate": float(s.gct_25_rate or 0),
        "gct_15_rate": float(s.gct_15_rate or 0),
        "caf_residential_jmd": float(s.caf_residential_jmd or 0),
        "caf_commercial_jmd": float(s.caf_commercial_jmd or 0),

        # Weight brackets
        "brackets": bracket_rows,
    }

        # Return both for convenience:
    return jsonify(ok=True, brackets=bracket_rows, rates=result)
