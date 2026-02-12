from datetime import datetime, timezone
from sqlalchemy import func

from app.extensions import db
from app.models import Invoice, Package, Payment, Discount


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
