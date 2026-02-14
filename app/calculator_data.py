# ===========================
# Calculator Data & Functions
# ===========================

import math

# --- Default category name ---
DEFAULT_CATEGORY = "Other"

# ===== STEP 1: Categories & Duty/GCT Rates =====
CATEGORIES = {
    "Clothing & Footwear": {"duty": 20, "gct": 16.5},
    "Laptops/Computers": {"duty": 0, "gct": 16.5},
    "Beauty, Cosmetics & Perfume": {"duty": 20, "gct": 16.5},
    "Appliances & Furniture": {"duty": 20, "gct": 16.5},
    "Car Accessories": {"duty": 20, "gct": 16.5},
    "Auto Parts": {"duty": 30, "gct": 16.5},
    "Textbooks": {"duty": 0, "gct": 16.5},
    "Sporting Goods / Exercise Equipment": {"duty": 20, "gct": 16.5},
    "Food, Groceries, Vitamins": {"duty": 20, "gct": 16.5},
    "Handbags, Luggage, Accessories": {"duty": 20, "gct": 16.5},
    "Small Kitchenware & Tools": {"duty": 20, "gct": 16.5},
    "Baby Items": {"duty": 20, "gct": 16.5},
    "Toys & Hobby Items": {"duty": 20, "gct": 16.5},
    "Pet Supplies": {"duty": 20, "gct": 16.5},
    "Lighting & Home Décor": {"duty": 20, "gct": 16.5},
    "Musical Instruments & Audio Gear": {"duty": 20, "gct": 16.5},
    "Protective / Work Gear": {"duty": 20, "gct": 16.5},
    "Seasonal / Holiday Items": {"duty": 20, "gct": 16.5},
    "Paper Goods": {"duty": 20, "gct": 16.5},
    "Cellphone": {"duty": 20, "gct": 25},
    "Television": {"duty": 20, "gct": 16.5},
    "Jewellery": {"duty": 20, "gct": 16.5},
    "Hair": {"duty": 20, "gct": 16.5},
    "Audio Equipment": {"duty": 25, "gct": 16.5},
    "Other": {"duty": 20, "gct": 16.5},
}

categories = list(CATEGORIES.keys())  # for dropdown choices


# ===== STEP 2: Constants (fallbacks only) =====
USD_TO_JMD_FALLBACK = 165.0
USD_TO_JMD = USD_TO_JMD_FALLBACK  
DIMINIMIS_USD_FALLBACK = 100.0


def _to_float(x, default=0.0):
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def normalize_category(category: str) -> str:
    if not category:
        return DEFAULT_CATEGORY
    category = str(category).strip()
    if category not in CATEGORIES:
        return DEFAULT_CATEGORY
    return category


def _round_weight(w_raw: float, method: str = "round_up") -> float:
    """
    Settings.weight_round_method options we support:
      - "round_up" (default)
      - "nearest"
      - "none"
    """
    w_raw = _to_float(w_raw, 0.0)
    if w_raw <= 0:
        return 0.0

    m = (method or "round_up").lower().strip()
    if m in ("nearest", "round_nearest"):
        return float(int(round(w_raw)))
    if m in ("none", "exact"):
        return float(w_raw)

    # default round_up
    return float(int(math.ceil(w_raw)))


def get_freight(weight, *, settings=None) -> float:
    """
    Returns FREIGHT ONLY (JMD). Handling is calculated separately in calculate_charges().
    Uses:
      - Settings special below 1lb rates
      - AdminRate brackets for >= 1lb (and <= 100lb)
      - Settings per_lb_above_100_jmd for > 100lb (if configured)
    """
    from app.models import AdminRate, Settings

    if settings is None:
        settings = Settings.query.get(1)

    w_raw = _to_float(weight, 0.0)
    if w_raw <= 0:
        return 0.0

    # rounding rules
    round_method = getattr(settings, "weight_round_method", "round_up") if settings else "round_up"
    w_rounded = _round_weight(w_raw, round_method)

    # minimum billable weight
    min_billable = int(_to_float(getattr(settings, "min_billable_weight", 1), 1)) if settings else 1
    if w_rounded > 0 and w_rounded < min_billable:
        w_rounded = float(min_billable)

    # ---- BELOW 1LB special ----
    if w_raw < 1:
        special = _to_float(getattr(settings, "special_below_1lb_jmd", 0), 0) if settings else 0
        per_01 = _to_float(getattr(settings, "per_0_1lb_below_1lb_jmd", 0), 0) if settings else 0

        # bill per 0.1lb steps for weights below 1lb
        steps = int(math.ceil(w_raw * 10.0))  # 0.1lb ->1 step, 0.9lb ->9 steps
        return float(special + (steps * per_01))

    # ---- ABOVE 100LB optional rule ----
    if w_rounded > 100:
        per_lb = _to_float(getattr(settings, "per_lb_above_100_jmd", 0), 0) if settings else 0
        if per_lb > 0:
            return float(w_rounded * per_lb)

    # ---- Use AdminRate table (rate_brackets) ----
    w_int = int(w_rounded)
    bracket = (
        AdminRate.query
        .filter(AdminRate.max_weight >= w_int)
        .order_by(AdminRate.max_weight.asc())
        .first()
    )
    if bracket:
        return float(bracket.rate or 0)

    # fallback if no brackets exist
    last_bracket = AdminRate.query.order_by(AdminRate.max_weight.desc()).first()
    if last_bracket:
        extra = w_int - int(last_bracket.max_weight or 0)
        return float(last_bracket.rate or 0) + max(extra, 0) * 500.0

    return 0.0


