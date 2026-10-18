"""Persisted weather preferences and short, generation-guarded fetch transactions."""
import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text

from app import clock, settings_store
from app.config import config
from app.db import SessionLocal
from app.weather.client import SAFE_ERRORS, UNAVAILABLE, fetch_forecast, validate_location
from app.weather.types import WeatherError, WeatherInputs, location_key, timestamp

log = logging.getLogger(__name__)
DEFAULT_CONFIG = {"enabled": False, "mode": "daily", "unit": "celsius", "location": None, "fetch_generation": 0}
CONFIG_ERROR = "Saved weather settings were invalid; please select a location and save again"


def decode(raw: str | None) -> object:
    try:
        return json.loads(raw) if raw is not None else None
    except (ValueError, RecursionError):
        return None


def parse_config(raw: str | None) -> dict:
    if raw is None:
        return DEFAULT_CONFIG.copy()
    value = decode(raw)
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool or value.get("mode") not in ("daily", "2h", "3h") or value.get("unit") not in ("celsius", "fahrenheit") or type(value.get("fetch_generation")) is not int or value["fetch_generation"] < 0:
        raise WeatherError(CONFIG_ERROR)
    try:
        location = validate_location(value["location"]) if value.get("location") is not None else None
    except WeatherError:
        raise WeatherError(CONFIG_ERROR) from None
    if value["enabled"] and location is None:
        raise WeatherError(CONFIG_ERROR)
    return {"enabled": value["enabled"], "mode": value["mode"], "unit": value["unit"], "location": location, "fetch_generation": value["fetch_generation"]}


def load_config(session) -> dict:
    try:
        return parse_config(settings_store.get(session, "weather.config"))
    except WeatherError:
        return DEFAULT_CONFIG.copy()


def load_weather(session) -> WeatherInputs:
    prefs = load_config(session)
    snapshot = decode(settings_store.get(session, "weather.snapshot"))
    return WeatherInputs(prefs, snapshot if isinstance(snapshot, dict) else None)


def status_for(prefs: dict) -> dict:
    return {"location_key": location_key(prefs["location"]) if prefs["location"] else None, "fetch_generation": prefs["fetch_generation"], "last_attempt_at": None, "last_error": None}


def read_status(session, prefs: dict) -> dict:
    value = decode(settings_store.get(session, "weather.status"))
    expected = status_for(prefs)
    if not isinstance(value, dict) or value.get("location_key") != expected["location_key"] or value.get("fetch_generation") != expected["fetch_generation"]:
        return expected
    return {**expected, "last_attempt_at": value.get("last_attempt_at") if timestamp(value.get("last_attempt_at")) else None, "last_error": value.get("last_error") if isinstance(value.get("last_error"), str) and value["last_error"] in SAFE_ERRORS else None}


def put_json(session, key: str, value: dict) -> None:
    settings_store.put(session, key, json.dumps(value, ensure_ascii=False, allow_nan=False))


@contextmanager
def write_session(session_factory):
    # Fresh connection: reserve the SQLite writer before reading mutable state.
    with session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def save_location(session_factory, location: dict) -> None:
    selected = validate_location(location)
    with write_session(session_factory) as session:
        prefs = load_config(session)
        changed = prefs["location"] is None or location_key(prefs["location"]) != location_key(selected)
        prefs["location"] = selected
        if changed:
            prefs["fetch_generation"] += 1
            settings_store.delete(session, "weather.snapshot")
            put_json(session, "weather.status", status_for(prefs))
        put_json(session, "weather.config", prefs)


def save_preferences(session_factory, *, enabled: bool, mode: str, unit: str, api_key: str | None) -> None:
    if mode not in ("daily", "2h", "3h") or unit not in ("celsius", "fahrenheit"):
        raise WeatherError("Choose a valid weather mode and temperature unit")
    with write_session(session_factory) as session:
        prefs = load_config(session)
        if enabled and not prefs["location"]:
            raise WeatherError("Select a weather location before enabling weather")
        if enabled and not api_key:
            raise WeatherError("Weather API key is missing")
        if prefs["enabled"] != enabled:
            prefs["enabled"] = enabled
            prefs["fetch_generation"] += 1
            put_json(session, "weather.status", status_for(prefs))
        prefs.update(mode=mode, unit=unit)
        put_json(session, "weather.config", prefs)


def settings_context(session) -> dict:
    inputs = load_weather(session)
    prefs = inputs.config
    error = None
    try:
        parse_config(settings_store.get(session, "weather.config"))
    except WeatherError as exc:
        error = str(exc)
    snapshot = inputs.snapshot
    if snapshot and (not prefs["location"] or snapshot.get("location_key") != location_key(prefs["location"])):
        snapshot = None
    status = read_status(session, prefs)
    return {"config": prefs, "key_configured": bool(config.weather_api_key), "error": error, "last_error": status["last_error"], "last_success": snapshot.get("fetched_at") if snapshot and timestamp(snapshot.get("fetched_at")) else None, "timezone": snapshot.get("timezone") if snapshot and isinstance(snapshot.get("timezone"), str) else None}


def _due(prefs: dict, status: dict, key: str | None, now: datetime) -> bool:
    if not prefs["enabled"] or not prefs["location"] or not key:
        return False
    attempted = timestamp(status["last_attempt_at"])
    return not attempted or (now - attempted.astimezone(timezone.utc)).total_seconds() >= 1800


def _claim_attempt(session_factory, key: str | None, now: datetime):
    """Marks an attempt and returns (generation, location_key, location), or None when nothing is due."""
    # Plain read first: the 5-second tick must not take the SQLite writer lock when nothing is due.
    with session_factory() as session:
        prefs = load_config(session)
        if not _due(prefs, read_status(session, prefs), key, now):
            return None
    with write_session(session_factory) as session:
        prefs = load_config(session)
        status = read_status(session, prefs)
        if not _due(prefs, status, key, now):
            return None
        status["last_attempt_at"] = now.isoformat()
        put_json(session, "weather.status", status)
        return prefs["fetch_generation"], location_key(prefs["location"]), prefs["location"]


def _complete_attempt(session_factory, generation: int, captured_key: str, snapshot: dict | None, error: str | None) -> None:
    with write_session(session_factory) as session:
        current = load_config(session)
        if not current["enabled"] or current["fetch_generation"] != generation or not current["location"] or location_key(current["location"]) != captured_key:
            return
        status = read_status(session, current)
        status["last_error"] = error
        if snapshot is not None:
            put_json(session, "weather.snapshot", snapshot)
        put_json(session, "weather.status", status)


async def run_once(*, session_factory=SessionLocal, now: datetime | None = None, api_key: str | None = None, provider=fetch_forecast) -> bool:
    now = (now or clock.now()).astimezone(timezone.utc)
    key = api_key if api_key is not None else config.weather_api_key
    claimed = await asyncio.to_thread(_claim_attempt, session_factory, key, now)
    if claimed is None:
        return False
    generation, captured_key, selected = claimed
    snapshot, error = None, None
    try:
        snapshot = await provider(selected, key, now)
    except WeatherError as exc:
        error = str(exc) if str(exc) in SAFE_ERRORS else UNAVAILABLE
    except Exception:
        error = UNAVAILABLE
    await asyncio.to_thread(_complete_attempt, session_factory, generation, captured_key, snapshot, error)
    return True


async def run_weather_loop() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            # Do not log exception strings/tracebacks containing provider URLs.
            log.warning("Weather service is unavailable")
        await asyncio.sleep(5)
