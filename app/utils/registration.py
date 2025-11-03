import sqlite3
from app.config import DB_PATH

def next_registration_number():
    prefix = "FAFL"
    admin_number = 10000
    customer_start = 10001

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get the highest customer registration number
    c.execute("""
        SELECT registration_number
        FROM users
        WHERE role = 'customer'
        ORDER BY id DESC
        LIMIT 1
    """)
    last = c.fetchone()
    conn.close()

    if last and last[0]:
        try:
            num = int(last[0].replace(prefix, ""))
            # If somehow admin's FAFL10000 is the highest, skip to customer start
            if num < customer_start:
                return f"{prefix}{customer_start}"
            return f"{prefix}{num + 1}"
        except ValueError:
            # Fallback if something goes wrong
            return f"{prefix}{customer_start}"
    else:
        # No customers yet → start at 10001
        return f"{prefix}{customer_start}"
