# app/utils/referrals.py
import random
import string

from app.extensions import db
from app.models import User


def generate_unique_referral_code(length: int = 8) -> str:
    """
    Generate a unique referral code (e.g. '7G9XK2PA').
    Ensures no collision in the users table.
    """
    chars = string.ascii_uppercase + string.digits

    while True:
        code = ''.join(random.choices(chars, k=length))
        # Check if this code already exists
        existing = User.query.filter_by(referral_code=code).first()
        if not existing:
            return code


def ensure_user_referral_code(user: User) -> str:
    """
    Ensure the given user has a referral_code.
    If missing, generate one, save to DB, and return it.
    """
    if not user.referral_code:
        user.referral_code = generate_unique_referral_code()
        db.session.commit()
    return user.referral_code
