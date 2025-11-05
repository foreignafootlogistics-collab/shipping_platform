# app/utils/registration.py

import re
from typing import Optional

from app.extensions import db

REG_PREFIX = "FAFL"
REG_WIDTH = 5  # FAFL10001, FAFL10059, etc.

_reg_re = re.compile(r"^FAFL(\d+)$", re.IGNORECASE)


def _extract_number(reg: str) -> Optional[int]:
    if not reg:
        return None
    m = _reg_re.match(reg.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _format_reg(n: int) -> str:
    return f"{REG_PREFIX}{n:0{REG_WIDTH}d}"


def _max_reg_from_users() -> int:
    """
    Scan users.registration_number, pull the numeric part,
    and return the max number found. If none, seed sensibly.
    """
    rows = db.session.execute(
        db.text("SELECT registration_number FROM users WHERE registration_number IS NOT NULL")
    ).fetchall()

    max_n = 0
    for (reg,) in rows:
        n = _extract_number(str(reg))
        if n and n > max_n:
            max_n = n

    # Seed if database has no FAFL numbers yet; start from 10000 so first is FAFL10001
    if max_n == 0:
        max_n = 10000
    return max_n


def next_registration_number() -> str:
    """
    Generates the next unique FAFL registration number using the current DB contents.
    Safe for Postgres; no sqlite access.
    """
    # Try a SQL max() using Postgres regexp; fallback to Python scan if not available
    try:
        row = db.session.execute(
            db.text("""
                SELECT MAX(
                    CAST( REGEXP_REPLACE(LOWER(registration_number), '^fafl', '') AS INTEGER )
                ) AS max_num
                FROM users
                WHERE registration_number ILIKE 'FAFL%'
            """)
        ).first()
        max_num = (row.max_num or 0)
        if not max_num:
            max_num = _max_reg_from_users()
    except Exception:
        max_num = _max_reg_from_users()

    next_num = max_num + 1
    return _format_reg(next_num)
