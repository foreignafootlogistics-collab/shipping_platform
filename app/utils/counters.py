# app/utils/counters.py

from app.extensions import db
from app.models import Counter

__all__ = ["ensure_counters_exist", "next_bill_number", "next_invoice_number"]


def ensure_counters_exist():
    """
    Ensure required counters exist.
    Safe for Postgres & SQLite.
    """
    required = ["invoice_seq", "bill_seq", "shipment_seq"]

    for name in required:
        if not Counter.query.get(name):
            db.session.add(Counter(name=name, value=0))

    db.session.commit()


def _increment(name: str) -> int:
    """
    Atomically increment a named counter using SELECT...FOR UPDATE.

    Works on Postgres and SQLite.
    """
    # Obtain row with FOR UPDATE lock
    counter = (
        db.session.query(Counter)
        .with_for_update()     # prevents race conditions
        .filter(Counter.name == name)
        .first()
    )

    if not counter:
        counter = Counter(name=name, value=0)
        db.session.add(counter)

    counter.value += 1
    db.session.commit()

    return counter.value


def next_invoice_number():
    """
    Returns sequential invoice number like 'INV00001'.
    """
    seq = _increment("invoice_seq")
    return f"INV{seq:05d}"


def next_bill_number():
    """
    Returns sequential bill number like 'BILL00001'.
    """
    seq = _increment("bill_seq")
    return f"BILL{seq:05d}"
