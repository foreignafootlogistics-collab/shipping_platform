# ===========================
# Calculator Data & Functions
# ===========================

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

# ===== STEP 2: Freight Rate Table =====


def get_freight(weight):
    from app.models import AdminRate  # lazy import to avoid circular import
    weight = int(round(weight))

    bracket = AdminRate.query.filter(AdminRate.max_weight >= weight).order_by(AdminRate.max_weight.asc()).first()
    if bracket:
        return bracket.rate

    last_bracket = AdminRate.query.order_by(AdminRate.max_weight.desc()).first()
    if last_bracket:
        extra = weight - last_bracket.max_weight
        return last_bracket.rate + extra * 500

    return 0

# ===== STEP 3: Constants =====
USD_TO_JMD = 165

# ===== STEP 4: Calculation Function =====
def calculate_charges(category, invoice_usd, weight):
    """
    Calculate customs and freight charges for a shipment.
    If invoice <= $100, all customs charges are 0.
    """
    rates = CATEGORIES.get(category, {"duty": 20, "gct": 16.5})
    base_jmd = invoice_usd * USD_TO_JMD

    if invoice_usd <= 100:
        duty = scf = envl = caf = gct = stamp = 0
        customs_total = 0
    else:
        duty = base_jmd * (rates["duty"] / 100)
        scf = base_jmd * 0.003
        envl = base_jmd * 0.005
        caf = 2500
        gct = (base_jmd + duty + scf + envl + caf) * (rates["gct"] / 100)
        stamp = 100
        customs_total = duty + scf + envl + caf + gct + stamp

    # Freight & Handling
    freight = get_freight(weight)
    handling = 0
    if 40 < weight <= 50:
        handling = 2000
    elif 51 <= weight <= 60:
        handling = 3000
    elif 61 <= weight <= 80:
        handling = 5000
    elif 81 <= weight <= 100:
        handling = 10000
    elif weight > 100:
        handling = 20000

    freight_total = freight + handling
    grand_total = customs_total + freight_total

    return {
        "base_jmd": round(base_jmd, 2),
        "duty": round(duty, 2),
        "scf": round(scf, 2),
        "envl": round(envl, 2),
        "caf": round(caf, 2),
        "gct": round(gct, 2),
        "stamp": round(stamp, 2),
        "customs_total": round(customs_total, 2),
        "freight": round(freight, 2),
        "handling": round(handling, 2),
        "freight_total": round(freight_total, 2),
        "grand_total": round(grand_total, 2),
    }
