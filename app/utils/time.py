from datetime import timezone
from zoneinfo import ZoneInfo  # Python 3.9+

JAMAICA_TZ = ZoneInfo("America/Jamaica")

def to_jamaica(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JAMAICA_TZ)
