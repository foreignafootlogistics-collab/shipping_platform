# app/utils/unassigned.py
import sqlite3
from datetime import datetime
from app.config import DB_PATH

# Precomputed bcrypt hash for the string "!UNASSIGNED!"
# (so we don't need bcrypt at import time).
# You can change it later; this account is never meant to log in.
DUMMY_BCRYPT = "$2b$12$9b8yGx1l0vI6k3eYb0wLeO0nCq9M8lUq7G5c3kq6iZpXnq9m5m4rS"  # placeholder strong hash

def ensure_unassigned_user():
    """
    Create or fix the special UNASSIGNED user:
    - registration_number: 'UNASSIGNED'
    - full_name: 'UNASSIGNED'
    - email: 'unassigned@foreign-a-foot.local'
    - password: dummy bcrypt hash (NOT NULL)
    - is_active: 1 (if column exists)
    - date_registered/created_at: today if missing
    Also upgrades any existing partial row to have registration_number set.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Figure out which columns exist (schema varies across installs).
    cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    has_is_active      = "is_active"        in cols
    has_date_registered= "date_registered"  in cols
    has_created_at     = "created_at"       in cols
    has_password       = "password"         in cols
    has_email          = "email"            in cols

    # 1) If there’s a row with registration_number='UNASSIGNED', make sure required fields are set.
    row = c.execute(
        "SELECT * FROM users WHERE registration_number='UNASSIGNED' LIMIT 1"
    ).fetchone()

    now_iso = datetime.utcnow().strftime("%Y-%m-%d")
    created_iso = datetime.utcnow().isoformat(timespec="seconds")

    if row:
        # Patch any missing/empty fields on the existing UNASSIGNED row
        updates = []
        params = []

        if has_email and (not row["email"] or str(row["email"]).strip() == ""):
            updates.append("email=?")
            params.append("unassigned@foreign-a-foot.local")

        if has_password and (not row["password"] or str(row["password"]).strip() == ""):
            updates.append("password=?")
            params.append(DUMMY_BCRYPT)

        if has_is_active and (row["is_active"] is None):
            updates.append("is_active=?")
            params.append(1)

        if has_date_registered and (not row["date_registered"] or str(row["date_registered"]).strip() == ""):
            updates.append("date_registered=?")
            params.append(now_iso)

        if has_created_at and (not row["created_at"] or str(row["created_at"]).strip() == ""):
            updates.append("created_at=?")
            params.append(created_iso)

        # Make sure full_name is set
        if "full_name" in cols and (not row["full_name"] or str(row["full_name"]).strip() == ""):
            updates.append("full_name=?")
            params.append("UNASSIGNED")

        if updates:
            sql = "UPDATE users SET " + ", ".join(updates) + " WHERE id=?"
            params.append(row["id"])
            c.execute(sql, params)
            conn.commit()
        conn.close()
        return

    # 2) Otherwise, see if there is a placeholder row with missing reg number we can upgrade.
    #    (Some uploads may have created an empty user shell.)
    row2 = c.execute("""
        SELECT * FROM users
        WHERE (registration_number IS NULL OR TRIM(registration_number)='')
        ORDER BY id ASC
        LIMIT 1
    """).fetchone()

    if row2:
        updates = ["registration_number=?"]
        params = ["UNASSIGNED"]

        if "full_name" in cols:
            updates.append("full_name=?")
            params.append("UNASSIGNED")

        if has_email:
            updates.append("email=?")
            params.append("unassigned@foreign-a-foot.local")

        if has_password:
            updates.append("password=?")
            params.append(DUMMY_BCRYPT)

        if has_is_active:
            updates.append("is_active=?")
            params.append(1)

        if has_date_registered:
            updates.append("date_registered=?")
            params.append(now_iso)

        if has_created_at:
            updates.append("created_at=?")
            params.append(created_iso)

        sql = "UPDATE users SET " + ", ".join(updates) + " WHERE id=?"
        params.append(row2["id"])
        c.execute(sql, params)
        conn.commit()
        conn.close()
        return

    # 3) No existing row — INSERT a clean UNASSIGNED user.
    fields = ["registration_number", "full_name"]
    values = ["UNASSIGNED", "UNASSIGNED"]
    if has_email:
        fields.append("email"); values.append("unassigned@foreign-a-foot.local")
    if has_password:
        fields.append("password"); values.append(DUMMY_BCRYPT)
    if has_is_active:
        fields.append("is_active"); values.append(1)
    if has_date_registered:
        fields.append("date_registered"); values.append(now_iso)
    if has_created_at:
        fields.append("created_at"); values.append(created_iso)

    placeholders = ",".join(["?"] * len(values))
    sql = f"INSERT INTO users ({','.join(fields)}) VALUES ({placeholders})"
    c.execute(sql, values)
    conn.commit()
    conn.close()
