"""Small JSON-compatible weather boundary types and validation."""
import math
from dataclasses import dataclass
from datetime import datetime


class WeatherError(ValueError):
    """A safe, user-facing weather failure (never an upstream exception)."""


@dataclass
class WeatherInputs:
    config: dict
    snapshot: dict | None = None


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def probability(value: object) -> float | None:
    result = number(value)
    return result if result is not None and 0 <= result <= 100 else None


def location_key(location: dict) -> str:
    return ",".join(format(float(location[field]) or 0, ".15g") for field in ("lat", "lon"))


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None
