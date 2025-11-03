import sqlite3

DB_PATH = "shipping_platform.db"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

columns_to_add = {
    "date_submitted": "TEXT",
    "date_paid": "TEXT",
    "due_date": "TEXT",
    "weight": "REAL",
    "rate": "REAL"
}

for column, col_type in columns_to_add.items():
    try:
        c.execute(f"ALTER TABLE invoices ADD COLUMN {column} {col_type}")
        print(f"✅ Column '{column}' added")
    except sqlite3.OperationalError:
        print(f"⚠️ Column '{column}' already exists")

conn.commit()
conn.close()
print("All done!")
