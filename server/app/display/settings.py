"""Settings-controlled night pause, shared by the settings form and panel client."""
import json
import re

from sqlalchemy.orm import Session

from app import clock, settings_store
from app.errors import UserFacingError

DEFAULTS = {"night_enabled": True, "night_start": "23:00", "night_end": "06:00"}


def _clock_time(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise UserFacingError("Enter night pause times in 24-hour HH:MM format.")
    return value


def load_policy(session: Session) -> dict:
    policy = DEFAULTS.copy()
    raw = settings_store.get(session, "display.policy")
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict) and type(saved.get("night_enabled")) is bool:
                start = _clock_time(saved["night_start"])
                end = _clock_time(saved["night_end"])
                if not saved["night_enabled"] or start != end:
                    policy = {"night_enabled": saved["night_enabled"], "night_start": start, "night_end": end}
        except (ValueError, KeyError, TypeError, AttributeError, UserFacingError):
            pass
    return {**policy, "timezone": clock.tz().key}


def settings_context(session: Session) -> dict:
    return {"config": load_policy(session), "error": None}


def save_policy(session: Session, *, night_enabled: bool, night_start: str, night_end: str) -> None:
    start, end = _clock_time(night_start), _clock_time(night_end)
    if night_enabled and start == end:
        raise UserFacingError("Night pause start and end must be different.")
    settings_store.put(session, "display.policy", json.dumps({
        "night_enabled": night_enabled, "night_start": start, "night_end": end,
    }))
