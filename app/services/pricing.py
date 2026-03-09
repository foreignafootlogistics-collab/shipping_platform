from app.extensions import db
from app.models import Settings, Package

def get_settings():
    # assuming you keep one row
    return Settings.query.first()

def apply_breakdown_to_package(pkg: Package, breakdown: dict, lock: bool = True):
    """
    Save the full breakdown into the Package row.
    """
    pkg.duty  = float(breakdown.get("duty", 0) or 0)
    pkg.gct   = float(breakdown.get("gct", 0) or 0)
    pkg.scf   = float(breakdown.get("scf", 0) or 0)
    pkg.envl  = float(breakdown.get("envl", 0) or 0)
    pkg.caf   = float(breakdown.get("caf", 0) or 0)
    pkg.stamp = float(breakdown.get("stamp", 0) or 0)

    pkg.customs_total = float(breakdown.get("customs_total", 0) or 0)

    pkg.freight_fee  = float(breakdown.get("freight", 0) or 0)
    pkg.handling_fee = float(breakdown.get("handling", 0) or 0)

    # keep normal "other charges" separate
    pkg.other_charges = float(breakdown.get("other_charges", pkg.other_charges or 0) or 0)

    # ✅ AUTO BAD ADDRESS FEE
    settings = get_settings()
    default_bad_address_fee = float(
        getattr(settings, "bad_address_fee_jmd", 500) or 500
    )

    epc_flag = bool(getattr(pkg, "epc", False))
    manual_bad_address = bool(getattr(pkg, "bad_address", False))

    if epc_flag or manual_bad_address:
        pkg.bad_address = True
        if float(getattr(pkg, "bad_address_fee", 0) or 0) <= 0:
            pkg.bad_address_fee = default_bad_address_fee
    else:
        pkg.bad_address = False
        pkg.bad_address_fee = 0.0

    pkg.freight_total = float(breakdown.get("freight_total", 0) or 0)

    # ✅ grand total must include bad address fee
    base_grand_total = float(breakdown.get("grand_total", 0) or 0)
    pkg.grand_total = base_grand_total + float(pkg.bad_address_fee or 0)

    pkg.amount_due = pkg.grand_total
    pkg.pricing_locked = bool(lock)