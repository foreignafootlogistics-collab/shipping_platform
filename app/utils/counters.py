# app/utils/counters.py
import sqlite3
from app.config import DB_PATH

__all__ = ["ensure_counters_table", "next_bill_number_tx", "next_invoice_number_tx"]

def ensure_counters_table():
    """
    Create the counters table if missing and seed basic rows.
    SQLite-safe: no BEGIN IMMEDIATE, no WAL toggling, no cross-table scans.
    """
    # Small timeout so brief write locks don't explode
    conn = sqlite3.connect(DB_PATH, timeout=5)
    try:
        c = conn.cursor()

        # Create the simple (name, value) table
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
              name  TEXT PRIMARY KEY,
              value INTEGER NOT NULL
            )
        """)

        # Seed the counters you use; adjust names to your code paths
        for k in ("shipment_seq", "invoice_seq", "bill_seq"):
            c.execute("INSERT OR IGNORE INTO counters(name, value) VALUES (?, 0)", (k,))

        conn.commit()
    finally:
        conn.close()

def _increment_counter_tx(cur: sqlite3.Cursor, name: str) -> int:
    """
    Increment a named counter inside the *current* transaction and return its value.
    Caller should already be in a transaction (BEGIN/COMMIT handled by caller).
    """
    cur.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO counters(name, value) VALUES(?, 1)", (name,))
        return 1
    row = cur.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
    return int(row[0])


def next_bill_number_tx(conn: sqlite3.Connection) -> str:
    """
    Returns a sequential bill number like 'BILL00001'.
    Must be called inside an open transaction using the provided conn.
    """
    cur = conn.cursor()
    seq = _increment_counter_tx(cur, "bill_seq")
    return f"BILL{seq:05d}"


def next_invoice_number_tx(conn: sqlite3.Connection) -> str:
    """
    Returns a sequential invoice number like 'INV00001'.
    Must be called inside an open transaction using the provided conn.
    """
    cur = conn.cursor()
    seq = _increment_counter_tx(cur, "invoice_seq")
    return f"INV{seq:05d}"
