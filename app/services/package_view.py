from datetime import datetime
from sqlalchemy import func
from app.extensions import db
from app.models import Package, PackageAttachment, User, Invoice, Payment

def _parse_dt_maybe(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None

def _effective_value_dict(p_dict: dict) -> float:
    # declared_value ALWAYS wins
    dv = p_dict.get("declared_value")
    if dv is not None:
        try:
            return float(dv)
        except Exception:
            return 0.0
    v = p_dict.get("value")
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def _extract_package_from_row(row):
    # Already a Package ORM object
    if isinstance(row, Package):
        return row

    # SQLAlchemy Row
    if hasattr(row, "_mapping"):
        for v in row._mapping.values():
            if isinstance(v, Package):
                return v

    # tuple/list fallback
    if isinstance(row, (tuple, list)):
        for v in row:
            if isinstance(v, Package):
                return v

    return None


def fetch_packages_normalized(
    *,
    base_query,
    include_user=True,
    include_attachments=True,
):
    """
    base_query SHOULD return rows like:
      (Package, User.full_name, User.registration_number)
    but we handle Row objects safely too.
    """

    rows = base_query.all()

    # -------- helper: extract Package from Row / tuple / object ----------
    def _get_pkg(row):
        # tuple style: (Package, ...)
        if isinstance(row, tuple) and len(row) > 0:
            return row[0]

        # SQLAlchemy Row style
        if hasattr(row, "_mapping"):
            m = row._mapping

            # common cases
            if "Package" in m:
                return m["Package"]
            if Package in m:
                return m[Package]

            # fallback: first value that looks like a model instance
            for v in m.values():
                if hasattr(v, "__table__") and getattr(v, "__tablename__", None) == Package.__tablename__:
                    return v

            # last resort: first mapping value
            return next(iter(m.values()), None)

        # already ORM instance
        return row

    # 1) collect package ids + invoice ids
    pkg_ids = []
    invoice_ids = set()

    for row in rows:
        pkg = _get_pkg(row)
        if not pkg:
            continue
        pid = getattr(pkg, "id", None)
        if pid is not None:
            pkg_ids.append(pid)

        inv_id = getattr(pkg, "invoice_id", None)
        if inv_id:
            invoice_ids.add(inv_id)

    # 2) attachments in ONE query
    attachments_by_pkg = {}
    if include_attachments and pkg_ids:
        att_rows = (
            db.session.query(
                PackageAttachment.id,
                PackageAttachment.package_id,
                PackageAttachment.original_name,
                PackageAttachment.file_name,
            )
            .filter(PackageAttachment.package_id.in_(pkg_ids))
            .order_by(PackageAttachment.id.desc())
            .all()
        )
        for att_id, pkg_id, original_name, file_name in att_rows:
            attachments_by_pkg.setdefault(pkg_id, []).append({
                "id": att_id,
                "original_name": original_name,
                "file_name": file_name,
            })

    # 3) invoice paid map (Invoice grand_total - sum(Payments))
    invoice_meta = {}
    if invoice_ids:
        pay_rows = (
            db.session.query(
                Invoice.id.label("invoice_id"),
                func.coalesce(Invoice.grand_total, Invoice.amount, 0).label("total"),
                func.coalesce(func.sum(Payment.amount_jmd), 0).label("paid_sum"),
                Invoice.status.label("status"),
            )
            .outerjoin(Payment, Payment.invoice_id == Invoice.id)
            .filter(Invoice.id.in_(list(invoice_ids)))
            .group_by(Invoice.id, Invoice.grand_total, Invoice.amount, Invoice.status)
            .all()
        )

        for inv_id, total, paid_sum, status in pay_rows:
            total = float(total or 0)
            paid_sum = float(paid_sum or 0)
            balance = max(total - paid_sum, 0.0)

            # treat status paid OR balance <= 0 as paid
            is_paid = (str(status or "").strip().lower() == "paid") or (balance <= 0.00001)

            invoice_meta[int(inv_id)] = {
                "total": total,
                "paid_sum": paid_sum,
                "balance": balance,
                "is_paid": is_paid,
            }

    # 4) normalize to dicts
    out = []
    for row in rows:
        pkg = _get_pkg(row)
        if not pkg:
            continue

        # user fields (depending on how query was built)
        full_name = None
        reg = None
        if include_user:
            if isinstance(row, tuple):
                full_name = row[1] if len(row) > 1 else None
                reg = row[2] if len(row) > 2 else None
            elif hasattr(row, "_mapping"):
                m = row._mapping
                full_name = m.get("full_name") or m.get(User.full_name) or None
                reg = m.get("registration_number") or m.get(User.registration_number) or None

        inv_id = getattr(pkg, "invoice_id", None)
        meta = invoice_meta.get(int(inv_id)) if inv_id else None

        d = {
            "id": getattr(pkg, "id", None),
            "user_id": getattr(pkg, "user_id", None),
            "full_name": full_name,
            "registration_number": reg,

            "tracking_number": getattr(pkg, "tracking_number", None),
            "house_awb": getattr(pkg, "house_awb", None),
            "description": getattr(pkg, "description", None),

            "status": getattr(pkg, "status", None),
            "weight": float(getattr(pkg, "weight", 0) or 0),

            "date_received": _parse_dt_maybe(getattr(pkg, "date_received", None)),
            "created_at": _parse_dt_maybe(getattr(pkg, "created_at", None)),

            "declared_value": getattr(pkg, "declared_value", None),
            "value": getattr(pkg, "value", None),

            "amount_due": float(getattr(pkg, "amount_due", 0) or 0),

            "invoice_id": inv_id,
            "epc": int(getattr(pkg, "epc", 0) or 0),

            "attachments": attachments_by_pkg.get(getattr(pkg, "id", None), []),

            # ✅ NEW
            "invoice_paid": bool(meta["is_paid"]) if meta else False,
            "invoice_balance": float(meta["balance"]) if meta else None,
        }

        d["effective_value"] = _effective_value_dict(d)
        out.append(d)

    return out
