"""Fetch and normalise ICS feeds (Google Calendar secret iCal URLs)."""
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from app.errors import UserFacingError

MAX_BYTES = 10 * 1024 * 1024
PRIVATE_CLASSES = {"PRIVATE", "CONFIDENTIAL"}
GOOGLE_HOSTS = {"calendar.google.com", "accounts.google.com"}
NOT_A_FEED_GOOGLE = (
    "That's a link to the calendar page, not its feed. In Google Calendar, open the calendar's "
    "Settings, then copy the \"Secret address in iCal format\"."
)
NOT_A_FEED = "That address opened a web page, not a calendar feed. Use the calendar's iCal (.ics) address."
UNREADABLE = "That address didn't return a calendar the app could read. Use the calendar's iCal (.ics) address."
INVALID_ADDRESS = "That isn't a valid web address. Check it was copied in full."


class FeedError(UserFacingError):
    """A calendar feed problem the household can act on."""


def _status_error(code: int) -> FeedError:
    detail = f"HTTP {code}"
    if code in (401, 403):
        return FeedError("The calendar server refused access. Use the calendar's secret iCal address.", detail)
    if code == 404:
        return FeedError("No calendar was found at that address. Check it was copied in full.", detail)
    return FeedError(f"The calendar server answered with an error ({detail}). Try again later.", detail)


def masked_url(url: str) -> str:
    """Enough of a stored feed address for the settings page to tell feeds apart."""
    return f"{urlsplit(url).hostname or ''}…{url[-10:]}"


def log_label(url: str) -> str:
    """Names a feed in the log by host and hash: no part of the address's path or query."""
    try:
        host = urlsplit(url).hostname or "no host"
    except ValueError:
        host = "unparseable address"
    return f"{host} #{hashlib.sha256(url.encode()).hexdigest()[:8]}"




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
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as e:
        raise FeedError(INVALID_ADDRESS, detail=f"urlsplit: {e}") from e
    if scheme not in ("http", "https"):
        raise FeedError("Use an address that starts with https:// or webcal://.", detail=f"scheme {scheme!r}")
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise _status_error(resp.status_code)
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FeedError("That calendar feed is larger than 10 MB, too big to load.", detail=f"over {MAX_BYTES} bytes")
                    chunks.append(chunk)
                final_host = resp.url.host
    except httpx.InvalidURL as e:
        raise FeedError(INVALID_ADDRESS, detail=f"httpx: {e}") from e
    data = b"".join(chunks)
    if not data.lstrip(b"\xef\xbb\xbf \t\r\n").upper().startswith(b"BEGIN:VCALENDAR"):
        google = final_host in GOOGLE_HOSTS or urlsplit(url).hostname in GOOGLE_HOSTS
        raise FeedError(NOT_A_FEED_GOOGLE if google else NOT_A_FEED, detail=f"not iCal: {data[:60]!r} from {final_host}")
    return data


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
    try:
        cal = icalendar.Calendar.from_ical(data)
    except ValueError as e:
        raise FeedError(UNREADABLE, detail=f"icalendar: {e}") from e
    rows = []
    for event in recurring_ical_events.of(cal).between(window_start, window_end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if owner_email and _declined_by(event, owner_email):
            continue
        rows.append(_row(event, tz))
    return rows
