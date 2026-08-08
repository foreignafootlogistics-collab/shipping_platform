from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_

from app.extensions import db
from app.models import Invoice, Payment, ScheduledDelivery


EXCLUDED_INVOICE_STATUSES = {
    "draft",
    "quoted",
    "cancelled",
    "canceled",
    "void",
    "voided",
}

CLOSED_INVOICE_STATUSES = {
    "paid",
}

COMPLETED_PAYMENT_STATUSES = {
    "completed",
    "settled",
}

CASH_PAYMENT_TYPES = {
    "invoice_payment",
    "subscription_payment",
    "subscription_upgrade_payment",
}

WAIVER_PAYMENT_TYPES = {
    "subscription_waiver",
}

BALANCE_CREDIT_TYPES = (
    CASH_PAYMENT_TYPES
    | WAIVER_PAYMENT_TYPES
)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _normalized(value) -> str:
    return str(value or "").strip().lower()


def invoice_net_total(invoice) -> Decimal:
    """
    Return the final invoice value after discount.

    grand_total is already the discounted total, so the discount
    must not be subtracted from it again.
    """
    grand_total = getattr(invoice, "grand_total", None)

    if grand_total is not None:
        return max(
            _decimal(grand_total),
            Decimal("0.00"),
        )

    amount_due = getattr(invoice, "amount_due", None)

    if amount_due is not None:
        return max(
            _decimal(amount_due),
            Decimal("0.00"),
        )

    amount = getattr(invoice, "amount", None)

    if amount is not None:
        return max(
            _decimal(amount),
            Decimal("0.00"),
        )

    subtotal = _decimal(
        getattr(
            invoice,
            "subtotal_before_discount",
            0,
        )
    )

    discount = _decimal(
        getattr(
            invoice,
            "discount_total",
            0,
        )
    )

    return max(
        subtotal - discount,
        Decimal("0.00"),
    )


def _customer_invoice_query(user):
    conditions = []

    if hasattr(Invoice, "user_id"):
        conditions.append(
            Invoice.user_id == user.id
        )

    if hasattr(Invoice, "customer_id"):
        conditions.append(
            Invoice.customer_id == user.id
        )

    registration_number = (
        getattr(user, "registration_number", None)
        or ""
    ).strip()

    if (
        registration_number
        and hasattr(Invoice, "customer_code")
    ):
        conditions.append(
            Invoice.customer_code == registration_number
        )

    query = Invoice.query

    if conditions:
        query = query.filter(or_(*conditions))
    else:
        query = query.filter(False)

    return query


def calculate_customer_balance_summary(user) -> dict:
    """
    Shared customer balance calculation used by both the
    customer dashboard and the administrator account page.
    """
    invoices = _customer_invoice_query(user).all()

    invoice_ids = [
        invoice.id
        for invoice in invoices
    ]

    payments_by_invoice = defaultdict(list)

    if invoice_ids:
        payments = (
            Payment.query
            .filter(Payment.invoice_id.in_(invoice_ids))
            .all()
        )

        for payment in payments:
            payments_by_invoice[
                payment.invoice_id
            ].append(payment)

    total_invoice_value = Decimal("0.00")
    recorded_payments = Decimal("0.00")
    subscription_covered = Decimal("0.00")
    invoice_outstanding = Decimal("0.00")
    pending_invoice_count = 0

    for invoice in invoices:
        invoice_status = _normalized(
            getattr(invoice, "status", None)
        )

        # Draft, cancelled and void invoices are not financial charges.
        if invoice_status in EXCLUDED_INVOICE_STATUSES:
            continue

        net_total = invoice_net_total(invoice)
        total_invoice_value += net_total

        invoice_cash = Decimal("0.00")
        invoice_waiver = Decimal("0.00")

        for payment in payments_by_invoice.get(
            invoice.id,
            [],
        ):
            payment_status = _normalized(
                getattr(payment, "status", None)
            )

            if payment_status not in COMPLETED_PAYMENT_STATUSES:
                continue

            transaction_type = _normalized(
                getattr(
                    payment,
                    "transaction_type",
                    None,
                )
            )

            amount = _decimal(
                getattr(
                    payment,
                    "amount_jmd",
                    getattr(payment, "amount", 0),
                )
            )

            if transaction_type in CASH_PAYMENT_TYPES:
                invoice_cash += amount

            elif transaction_type in WAIVER_PAYMENT_TYPES:
                invoice_waiver += amount

        recorded_payments += invoice_cash
        subscription_covered += invoice_waiver

        # A paid invoice must not appear as outstanding.
        if invoice_status in CLOSED_INVOICE_STATUSES:
            continue

        balance_credits = (
            invoice_cash
            + invoice_waiver
        )

        outstanding = max(
            net_total - balance_credits,
            Decimal("0.00"),
        )

        if outstanding > Decimal("0.01"):
            invoice_outstanding += outstanding
            pending_invoice_count += 1

    unpaid_delivery_fees = Decimal("0.00")
    pending_delivery_count = 0

    deliveries = (
        ScheduledDelivery.query
        .filter(ScheduledDelivery.user_id == user.id)
        .all()
    )

    for delivery in deliveries:
        delivery_status = _normalized(
            getattr(delivery, "status", None)
        )

        fee_status = _normalized(
            getattr(delivery, "fee_status", None)
        )

        if delivery_status in {
            "cancelled",
            "canceled",
        }:
            continue

        if fee_status in {
            "paid",
            "waived",
        }:
            continue

        delivery_fee = _decimal(
            getattr(delivery, "delivery_fee", 0)
        )

        if delivery_fee > Decimal("0.01"):
            unpaid_delivery_fees += delivery_fee
            pending_delivery_count += 1

    total_customer_owing = (
        invoice_outstanding
        + unpaid_delivery_fees
    )

    return {
        "total_invoice_value": float(total_invoice_value),
        "recorded_payments": float(recorded_payments),
        "subscription_covered": float(subscription_covered),
        "invoice_outstanding": float(invoice_outstanding),
        "unpaid_delivery_fees": float(unpaid_delivery_fees),
        "total_customer_owing": float(total_customer_owing),
        "pending_invoice_count": pending_invoice_count,
        "pending_delivery_count": pending_delivery_count,
        "pending_charge_count": (
            pending_invoice_count
            + pending_delivery_count
        ),
    }