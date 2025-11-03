# app/utils/ids.py
from datetime import datetime
from sqlalchemy import desc
from app.extensions import db
from app.models import ShipmentLog

def next_shipment_log_id():
    """Return next ID like SL-YYYYMMDD-00001 (ascending per day)."""
    date_str = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"SL-{date_str}-"

    # Find the last sequence for today
    last = (
        db.session.query(ShipmentLog)
        .filter(ShipmentLog.sl_id.like(prefix + "%"))
        .order_by(desc(ShipmentLog.sl_id))
        .first()
    )
    if last:
        try:
            seq = int(last.sl_id.split("-")[-1])
        except Exception:
            seq = 0
    else:
        seq = 0

    return f"{prefix}{seq + 1:05d}"
