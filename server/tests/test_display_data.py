from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings_store
from app.display.data import compose, load_inputs
from app.models import Calendar, CalendarEvent, MenuEntry

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=LONDON)  # Wednesday
TODAY = NOW.date()


def build_display(session, now, uptime=None):
    return compose(load_inputs(session, now, uptime), now)


@pytest.fixture
def cal(session):
    c = Calendar(name="Family", ics_url="https://example.com/a.ics")
    session.add(c)
    session.commit()
    return c


@pytest.fixture
def timed(session, cal):
    def _add(day: date, hour: int, title: str, hours: int = 1):
        start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=LONDON)
        session.add(CalendarEvent(
            calendar_id=cal.id, uid=title, summary=title, all_day=False,
            start_at=start.isoformat(), end_at=(start + timedelta(hours=hours)).isoformat(),
        ))
        session.commit()

    return _add


@pytest.fixture
def all_day(session, cal):
    def _add(start: date, end_excl: date, title: str):
        session.add(CalendarEvent(
            calendar_id=cal.id, uid=title, summary=title, all_day=True,
            start_date=start, end_date=end_excl,
        ))
        session.commit()

    return _add


def texts(model):
    return [line.text for line in model.agenda]


def test_fifteen_events_today_capped(session, timed):
    for i in range(15):
        timed(TODAY, 8 + i % 12, f"Event {i:02d}")
    timed(TODAY + timedelta(days=1), 10, "Tomorrow thing")
    lines = build_display(session, NOW).agenda
    assert len(lines) == 11
    assert lines[0].kind == "heading" and lines[0].text == "Today"
    assert [line.kind for line in lines[1:10]] == ["event"] * 9
    assert lines[10].text == "+6 more today"


def test_today_first_then_later_days_beyond_a_week(session, timed):
    timed(TODAY, 18, "Dentist")
    timed(TODAY, 8, "School run")
    for k in range(1, 12):
        timed(TODAY + timedelta(days=5 * k), 12, f"Later {k}")
    lines = build_display(session, NOW).agenda
    assert len(lines) <= 11
    assert [line.text for line in lines[:3]] == ["Today", "School run", "Dentist"]
    assert [line.time_text for line in lines[1:3]] == ["08:00", "18:00"]
    assert lines[3].text == "Mon 26 Oct"
    headings = [line.text for line in lines if line.kind == "heading"]
    assert "Sat 31 Oct" in headings  # 10 days ahead
    assert lines[-1].kind == "more"
    shown = sum(1 for line in lines if line.kind == "event")
    assert lines[-1].text == f"+{13 - shown} more"
    assert lines[-2].kind == "event"  # no orphaned heading before the overflow line


def test_tomorrow_heading(session, timed):
    timed(TODAY + timedelta(days=1), 10, "Swim")
    assert texts(build_display(session, NOW))[1:3] == ["Tomorrow", "Swim"]


def test_no_events_today(session, timed):
    timed(TODAY + timedelta(days=2), 10, "Later")
    model = build_display(session, NOW)
    assert model.agenda[0].text == "Today · nothing planned"
    assert texts(model)[1:] == ["Fri 23 Oct", "Later"]


def test_horizon_is_60_days(session, timed):
    timed(TODAY + timedelta(days=59), 10, "Edge")
    timed(TODAY + timedelta(days=60), 10, "Too far")
    assert texts(build_display(session, NOW)) == ["Today · nothing planned", "Sat 19 Dec", "Edge"]


def test_ongoing_multi_day_event_once_under_today(session, all_day, timed):
    all_day(TODAY - timedelta(days=1), TODAY + timedelta(days=4), "Half term")
    timed(TODAY, 7, "Breakfast")
    model = build_display(session, NOW)
    assert texts(model) == ["Today", "Half term (until Sat 24 Oct)", "Breakfast"]
    assert model.agenda[1].time_text == "All day"


def test_multi_day_event_marks_every_day(session, all_day):
    all_day(date(2026, 10, 27), date(2026, 10, 30), "Trip")
    cells = {c.day: c for week in build_display(session, NOW).month_grid for c in week if c.in_month}
    assert [d for d, c in cells.items() if c.has_events] == [27, 28, 29]
    assert cells[21].is_today


