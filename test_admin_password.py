import sqlite3
import bcrypt

DB_PATH = "shipping_platform.db"  # change if your DB file is different

email = input("Enter admin email: ").strip()
password = input("Enter password: ").strip()

# Connect to DB
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Look up admin user
c.execute("SELECT password FROM users WHERE email = ? AND role = 'admin'", (email,))
row = c.fetchone()
conn.close()

if not row:
    print("❌ No admin account found with that email.")
else:
    stored_hash = row[0]  # This should be bytes from the DB

    # bcrypt requires bytes — make sure it’s the right type
    if isinstance(stored_hash, str):  
        stored_hash = stored_hash.encode()

    # Check password
    if bcrypt.checkpw(password.encode(), stored_hash):
        print("✅ Password is correct!")
    else:
        print("❌ Password does NOT match.")
