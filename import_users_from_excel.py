import os
from datetime import datetime, timedelta

import pandas as pd
import bcrypt

from app import create_app
from app.extensions import db
from app.models import User


# 🔹 CHANGE THIS PATH if your Excel file is different
EXCEL_PATH = r"C:\Users\forei\OneDrive\Desktop\foreign_a_foot_project\users_export.xlsx"


def excel_date_to_str(value) -> str | None:
    """
    Convert whatever is in the 'Date Registered' column to 'YYYY-MM-DD'.

    Handles:
      - Excel serial numbers (e.g. 45391)
      - Real datetime objects
      - Strings like '4/9/2024', '2024-04-09', etc.
    """
    # 1) Empty / NaN
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    # 2) Excel serial (int/float)
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=int(value))
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    # 3) Already a datetime
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    # 4) String formats
    s = str(value).strip()
    if not s:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Fallback: store raw string if we really can't parse
    return s


def main():
    if not os.path.exists(EXCEL_PATH):
        print("Excel file not found:", EXCEL_PATH)
        return

    df = pd.read_excel(EXCEL_PATH)
    print("Columns in Excel:", list(df.columns))
    print("Total rows in Excel:", len(df))

    app = create_app()
    with app.app_context():
        added = 0
        skipped = 0

        for _, row in df.iterrows():
            reg_no = str(row.get("UserCode") or "").strip()
            full_name = str(row.get("Full Name") or "").strip()
            email = str(row.get("Email") or "").strip()
            mobile = str(row.get("Mobile") or "").strip()
            trn = str(row.get("TRN") or "").strip()
            date_val = row.get("Date Registered")
            date_registered = excel_date_to_str(date_val)
            password_plain = str(row.get("Password") or "").strip()

            if not email:
                skipped += 1
                continue

            if User.query.filter_by(email=email).first():
                skipped += 1
                continue

            if password_plain:
                hashed_pw = bcrypt.hashpw(password_plain.encode("utf-8"), bcrypt.gensalt())
            else:
                hashed_pw = bcrypt.hashpw(b"password123", bcrypt.gensalt())

            u = User(
                email=email,
                password=hashed_pw,
                role="customer",
                full_name=full_name,
                mobile=mobile,
                trn=trn,
                registration_number=reg_no,
                date_registered=date_registered,
                created_at=date_registered or datetime.utcnow().strftime("%Y-%m-%d"),
            )

            db.session.add(u)
            added += 1

        db.session.commit()
        print("Import complete.")
        print("Users added:", added)
        print("Rows skipped (missing email or already existed):", skipped)


if __name__ == "__main__":
    main()
