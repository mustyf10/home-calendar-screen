import os
import shutil
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from starlette.datastructures import QueryParams

from app import clock
from app.display import preview
from app.display.preview import PreviewParams, parse_params, preview_model, version
from app.display.scenarios import SCENARIO_LABELS, SCENARIOS
from app.models import Calendar, CalendarEvent, MenuEntry

LONDON = ZoneInfo("Europe/London")
pytestmark = pytest.mark.usefixtures("fixed_clock")


def params(qs: str = "") -> PreviewParams:
    return parse_params(QueryParams(qs))


# --- parse_params / query ------------------------------------------------


def test_parse_valid_values():
    p = params("at=2026-10-17T09:00&scenario=busy&warn=backup&warn=no-calendars&offline=07:30")
    assert p.at == datetime(2026, 10, 17, 9, 0, tzinfo=clock.tz())
    assert p.scenario == "busy"
    assert p.warn == ("no-calendars", "backup")
    assert p.offline == time(7, 30)


def test_parse_invalid_values_fall_back():
    p = params("at=tomorrow&scenario=chaos&warn=fire&warn=backup&offline=25:99")
    assert p == PreviewParams(at=None, scenario="real", warn=("backup",), offline=None)


def test_parse_empty_values_fall_back():
    assert params("at=&scenario=&warn=&offline=") == PreviewParams(None, "real", (), None)


def test_parse_ignores_at_more_than_400_days_away():
    assert params("at=2027-11-20T09:00").at is not None  # 400 days after 2026-10-16
    assert params("at=2027-11-21T09:00").at is None
    assert params("at=2025-09-11T09:00").at is not None  # 400 days before
    assert params("at=2025-09-10T09:00").at is None


def test_query_is_canonical():
    p = params("warn=backup&scenario=busy&offline=07:30&at=2026-10-17T09:00&warn=no-calendars")
    assert p.query() == "at=2026-10-17T09%3A00&offline=07%3A30&scenario=busy&warn=no-calendars&warn=backup"
    assert params("at=&scenario=real&offline=").query() == ""
    assert params(p.query()) == p


# --- preview_model -------------------------------------------------------


def test_pretend_saturday_on_real_data(session):
    model, _ = preview_model(session, params("at=2026-10-17T09:00"))
    assert (model.header.weekday, model.header.day) == ("SATURDAY", 17)
    saturday = model.dinners.rows[0]
    assert (saturday.label, saturday.is_today, saturday.title) == ("Sat 17", True, "Takeaway")


def test_busy_scenario_caps_today():
    model, _ = preview_model(None, params("scenario=busy"))
    assert len(model.agenda) == 11
    assert model.agenda[10].text == "+6 more today"


def test_nothing_scenario():
    model, _ = preview_model(None, params("scenario=nothing"))
    assert model.agenda[0].text == "Today · nothing planned"
    assert [r.title for r in model.dinners.rows] == ["Takeaway"] + [None] * 6


def test_trip_scenario_shows_until():
    model, _ = preview_model(None, params("scenario=trip"))
    assert any("(until " in line.text for line in model.agenda)


def test_month_boundary_week_spans_two_months():
    months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
    # 2026-07-31 is a Friday: no Sat–Fri menu week crosses that month end.
    for at in ("2026-10-16T12:00", "2026-12-02T12:00", "2027-02-10T12:00", "2026-07-01T12:00"):
        model, _ = preview_model(None, params(f"at={at}&scenario=month-boundary"))
        text = model.dinners.range_text
        assert sum(m in text for m in months) == 2, (at, text)
        assert model.header.weekday == "THURSDAY"


def test_every_scenario_composes():
    for name in SCENARIOS:
        model, note = preview_model(None, params(f"scenario={name}"))
        assert model.agenda and len(model.dinners.rows) == 7 and note is None


def test_labels_come_from_the_tables():
    assert list(SCENARIO_LABELS) == ["real", *SCENARIOS]
    assert list(preview.WARNING_LABELS) == list(preview.FORCED_WARNINGS)


