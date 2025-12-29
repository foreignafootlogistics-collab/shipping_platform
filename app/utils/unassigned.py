# app/utils/unassigned.py

from datetime import datetime

from app.extensions import db
from app.models import User

# Precomputed bcrypt hash for the string "!UNASSIGNED!"
# Keep it as bytes so it matches User.password (LargeBinary).
DUMMY_BCRYPT_BYTES = b"$2b$12$9b8yGx1l0vI6k3eYb0wLeO0nCq9M8lUq7G5c3kq6iZpXnq9m5m4rS"


def ensure_unassigned_user() -> int:
    """
    Ensure there is a special UNASSIGNED user with sane defaults.

    - registration_number = 'UNASSIGNED'
    - full_name           = 'UNASSIGNED'
    - email               = 'unassigned@foreign-a-foot.local'
    - role                = 'customer'
    - password            = dummy bcrypt bytes
    - is_admin            = False
    - date_registered / created_at filled if empty

    Returns the user.id of the UNASSIGNED user.
    """

    # try to find existing UNASSIGNED by registration_number
    user = User.query.filter_by(registration_number="UNASSIGNED").first()

    # fallback: maybe only email exists from earlier setup
    if not user:
        user = User.query.filter_by(email="unassigned@foreign-a-foot.local").first()

    now = datetime.utcnow()
    reg_date_str = now.strftime("%Y-%m-%d")
    created_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if user:
        # --- upgrade / normalize existing row ---
        if not user.registration_number:
            user.registration_number = "UNASSIGNED"

        if not (user.full_name or "").strip():
            user.full_name = "UNASSIGNED"

        if not (user.email or "").strip():
            user.email = "unassigned@foreign-a-foot.local"

        if not (user.role or "").strip():
            user.role = "customer"

        # fill date fields if they are blank / None
        if not (user.date_registered or "").strip():
            user.date_registered = reg_date_str

        if not (user.created_at or "").strip():
            user.created_at = created_str

        # ensure password is set to a valid bcrypt hash
        if not user.password:
            user.password = DUMMY_BCRYPT_BYTES

        # ensure is_admin is False (so it doesn't count as an admin account)
        if user.is_admin is None:
            user.is_admin = False

        db.session.commit()
        return user.id

    # --- no existing UNASSIGNED, create a fresh one ---
    new_user = User(
        registration_number="UNASSIGNED",
        full_name="UNASSIGNED",
        email="unassigned@foreign-a-foot.local",
        role="customer",
        date_registered=reg_date_str,
        created_at=created_str,
        password=DUMMY_BCRYPT_BYTES,
        is_admin=False,
    )

    db.session.add(new_user)
    db.session.commit()
    return new_user.id
