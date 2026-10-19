from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.calendars import sync
from app.calendars.ics import FeedError, owner_email_from_url, parse_events
from app.models import Calendar, CalendarEvent

FIXTURES = Path(__file__).parent / "fixtures"
LONDON = ZoneInfo("Europe/London")


def parse(name, start=date(2026, 10, 1), end=date(2026, 11, 1), owner=None):
    return parse_events((FIXTURES / name).read_bytes(), start, end, LONDON, owner)


def by_uid(rows):
    return {r.uid: r for r in rows}


def test_all_day_exclusive_end_and_missing_dtend():
    rows = by_uid(parse("allday.ics"))
    ranged = rows["allday-range"]
    assert ranged.all_day
    assert (ranged.start_date, ranged.end_date) == (date(2026, 10, 20), date(2026, 10, 22))
    assert ranged.start_at is None and ranged.end_at is None
    single = rows["allday-single"]
    assert (single.start_date, single.end_date) == (date(2026, 10, 25), date(2026, 10, 26))


def test_utc_converted_across_bst_change():
    rows = by_uid(parse("bst.ics"))
    before = rows["before-change"]
    after = rows["after-change"]
    assert not before.all_day
    assert before.start_at == datetime(2026, 10, 24, 18, 0, tzinfo=LONDON)
    assert before.start_at.hour == 18 and before.start_at.utcoffset().total_seconds() == 3600
    assert after.start_at.hour == 17 and after.start_at.utcoffset().total_seconds() == 0
    assert after.end_at.hour == 18


def test_floating_time_is_local():
    (row,) = parse("floating.ics")
    assert row.start_at.tzinfo is not None
    assert (row.start_at.hour, row.start_at.minute) == (19, 0)
    assert row.start_at.utcoffset().total_seconds() == 3600  # BST on 21 Oct
    assert (row.end_at.hour, row.end_at.minute) == (20, 30)
    assert row.location == "Home"


def test_weekly_rrule_expanded_with_exdate():
    rows = parse("weekly.ics")
    days = sorted(r.start_at.date() for r in rows)
    assert days == [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 26)]
    assert all(r.start_at.hour == 18 for r in rows)
    assert all(r.summary == "Swimming" for r in rows)


def test_window_limits_expansion():
    rows = parse("weekly.ics", start=date(2026, 10, 10), end=date(2026, 10, 20))
    assert [r.start_at.date() for r in rows] == [date(2026, 10, 12)]


def test_cancelled_skipped():
    assert "cancelled" not in by_uid(parse("statuses.ics"))


def test_declined_by_owner_skipped():
    assert "declined" in by_uid(parse("statuses.ics"))
    rows = by_uid(parse("statuses.ics", owner="owner@example.com"))
    assert "declined" not in rows
    assert "accepted" in rows


def test_private_events_become_busy():
    rows = by_uid(parse("statuses.ics"))
    assert (rows["private"].summary, rows["private"].location) == ("Busy", "")
    assert (rows["confidential"].summary, rows["confidential"].location) == ("Busy", "")
    assert rows["confidential"].all_day


def test_missing_summary_and_end():
    row = by_uid(parse("statuses.ics"))["untitled"]
    assert row.summary == "(No title)"
    assert row.end_at == row.start_at


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://calendar.google.com/calendar/ical/someone%40gmail.com/private-abc/basic.ics",
            "someone@gmail.com",
        ),
        (
            "https://calendar.google.com/calendar/ical/abc123group.calendar.google.com/private-x/basic.ics",
            None,
        ),
        ("https://example.com/cal/someone%40gmail.com/basic.ics", None),
    ],
)
def test_owner_email_from_url(url, expected):
    assert owner_email_from_url(url) == expected


def test_sync_replaces_rows_and_keeps_them_on_failure(session, monkeypatch):
    cal = Calendar(name="Family", ics_url="https://example.com/weekly.ics")
    session.add(cal)
    session.commit()
    monkeypatch.setattr(sync, "fetch_ics", lambda url: (FIXTURES / "weekly.ics").read_bytes())
    sync.sync_calendar(session, cal)
    sync.sync_calendar(session, cal)
    count = session.query(CalendarEvent).count()
    assert count >= 8 and cal.last_ok_at and cal.last_error is None

    def boom(url):
        raise FeedError("No calendar was found at that address.", detail="HTTP 404")

    monkeypatch.setattr(sync, "fetch_ics", boom)
    sync.sync_calendar(session, cal)
    first_since = cal.last_error_since
    sync.sync_calendar(session, cal)
    assert (cal.last_error, cal.last_error_since) == ("Couldn't update this calendar. No calendar was found at that address.", first_since)
    assert session.query(CalendarEvent).count() == count

    monkeypatch.setattr(sync, "fetch_ics", lambda url: (FIXTURES / "weekly.ics").read_bytes())
    sync.sync_calendar(session, cal)
    assert cal.last_error is None and cal.last_error_since is None
