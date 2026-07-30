from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from config import TIMEZONE
except Exception:
    TIMEZONE = "Asia/Kolkata"

TZ = ZoneInfo(TIMEZONE)

# Short label shown to users, derived from the configured timezone.
try:
    TZ_LABEL = datetime.now(TZ).strftime("%Z") or TIMEZONE.split("/")[-1]
except Exception:
    TZ_LABEL = TIMEZONE.split("/")[-1]


def now():
    return datetime.now(TZ)


def now_text():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def today_prefix():
    return now().strftime("%Y-%m-%d")


def month_prefix():
    return now().strftime("%Y-%m")


def display_time(value):
    if not value:
        return ""
    try:
        text = str(value).replace("T", " ").split(".")[0]
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        # Was hardcoded "IST" regardless of the TIMEZONE env var.
        return dt.strftime("%d %b %Y, %I:%M %p ") + TZ_LABEL
    except Exception:
        return str(value)
