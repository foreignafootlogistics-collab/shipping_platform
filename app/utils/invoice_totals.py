from datetime import datetime, timezone

from sqlalchemy import func

from app.extensions import db
from app.models import (
    Invoice,
    Package,
    Payment,
    Discount,
    ShipmentLog,
    shipment_packages,
)
from app.utils.scheduled_pickups import (
    sync_scheduled_pickups_for_delivered_package,
)


def fetch_invoice_totals_pg(invoice_id: int):
    """
    Calculate the live invoice totals from the database.

    Returns:
        subtotal:
            Invoice total before discounts.

        discount_total:
            Total invoice discount.

        payments_total:
            Total completed invoice payments only.
            Pending, cancelled and settled placeholder records
            are not counted.

        total_due:
            Remaining invoice balance.
    """

    inv = Invoice.query.get(invoice_id)

    if not inv:
        return 0.0, 0.0, 0.0, 0.0

    # ---------------------------------------------------------
    # Package total
    # ---------------------------------------------------------
    package_sum = (
        db.session.query(
            func.coalesce(
                func.sum(Package.amount_due),
                0.0,
            )
        )
        .filter(Package.invoice_id == invoice_id)
        .scalar()
        or 0.0
    )

    # ---------------------------------------------------------
    # Invoice subtotal before discount
    # ---------------------------------------------------------
    subtotal = float(
        getattr(inv, "subtotal_before_discount", 0)
        or package_sum
        or getattr(inv, "grand_total", 0)
        or getattr(inv, "amount", 0)
        or getattr(inv, "invoice_value", 0)
        or 0.0
    )

    subtotal = round(
        max(subtotal, 0.0),
        2,
    )

    # ---------------------------------------------------------
    # Discount rows attached to the invoice
    # ---------------------------------------------------------
    discount_rows_total = (
        db.session.query(
            func.coalesce(
                func.sum(Discount.amount_jmd),
                0.0,
            )
        )
        .filter(Discount.invoice_id == invoice_id)
        .scalar()
        or 0.0
    )

    discount_rows_total = round(
        max(float(discount_rows_total), 0.0),
        2,
    )

    # ---------------------------------------------------------
    # Discount saved directly on the Invoice
    # ---------------------------------------------------------
    saved_discount_total = round(
        max(
            float(
                getattr(inv, "discount_total", 0)
                or 0.0
            ),
            0.0,
        ),
        2,
    )

    # Older invoices may use Discount rows, while newer invoices
    # may store the total directly on Invoice.discount_total.
    #
    # Use the larger value rather than adding them together so
    # that the same discount is not counted twice.
    discount_total = max(
        discount_rows_total,
        saved_discount_total,
    )

    # A discount cannot exceed the invoice subtotal.
    if discount_total > subtotal:
        discount_total = subtotal

    discount_total = round(discount_total, 2)

    # ---------------------------------------------------------
    # Completed invoice payments
    # ---------------------------------------------------------
    payments_q = (
        db.session.query(
            func.coalesce(
                func.sum(Payment.amount_jmd),
                0.0,
            )
        )
        .filter(Payment.invoice_id == invoice_id)
    )

    if hasattr(Payment, "transaction_type"):
        payments_q = payments_q.filter(
            Payment.transaction_type == "invoice_payment"
        )

    if hasattr(Payment, "status"):
        payments_q = payments_q.filter(
            func.lower(Payment.status) == "completed"
        )

    payments_total = payments_q.scalar() or 0.0

    payments_total = round(
        max(float(payments_total), 0.0),
        2,
    )

    # ---------------------------------------------------------
    # Remaining balance
    # ---------------------------------------------------------
    total_due = round(
        max(
            subtotal
            - discount_total
            - payments_total,
            0.0,
        ),
        2,
    )

    return (
        float(subtotal),
        float(discount_total),
        float(payments_total),
        float(total_due),
    )


