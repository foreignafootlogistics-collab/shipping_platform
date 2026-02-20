# app/utils/unassigned.py

from datetime import datetime
from flask import g

from app.extensions import db
from app.models import User

# Precomputed bcrypt hash for the string "!UNASSIGNED!"
# Keep it as bytes so it matches User.password (LargeBinary).
DUMMY_BCRYPT_BYTES = b"$2b$12$9b8yGx1l0vI6k3eYb0wLeO0nCq9M8lUq7G5c3kq6iZpXnq9m5m4rS"


def ensure_unassigned_user() -> int:
    """
    Ensure there is a special UNASSIGNED user with sane defaults.
    Returns the user.id of the UNASSIGNED user.
    """

    user = User.query.filter_by(registration_number="UNASSIGNED").first()
    if not user:
        user = User.query.filter_by(email="unassigned@foreign-a-foot.local").first()

    now = datetime.utcnow()
    reg_date_str = now.strftime("%Y-%m-%d")
    created_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if user:
        changed = False

        if not user.registration_number:
            user.registration_number = "UNASSIGNED"
            changed = True

        if not (user.full_name or "").strip():
            user.full_name = "UNASSIGNED"
            changed = True

        if not (user.email or "").strip():
            user.email = "unassigned@foreign-a-foot.local"
            changed = True

        if not (user.role or "").strip():
            user.role = "customer"
            changed = True

        if not (user.date_registered or "").strip():
            user.date_registered = reg_date_str
            changed = True

        if not (user.created_at or "").strip():
            user.created_at = created_str
            changed = True

        if not user.password:
            user.password = DUMMY_BCRYPT_BYTES
            changed = True

        if user.is_admin is None:
            user.is_admin = False
            changed = True

        if changed:
            db.session.commit()

        return user.id

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


def get_unassigned_user_id() -> int:
    """
    Always returns the UNASSIGNED user's id (creates it if missing).
    Caches per-request to avoid repeated DB hits in loops.
    """
    cached = getattr(g, "_unassigned_user_id", None)
    if cached:
        return int(cached)

    uid = ensure_unassigned_user()
    g._unassigned_user_id = int(uid)
    return int(uid)


def is_unassigned_user_id(user_id) -> bool:
    """
    True if the given user_id matches the UNASSIGNED user.
    """
    if not user_id:
        return False
    return int(user_id) == int(get_unassigned_user_id())


def is_pkg_unassigned(pkg) -> bool:
    """
    True if package.user_id points to UNASSIGNED.
    """
    if not pkg:
        return False
    return is_unassigned_user_id(getattr(pkg, "user_id", None))