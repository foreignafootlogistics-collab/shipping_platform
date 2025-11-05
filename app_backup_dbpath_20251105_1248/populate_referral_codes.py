import sqlite3
import random
import string
from app.config import DB_PATH  # adjust import if needed

def generate_referral_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT id, referral_code FROM users")
    users = c.fetchall()

    for user_id, code in users:
        if not code:
            new_code = generate_referral_code()
            c.execute("UPDATE users SET referral_code = ? WHERE id = ?", (new_code, user_id))
            print(f"Updated user {user_id} with referral code {new_code}")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    main()
