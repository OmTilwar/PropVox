"""IST helpers; works on Windows without the optional tzdata package."""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    try:
        IST = ZoneInfo("Asia/Kolkata")
    except Exception:
        IST = timezone(timedelta(hours=5, minutes=30))
except Exception:
    IST = timezone(timedelta(hours=5, minutes=30))


def now_str_ist():
    return datetime.now(IST).strftime("%A, %d %B %Y, %I:%M %p %Z")
