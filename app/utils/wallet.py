from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models import (
    Wallet,
    WalletTransaction,
    PendingReferral,
    User,
    Package,
)


__all__ = [
    "update_wallet",
    "apply_referral_bonus",
    "process_first_shipment_bonus",
    "update_wallet_balance",
    "debit_wallet_for_payment",
    "is_wallet_method",
]


def is_wallet_method(method):
    """
    Return True when the selected payment method is Wallet.
    """
    normalized = str(method or "").strip().lower()

    return normalized in {
        "wallet",
        "e-wallet",
        "ewallet",
        "wallet payment",
    }


def _change_wallet_balance(
    *,
    user_id,
    amount,
    description,
    transaction_type=None,
    action=None,
    reason=None,
    invoice_number=None,
    package_id=None,
    admin_id=None,
    commit=False,
):
    """
    Change the customer's wallet balance and create an audit transaction.

    This function does not commit unless commit=True. Payment routes should
    leave commit=False so the payment and wallet deduction are committed or
    rolled back together.
    """

    amount = round(float(amount or 0), 2)

    if amount == 0:
        raise ValueError("Wallet transaction amount cannot be zero.")

    user = (
        User.query
        .filter(User.id == user_id)
        .with_for_update()
        .first()
    )

    if not user:
        raise ValueError(f"User {user_id} not found.")

    current_balance = round(
        float(user.wallet_balance or 0),
        2,
    )

    new_balance = round(
        current_balance + amount,
        2,
    )

    if new_balance < -0.01:
        raise ValueError(
            f"Insufficient wallet balance. "
            f"Available: JMD {current_balance:,.2f}."
        )

    if abs(new_balance) <= 0.01:
        new_balance = 0.00

    # User.wallet_balance is the official balance.
    user.wallet_balance = new_balance

    # Keep the older Wallet table synchronized.
    wallet = (
        Wallet.query
        .filter(Wallet.user_id == user_id)
        .with_for_update()
        .first()
    )

    if not wallet:
        wallet = Wallet(
            user_id=user_id,
            ewallet_balance=new_balance,
            bucks_balance=0,
        )
        db.session.add(wallet)
    else:
        wallet.ewallet_balance = new_balance

    transaction = WalletTransaction(
        user_id=user_id,
        amount=amount,
        description=description,
        type=(
            transaction_type
            or ("credit" if amount > 0 else "debit")
        ),
        action=action,
        reason=reason,
        invoice_number=invoice_number,
        package_id=package_id,
        admin_id=admin_id,
        created_at=datetime.utcnow(),
    )

    db.session.add(transaction)

    if commit:
        db.session.commit()

    return transaction


def debit_wallet_for_payment(payment, invoice=None, admin_id=None):
    """
    Deduct a completed Wallet payment exactly once.

    The caller must flush the Payment first so payment.id is available.
    This function deliberately does not commit.
    """

    if not payment:
        raise ValueError("Payment is required.")

    if not payment.id:
        raise ValueError(
            "Payment must be flushed before deducting the wallet."
        )

    if not is_wallet_method(payment.method):
        return None

    if str(payment.status or "").strip().lower() != "completed":
        raise ValueError(
            "Wallet can only be deducted for a completed payment."
        )

    amount = round(
        float(payment.amount_jmd or 0),
        2,
    )

    if amount <= 0:
        raise ValueError(
            "Wallet payment amount must be greater than zero."
        )

    payment_reason = f"payment:{payment.id}"

    # Prevent the same Payment record from deducting twice.
    existing_transaction = WalletTransaction.query.filter_by(
        user_id=payment.user_id,
        action="payment_debit",
        reason=payment_reason,
    ).first()

    if existing_transaction:
        return existing_transaction

    invoice = invoice or getattr(payment, "invoice", None)

    invoice_number = None

    if invoice:
        invoice_number = (
            invoice.invoice_number
            or f"Invoice #{invoice.id}"
        )
    elif payment.invoice_id:
        invoice_number = f"Invoice #{payment.invoice_id}"

    description = (
        f"Wallet payment of JMD {amount:,.2f} for "
        f"{invoice_number or 'package payment'}. "
        f"Payment ID: {payment.id}."
    )

    return _change_wallet_balance(
        user_id=payment.user_id,
        amount=-amount,
        description=description,
        transaction_type="debit",
        action="payment_debit",
        reason=payment_reason,
        invoice_number=invoice_number,
        package_id=payment.package_id,
        admin_id=admin_id,
        commit=False,
    )


def update_wallet(user_id, amount, description):
    """
    Add or subtract an amount and commit immediately.

    Retained for referral bonuses and existing admin wallet updates.
    """

    return _change_wallet_balance(
        user_id=user_id,
        amount=amount,
        description=description,
        commit=True,
    )


def apply_referral_bonus(new_user_id, referrer_code):
    referrer = User.query.filter_by(
        referral_code=referrer_code
    ).first()

    new_user = db.session.get(User, new_user_id)

    if not new_user:
        raise ValueError("New user does not exist.")

    if not referrer:
        return

    new_user.referrer_id = referrer.id

    update_wallet(
        new_user_id,
        100,
        "Signup bonus (Referral)",
    )

    pending = PendingReferral(
        referrer_id=referrer.id,
        referred_email=new_user.email,
        accepted=False,
        created_at=datetime.utcnow(),
    )

    db.session.add(pending)
    db.session.commit()


def process_first_shipment_bonus(user_id):
    overseas_count = (
        Package.query
        .filter(
            Package.user_id == user_id,
            func.lower(Package.status) == "overseas",
        )
        .count()
    )

    if overseas_count != 1:
        return

    user = db.session.get(User, user_id)

    if not user:
        return

    pending = PendingReferral.query.filter_by(
        referred_email=user.email,
        accepted=False,
    ).first()

    if not pending:
        return

    update_wallet(
        pending.referrer_id,
        100,
        (
            f"Referral bonus: User {user_id} "
            f"first overseas shipment"
        ),
    )

    pending.accepted = True
    db.session.commit()


def update_wallet_balance(user_id, amount, description):
    return update_wallet(
        user_id,
        amount,
        description,
    )