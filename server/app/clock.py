from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import config


def tz() -> ZoneInfo:
    return ZoneInfo(config.tz)


def now() -> datetime:
    return datetime.now(tz())


def today() -> date:
    return now().date()


def week_start(d: date) -> date:
    """Saturday on or before d (menu weeks run Sat–Fri)."""
    return d - timedelta(days=(d.weekday() - 5) % 7)


def week_dates(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]


def fmt_day(d: date) -> str:
    """'Sat 17 Oct'."""
    return f"{d:%a} {d.day} {d:%b}"
