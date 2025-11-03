import sqlite3

# Path to your database
DB_PATH = 'shipping_platform.db'

# Shipping rates for 1 to 50 lbs
rates = [
    (1, 550),
    (2, 950),
    (3, 1350),
    (4, 1750),
    (5, 2000),
    (6, 2400),
    (7, 2800),
    (8, 3000),
    (9, 3400),
    (10, 3800),
    (11, 4200),
    (12, 4800),
    (13, 5000),
    (14, 5400),
    (15, 5600),
    (16, 6000),
    (17, 6400),
    (18, 6800),
    (19, 7200),
    (20, 7400),
    (21, 7800),
    (22, 8200),
    (23, 8600),
    (24, 8800),
    (25, 9200),
    (26, 9400),
    (27, 9800),
    (28, 10200),
    (29, 10400),
    (30, 10800),
    (31, 11200),
    (32, 11600),
    (33, 12000),
    (34, 12400),
    (35, 12800),
    (36, 13200),
    (37, 13600),
    (38, 14000),
    (39, 14400),
    (40, 14800),
    (41, 15200),
    (42, 15600),
    (43, 16000),
    (44, 16400),
    (45, 16800),
    (46, 17200),
    (47, 17600),
    (48, 18000),
    (49, 18400),
    (50, 18800)
]

# Connect to the database
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Step 1: Create the table if it doesn't exist
c.execute("""
    CREATE TABLE IF NOT EXISTS shipping_rates (
        weight_lb INTEGER PRIMARY KEY,
        amount_jmd REAL NOT NULL
    )
""")

# Step 2: Clear existing data to avoid duplicates
c.execute("DELETE FROM shipping_rates")

# Step 3: Insert the new rates
c.executemany("INSERT INTO shipping_rates (weight_lb, amount_jmd) VALUES (?, ?)", rates)

conn.commit()
conn.close()

print("✅ Shipping rates inserted successfully.")
