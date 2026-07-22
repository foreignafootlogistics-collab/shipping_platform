from datetime import datetime, timezone

from app.models import ScheduledPickup


def sync_scheduled_pickups_for_delivered_package(
    package,
    completed_at=None
):
    """
    Close active store pickup requests when all linked packages
    have been physically delivered/collected.

    This function does not commit. The calling route must commit.
    """
    completed_at = completed_at or datetime.now(timezone.utc)

    active_pickups = (
        package.scheduled_pickups
        .filter(
            ScheduledPickup.status.in_(
                ["Scheduled", "Ready"]
            )
        )
        .all()
    )

    for pickup in active_pickups:
        linked_packages = pickup.packages.all()

        if not linked_packages:
            continue

        all_delivered = all(
            (linked_package.status or "").strip().lower()
            == "delivered"
            for linked_package in linked_packages
        )

        if all_delivered:
            pickup.status = "Collected"
            pickup.completed_at = completed_at