def test_month_grid_is_monday_first(session):
    grid = build_display(session, NOW).month_grid
    assert len(grid[0]) == 7
    # 1 Oct 2026 is a Thursday: Mon–Wed are blank padding.
    assert [c.in_month for c in grid[0][:4]] == [False, False, False, True]
    # 31 Oct is a Saturday: the grid ends on Sunday 1 Nov.
    assert (grid[-1][-1].day, grid[-1][-1].in_month) == (1, False)


def test_running_timed_event_sorts_with_all_day(session, all_day, timed):
    timed(TODAY - timedelta(days=1), 20, "Night shift", hours=16)  # until 12:00 today
    all_day(TODAY, TODAY + timedelta(days=1), "Bin day")
    timed(TODAY, 9, "Call")
    lines = build_display(session, NOW).agenda
    assert [(line.time_text, line.text) for line in lines[1:]] == [
        ("All day", "Bin day"),
        ("All day", "Night shift (until Wed 21 Oct)"),
        ("09:00", "Call"),
    ]


def test_header(session):
    h = build_display(session, NOW).header
    assert (h.weekday, h.day, h.month_year) == ("WEDNESDAY", 21, "October 2026")


def test_range_text_within_and_across_months(session):
    assert build_display(session, NOW).dinners.range_text == "Sat 17 – Fri 23 Oct"
    nov = datetime(2026, 11, 2, 9, 0, tzinfo=LONDON)
    assert build_display(session, nov).dinners.range_text == "Sat 31 Oct – Fri 6 Nov"


def test_dinner_rows(session, make_dish, serve):
    dish = make_dish("Butter chicken", tags=["chicken"])
    serve(dish, TODAY)
    session.add(MenuEntry(date=date(2026, 10, 19), kind="takeaway"))
    session.add(MenuEntry(date=date(2026, 10, 23), kind="none"))
    session.commit()
    rows = build_display(session, NOW).dinners.rows
    assert [r.label for r in rows] == ["Sat 17", "Sun 18", "Mon 19", "Tue 20", "Wed 21", "Thu 22", "Fri 23"]
    assert rows[0].title == "Takeaway"  # Saturday default
    assert rows[2].title == "Takeaway"
    assert rows[4].title == "Butter chicken" and rows[4].is_today
    assert sum(r.is_today for r in rows) == 1
    assert rows[1].title is None and rows[6].title is None


def test_cleared_saturday_is_empty(session):
    session.add(MenuEntry(date=date(2026, 10, 17), kind="none"))
    session.commit()
    assert build_display(session, NOW).dinners.rows[0].title is None


def test_warning_no_calendars(session):
    assert build_display(session, NOW).warning == "No calendars set up"


def test_warning_calendar_failing(session, cal):
    fresh = Calendar(name="Work", ics_url="https://example.com/b.ics",
                     last_error="boom", last_error_since=datetime(2026, 10, 21, 7, 30))
    session.add(fresh)
    session.commit()
    assert build_display(session, NOW, uptime=timedelta(0)).warning is None
    cal.last_error = "HTTP 404"
    cal.last_error_since = datetime(2026, 10, 21, 5, 15)  # naive UTC → 06:15 BST
    session.commit()
    assert build_display(session, NOW, uptime=timedelta(0)).warning == (
        "Calendar 'Family' not updating since 06:15"
    )


def test_warning_backup_stale(session, cal):
    assert build_display(session, NOW, uptime=timedelta(hours=1)).warning is None
    assert build_display(session, NOW, uptime=timedelta(hours=25)).warning == "Backup never run"
    settings_store.put(session, "backup_last_ok_at", "2026-10-20T02:30:00+00:00")
    session.commit()
    assert build_display(session, NOW, uptime=timedelta(hours=25)).warning is None
    settings_store.put(session, "backup_last_ok_at", "2026-10-18T02:30:00+00:00")
    session.commit()
    assert build_display(session, NOW, uptime=timedelta(0)).warning == "Backup not run since Sun 18 Oct"
