import sqlite3

DB_PATH = "shipping_platform.db"  # ← change to your DB file

def column_exists(conn, table, column):
    """Check if a column exists in a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns

def add_full_name_column():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 1️⃣ Add column if it doesn't exist
    if not column_exists(conn, "users", "full_name"):
        print("🔄 Adding 'full_name' column to users table...")
        c.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        conn.commit()
        print("✅ 'full_name' column added.")
    else:
        print("ℹ 'full_name' column already exists. Skipping...")

    # 2️⃣ Fill in default names for admins
    print("🔄 Updating admin accounts with default names...")
    c.execute("""
        UPDATE users
        SET full_name = COALESCE(full_name, 'Administrator')
        WHERE role = 'admin'
    """)
    conn.commit()
    print("✅ Admin names set.")

    conn.close()
    print("🎉 Migration complete!")

if __name__ == "__main__":
    add_full_name_column()
