# app/utils/rates_db.py
from __future__ import annotations
from sqlalchemy import text
from app.models import db

def _first_scalar(row, default=0.0):
    if not row:
        return default
    # row can be a Row or tuple-like; try attribute and index access
    try:
        v = list(row._mapping.values())[0]  # SQLAlchemy Row
    except Exception:
        v = row[0] if isinstance(row, (tuple, list)) else row
    try:
        return float(v) if v is not None else default
    except Exception:
        return default

def get_rate_for_weight(weight_kg: float) -> float:
    """
    Pulls the matching bracket (min max_weight >= w),
    then adds base_rate + handling_fee from settings (id=1).
    No sqlite; uses SQLAlchemy on Postgres.
    """
    try:
        w = float(weight_kg or 0)
    except Exception:
        w = 0.0

    # 1) bracket rate
    row = db.session.execute(
        text("""
            SELECT rate
            FROM rate_brackets
            WHERE max_weight >= :w
            ORDER BY max_weight ASC
            LIMIT 1
        """),
        {"w": w},
    ).first()
    bracket_rate = _first_scalar(row, 0.0)

    # If weight is above all brackets, fall back to the highest bracket
    if bracket_rate == 0.0 and w > 0:
        top = db.session.execute(
            text("SELECT rate FROM rate_brackets ORDER BY max_weight DESC LIMIT 1")
        ).first()
        bracket_rate = _first_scalar(top, 0.0)

    # 2) base and handling from settings (id = 1)
    settings = db.session.execute(
        text("SELECT base_rate, handling_fee FROM settings WHERE id = 1 LIMIT 1")
    ).first()

    if settings:
        # support both attribute and index access
        try:
            base_rate = float(settings.base_rate or 0)
            handling_fee = float(settings.handling_fee or 0)
        except Exception:
            base_rate = float(settings[0] or 0)
            handling_fee = float(settings[1] or 0)
    else:
        base_rate = 0.0
        handling_fee = 0.0

    return round(base_rate + bracket_rate + handling_fee, 2)

def get_rate_table() -> list[tuple[float, float]]:
    """
    Returns [(max_weight, rate), ...] from DB (useful for UI).
    """
    rows = db.session.execute(
        text("SELECT max_weight, rate FROM rate_brackets ORDER BY max_weight ASC")
    ).all()
    out = []
    for r in rows:
        try:
            mw = float(getattr(r, "max_weight", r[0]))
            rt = float(getattr(r, "rate", r[1]))
            out.append((mw, rt))
        except Exception:
            continue
    return out
