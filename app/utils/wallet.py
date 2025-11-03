import sqlite3
from app.config import DB_PATH  # Adjust path if needed

def update_wallet(user_id, amount, description, conn=None):
    """Add or subtract amount from user's wallet and log the transaction."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_conn = True
    try:
        c = conn.cursor()

        c.execute("""
            UPDATE users
            SET wallet_balance = COALESCE(wallet_balance, 0) + ?
            WHERE id = ?
        """, (amount, user_id))

        c.execute("""
            INSERT INTO wallet_transactions (user_id, type, amount, description, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
        """, (
            user_id,
            "credit" if amount > 0 else "debit",
            amount,
            description
        ))

        if close_conn:
            conn.commit()

    except sqlite3.Error as e:
        print(f"DB error in update_wallet: {e}")
        if close_conn:
            conn.rollback()
        raise
    finally:
        if close_conn:
            conn.close()


def apply_referral_bonus(new_user_id, referrer_code):
    """
    Called during registration if user enters a referral code.
    - Give the new user a $100 signup bonus immediately.
    - Link new user to referrer by storing referrer_id.
    - Insert a pending referral record to pay referrer $100 after first overseas shipment.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()

        # Find referrer by referral code
        c.execute("SELECT id FROM users WHERE referral_code = ?", (referrer_code,))
        referrer = c.fetchone()

        if not referrer:
            print("Invalid referral code.")
            return  # No valid referrer, do nothing

        referrer_id = referrer["id"]

        # Update new user with referrer_id for tracking
        c.execute("UPDATE users SET referrer_id = ? WHERE id = ?", (referrer_id, new_user_id))

        # Give new user $100 signup bonus (separate from base signup bonus)
        update_wallet(new_user_id, 100, "Signup bonus (Referral)", conn=conn)

        # Insert pending referral to pay referrer after first overseas shipment
        c.execute("""
            INSERT INTO pending_referrals (referrer_id, referred_user_id, bonus_amount)
            VALUES (?, ?, ?)
        """, (referrer_id, new_user_id, 100))

        conn.commit()

    except sqlite3.Error as e:
        print(f"DB error in apply_referral_bonus: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def process_first_shipment_bonus(user_id):
    """
    Called when a package status is updated to 'overseas'.
    - Check if this is the user's first overseas package.
    - If so, credit $100 to the referrer (if any) and remove the pending referral record.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()

        # Count overseas packages for user
        c.execute("""
            SELECT COUNT(*) FROM packages
            WHERE user_id = ? AND status = 'overseas'
        """, (user_id,))
        overseas_count = c.fetchone()[0]

        # Only process bonus if this is the first overseas package
        if overseas_count == 1:
            # Check if user was referred and has pending referral bonus
            c.execute("""
                SELECT referrer_id, bonus_amount
                FROM pending_referrals
                WHERE referred_user_id = ?
            """, (user_id,))
            pending = c.fetchone()

            if pending:
                referrer_id = pending["referrer_id"]
                bonus_amount = pending["bonus_amount"]

                # Credit referrer wallet
                update_wallet(referrer_id, bonus_amount, f"Referral bonus (first overseas shipment by user {user_id})", conn=conn)

                # Remove pending referral record to prevent duplicate bonuses
                c.execute("DELETE FROM pending_referrals WHERE referred_user_id = ?", (user_id,))

        conn.commit()

    except sqlite3.Error as e:
        print(f"DB error in process_first_shipment_bonus: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def update_wallet_balance(user_id, amount, description):
    """
    Update the user's wallet balance by adding the amount (positive or negative)
    and log the transaction with the description.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Update wallet balance (handle NULL wallet_balance as 0)
    c.execute("""
        UPDATE users
        SET wallet_balance = COALESCE(wallet_balance, 0) + ?
        WHERE id = ?
    """, (amount, user_id))

    # Insert wallet transaction record
    trans_type = "credit" if amount > 0 else "debit"
    c.execute("""
        INSERT INTO wallet_transactions (user_id, type, amount, description, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (user_id, trans_type, amount, description))

    conn.commit()
    conn.close()

