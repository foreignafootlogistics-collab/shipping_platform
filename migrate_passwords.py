import sqlite3

DB_PATH = "shipping_platform.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Convert users passwords to bytes if needed
c.execute("SELECT id, password FROM users")
for uid, old_hash in c.fetchall():
    if isinstance(old_hash, str):
        byte_hash = old_hash.encode('utf-8')
        c.execute("UPDATE users SET password = ? WHERE id = ?", (byte_hash, uid))

# Convert admin passwords to bytes if needed
c.execute("SELECT id, password FROM admin")
for uid, old_hash in c.fetchall():
    if isinstance(old_hash, str):
        byte_hash = old_hash.encode('utf-8')
        c.execute("UPDATE admin SET password = ? WHERE id = ?", (byte_hash, uid))

conn.commit()
conn.close()

print("✅ Passwords converted to bytes successfully!")

