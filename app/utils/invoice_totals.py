from datetime import datetime, timezone
from sqlalchemy import func

from app.extensions import db
from app.models import Invoice, Package, Payment, Discount, ShipmentLog, shipment_packages


def fetch_invoice_totals_pg(invoice_id: int):
    """
    Compute totals from DB:
      subtotal        -> invoice base total (JMD)
      discount_total  -> total discounts (JMD)
      payments_total  -> total payments (JMD)
      total_due       -> remaining balance (JMD)
    """
    inv = Invoice.query.get(invoice_id)
    if not inv:
        return 0.0, 0.0, 0.0, 0.0

    package_sum = (
        db.session.query(func.coalesce(func.sum(Package.amount_due), 0.0))
        .filter(Package.invoice_id == invoice_id)
        .scalar()
        or 0.0
    )

    subtotal = float(
        inv.grand_total
        or package_sum
        or getattr(inv, "amount", 0)
        or getattr(inv, "invoice_value", 0)
        or 0.0
    )

    discount_total = (
        db.session.query(func.coalesce(func.sum(Discount.amount_jmd), 0.0))
        .filter(Discount.invoice_id == invoice_id)
        .scalar()
        or 0.0
    )

    pay_col = Payment.amount_jmd if hasattr(Payment, "amount_jmd") else Payment.amount
    payments_total = (
        db.session.query(func.coalesce(func.sum(pay_col), 0.0))
        .filter(Payment.invoice_id == invoice_id)
        .scalar()
        or 0.0
    )

    total_due = max(subtotal - discount_total - payments_total, 0.0)
    return float(subtotal), float(discount_total), float(payments_total), float(total_due)


def mark_invoice_packages_delivered(invoice_id: int):
    """Mark all packages on invoice as delivered."""
    Package.query.filter_by(invoice_id=invoice_id).update(
        {"status": "delivered"},
        synchronize_session=False
    )

def lock_delivered_packages_for_invoice(
    invoice_id: int,
    reason: str = "Invoice fully paid",
    actor_admin_id: int | None = None,
):
    """
    Locks + marks delivered all packages on an invoice.
    Also auto-archives any ShipmentLog whose packages are now ALL delivered.

    Returns: number of packages affected
    """
    now = datetime.now(timezone.utc)

    # Get all packages on this invoice
    pkgs = Package.query.filter_by(invoice_id=invoice_id).all()
    if not pkgs:
        return 0

    # Mark packages delivered + locked
    for p in pkgs:
        p.status = "delivered"     # keep lowercase everywhere
        p.is_locked = True
        p.locked_reason = reason
        p.locked_at = now

    # AUTO-ARCHIVE shipments that became fully delivered
    # Pull shipment_ids via association table (reliable)
    pkg_ids = [p.id for p in pkgs]
    shipment_ids: set[int] = set()

    if pkg_ids:
        sid_rows = (
            db.session.query(shipment_packages.c.shipment_id)
            .filter(shipment_packages.c.package_id.in_(pkg_ids))
            .distinct()
            .all()
        )
        shipment_ids = {r[0] for r in sid_rows if r and r[0]}

    if shipment_ids:
        for sid in shipment_ids:
            sh = ShipmentLog.query.get(sid)
            if not sh or bool(getattr(sh, "is_archived", False)):
                continue

            total_pkgs = (
                db.session.query(func.count(Package.id))
                .select_from(shipment_packages)
                .join(Package, Package.id == shipment_packages.c.package_id)
                .filter(shipment_packages.c.shipment_id == sid)
                .scalar()
                or 0
            )

            delivered_pkgs = (
                db.session.query(func.count(Package.id))
                .select_from(shipment_packages)
                .join(Package, Package.id == shipment_packages.c.package_id)
                .filter(
                    shipment_packages.c.shipment_id == sid,
                    func.lower(Package.status) == "delivered",
                )
                .scalar()
                or 0
            )

            if total_pkgs > 0 and delivered_pkgs == total_pkgs:
                sh.is_archived = True
                sh.archived_at = now

                # only set if your model has the field AND you passed an admin id
                if actor_admin_id is not None and hasattr(sh, "archived_by_admin_id"):
                    sh.archived_by_admin_id = actor_admin_id

                if hasattr(sh, "archive_reason"):
                    sh.archive_reason = "AUTO_ALL_PACKAGES_DELIVERED"

    return len(pkgs)