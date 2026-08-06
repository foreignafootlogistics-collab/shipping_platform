from decimal import Decimal, ROUND_HALF_UP


LB_TO_KG = Decimal("0.45359237")
FREIGHT_USD_PER_KG = Decimal("3.00")
UNKNOWN_PACKAGE_FEE_USD = Decimal("5.00")

WEIGHT_BANDS = (
    (Decimal("10.00"), "0 - 10 lbs", Decimal("1.60")),
    (Decimal("25.00"), "10.01 - 25 lbs", Decimal("2.15")),
    (Decimal("50.00"), "25.01 - 50 lbs", Decimal("3.65")),
    (Decimal("100.00"), "50.01 - 100 lbs", Decimal("7.00")),
    (None, "100+ lbs", Decimal("9.00")),
)


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def package_band(weight_lbs):
    weight = Decimal(str(weight_lbs or 0))
    for maximum, label, rate in WEIGHT_BANDS:
        if maximum is None or weight <= maximum:
            return label, rate
    raise RuntimeError("No supplier weight band found")


def calculate_expected_collection(packages, exchange_rate):
    rate = Decimal(str(exchange_rate or 0))
    if rate <= 0:
        raise ValueError("A valid USD to JMD exchange rate is required.")

    summary = {
        label: {"label": label, "rate_usd": float(band_rate), "quantity": 0, "subtotal_usd": 0.0}
        for _, label, band_rate in WEIGHT_BANDS
    }
    rows = []
    total_lbs = Decimal("0")
    band_total = Decimal("0")
    unknown_package_count = 0
    unknown_charge_total = Decimal("0")

    for package in packages:
        weight = Decimal(str(getattr(package, "weight", 0) or 0))
        if weight < 0:
            weight = Decimal("0")

        label, band_rate = package_band(weight)
        freight = weight * LB_TO_KG * FREIGHT_USD_PER_KG
        is_unknown_package = bool(int(getattr(package, "epc", 0) or 0))
        unknown_fee = (
            UNKNOWN_PACKAGE_FEE_USD
            if is_unknown_package
            else Decimal("0")
        )

        if is_unknown_package:
            unknown_package_count += 1
            unknown_charge_total += unknown_fee

        package_cost = band_rate + freight + unknown_fee

        summary[label]["quantity"] += 1
        summary[label]["subtotal_usd"] = float(
            money(Decimal(str(summary[label]["subtotal_usd"])) + band_rate)
        )
        total_lbs += weight
        band_total += band_rate

        user = getattr(package, "user", None)
        rows.append({
            "package_id": package.id,
            "tracking_number": getattr(package, "tracking_number", None) or "",
            "house_awb": getattr(package, "house_awb", None) or "",
            "customer_code": getattr(user, "registration_number", None) or "",
            "description": getattr(package, "description", None) or "",
            "weight_lbs": float(money(weight)),
            "weight_kg": float(money(weight * LB_TO_KG)),
            "band": label,
            "band_fee_usd": float(money(band_rate)),
            "freight_usd": float(money(freight)),
            "expected_cost_usd": float(money(package_cost)),
            "is_unknown_package": is_unknown_package,
            "unknown_fee_usd": float(money(unknown_fee)),
        })

    total_kg = total_lbs * LB_TO_KG
    freight_total = total_kg * FREIGHT_USD_PER_KG
    expected_usd = (
        band_total
        + freight_total
        + unknown_charge_total
    )
    expected_jmd = expected_usd * rate

    return {
        "package_count": len(rows),
        "total_weight_lbs": float(money(total_lbs)),
        "total_weight_kg": float(money(total_kg)),
        "freight_rate_usd_per_kg": float(FREIGHT_USD_PER_KG),
        "freight_total_usd": float(money(freight_total)),
        "band_total_usd": float(money(band_total)),
        "exchange_rate": float(money(rate)),
        "expected_total_usd": float(money(expected_usd)),
        "expected_total_jmd": float(money(expected_jmd)),
        "summary_rows": [row for row in summary.values() if row["quantity"]],
        "package_rows": rows,
        "unknown_package_count": unknown_package_count,
        "unknown_package_fee_usd": float(UNKNOWN_PACKAGE_FEE_USD),
        "unknown_charge_total_usd": float(money(unknown_charge_total)),
    }