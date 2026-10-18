"""Preview-only normalised weather fixtures; never fetch or persist anything."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from app.weather.types import WeatherInputs, location_key

WEATHER_LABELS = {
    "sample": "Sample forecast", "snow": "Snow", "missing": "Missing values",
    "stale": "Stale", "unavailable": "Unavailable", "off": "Off",
}


def sample_weather(now: datetime, actual_now: datetime, state: str, saved: WeatherInputs | None = None) -> WeatherInputs:
    config = dict(saved.config) if saved else {}
    config.update(enabled=state != "off", mode=config.get("mode", "daily"),
                  unit=config.get("unit", "celsius"), fetch_generation=0,
                  location={"name": "London", "region": "City of London", "country": "United Kingdom",
                            "lat": 51.5, "lon": -0.1})
    zone = ZoneInfo("Europe/London")
    today = now.astimezone(zone).date()
    age = timedelta(hours=2) if state == "stale" else timedelta(minutes=10)
    codes = [1213, 1279, 1000] if state == "snow" else [1000, 1003, 1183]
    days: list[dict[str, Any]] = [{"date": (today + timedelta(days=i)).isoformat(), "high_c": [40, 16, -2][i],
             "low_c": [-40, 8, -8][i], "condition_code": codes[i],
             "rain_pct": [0, 40, 80][i], "snow_pct": [60, 90, 0][i]} for i in range(3)]
    start = datetime.combine(today, time(), tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=3), time(), tzinfo=zone).astimezone(timezone.utc)
    hours: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        local = cursor.astimezone(zone)
        hours.append({"epoch": int(cursor.timestamp()), "temp_c": -2 if state == "snow" else 12,
                      "condition_code": codes[local.hour % 3], "is_day": 7 <= local.hour < 19,
                      "rain_pct": 40, "snow_pct": 70})
        cursor += timedelta(hours=1)
    if state == "missing":
        days[1].update(high_c=None, low_c=None, condition_code=None, rain_pct=None, snow_pct=None)
        for hour in hours:
            if datetime.fromtimestamp(hour["epoch"], zone).hour == 12:
                hour.update(temp_c=None, condition_code=None, rain_pct=None, snow_pct=None)
    snapshot = None if state == "unavailable" else {
        "location_key": location_key(config["location"]), "timezone": "Europe/London",
        "fetched_at": (actual_now.astimezone(timezone.utc) - age).isoformat(),
        "days": days, "hours": hours,
    }
    return WeatherInputs(config=config, snapshot=snapshot)
