"""WeatherAPI boundary: bounded responses and safe literal failures only."""
import json
import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.weather.types import WeatherError, location_key, number, probability

ORIGIN = "https://api.weatherapi.com"
MAX_BYTES = 2 * 1024 * 1024
UNAVAILABLE = "Weather service is unavailable"
INVALID = "Weather response was invalid"
SAFE_ERRORS = {"Weather API key is missing", "Weather API key is invalid", "Weather API quota exceeded", "Weather location was not found", UNAVAILABLE, INVALID}
ERROR_CODES = {1002: "Weather API key is missing", 2006: "Weather API key is invalid", 2008: "Weather API key is invalid", 2009: "Weather API key is invalid", 2007: "Weather API quota exceeded", 1006: "Weather location was not found"}

# HTTPX logs full query URLs at INFO; neither transport may expose credentials.
for logger_name in ("httpx", "httpcore"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)


def validate_location(value: object) -> dict:
    if not isinstance(value, dict):
        raise WeatherError("Select a valid weather location")
    labels = {}
    for field in ("name", "region", "country"):
        label = value.get(field)
        if not isinstance(label, str) or len(label.strip()) > 200 or (field == "name" and not label.strip()):
            raise WeatherError("Select a valid weather location")
        labels[field] = label.strip()
    lat, lon = number(value.get("lat")), number(value.get("lon"))
    if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise WeatherError("Select a valid weather location")
    return {**labels, "lat": lat, "lon": lon}


def condition(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    code = value.get("code")
    return code if type(code) is int else None


def normalize_forecast(payload: object, location: dict, now: datetime) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("location"), dict) or not isinstance(payload.get("forecast"), dict):
        raise WeatherError(INVALID)
    zone = payload["location"].get("tz_id")
    try:
        if not isinstance(zone, str):
            raise ValueError
        ZoneInfo(zone)
    except (ValueError, OSError, ZoneInfoNotFoundError):
        raise WeatherError(INVALID) from None
    records = payload["forecast"].get("forecastday")
    if not isinstance(records, list) or not records:
        raise WeatherError(INVALID)
    days, hours = {}, {}
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            day_date = date.fromisoformat(record.get("date", "")).isoformat()
        except (TypeError, ValueError):
            continue
        day = record.get("day")
        if isinstance(day, dict):
            days[day_date] = {"date": day_date, "high_c": number(day.get("maxtemp_c")), "low_c": number(day.get("mintemp_c")), "condition_code": condition(day.get("condition")), "rain_pct": probability(day.get("daily_chance_of_rain")), "snow_pct": probability(day.get("daily_chance_of_snow"))}
        hourly = record.get("hour", [])
        if not isinstance(hourly, list):
            continue
        for hour in hourly:
            if not isinstance(hour, dict):
                continue
            epoch = number(hour.get("time_epoch"))
            if epoch is None or not epoch.is_integer():
                continue
            try:
                datetime.fromtimestamp(epoch, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            hours[int(epoch)] = {"epoch": int(epoch), "temp_c": number(hour.get("temp_c")), "condition_code": condition(hour.get("condition")), "is_day": bool(hour["is_day"]) if type(hour.get("is_day")) in (int, bool) and hour["is_day"] in (0, 1) else None, "rain_pct": probability(hour.get("chance_of_rain")), "snow_pct": probability(hour.get("chance_of_snow"))}
    if not days and not hours:
        raise WeatherError(INVALID)
    return {"location_key": location_key(location), "timezone": zone, "fetched_at": now.astimezone(timezone.utc).isoformat(), "days": [days[key] for key in sorted(days)], "hours": [hours[key] for key in sorted(hours)]}


async def request_json(path: str, key: str | None, params: dict, *, transport=None) -> object:
    if not key:
        raise WeatherError("Weather API key is missing")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=transport) as client:
            async with client.stream("GET", ORIGIN + path, params={**params, "key": key}) as response:
                http_error = "Weather API quota exceeded" if response.status_code == 429 else "Weather API key is invalid" if response.status_code in (401, 403) else UNAVAILABLE
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise WeatherError(INVALID)
                    body.extend(chunk)
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise WeatherError(http_error if not 200 <= response.status_code < 300 else INVALID) from None
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict):
                    code = error.get("code")
                    raise WeatherError(ERROR_CODES.get(code, UNAVAILABLE) if type(code) is int else UNAVAILABLE)
                if not 200 <= response.status_code < 300:
                    raise WeatherError(http_error)
                return payload
    except httpx.HTTPError:
        raise WeatherError(UNAVAILABLE) from None


async def search_locations(query: str, key: str | None, *, transport=None) -> list[dict]:
    payload = await request_json("/v1/search.json", key, {"q": query}, transport=transport)
    if not isinstance(payload, list):
        raise WeatherError(INVALID)
    try:
        return [validate_location(row) for row in payload]
    except WeatherError:
        raise WeatherError(INVALID) from None


async def fetch_forecast(location: dict, key: str | None, now: datetime, *, transport=None) -> dict:
    payload = await request_json("/v1/forecast.json", key, {"q": location_key(location), "days": 3, "aqi": "no", "alerts": "no"}, transport=transport)
    return normalize_forecast(payload, location, now)
