# ===========================
# Calculator Data & Functions
# ===========================

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


def normalize_category(category: str) -> str:
    """
    Make sure any missing/unknown category becomes 'Other'.
    """
    if not category:
        return DEFAULT_CATEGORY
    category = str(category).strip()
    if category not in CATEGORIES:
        return DEFAULT_CATEGORY
    return category

# ===== STEP 2: Freight Rate Table =====


def get_freight(weight):
    from math import ceil
    from app.models import AdminRate  # lazy import to avoid circular import

    w_raw = float(weight or 0)
    weight = int(ceil(w_raw))
    if weight < 1 and w_raw > 0:
        weight = 1

    bracket = (AdminRate.query
               .filter(AdminRate.max_weight >= weight)
               .order_by(AdminRate.max_weight.asc())
               .first())
    if bracket:
        return float(bracket.rate or 0)

    last_bracket = AdminRate.query.order_by(AdminRate.max_weight.desc()).first()
    if last_bracket:
        extra = weight - int(last_bracket.max_weight or 0)
        return float(last_bracket.rate or 0) + extra * 500

    return 0.0

# ===== STEP 3: Constants (fallbacks only) =====
USD_TO_JMD = 165
DIMINIMIS_USD_FALLBACK = 100


def _to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)


def calculate_charges(category, invoice_usd, weight, *, settings=None):
    """
    Calculate customs and freight charges for a shipment.
    - Item value is USD.
    - ALL fees returned are JMD.
    - Does NOT include manual package other_charges (those live on the Package row).
    """

    # ✅ Normalize category
    category = normalize_category(category)
    rates = CATEGORIES.get(category, CATEGORIES[DEFAULT_CATEGORY])

    # ✅ Load Settings (optional injection for performance/testing)
    if settings is None:
        from app.models import Settings
        settings = Settings.query.get(1)

    # ✅ Pull rates from Settings with safe fallbacks
    customs_enabled = bool(getattr(settings, "customs_enabled", True)) if settings else True

    usd_to_jmd = _to_float(getattr(settings, "customs_exchange_rate", None), USD_TO_JMD_FALLBACK) if settings else USD_TO_JMD_FALLBACK
    diminimis_usd = _to_float(getattr(settings, "diminis_point_usd", None), DIMINIMIS_USD_FALLBACK) if settings else DIMINIMIS_USD_FALLBACK

    # Settings stores SCF/ENVL as percent (e.g. 0.3 means 0.3%)
    scf_rate_percent = _to_float(getattr(settings, "scf_rate", None), 0.3) if settings else 0.3
    envl_rate_percent = _to_float(getattr(settings, "envl_rate", None), 0.5) if settings else 0.5
    scf_rate = scf_rate_percent / 100.0
    envl_rate = envl_rate_percent / 100.0

    stamp = _to_float(getattr(settings, "stamp_duty_jmd", None), 100) if settings else 100
    caf = _to_float(getattr(settings, "caf_residential_jmd", None), 2500) if settings else 2500

    # GCT: keep your existing category mapping (Cellphone is 25, others 16.5 by your dict)
    gct_rate_percent = _to_float(rates.get("gct", 16.5), 16.5)

    base_jmd = _to_float(invoice_usd, 0.0) * usd_to_jmd

    # -------------------------
    # Customs block (JMD)
    # -------------------------
    if (not customs_enabled) or (_to_float(invoice_usd, 0.0) <= diminimis_usd):
        duty = scf = envl = gct = 0.0
        stamp_val = 0.0
        caf_val = 0.0
        customs_total = 0.0
    else:
        duty_rate_percent = _to_float(rates.get("duty", 20), 20)
        duty = base_jmd * (duty_rate_percent / 100.0)

        scf = base_jmd * scf_rate
        envl = base_jmd * envl_rate

        caf_val = caf
        stamp_val = stamp

        gct = (base_jmd + duty + scf + envl + caf_val) * (gct_rate_percent / 100.0)

        customs_total = duty + scf + envl + caf_val + gct + stamp_val

    # -------------------------
    # Freight & Handling (JMD)
    # -------------------------
    freight = _to_float(get_freight(weight), 0.0)

    handling = 0.0
    w = _to_float(weight, 0.0)
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
        "caf": round(caf_val, 2) if customs_enabled and _to_float(invoice_usd, 0.0) > diminimis_usd else 0.0,
        "gct": round(gct, 2),
        "stamp": round(stamp_val, 2),

        "customs_total": round(customs_total, 2),

        "freight": round(freight, 2),
        "handling": round(handling, 2),
        "freight_total": round(freight_total, 2),

        # IMPORTANT: calculator "other_charges" should be 0; manual other_charges live on Package row.
        "other_charges": 0.0,

        "grand_total": round(grand_total, 2),
    }