def mark_invoice_packages_delivered(invoice_id: int):
    """
    Mark every package attached to an invoice as delivered.

    This function does not commit the database transaction.
    The calling route is responsible for committing.
    """

    Package.query.filter_by(
        invoice_id=invoice_id
    ).update(
        {
            "status": "delivered",
        },
        synchronize_session=False,
    )


def lock_delivered_packages_for_invoice(
    invoice_id: int,
    reason: str = "Invoice fully paid",
    actor_admin_id: int | None = None,
):
    """
    Mark and lock every package attached to an invoice.

    If every package in a shipment has been delivered, the
    shipment is automatically archived.

    Args:
        invoice_id:
            ID of the invoice whose packages should be locked.

        reason:
            Reason stored against the locked packages.

        actor_admin_id:
            Administrator responsible for the action.

    Returns:
        Number of packages affected.

    This function does not commit the database transaction.
    The calling route is responsible for committing.
    """

    now_utc = datetime.now(timezone.utc)

    # ---------------------------------------------------------
    # Get all packages attached to the invoice
    # ---------------------------------------------------------
    packages = Package.query.filter_by(
        invoice_id=invoice_id
    ).all()

    if not packages:
        return 0

    # ---------------------------------------------------------
    # Mark packages Delivered and lock them
    # ---------------------------------------------------------
    for package in packages:
        package.status = "delivered"
        package.is_locked = True
        package.locked_reason = reason
        package.locked_at = now_utc

        if (
            actor_admin_id is not None
            and hasattr(package, "locked_by_admin_id")
        ):
            package.locked_by_admin_id = actor_admin_id

    # ---------------------------------------------------------
    # Close completed store pickup requests
    # ---------------------------------------------------------
    for package in packages:
        sync_scheduled_pickups_for_delivered_package(
            package,
            now_utc
        )

    # ---------------------------------------------------------
    # Find shipments containing these packages
    # ---------------------------------------------------------
    package_ids = [
        package.id
        for package in packages
    ]

    shipment_ids: set[int] = set()

    if package_ids:
        shipment_id_rows = (
            db.session.query(
                shipment_packages.c.shipment_id
            )
            .filter(
                shipment_packages.c.package_id.in_(
                    package_ids
                )
            )
            .distinct()
            .all()
        )

        shipment_ids = {
            row[0]
            for row in shipment_id_rows
            if row and row[0]
        }

    # ---------------------------------------------------------
    # Archive shipments when every package is delivered
    # ---------------------------------------------------------
    if shipment_ids:
        for shipment_id in shipment_ids:
            shipment = ShipmentLog.query.get(
                shipment_id
            )

            if not shipment:
                continue

            if bool(
                getattr(
                    shipment,
                    "is_archived",
                    False,
                )
            ):
                continue

            total_packages = (
                db.session.query(
                    func.count(Package.id)
                )
                .select_from(shipment_packages)
                .join(
                    Package,
                    Package.id
                    == shipment_packages.c.package_id,
                )
                .filter(
                    shipment_packages.c.shipment_id
                    == shipment_id
                )
                .scalar()
                or 0
            )

            delivered_packages = (
                db.session.query(
                    func.count(Package.id)
                )
                .select_from(shipment_packages)
                .join(
                    Package,
                    Package.id
                    == shipment_packages.c.package_id,
                )
                .filter(
                    shipment_packages.c.shipment_id
                    == shipment_id,
                    func.lower(Package.status)
                    == "delivered",
                )
                .scalar()
                or 0
            )

            if (
                total_packages > 0
                and delivered_packages == total_packages
            ):
                shipment.is_archived = True
                shipment.archived_at = now_utc

                if (
                    actor_admin_id is not None
                    and hasattr(
                        shipment,
                        "archived_by_admin_id",
                    )
                ):
                    shipment.archived_by_admin_id = (
                        actor_admin_id
                    )

                if hasattr(shipment, "archive_reason"):
                    shipment.archive_reason = (
                        "AUTO_ALL_PACKAGES_DELIVERED"
                    )

    return len(packages)