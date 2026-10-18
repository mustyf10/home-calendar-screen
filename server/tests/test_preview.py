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


def test_scenario_weather_version_advances_at_next_hourly_boundary(session, monkeypatch):
    from app import settings_store
    import json

    settings_store.put(session, "weather.config", json.dumps({
        "enabled": False, "mode": "2h", "unit": "celsius",
        "location": None, "fetch_generation": 0,
    }))
    session.commit()
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 9, 59, tzinfo=LONDON))
    before = version(session, params("scenario=busy"))
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 16, 10, 0, tzinfo=LONDON))
    assert version(session, params("scenario=busy")) != before


def save_weather(session, now, mode="daily"):
    import json
    from app import settings_store
    from app.weather.scenarios import sample_weather

    weather = sample_weather(now, now, "sample")
    weather.config["mode"] = mode
    settings_store.put(session, "weather.config", json.dumps(weather.config))
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    return weather


def test_corrupt_timezone_degrades_weather_and_recovers_when_cache_is_repaired(session):
    import json
    from app import settings_store

    weather = save_weather(session, clock.now())
    healthy = version(session, params())
    weather.snapshot["timezone"] = "x" * 1000
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    broken = version(session, params())
    model, _ = preview_model(session, params())
    assert model.weather["unavailable"] is True
    assert broken != healthy
    weather.snapshot["timezone"] = "Europe/London"
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    model, _ = preview_model(session, params())
    assert model.weather["unavailable"] is False
    assert version(session, params()) != broken


@pytest.mark.parametrize("state", ["sample", "snow", "missing", "stale", "unavailable", "off"])
def test_weather_query_round_trips_canonically(state):
    p = params(f"weather={state}&scenario=busy&warn=backup")
    assert p.weather == state
    assert p.query() == f"scenario=busy&warn=backup&weather={state}"
    assert params(p.query()) == p
    assert params("weather=secret-key").weather is None


@pytest.mark.parametrize("path_name", ["WEATHER_STRIP", "WEATHER_ICONS"])
def test_version_changes_with_weather_partial_mtime(session, tmp_path, monkeypatch, path_name):
    source = getattr(preview, path_name)
    copy = tmp_path / source.name
    shutil.copy(source, copy)
    monkeypatch.setattr(preview, path_name, copy)
    p = params("scenario=quiet")
    before = version(session, p)
    st = copy.stat()
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert version(session, p) != before


@pytest.mark.parametrize("threshold", [3600, 21600])
def test_real_weather_version_changes_with_actual_age_despite_pretend_time(session, monkeypatch, threshold):
    now = clock.now()
    weather = save_weather(session, now)
    fetched = datetime.fromisoformat(weather.snapshot["fetched_at"])
    p = params("at=2026-10-17T09:00")
    monkeypatch.setattr(clock, "now", lambda: fetched + timedelta(seconds=threshold - 1))
    before = version(session, p)
    monkeypatch.setattr(clock, "now", lambda: fetched + timedelta(seconds=threshold))
    assert version(session, p) != before


def test_real_weather_version_changes_at_local_boundary_without_fetch(session, monkeypatch):
    now = datetime(2026, 10, 16, 9, 59, tzinfo=LONDON)
    save_weather(session, now, "2h")
    monkeypatch.setattr(clock, "now", lambda: now)
    before = version(session, params())
    monkeypatch.setattr(clock, "now", lambda: now + timedelta(minutes=1))
    assert version(session, params()) != before


def test_daily_weather_version_changes_at_dst_heading_transition(session, monkeypatch):
    now = datetime(2026, 10, 25, 0, 59, tzinfo=ZoneInfo("UTC"))
    save_weather(session, now)
    monkeypatch.setattr(clock, "now", lambda: now)
    before = version(session, params())
    monkeypatch.setattr(clock, "now", lambda: now + timedelta(minutes=1))
    assert version(session, params()) != before


def test_real_weather_version_tracks_location_midnight_not_calendar_midnight(session, monkeypatch):
    import json
    from app import settings_store
    from app.weather.types import location_key

    now = datetime(2026, 10, 16, 15, 59, tzinfo=LONDON)
    weather = save_weather(session, now)
    weather.config["location"] = {"name": "Tokyo", "region": "", "country": "Japan", "lat": 35.7, "lon": 139.7}
    weather.snapshot.update(timezone="Asia/Tokyo", location_key=location_key(weather.config["location"]))
    settings_store.put(session, "weather.config", json.dumps(weather.config))
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    monkeypatch.setattr(clock, "now", lambda: now)
    before = version(session, params())
    monkeypatch.setattr(clock, "now", lambda: now + timedelta(minutes=1))
    assert version(session, params()) != before


@pytest.mark.parametrize("field,value", [("mode", "3h"), ("unit", "fahrenheit"), ("enabled", False)])
def test_real_weather_version_tracks_saved_preferences(session, field, value):
    import json
    from app import settings_store

    weather = save_weather(session, clock.now())
    before = version(session, params())
    weather.config[field] = value
    settings_store.put(session, "weather.config", json.dumps(weather.config))
    session.commit()
    assert version(session, params()) != before


def test_scenario_weather_follows_shifted_month_boundary_time():
    model, _ = preview_model(None, params("scenario=month-boundary&at=2026-10-16T12:00"))
    assert model.weather["unavailable"] is False
    assert model.weather["cells"][0]["label"] == f"{model.header.weekday[:3].title()} {model.header.day}"


def test_scenario_weather_defaults_to_sample_and_can_be_turned_off():
    sample, _ = preview_model(None, params("scenario=quiet"))
    off, _ = preview_model(None, params("scenario=quiet&weather=off"))
    assert sample.weather["unavailable"] is False
    assert off.weather is None


@pytest.mark.parametrize("change", ["location", "snapshot"])
def test_real_weather_version_tracks_location_and_snapshot_identity(session, change):
    import json
    from app import settings_store

    weather = save_weather(session, clock.now())
    before = version(session, params())
    if change == "location":
        weather.config["location"]["lon"] = -0.2
        settings_store.put(session, "weather.config", json.dumps(weather.config))
    else:
        weather.snapshot["fetched_at"] = (clock.now() - timedelta(minutes=5)).isoformat()
        settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    assert version(session, params()) != before


def test_weather_config_key_order_does_not_invalidate_identical_preview(session):
    import json
    from app import settings_store

    weather = save_weather(session, clock.now())
    before = version(session, params())
    settings_store.put(session, "weather.config", json.dumps(dict(reversed(list(weather.config.items())))))
    session.commit()
    assert version(session, params()) == before
