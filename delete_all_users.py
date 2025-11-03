import sqlite3

conn = sqlite3.connect('shipping_platform.db')
c = conn.cursor()

confirm = input("⚠️ This will delete ALL USERS and reset registration numbering. Type 'yes' to continue: ").lower()

if confirm == 'yes':
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    conn.commit()
    print("✅ Users deleted and registration number reset to FAFL10001.")
else:
    print("❌ Operation cancelled.")

conn.close()