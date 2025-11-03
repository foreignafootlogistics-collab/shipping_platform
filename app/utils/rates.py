import sqlite3
from app.sqlite_utils import get_db 

def get_rate_for_weight(weight):
    """
    Returns the total rate for a given weight using:
    - rate_brackets for weight-based pricing
    - base_rate and handling_fee from settings table
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get nearest weight bracket
    c.execute("""
        SELECT rate
        FROM rate_brackets
        WHERE max_weight >= ?
        ORDER BY max_weight ASC
        LIMIT 1
    """, (weight,))
    row = c.fetchone()
    bracket_rate = row['rate'] if row else 0

    # Get base_rate and handling_fee from settings
    c.execute("SELECT base_rate, handling_fee FROM settings WHERE id = 1")
    settings = c.fetchone()
    base_rate = settings['base_rate'] if settings else 0
    handling_fee = settings['handling_fee'] if settings else 0

    conn.close()
    return base_rate + bracket_rate + handling_fee
