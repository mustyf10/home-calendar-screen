"""Preview-only "what if" parameters shared by /display, the preview PNG and the version poll."""
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, settings_store
from app.calendars.sync import sync_window
from app.clock import fmt_day
from app.display.data import DisplayModel, compose, load_inputs, warning
from app.display.scenarios import SCENARIOS, scenario_now
from app.models import Calendar, CalendarEvent, Dish, MenuEntry
from app.weather.forecast import boundaries, select_weather
from app.weather.scenarios import WEATHER_LABELS, sample_weather
from app.weather.service import load_config, load_weather
from app.weather.types import WeatherInputs, timestamp

_APP = Path(__file__).resolve().parent.parent
DISPLAY_TEMPLATE = _APP / "templates" / "display.html"
APP_CSS = _APP / "static" / "app.css"
WEATHER_STRIP = _APP / "templates" / "weather" / "strip.html"
WEATHER_ICONS = _APP / "templates" / "weather" / "icons.html"
_PROCESS_ID = uuid.uuid4().hex  # changes on every `uvicorn --reload` restart
MAX_AT_DAYS = 400

FORCED_WARNINGS = {
    "no-calendars": lambda today: "No calendars set up",
    "calendar-failing": lambda today: "Calendar 'Family' not updating since 07:15",
    "backup": lambda today: f"Backup not run since {fmt_day(today - timedelta(days=3))}",
}
WARNING_LABELS = {
    "no-calendars": "No calendars",
    "calendar-failing": "Calendar failing",
    "backup": "Backup stale",
}


@dataclass(frozen=True)
class PreviewParams:
    at: datetime | None
    scenario: str
    warn: tuple[str, ...]
    offline: time | None
    weather: str | None = None

    def query(self) -> str:
        """Canonical query string: keys sorted, defaults and empty values omitted."""
        pairs = []
        if self.at:
            pairs.append(("at", f"{self.at:%Y-%m-%dT%H:%M}"))
        if self.offline:
            pairs.append(("offline", f"{self.offline:%H:%M}"))
        if self.scenario != "real":
            pairs.append(("scenario", self.scenario))
        pairs += [("warn", w) for w in self.warn]
        if self.weather:
            pairs.append(("weather", self.weather))
        return urlencode(pairs)


def _parse(value: str | None, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(value or "", fmt)
    except ValueError:
        return None


def parse_params(query) -> PreviewParams:
    """query: a multi-dict such as Starlette's QueryParams. Invalid values fall back silently."""
    at = _parse(query.get("at"), "%Y-%m-%dT%H:%M")
    if at and abs((at.date() - clock.today()).days) > MAX_AT_DAYS:
        at = None
    offline = _parse(query.get("offline"), "%H:%M")
    scenario = query.get("scenario")
    warn = set(query.getlist("warn"))
    return PreviewParams(
        at=at.replace(tzinfo=clock.tz()) if at else None,
        scenario=scenario if scenario in SCENARIOS else "real",
        warn=tuple(w for w in FORCED_WARNINGS if w in warn),
        offline=offline.time() if offline else None,
        weather=query.get("weather") if query.get("weather") in WEATHER_LABELS else None,
    )


def preview_now(params: PreviewParams) -> datetime:
    return scenario_now(params.scenario, params.at or clock.now())


def preview_weather(session: Session | None, params: PreviewParams, now: datetime, actual_now: datetime) -> WeatherInputs:
    state = params.weather or ("sample" if params.scenario != "real" else None)
    if state:
        saved = WeatherInputs(load_config(session)) if session is not None else None
        return sample_weather(now, actual_now, state, saved)
    assert session is not None
    return load_weather(session)


def preview_model(session: Session, params: PreviewParams) -> tuple[DisplayModel, str | None]:
    """The display model for these params, plus a note when a pretend date is outside the synced data."""
    now = preview_now(params)
    actual_now = clock.now()
    if params.scenario == "real":
        inputs = load_inputs(session, now)
    else:
        inputs = SCENARIOS[params.scenario].build(now)
    if params.scenario != "real" or params.weather:
        inputs.weather = preview_weather(session, params, now, actual_now)
    if params.warn:
        inputs.warning = " · ".join(FORCED_WARNINGS[w](now.date()) for w in params.warn)
    note = None
    if params.scenario == "real" and params.at:
        start, end = sync_window(clock.today())
        if not start <= params.at.date() <= end:
            note = f"Calendar data only covers {fmt_day(start)}–{fmt_day(end)}"
    return compose(inputs, now, actual_now=actual_now), note


def _data_stamp(session: Session, now: datetime) -> tuple:
    """Cheap aggregates that change whenever the real display data could."""
    row = session.execute(select(
        select(func.max(MenuEntry.updated_at)).scalar_subquery(),
        select(func.count()).select_from(MenuEntry).scalar_subquery(),
        select(func.max(Dish.updated_at)).scalar_subquery(),
        select(func.count()).select_from(CalendarEvent).scalar_subquery(),
        select(func.max(Calendar.last_ok_at)).scalar_subquery(),
        select(func.max(Calendar.last_error_since)).scalar_subquery(),
        select(func.count()).select_from(Calendar).scalar_subquery(),
    )).one()
    return (
        *row,
        settings_store.get(session, "backup_last_ok_at"),
        settings_store.get(session, "backup_last_error"),
        # Staleness warnings flip with the passing of time, not only with data changes.
        warning(session, now),
    )


def version(session: Session, params: PreviewParams) -> str:
    """Changes whenever what the preview shows could change: data, params, layout files or a reload.

    Never composes the model: the page polls this every 2 s.
    """
    now = preview_now(params)
    parts: list = [params.query(), now.date().isoformat()]
    if params.scenario == "real":
        parts += _data_stamp(session, now)
    actual_now = clock.now()
    weather = preview_weather(session, params, now, actual_now)
    selected = select_weather(weather, now, actual_now=actual_now)
    snapshot = weather.snapshot
    identity = None
    heading_zone = None
    expected: list[str] | list[int] | None = None
    freshness = None
    if snapshot and weather.config.get("enabled"):
        try:
            zone = ZoneInfo(snapshot["timezone"])
            local = now.astimezone(zone)
            heading_zone = (local.tzname(), local.utcoffset())
            if weather.config.get("mode", "daily") == "daily":
                expected = [(local.date() + timedelta(days=i)).isoformat() for i in range(3)]
            else:
                expected = [int(slot.timestamp()) for slot in boundaries(now, zone, 2 if weather.config["mode"] == "2h" else 3)]
        except (KeyError, TypeError, ValueError, OSError, ZoneInfoNotFoundError):
            pass  # Invalid caches render unavailable, just as in select_weather.
        fetched = timestamp(snapshot.get("fetched_at"))
        if fetched:
            age = actual_now.timestamp() - fetched.timestamp()
            if age < 0:
                freshness = "invalid"
            elif age < 3600:
                freshness = "fresh"
            elif age < 21600:
                freshness = "stale"
            else:
                freshness = "unavailable"
        if params.scenario == "real" and params.weather is None:
            identity = (snapshot.get("location_key"), snapshot.get("fetched_at"))
    parts += [json.dumps(weather.config, sort_keys=True), identity, selected, heading_zone, expected, freshness]
    parts += [path.stat().st_mtime_ns for path in (DISPLAY_TEMPLATE, APP_CSS, WEATHER_STRIP, WEATHER_ICONS)]
    parts.append(_PROCESS_ID)
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:16]
