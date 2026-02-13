from app.extensions import db
from app.models import Settings, Package

def get_settings():
    # assuming you keep one row
    return Settings.query.first()

def apply_breakdown_to_package(pkg: Package, breakdown: dict, lock: bool = True):
    """
    Save the full breakdown into the Package row.
    breakdown keys expected:
    duty,gct,scf,envl,caf,stamp,customs_total,
    freight,handling,other_charges,freight_total,
    grand_total
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

    # other charges already exists on Package
    pkg.other_charges = float(breakdown.get("other_charges", pkg.other_charges or 0) or 0)

    pkg.freight_total = float(breakdown.get("freight_total", 0) or 0)
    pkg.grand_total   = float(breakdown.get("grand_total", 0) or 0)

    # amount_due should match grand_total (unless you do discounts later)
    pkg.amount_due = pkg.grand_total

    pkg.pricing_locked = bool(lock)
