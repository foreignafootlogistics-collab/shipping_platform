# fix_user_dates_from_excel.py

from datetime import datetime, timedelta

from app import create_app
from app.extensions import db
from app.models import User


def excel_serial_to_str(val: str | int | float | None) -> str | None:
    """
    Convert an Excel serial date (e.g. 45391) to 'YYYY-MM-DD' string.
    Returns None if the value can't be converted.
    """
    if val is None:
        return None

    # Work with it as an int (e.g. "45391" -> 45391)
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        return None

    base = datetime(1899, 12, 30)  # Excel date base
    dt = base + timedelta(days=n)
    return dt.strftime("%Y-%m-%d")


def main():
    app = create_app()
    with app.app_context():
        users = User.query.all()
        fixed = 0

        for u in users:
            raw = (u.date_registered or "").strip()
            # Only touch pure digits like "45391"
            if raw and raw.isdigit():
                new_val = excel_serial_to_str(raw)
                if new_val:
                    u.date_registered = new_val
                    fixed += 1

        db.session.commit()
        print(f"Updated date_registered for {fixed} users.")


if __name__ == "__main__":
    main()
