from datetime import datetime, timezone

from app.extensions import db
from app.models import Prealert, PackageAttachment


def upsert_prealert_from_package(pkg) -> Prealert | None:
    """
    Package ➜ Prealert
    Create or update a Prealert for the same customer + tracking number.
    Safe to call multiple times.
    """
    tracking = (getattr(pkg, "tracking_number", "") or "").strip()
    if not tracking:
        return None

    customer_id = getattr(pkg, "user_id", None)
    if not customer_id:
        return None

    pa = (Prealert.query
          .filter(
              Prealert.customer_id == customer_id,
              Prealert.tracking_number.ilike(tracking),
          )
          .order_by(Prealert.created_at.desc(), Prealert.id.desc())
          .first())

    if not pa:
        pa = Prealert(
            customer_id=customer_id,
            tracking_number=tracking,
        )
        db.session.add(pa)
        db.session.flush()  # ensure pa.id exists

    _maybe_set(pa, "description", getattr(pkg, "description", None))
    _maybe_set(pa, "house_awb", getattr(pkg, "house_awb", None))
    _maybe_set(pa, "weight", getattr(pkg, "weight", None))
    _maybe_set(pa, "value", getattr(pkg, "value", None))

    _maybe_set(pa, "linked_package_id", getattr(pkg, "id", None))
    _maybe_set(pa, "linked_at", datetime.now(timezone.utc))

    return pa


def _maybe_set(obj, field, value):
    if not hasattr(obj, field):
        return
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    setattr(obj, field, value)


def sync_prealert_invoice_to_package(pkg) -> bool:
    """
    Prealert ➜ Package
    If a Prealert exists for this package's user + tracking number,
    and it has an invoice, create a PackageAttachment on the package.

    Safe to call multiple times (won't duplicate).
    """
    tracking = (getattr(pkg, "tracking_number", "") or "").strip()
    if not tracking:
        return False

    pa = (Prealert.query
          .filter(
              Prealert.customer_id == pkg.user_id,
              Prealert.tracking_number.ilike(tracking),
          )
          .order_by(Prealert.created_at.desc(), Prealert.id.desc())
          .first())

    if not pa:
        return False

    invoice_url = (getattr(pa, "invoice_filename", None) or "").strip()
    if not invoice_url:
        return False

    # If already linked to a different package, do nothing
    if pa.linked_package_id and pa.linked_package_id != pkg.id:
        return False

    pub_id = (getattr(pa, "invoice_public_id", None) or "").strip()
    rtype  = (getattr(pa, "invoice_resource_type", None) or "").strip() or "raw"
    orig   = (getattr(pa, "invoice_original_name", None) or "").strip() or "prealert_invoice"

    # Dedup: prefer matching cloud_public_id
    existing = None
    if pub_id:
        existing = (PackageAttachment.query
                    .filter_by(package_id=pkg.id, cloud_public_id=pub_id)
                    .first())

    # Fallback dedup: match same url
    if not existing:
        existing = (PackageAttachment.query
                    .filter_by(package_id=pkg.id, file_url=invoice_url)
                    .first())

    if existing:
        # mark linked if not yet marked (NO commit here)
        if not pa.linked_package_id:
            pa.linked_package_id = pkg.id
            pa.linked_at = datetime.now(timezone.utc)
            db.session.flush()
        return True

    # Create attachment row from prealert invoice
    db.session.add(PackageAttachment(
        package_id=pkg.id,
        file_name=invoice_url,          # legacy
        file_url=invoice_url,           # required NOT NULL
        original_name=orig,
        cloud_public_id=(pub_id or None),
        cloud_resource_type=(rtype or None),
    ))

    # Optional: mirror onto package main invoice field too
    if not (getattr(pkg, "invoice_file", None) or "").strip():
        pkg.invoice_file = invoice_url

    # Mark prealert linked
    pa.linked_package_id = pkg.id
    pa.linked_at = datetime.now(timezone.utc)

    db.session.flush()
    return True


def sync_package_and_prealert(pkg) -> bool:
    """
    1) Package ➜ Prealert (create/update)
    2) Prealert invoice ➜ PackageAttachment
    Commit ONCE here.
    """
    upsert_prealert_from_package(pkg)
    synced = sync_prealert_invoice_to_package(pkg)

    db.session.commit()
    return synced