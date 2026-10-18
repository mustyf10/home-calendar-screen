"""Pure forecast selection; no database or provider requests."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import clock
from app.config import config
from app.weather.types import WeatherInputs, location_key, number, probability, timestamp

CONDITIONS = {
    **dict.fromkeys((1006, 1009), ("cloud", "Rain")),
    **dict.fromkeys((1030, 1135, 1147, 1012, 1015, 1018, 1021, 1024, 1027, 1033, 1036, 1039, 1042, 1045, 1048), ("fog", "Rain")),
    **dict.fromkeys((1063, 1072, 1150, 1153, 1168, 1171, 1180, 1183, 1186, 1189, 1192, 1195, 1198, 1201, 1240, 1243, 1246), ("rain", "Rain")),
    **dict.fromkeys((1066, 1069, 1114, 1117, 1204, 1207, 1210, 1213, 1216, 1219, 1222, 1225, 1237, 1249, 1252, 1255, 1258, 1261, 1264), ("snow", "Snow")),
    **dict.fromkeys((1087, 1273, 1276), ("thunder", "Rain")),
    **dict.fromkeys((1279, 1282), ("thunder-snow", "Snow")),
    1000: ("clear-day", "Rain"), 1003: ("partly-cloudy", "Rain"),
}


def rounded(value: float) -> str:
    whole = Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP)
    return "0" if whole == 0 else format(whole, "f")


def temperature(value: object, unit: str) -> str:
    parsed = number(value)
    if parsed is None:
        return "—"
    if unit == "fahrenheit":
        parsed = number(parsed * 9 / 5 + 32)
        if parsed is None:
            return "—"
    return rounded(parsed) + "°"


def boundaries(now: datetime, zone: ZoneInfo, step: int) -> list[datetime]:
    candidate = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    result = []
    # Walking UTC minutes handles even fractional-hour timezone/DST transitions.
    for _ in range(24 * 60):
        local = candidate.astimezone(zone)
        if local.minute == 0 and local.hour % step == 0:
            result.append(local)
            if len(result) == 4:
                break
        candidate += timedelta(minutes=1)
    return result


def select_weather(inputs: WeatherInputs, now: datetime, *, actual_now: datetime | None = None, app_timezone: str | None = None) -> dict | None:
    prefs = inputs.config
    if not prefs.get("enabled"):
        return None
    location = prefs.get("location") or {}
    unit = prefs.get("unit", "celsius")
    result: dict = {"location": str(location.get("name", "")), "unit_label": "°F" if unit == "fahrenheit" else "°C", "timezone_label": "", "stale": False, "unavailable": True, "cells": []}
    snapshot = inputs.snapshot
    if not snapshot or not location:
        return result
    try:
        if snapshot.get("location_key") != location_key(location):
            return result
        zone = ZoneInfo(snapshot["timezone"])
        local = now.astimezone(zone)
    except (KeyError, TypeError, ValueError, OSError, ZoneInfoNotFoundError):
        return result
    if zone.key != (app_timezone or config.tz):
        result["timezone_label"] = local.tzname() or local.strftime("%z")
    fetched = timestamp(snapshot.get("fetched_at"))
    if fetched is None:
        return result
    age = ((actual_now or clock.now()).astimezone(timezone.utc) - fetched.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age >= 21600:
        return result
    result["stale"] = age >= 3600
    daily = prefs.get("mode", "daily") == "daily"
    expected = [local + timedelta(days=n) for n in range(3)] if daily else boundaries(now, zone, 2 if prefs.get("mode") == "2h" else 3)
    rows = snapshot.get("days" if daily else "hours", [])
    if not isinstance(rows, list):
        return result
    index = {row.get("date" if daily else "epoch"): row for row in rows if isinstance(row, dict) and isinstance(row.get("date" if daily else "epoch"), (str, int))}
    labels = Counter(item.strftime("%H:%M") for item in expected) if not daily else Counter()
    cells = []
    matched = False
    for item in expected:
        identity = item.date().isoformat() if daily else int(item.timestamp())
        row = index.get(identity)
        matched = matched or (row is not None and (
            any(number(row.get(field)) is not None for field in ("high_c", "low_c", "temp_c"))
            or any(probability(row.get(field)) is not None for field in ("rain_pct", "snow_pct"))
            or type(row.get("condition_code")) is int
        ))
        row = row or {}
        code = row.get("condition_code")
        icon, precipitation = CONDITIONS.get(code, ("unknown", "Rain")) if isinstance(code, int) else ("unknown", "Rain")
        if icon == "clear-day" and not daily and row.get("is_day") is False:
            icon = "clear-night"
        chance = probability(row.get("snow_pct" if precipitation == "Snow" else "rain_pct"))
        label = item.strftime("%a %d").replace(" 0", " ") if daily else item.strftime("%H:%M")
        if not daily and labels[label] > 1:
            label += " " + (item.tzname() or item.strftime("%z"))
        cells.append({"label": label, "day_label": "" if daily or item.date() == local.date() else item.strftime("%a %d").replace(" 0", " "), "temperature": (temperature(row.get("high_c"), unit) + "/" + temperature(row.get("low_c"), unit)) if daily and row else temperature(row.get("temp_c"), unit), "icon": icon, "precipitation": precipitation, "probability": "—" if chance is None else rounded(chance) + "%", "epoch": identity})
    if matched:
        result.update(unavailable=False, cells=cells)
    return result
