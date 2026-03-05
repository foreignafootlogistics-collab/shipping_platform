# app/utils/shipment_archive.py
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from sqlalchemy import func, case, distinct
from app.extensions import db
from app.models import ShipmentLog, Package, ShipmentArchiveLog, shipment_packages

DELIVERED = "DELIVERED"


def _utcnow():
    return datetime.now(timezone.utc)


def shipment_is_fully_delivered(shipment_id: int) -> bool:
    """
    True iff shipment has >=1 package and ALL are DELIVERED.
    Uses shipment_packages M2M table.
    """
    total, delivered = (
        db.session.query(
            func.count(distinct(Package.id)),
            func.coalesce(
                func.sum(case((func.upper(Package.status) == DELIVERED, 1), else_=0)),
                0
            ),
        )
        .select_from(shipment_packages)
        .join(Package, Package.id == shipment_packages.c.package_id)
        .filter(shipment_packages.c.shipment_id == shipment_id)
        .one()
    )

    total = int(total or 0)
    delivered = int(delivered or 0)
    return total > 0 and total == delivered


def archive_shipment(
    shipment: ShipmentLog,
    actor_admin_id: int | None,
    reason: str = "AUTO_ALL_DELIVERED",
) -> bool:
    """
    Archive shipment if eligible. Returns True if it changed.
    """
    if not shipment:
        return False

    # already archived?
    if bool(getattr(shipment, "is_archived", False)) or getattr(shipment, "archived_at", None):
        return False

    # optional: prevent auto-archive if an override window exists
    now = _utcnow()
    if hasattr(shipment, "unarchive_override_until"):
        until = getattr(shipment, "unarchive_override_until", None)
        if until and until > now:
            return False

    if not shipment_is_fully_delivered(shipment.id):
        return False

    shipment.is_archived = True
    shipment.archived_at = now
    shipment.archived_by_admin_id = actor_admin_id

    # optional: store reason if you have the column
    if hasattr(shipment, "archive_reason"):
        shipment.archive_reason = reason

    db.session.add(
        ShipmentArchiveLog(
            shipment_id=shipment.id,
            action="ARCHIVE",
            reason=reason,
            actor_admin_id=actor_admin_id,
        )
    )
    return True


def unarchive_shipment(
    shipment: ShipmentLog,
    actor_admin_id: int | None,
    reason: str = "MANUAL",
    override_days: int = 7,
) -> bool:
    """
    Unarchive shipment. Returns True if it changed.
    Also sets override window if column exists (prevents immediate auto-rearchive).
    """
    if not shipment:
        return False

    if not bool(getattr(shipment, "is_archived", False)) and getattr(shipment, "archived_at", None) is None:
        return False

    shipment.is_archived = False
    shipment.archived_at = None
    shipment.archived_by_admin_id = None

    # optional: store reason + override window if columns exist
    if hasattr(shipment, "archive_reason"):
        shipment.archive_reason = reason

    if hasattr(shipment, "unarchive_override_until"):
        shipment.unarchive_override_until = _utcnow() + timedelta(days=int(override_days or 0))

    db.session.add(
        ShipmentArchiveLog(
            shipment_id=shipment.id,
            action="UNARCHIVE",
            reason=reason,
            actor_admin_id=actor_admin_id,
        )
    )
    return True


def sync_auto_archive_for_eligible_shipments(limit: int = 200) -> int:
    """
    Safety net: archive any non-archived shipments where all packages are delivered.
    Uses M2M table. Returns how many shipments were archived.
    """
    # Find shipment_ids where total == delivered
    rows = (
        db.session.query(
            shipment_packages.c.shipment_id.label("sid"),
            func.count(distinct(Package.id)).label("total"),
            func.coalesce(
                func.sum(case((func.upper(Package.status) == DELIVERED, 1), else_=0)),
                0
            ).label("delivered"),
        )
        .select_from(shipment_packages)
        .join(Package, Package.id == shipment_packages.c.package_id)
        .group_by(shipment_packages.c.shipment_id)
        .having(func.count(distinct(Package.id)) > 0)
        .having(
            func.count(distinct(Package.id))
            == func.coalesce(func.sum(case((func.upper(Package.status) == DELIVERED, 1), else_=0)), 0)
        )
        .limit(int(limit or 200))
        .all()
    )

    if not rows:
        return 0

    sids = [r.sid for r in rows if r.sid]
    if not sids:
        return 0

    q = ShipmentLog.query.filter(
        ShipmentLog.id.in_(sids),
        ShipmentLog.is_archived.is_(False),
    )

    # optional: exclude override window shipments if column exists
    now = _utcnow()
    if hasattr(ShipmentLog, "unarchive_override_until"):
        q = q.filter(
            (ShipmentLog.unarchive_override_until.is_(None)) |
            (ShipmentLog.unarchive_override_until <= now)
        )

    shipments = q.all()

    changed = 0
    for s in shipments:
        if archive_shipment(s, actor_admin_id=None, reason="AUTO_ALL_DELIVERED"):
            changed += 1

    if changed:
        db.session.commit()

    return changed