def test_forced_warnings_in_canonical_order(session):
    model, _ = preview_model(session, params("at=2026-10-21T09:00&warn=backup&warn=no-calendars"))
    assert model.warning == "No calendars set up · Backup not run since Sun 18 Oct"


def test_forced_warnings_replace_real_warning(session):
    model, _ = preview_model(session, params("warn=calendar-failing"))
    assert model.warning == "Calendar 'Family' not updating since 07:15"


def test_range_note_only_for_real_data_out_of_window(session):
    ahead = (clock.today() + timedelta(days=90)).isoformat() + "T09:00"
    _, note = preview_model(session, params(f"at={ahead}"))
    assert note is not None and note.startswith("Calendar data only covers ")
    _, note = preview_model(session, params(f"at={ahead}&scenario=quiet"))
    assert note is None
    _, note = preview_model(session, params(f"at={clock.today().isoformat()}T09:00"))
    assert note is None
    _, note = preview_model(session, params())
    assert note is None


# --- version -------------------------------------------------------------


def test_version_is_stable_and_short(session):
    p = params("at=2026-10-21T09:00")
    v = version(session, p)
    assert len(v) == 16 and int(v, 16) >= 0
    assert version(session, p) == v


def test_version_changes_with_menu(session, make_dish, serve):
    p = params("at=2026-10-21T09:00")
    before = version(session, p)
    serve(make_dish("Butter chicken"), date(2026, 10, 21))
    assert version(session, p) != before


def test_version_changes_with_params(session):
    assert version(session, params("at=2026-10-21T09:00")) != version(
        session, params("at=2026-10-21T09:00&offline=07:30")
    )


def test_version_changes_with_template_mtime(session, tmp_path, monkeypatch):
    copy = tmp_path / "display.html"
    shutil.copy(preview.DISPLAY_TEMPLATE, copy)
    monkeypatch.setattr(preview, "DISPLAY_TEMPLATE", copy)
    p = params("at=2026-10-21T09:00")
    before = version(session, p)
    assert version(session, p) == before
    st = copy.stat()
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert version(session, p) != before


def test_version_does_not_compose_the_model(session, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("version() must stay cheap")

    monkeypatch.setattr(preview, "compose", boom)
    monkeypatch.setattr(preview, "load_inputs", boom)
    assert len(version(session, params())) == 16
    assert len(version(session, params("scenario=busy"))) == 16


def test_version_changes_when_a_menu_row_is_deleted(session, make_dish, serve):
    serve(make_dish("Fish pie"), date(2026, 10, 13))
    serve(make_dish("Pad thai"), date(2026, 10, 14))
    before = version(session, params())
    session.execute(delete(MenuEntry).where(MenuEntry.date == date(2026, 10, 13)))
    session.commit()
    assert version(session, params()) != before


def test_version_changes_on_calendar_sync(session):
    cal = Calendar(name="Family", ics_url="https://example.com/a.ics", last_ok_at=datetime(2026, 10, 16, 10, 0))
    session.add(cal)
    session.flush()
    session.add(CalendarEvent(calendar_id=cal.id, uid="a", summary="Swim", all_day=True, start_date=date(2026, 10, 17)))
    session.commit()
    before = version(session, params())
    # A sync replaces the events wholesale (same count here) and records last_ok_at.
    session.execute(delete(CalendarEvent))
    session.add(CalendarEvent(calendar_id=cal.id, uid="b", summary="Gym", all_day=True, start_date=date(2026, 10, 17)))
    cal.last_ok_at = datetime(2026, 10, 16, 10, 15)
    session.commit()
    assert version(session, params()) != before


def test_version_changes_at_midnight(session, monkeypatch):
    before = version(session, params())
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 17, 0, 1, tzinfo=clock.tz()))
    assert version(session, params()) != before


def test_scenario_version_ignores_the_db(session, make_dish, serve):
    p = params("scenario=busy")
    before = version(session, p)
    serve(make_dish("Fish pie"), date(2026, 10, 16))
    assert version(session, p) == before
