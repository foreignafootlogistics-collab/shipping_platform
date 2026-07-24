from app.models import PurchaseRequest


PAYABLE_SHOP_FOR_ME_STATUSES = {
    "awaiting_payment",
    "paid",
}


def get_shop_for_me_request(invoice):
    """
    Return the Shop For Me request linked to an invoice.

    Returns None for ordinary package, subscription,
    delivery, and other invoices.
    """
    if not invoice or not getattr(invoice, "id", None):
        return None

    return (
        PurchaseRequest.query
        .filter_by(invoice_id=invoice.id)
        .first()
    )


def shop_for_me_invoice_is_payable(invoice):
    """
    Ordinary invoices are payable.

    A Shop For Me invoice is only payable after the
    customer approves the quote.
    """
    shop_request = get_shop_for_me_request(
        invoice
    )

    if not shop_request:
        return True

    request_status = (
        shop_request.status or ""
    ).strip().lower()

    invoice_status = (
        getattr(invoice, "status", "")
        or ""
    ).strip().lower()

    return (
        request_status
        in PAYABLE_SHOP_FOR_ME_STATUSES
        and invoice_status
        not in {
            "draft",
            "quoted",
            "cancelled",
        }
    )


def sync_shop_for_me_payment_status(
    invoice,
    *,
    total_due=None,
):
    """
    Synchronize a linked Shop For Me request with its
    invoice after payments, discounts, refunds, or
    reversals are processed.

    This function does not commit. The calling route
    remains responsible for committing its transaction.
    """
    shop_request = get_shop_for_me_request(
        invoice
    )

    if not shop_request:
        return None

    request_status = (
        shop_request.status or ""
    ).strip().lower()

    invoice_status = (
        getattr(invoice, "status", "")
        or ""
    ).strip().lower()

    # Do not allow a payment calculation to activate
    # an unapproved, expired, or cancelled quote.
    if request_status in {
        "requested",
        "quoted",
        "quote_expired",
        "cancelled",
    }:
        return shop_request

    # A purchased request has already progressed beyond
    # payment. Do not move it backwards automatically.
    if request_status == "purchased":
        return shop_request

    if total_due is None:
        total_due = getattr(
            invoice,
            "amount_due",
            0,
        )

    try:
        remaining_balance = max(
            float(total_due or 0),
            0.0,
        )
    except (TypeError, ValueError):
        remaining_balance = 0.0

    invoice_is_paid = (
        invoice_status == "paid"
        or remaining_balance <= 0.01
    )

    if invoice_is_paid:
        shop_request.status = "paid"

    elif request_status == "paid":
        # A refund, payment deletion, or reversal reopened
        # the invoice.
        shop_request.status = "awaiting_payment"

    return shop_request

def _normalize_shop_tracking(value):
    """
    Normalize tracking numbers for safe matching.

    Examples:
    1Z 123-ABC -> 1Z123ABC
    1z123abc   -> 1Z123ABC
    """
    return "".join(
        character
        for character in str(
            value or ""
        ).upper()
        if character.isalnum()
    )


def link_shop_for_me_package(package):
    """
    Link a newly created/imported package to its
    purchased Shop For Me request.

    Matching requires:
    - same customer;
    - same normalized merchant tracking number;
    - request status purchased;
    - request is not already linked to another package.

    This function does not commit.
    """
    if not package:
        return None

    package_id = getattr(
        package,
        "id",
        None,
    )

    user_id = getattr(
        package,
        "user_id",
        None,
    )

    package_tracking = (
        _normalize_shop_tracking(
            getattr(
                package,
                "tracking_number",
                "",
            )
        )
    )

    if (
        not package_id
        or not user_id
        or not package_tracking
    ):
        return None

    possible_requests = (
        PurchaseRequest.query
        .filter(
            PurchaseRequest.user_id == user_id,
            PurchaseRequest.status == "purchased",
            PurchaseRequest.merchant_tracking_number.isnot(
                None
            ),
        )
        .order_by(
            PurchaseRequest.purchased_at.desc(),
            PurchaseRequest.id.desc(),
        )
        .all()
    )

    for shop_request in possible_requests:
        request_tracking = (
            _normalize_shop_tracking(
                shop_request.merchant_tracking_number
            )
        )

        if request_tracking != package_tracking:
            continue

        existing_package_id = getattr(
            shop_request,
            "package_id",
            None,
        )

        # Never move a Shop For Me request from one
        # package to a different duplicate package.
        if (
            existing_package_id
            and int(existing_package_id)
            != int(package_id)
        ):
            continue

        shop_request.package_id = package_id
        return shop_request

    return None