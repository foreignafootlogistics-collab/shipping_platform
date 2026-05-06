# app/utils/registration.py

import re
from typing import Optional

from app.extensions import db

REG_PREFIX = "FAFL"
REG_WIDTH = 5  # FAFL10001, FAFL10059, etc.

_reg_re = re.compile(r"^[A-Z]+(\d+)$", re.IGNORECASE)


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
    Generates the next registration number using the Counter table.
    Example: FAFL10001, FAFL10002, etc.
    """
    from app.models import Settings, Counter

    settings = db.session.get(Settings, 1)

    prefix = "FAFL"
    width = 5

    if settings:
        prefix = settings.registration_prefix or "FAFL"
        width = settings.registration_number_width or 5

    counter = db.session.get(Counter, "registration_number")

    if not counter:
        # Seed from existing users so we do not accidentally reuse old numbers
        max_num = _max_reg_from_users()
        counter = Counter(name="registration_number", value=max_num)
        db.session.add(counter)
        db.session.flush()

    counter.value += 1

    reg_number = f"{prefix}{counter.value:0{width}d}"

    db.session.commit()

    return reg_number