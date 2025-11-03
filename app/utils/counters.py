# app/utils/counters.py
import sqlite3
from app.config import DB_PATH

__all__ = ["ensure_counters_table", "next_bill_number_tx", "next_invoice_number_tx"]

def ensure_counters_table():
    """
    Create/upgrade the `counters` table and seed sequences from existing data.
    - Uses (name, value) schema.
    - Migrates old (key, value) -> (name, value) if present.
    - Seeds shipment_seq, invoice_seq, bill_seq from existing rows.
    - Adds helpful unique indexes (best-effort).
    - Enables WAL + busy timeout to reduce SQLITE_LOCKED issues.
    """
    # longer connection timeout helps when another writer briefly holds the DB
    conn = sqlite3.connect(DB_PATH, timeout=30)
    c = conn.cursor()
    try:
        # --- reduce lock contention ---
        c.execute("PRAGMA journal_mode=WAL;")      # better concurrency
        c.execute("PRAGMA busy_timeout=5000;")     # 5s wait before 'database is locked'

        # Try to keep the following changes atomic to avoid partial writes
        c.execute("BEGIN IMMEDIATE")

        # Ensure table exists with the NEW schema
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
              name  TEXT PRIMARY KEY,
              value INTEGER NOT NULL
            )
        """)

        # If an OLD schema existed (key,value), migrate it
        cols = [r[1] for r in c.execute("PRAGMA table_info(counters)").fetchall()]
        if ("key" in cols) and ("name" not in cols):
            c.execute("ALTER TABLE counters RENAME TO counters_old")
            c.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            c.execute("INSERT INTO counters(name,value) SELECT key,value FROM counters_old")
            c.execute("DROP TABLE counters_old")

        # Seed rows if missing
        for k in ("shipment_seq", "invoice_seq", "bill_seq"):
            c.execute("INSERT OR IGNORE INTO counters(name,value) VALUES(?,0)", (k,))

        # Initialize shipment_seq from existing shipment_log.sl_id suffix (xxxxx)
        try:
            mx_ship = c.execute("""
                SELECT COALESCE(MAX(CAST(substr(sl_id, -5) AS INTEGER)), 0)
                FROM shipment_log
                WHERE sl_id IS NOT NULL AND sl_id <> ''
            """).fetchone()[0] or 0
            c.execute("""
                UPDATE counters SET value=?
                WHERE name='shipment_seq' AND value < ?
            """, (int(mx_ship), int(mx_ship)))
        except Exception:
            # table might not exist yet; ignore
            pass

        # Initialize invoice_seq from invoices.invoice_number trailing 5 digits
        try:
            mx_inv = c.execute("""
                SELECT COALESCE(MAX(CAST(substr(invoice_number, -5) AS INTEGER)), 0)
                FROM invoices
                WHERE invoice_number GLOB '*[0-9][0-9][0-9][0-9][0-9]'
            """).fetchone()[0] or 0
            c.execute("""
                UPDATE counters SET value=?
                WHERE name='invoice_seq' AND value < ?
            """, (int(mx_inv), int(mx_inv)))
        except Exception:
            pass

        # Initialize bill_seq from bills.bill_number trailing 5 digits
        try:
            mx_bill = c.execute("""
                SELECT COALESCE(MAX(CAST(substr(bill_number, -5) AS INTEGER)), 0)
                FROM bills
                WHERE bill_number IS NOT NULL AND bill_number <> ''
                  AND bill_number GLOB '*[0-9][0-9][0-9][0-9][0-9]'
            """).fetchone()[0] or 0
            c.execute("""
                UPDATE counters SET value=?
                WHERE name='bill_seq' AND value < ?
            """, (int(mx_bill), int(mx_bill)))
        except Exception:
            pass

        # Helpful unique indexes (ignore if tables don’t exist yet)
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_log_slid ON shipment_log(sl_id)")
        except Exception:
            pass
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_shipment_packages_package ON shipment_packages(package_id)")
        except Exception:
            pass
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_bills_bill_number ON bills(bill_number)")
        except Exception:
            pass

        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
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
