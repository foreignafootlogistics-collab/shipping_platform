# app/utils/counters.py  (SQLAlchemy version)

from app.extensions import db
from app.models import Wallet, WalletTransaction, PendingReferral, User, Package
from datetime import datetime
from sqlalchemy import func


__all__ = [
    "update_wallet",
    "apply_referral_bonus",
    "process_first_shipment_bonus",
    "update_wallet_balance",
]


def update_wallet(user_id, amount, description):
    """
    Add/subtract amount from user's wallet and insert a wallet transaction.
    """

    user = db.session.get(User, user_id)
    if not user:
        raise ValueError(f"User {user_id} not found")

    # Adjust wallet balance safely
    current_balance = user.wallet_balance or 0
    user.wallet_balance = current_balance + float(amount)

    # Insert transaction log
    tx = WalletTransaction(
        user_id=user_id,
        amount=float(amount),
        description=description,
        type="credit" if amount > 0 else "debit",
        created_at=datetime.utcnow()
    )

    db.session.add(tx)
    db.session.commit()


def apply_referral_bonus(new_user_id, referrer_code):
    """
    During registration:
      - Link new user to referrer
      - Give new user a $100 bonus
      - Create PendingReferral for referrer
    """

    # Lookup referrer
    referrer = User.query.filter_by(referral_code=referrer_code).first()
    new_user = db.session.get(User, new_user_id)

    if not new_user:
        raise ValueError("New user does not exist")

    if not referrer:
        print("Invalid referral code")
        return

    # Link new user to referrer
    new_user.referrer_id = referrer.id

    # Give referral signup bonus
    update_wallet(new_user_id, 100, "Signup bonus (Referral)")

    # Create pending referral record (referrer gets paid after first overseas shipment)
    pending = PendingReferral(
        referrer_id=referrer.id,
        referred_email=new_user.email,
        accepted=False,
        created_at=datetime.utcnow()
    )

    db.session.add(pending)
    db.session.commit()


def process_first_shipment_bonus(user_id):
    """
    Called when a package becomes 'overseas':
      - If this is first overseas package for that user,
        reward referrer if there's a pending referral.
    """

    overseas_count = (
        Package.query
        .filter(
            Package.user_id == user_id,
            func.lower(Package.status) == "overseas"
        )
        .count()
    )


    # Only reward if first overseas package
    if overseas_count != 1:
        return

    # Check pending referral
    pending = PendingReferral.query.filter_by(
        referred_email=User.query.get(user_id).email,
        accepted=False
    ).first()

    if not pending:
        return

    referrer_id = pending.referrer_id

    # Pay referrer the $100 bonus
    update_wallet(referrer_id, 100, f"Referral bonus: User {user_id} first overseas shipment")

    # Mark referral as completed
    pending.accepted = True

    db.session.commit()


def update_wallet_balance(user_id, amount, description):
    """
    Simple helper — same as update_wallet but kept for backwards compatibility.
    """

    update_wallet(user_id, amount, description)
