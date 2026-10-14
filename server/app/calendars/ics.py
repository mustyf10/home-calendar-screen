"""Fetch and normalise ICS feeds (Google Calendar secret iCal URLs)."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

MAX_BYTES = 10 * 1024 * 1024
PRIVATE_CLASSES = {"PRIVATE", "CONFIDENTIAL"}


@dataclass
class EventRow:
    uid: str
    summary: str
    location: str
    all_day: bool
    start_at: datetime | None = None  # aware, in the display tz
    end_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None  # exclusive


def fetch_ics(url: str) -> bytes:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError("Only http(s) and webcal URLs are supported")
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Calendar feed is larger than 10 MB")
                chunks.append(chunk)
    return b"".join(chunks)


def owner_email_from_url(url: str) -> str | None:
    parts = urlsplit(url)
    segments = parts.path.split("/")
    if parts.hostname != "calendar.google.com" or "ical" not in segments:
        return None
    i = segments.index("ical")
    if i + 1 >= len(segments):
        return None
    ident = unquote(segments[i + 1])
    return ident if "@" in ident else None


def _declined_by(event, owner_email: str) -> bool:
    attendees = event.get("ATTENDEE")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    target = f"mailto:{owner_email}".lower()
    return any(
        str(a).lower() == target and str(a.params.get("PARTSTAT", "")).upper() == "DECLINED"
        for a in attendees
    )


def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _row(event, tz: ZoneInfo) -> EventRow:
    start = event.decoded("DTSTART")
    end = event.decoded("DTEND") if "DTEND" in event else None
    duration = event.decoded("DURATION") if "DURATION" in event else None
    summary = str(event.get("SUMMARY") or "").strip() or "(No title)"
    location = str(event.get("LOCATION") or "").strip()
    if str(event.get("CLASS", "")).upper() in PRIVATE_CLASSES:
        summary, location = "Busy", ""
    row = EventRow(uid=str(event.get("UID", "")), summary=summary, location=location, all_day=False)

    if not isinstance(start, datetime):
        row.all_day = True
        row.start_date = start
        if isinstance(end, date) and not isinstance(end, datetime):
            row.end_date = end
        elif duration:
            row.end_date = start + timedelta(days=max(duration.days, 1))
        else:
            row.end_date = start + timedelta(days=1)
        if row.end_date <= start:
            row.end_date = start + timedelta(days=1)
        return row

    row.start_at = _to_local(start, tz)
    if isinstance(end, datetime):
        row.end_at = _to_local(end, tz)
    elif duration:
        row.end_at = row.start_at + duration
    else:
        row.end_at = row.start_at
    return row


def parse_events(
    data: bytes,
    window_start: date,
    window_end: date,
    tz: ZoneInfo,
    owner_email: str | None,
) -> list[EventRow]:
    cal = icalendar.Calendar.from_ical(data)
    rows = []
    for event in recurring_ical_events.of(cal).between(window_start, window_end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if owner_email and _declined_by(event, owner_email):
            continue
        rows.append(_row(event, tz))
    return rows
