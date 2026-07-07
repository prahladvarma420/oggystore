from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from config import TIMEZONE
except Exception:
    TIMEZONE = "Asia/Kolkata"

TZ = ZoneInfo(TIMEZONE)


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
        return dt.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return str(value)
