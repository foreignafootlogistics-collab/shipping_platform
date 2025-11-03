# app/utils/unassigned.py
import sqlite3
from datetime import datetime
from app.config import DB_PATH

# Precomputed bcrypt hash for the string "!UNASSIGNED!"
# Keep it as bytes so we can store as BLOB (LargeBinary).
DUMMY_BCRYPT_BYTES = b"$2b$12$9b8yGx1l0vI6k3eYb0wLeO0nCq9M8lUq7G5c3kq6iZpXnq9m5m4rS"

def ensure_unassigned_user():
    """
    Create or fix the special UNASSIGNED user with a valid role and password BLOB.
    - registration_number: 'UNASSIGNED'
    - full_name: 'UNASSIGNED'
    - email: 'unassigned@foreign-a-foot.local'
    - role: 'customer'
    - password: dummy bcrypt hash (BLOB)
    - is_active: 1 (if column exists)
    - date_registered/created_at filled if present
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Discover available columns
    cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    has_is_active       = "is_active"        in cols
    has_date_registered = "date_registered"  in cols
    has_created_at      = "created_at"       in cols
    has_password        = "password"         in cols
    has_email           = "email"            in cols
    has_role            = "role"             in cols
    has_full_name       = "full_name"        in cols
    has_is_admin        = "is_admin"         in cols

    now_iso = datetime.utcnow().strftime("%Y-%m-%d")
    created_iso = datetime.utcnow().isoformat(timespec="seconds")

    # 1) Try existing UNASSIGNED row
    row = c.execute(
        "SELECT * FROM users WHERE registration_number='UNASSIGNED' LIMIT 1"
    ).fetchone()

    if row:
        updates, params = [], []

        if has_email and (not row["email"] or str(row["email"]).strip() == ""):
            updates.append("email=?"); params.append("unassigned@foreign-a-foot.local")

        if has_password and (not row["password"] or (isinstance(row["password"], str) and not row["password"].strip())):
            updates.append("password=?"); params.append(sqlite3.Binary(DUMMY_BCRYPT_BYTES))

        if has_role and (not row["role"] or str(row["role"]).strip() == ""):
            updates.append("role=?"); params.append("customer")

        if has_is_active and (row["is_active"] is None):
            updates.append("is_active=?"); params.append(1)

        if has_date_registered and (not row["date_registered"] or str(row["date_registered"]).strip() == ""):
            updates.append("date_registered=?"); params.append(now_iso)

        if has_created_at and (not row["created_at"] or str(row["created_at"]).strip() == ""):
            updates.append("created_at=?"); params.append(created_iso)

        if has_full_name and (not row["full_name"] or str(row["full_name"]).strip() == ""):
            updates.append("full_name=?"); params.append("UNASSIGNED")

        if has_is_admin and row["is_admin"] is None:
            updates.append("is_admin=?"); params.append(0)

        if updates:
            sql = "UPDATE users SET " + ", ".join(updates) + " WHERE id=?"
            params.append(row["id"])
            c.execute(sql, params)
            conn.commit()

        conn.close()
        return

    # 2) Upgrade a blank shell user if present
    row2 = c.execute("""
        SELECT * FROM users
        WHERE (registration_number IS NULL OR TRIM(registration_number)='')
        ORDER BY id ASC
        LIMIT 1
    """).fetchone()

    if row2:
        updates, params = ["registration_number=?"], ["UNASSIGNED"]

        if has_full_name:
            updates.append("full_name=?"); params.append("UNASSIGNED")
        if has_email:
            updates.append("email=?"); params.append("unassigned@foreign-a-foot.local")
        if has_password:
            updates.append("password=?"); params.append(sqlite3.Binary(DUMMY_BCRYPT_BYTES))
        if has_role:
            updates.append("role=?"); params.append("customer")
        if has_is_active:
            updates.append("is_active=?"); params.append(1)
        if has_date_registered:
            updates.append("date_registered=?"); params.append(now_iso)
        if has_created_at:
            updates.append("created_at=?"); params.append(created_iso)
        if has_is_admin:
            updates.append("is_admin=?"); params.append(0)

        sql = "UPDATE users SET " + ", ".join(updates) + " WHERE id=?"
        params.append(row2["id"])
        c.execute(sql, params)
        conn.commit()
        conn.close()
        return

    # 3) Insert a fresh UNASSIGNED row
    fields, values = ["registration_number", "full_name"], ["UNASSIGNED", "UNASSIGNED"]
    if has_email:
        fields.append("email"); values.append("unassigned@foreign-a-foot.local")
    if has_password:
        fields.append("password"); values.append(sqlite3.Binary(DUMMY_BCRYPT_BYTES))
    if has_role:
        fields.append("role"); values.append("customer")
    if has_is_active:
        fields.append("is_active"); values.append(1)
    if has_date_registered:
        fields.append("date_registered"); values.append(now_iso)
    if has_created_at:
        fields.append("created_at"); values.append(created_iso)
    if has_is_admin:
        fields.append("is_admin"); values.append(0)

    placeholders = ",".join(["?"] * len(values))
    sql = f"INSERT INTO users ({','.join(fields)}) VALUES ({placeholders})"
    c.execute(sql, values)
    conn.commit()
    conn.close()
