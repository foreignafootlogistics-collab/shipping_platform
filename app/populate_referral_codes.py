# app/populate_referral_codes.py
# SQLAlchemy-only helpers for referral codes (no sqlite3 / DB_PATH).

from __future__ import annotations
import secrets
import string
from typing import Optional

from sqlalchemy import text
from app.extensions import db
from app.models import User

ALPHABET = string.ascii_uppercase + string.digits


def _random_code(length: int = 8) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _is_code_taken(code: str) -> bool:
    return db.session.execute(
        text("SELECT 1 FROM users WHERE referral_code = :c LIMIT 1"),
        {"c": code},
    ).scalar() is not None


def generate_referral_code(user: Optional[User] = None, length: int = 8) -> str:
    """
    Public API kept for back-compat with auth_routes import.
    If a User is provided, caller may set user.referral_code = generated and commit.
    """
    for _ in range(25):
        code = _random_code(length)
        if not _is_code_taken(code):
            return code
    for _ in range(25):
        code = _random_code(length + 2)
        if not _is_code_taken(code):
            return code
    return _random_code(length + 4)


def assign_missing_referral_codes(commit: bool = True) -> int:
    """Backfill: assign codes to users missing referral_code. Returns count."""
    updated = 0
    users = db.session.query(User).filter(
        (User.referral_code.is_(None)) | (User.referral_code == "")
    ).all()

    for u in users:
        u.referral_code = generate_referral_code(u)
        updated += 1

    if updated and commit:
        db.session.commit()
    return updated
