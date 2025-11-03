import sqlite3
import bcrypt
from datetime import datetime

DB_PATH = "shipping_platform.db"  # adjust if needed

# Admin details
full_name = "FAFL Admin"
email = "foreignafootlogistics@gmail.com"
password = "AdminPass@123"

# Hash password
hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode("utf-8")

# Connect to DB
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

try:
    c.execute("""
        INSERT INTO users (full_name, email, password, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (full_name, email, hashed_pw, 1, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    print("✅ Admin user created successfully in users table!")
except sqlite3.IntegrityError:
    print("⚠️ Admin with this email already exists in users table.")
finally:
    conn.close()