def calculate_charges(category, invoice_usd, weight, *, settings=None):
    """
    Calculate customs and freight charges for a shipment.
    - Item value is USD.
    - ALL fees returned are JMD.
    - Does NOT include manual package other_charges (those live on the Package row).
    """
    category = normalize_category(category)
    rates = CATEGORIES.get(category, CATEGORIES[DEFAULT_CATEGORY])

    if settings is None:
        from app.models import Settings
        settings = Settings.query.get(1)

    # ---------- SETTINGS ----------
    customs_enabled = True if (settings and getattr(settings, "customs_enabled", None) is None) else bool(getattr(settings, "customs_enabled", True)) if settings else True


    usd_to_jmd = _to_float(getattr(settings, "customs_exchange_rate", None), USD_TO_JMD_FALLBACK) if settings else USD_TO_JMD_FALLBACK
    diminimis_usd = _to_float(getattr(settings, "diminis_point_usd", None), DIMINIMIS_USD_FALLBACK) if settings else DIMINIMIS_USD_FALLBACK

    scf_rate_percent = _to_float(getattr(settings, "scf_rate", None), 0.3) if settings else 0.3
    envl_rate_percent = _to_float(getattr(settings, "envl_rate", None), 0.5) if settings else 0.5
    scf_rate = scf_rate_percent / 100.0
    envl_rate = envl_rate_percent / 100.0

    stamp = _to_float(getattr(settings, "stamp_duty_jmd", None), 100) if settings else 100
    caf = _to_float(getattr(settings, "caf_residential_jmd", None), 2500) if settings else 2500

    # Category rates
    gct_rate_percent = _to_float(rates.get("gct", 16.5), 16.5)
    duty_rate_percent = _to_float(rates.get("duty", 20), 20)

    # ---------- INPUTS ----------
    invoice_usd = _to_float(invoice_usd, 0.0)
    weight_raw = _to_float(weight, 0.0)

    # base value in JMD
    base_jmd = invoice_usd * usd_to_jmd

    # ---------- CUSTOMS ----------
    if (not customs_enabled) or (invoice_usd <= diminimis_usd):
        duty = scf = envl = gct = 0.0
        stamp_val = 0.0
        caf_val = 0.0
        customs_total = 0.0
    else:
        duty = base_jmd * (duty_rate_percent / 100.0)
        scf = base_jmd * scf_rate
        envl = base_jmd * envl_rate

        caf_val = caf
        stamp_val = stamp

        gct = (base_jmd + duty + scf + envl + caf_val) * (gct_rate_percent / 100.0)
        customs_total = duty + scf + envl + caf_val + gct + stamp_val

    # ---------- FREIGHT ----------
    freight = _to_float(get_freight(weight_raw, settings=settings), 0.0)

    # ---------- HANDLING ----------
    # keep your original handling rules, BUT support Settings handling_above_100_jmd
    w_for_handling = weight_raw
    if settings and _to_float(getattr(settings, "handling_above_100_jmd", 0), 0) > 0 and w_for_handling > 100:
        handling = _to_float(getattr(settings, "handling_above_100_jmd", 0), 0)
    else:
        handling = 0.0
        w = _to_float(w_for_handling, 0.0)
        if 40 < w <= 50:
            handling = 2000
        elif 51 <= w <= 60:
            handling = 3000
        elif 61 <= w <= 80:
            handling = 5000
        elif 81 <= w <= 100:
            handling = 10000
        elif w > 100:
            handling = 20000

    freight_total = freight + handling
    grand_total = customs_total + freight_total

    return {
        "category": category,
        "base_jmd": round(base_jmd, 2),

        "duty": round(duty, 2),
        "scf": round(scf, 2),
        "envl": round(envl, 2),
        "caf": round(caf_val, 2) if customs_enabled and invoice_usd > diminimis_usd else 0.0,
        "gct": round(gct, 2),
        "stamp": round(stamp_val, 2),

        "customs_total": round(customs_total, 2),

        "freight": round(freight, 2),
        "handling": round(handling, 2),
        "freight_total": round(freight_total, 2),

        # manual other_charges live on Package
        "other_charges": 0.0,

        "grand_total": round(grand_total, 2),
    }
