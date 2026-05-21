# app/utils/registration.py

import re
from typing import Optional

from app.extensions import db

REG_PREFIX = "FAFL"
REG_WIDTH = 5  # fallback only

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

    if max_n == 0:
        max_n = 10000

    return max_n


def _get_registration_format():
    """
    Returns dynamic registration settings.
    Falls back safely if settings row does not exist yet.
    """
    from app.models import Settings

    settings = db.session.get(Settings, 1)

    prefix = REG_PREFIX
    width = REG_WIDTH

    if settings:
        prefix = settings.registration_prefix or REG_PREFIX
        width = settings.registration_number_width or REG_WIDTH

    try:
        width = int(width)
    except Exception:
        width = REG_WIDTH

    if width < 1:
        width = REG_WIDTH

    return prefix.strip().upper(), width


def _format_reg(n: int, prefix: str | None = None, width: int | None = None) -> str:
    prefix = (prefix or REG_PREFIX).strip().upper()
    width = int(width or REG_WIDTH)
    return f"{prefix}{int(n):0{width}d}"


def next_registration_number() -> str:
    """
    Generates the next registration number using the Counter table.

    IMPORTANT:
    - Counter value means LAST USED number.
    - Next customer receives counter.value + 1.
    - If the number already exists, keep moving forward until an unused number is found.
    - Do NOT commit here. The registration route commits the user + counter together.
    """
    from app.models import Counter, User

    prefix, width = _get_registration_format()

    counter = db.session.get(Counter, "registration_number")

    if not counter:
        max_num = _max_reg_from_users()
        counter = Counter(name="registration_number", value=max_num)
        db.session.add(counter)
        db.session.flush()

    next_num = int(counter.value or 10000) + 1

    while True:
        reg_number = _format_reg(next_num, prefix, width)

        exists = User.query.filter_by(registration_number=reg_number).first()

        if not exists:
            counter.value = next_num
            db.session.flush()
            return reg_number

        next_num += 1