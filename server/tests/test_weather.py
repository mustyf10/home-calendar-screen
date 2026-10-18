from datetime import datetime, timezone


def test_missing_middle_boundary_keeps_its_labelled_cell():
    from app.weather.forecast import select_weather
    from app.weather.types import WeatherInputs

    now = datetime(2026, 10, 18, 9, 20, tzinfo=timezone.utc)
    inputs = WeatherInputs(
        config={"enabled": True, "mode": "2h", "unit": "celsius", "location": {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1}, "fetch_generation": 0},
        snapshot={"location_key": "51.5,-0.1", "timezone": "UTC", "fetched_at": now.isoformat(), "days": [], "hours": [
            {"epoch": int(now.replace(hour=hour, minute=0).timestamp()), "temp_c": 12, "condition_code": 1000, "is_day": True, "rain_pct": 0, "snow_pct": 0}
            for hour in (10, 14, 16)
        ]},
    )
    result = select_weather(inputs, now, actual_now=now, app_timezone="UTC")
    assert [cell["label"] for cell in result["cells"]] == ["10:00", "12:00", "14:00", "16:00"]
    assert [cell["temperature"] for cell in result["cells"]] == ["12°", "—", "12°", "12°"]
    assert result["cells"][0]["probability"] == "0%"


def test_normalization_preserves_zero_and_missing_values():
    from app.weather.client import normalize_forecast

    now = datetime(2026, 10, 18, 9, 20, tzinfo=timezone.utc)
    payload = {"location": {"tz_id": "Europe/London"}, "forecast": {"forecastday": [
        {"date": "2026-10-18", "day": {"maxtemp_c": 12.5, "mintemp_c": None, "condition": {"code": 1210}, "daily_chance_of_rain": "0", "daily_chance_of_snow": "101"}, "hour": [
            {"time_epoch": 1792317600, "temp_c": "-2.5", "condition": {"code": 9999}, "is_day": 0, "chance_of_rain": "40", "chance_of_snow": None}
        ]}
    ]}}
    snapshot = normalize_forecast(payload, {"lat": 51.5, "lon": -0.1}, now)
    assert snapshot["location_key"] == "51.5,-0.1"
    assert snapshot["days"][0] == {"date": "2026-10-18", "high_c": 12.5, "low_c": None, "condition_code": 1210, "rain_pct": 0, "snow_pct": None}
    assert snapshot["hours"][0] == {"epoch": 1792317600, "temp_c": -2.5, "condition_code": 9999, "is_day": False, "rain_pct": 40, "snow_pct": None}


import asyncio
import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest


def weather_inputs(mode="daily", unit="celsius", zone="UTC"):
    from app.weather.types import WeatherInputs
    now = datetime(2026, 10, 18, 9, 20, tzinfo=timezone.utc)
    return WeatherInputs(
        config={"enabled": True, "mode": mode, "unit": unit, "location": {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1}, "fetch_generation": 0},
        snapshot={"location_key": "51.5,-0.1", "timezone": zone, "fetched_at": now.isoformat(),
                  "days": [{"date": f"2026-10-{day}", "high_c": 16.5, "low_c": -2.5, "condition_code": 1210, "rain_pct": 99, "snow_pct": 0} for day in (18, 20)],
                  "hours": [{"epoch": int((now.replace(hour=0, minute=0) + timedelta(hours=hour)).timestamp()), "temp_c": -0.1, "condition_code": 1000, "is_day": False, "rain_pct": 40, "snow_pct": 0} for hour in range(72)]},
    ), now


@pytest.mark.parametrize("age,stale,unavailable", [(3599, False, False), (3600, True, False), (21599, True, False), (21600, False, True), (-1, False, True)])
def test_age_uses_actual_clock(age, stale, unavailable):
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    result = select_weather(inputs, now, actual_now=now + timedelta(seconds=age))
    assert result["stale"] is stale
    assert result["unavailable"] is unavailable


def test_daily_dates_snow_probability_and_rounding():
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    result = select_weather(inputs, now, actual_now=now)
    assert [cell["label"] for cell in result["cells"]] == ["Sun 18", "Mon 19", "Tue 20"]
    assert [cell["temperature"] for cell in result["cells"]] == ["17°/-3°", "—", "17°/-3°"]
    assert result["cells"][0]["precipitation"] == "Snow"
    assert result["cells"][0]["probability"] == "0%"
    inputs.config["unit"] = "fahrenheit"
    assert select_weather(inputs, now, actual_now=now)["cells"][0]["temperature"] == "62°/28°"
    assert select_weather(inputs, now + timedelta(days=8), actual_now=now)["unavailable"]


@pytest.mark.parametrize("mode,labels", [("2h", ["10:00", "12:00", "14:00", "16:00"]), ("3h", ["12:00", "15:00", "18:00", "21:00"])])
def test_fixed_boundaries_and_exact_boundary(mode, labels):
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs(mode)
    result = select_weather(inputs, now, actual_now=now)
    assert [cell["label"] for cell in result["cells"]] == labels
    assert result["cells"][0]["temperature"] == "0°"
    assert result["cells"][0]["icon"] == "clear-night"
    exact = now.replace(hour=12, minute=0)
    assert select_weather(inputs, exact, actual_now=now)["cells"][0]["label"] == ("14:00" if mode == "2h" else "15:00")


def test_cross_midnight_and_dst_boundaries():
    from app.weather.forecast import boundaries, select_weather
    inputs, now = weather_inputs("2h")
    result = select_weather(inputs, now.replace(hour=23), actual_now=now)
    assert result["cells"][0]["label"] == "00:00"
    assert result["cells"][0]["day_label"] == "Mon 19"
    zone = ZoneInfo("Europe/Berlin")
    spring = boundaries(datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), zone, 2)
    assert [dt.strftime("%H:%M") for dt in spring] == ["04:00", "06:00", "08:00", "10:00"]
    autumn = boundaries(datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc) - timedelta(minutes=1), zone, 2)
    assert [int(dt.timestamp()) for dt in autumn[:2]] == [1792886400, 1792890000]
    assert [dt.strftime("%H:%M") for dt in autumn[:2]] == ["02:00", "02:00"]


@pytest.mark.parametrize("payload", [None, [], {}, {"location": {"tz_id": "bad"}, "forecast": {"forecastday": []}}, {"location": {"tz_id": "UTC"}, "forecast": {"forecastday": []}}])
def test_malformed_envelope_is_rejected(payload):
    from app.weather.client import normalize_forecast
    from app.weather.types import WeatherError
    with pytest.raises(WeatherError, match="Weather response was invalid"):
        normalize_forecast(payload, {"lat": 0, "lon": 0}, datetime.now(timezone.utc))


@pytest.mark.parametrize("code,message", [(2006, "Weather API key is invalid"), (2007, "Weather API quota exceeded"), (1006, "Weather location was not found")])
def test_provider_errors_are_safe(code, message, caplog):
    from app.weather.client import search_locations
    from app.weather.types import WeatherError
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={"error": {"code": code, "message": "SECRET raw provider error"}}))
    with pytest.raises(WeatherError, match=message) as error:
        asyncio.run(search_locations("London", "SECRET", transport=transport))
    assert "SECRET" not in str(error.value)
    assert "SECRET" not in caplog.text


def test_provider_shape_and_bounded_body():
    from app.weather.client import fetch_forecast, search_locations
    from app.weather.types import WeatherError
    fixtures = Path(__file__).parent / "fixtures" / "weather"
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=json.loads((fixtures / ("search.json" if "search" in request.url.path else "forecast.json")).read_text()))
    transport = httpx.MockTransport(respond)
    locations = asyncio.run(search_locations("London", "secret", transport=transport))
    assert [row["country"] for row in locations] == ["United Kingdom", "Canada"]
    assert "tz_id" not in locations[0]
    snapshot = asyncio.run(fetch_forecast(locations[0], "secret", datetime.now(timezone.utc), transport=transport))
    assert snapshot["timezone"] == "Europe/London"
    assert requests[-1].url.params["days"] == "3"
    assert requests[-1].url.params["q"] == "51.5,-0.1"
    with pytest.raises(WeatherError, match="Weather response was invalid"):
        asyncio.run(search_locations("London", "secret", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1)))))


def test_empty_rows_are_unavailable_and_corrupt_fields_do_not_break_selection():
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    inputs.snapshot["days"] = [{"date": "2026-10-18", "high_c": None, "low_c": None, "condition_code": [], "rain_pct": None, "snow_pct": None}, {"date": []}]
    assert select_weather(inputs, now, actual_now=now)["unavailable"]


@pytest.mark.parametrize("status,message", [(503, "Weather service is unavailable"), (401, "Weather API key is invalid"), (429, "Weather API quota exceeded")])
def test_non_json_http_failures_keep_safe_http_classification(status, message):
    from app.weather.client import search_locations
    from app.weather.types import WeatherError
    with pytest.raises(WeatherError, match=message):
        asyncio.run(search_locations("London", "secret", transport=httpx.MockTransport(lambda request: httpx.Response(status, content=b"<html>upstream failure</html>"))))


def test_timeouts_redirects_and_invalid_json_never_expose_key():
    from app.weather.client import search_locations
    from app.weather.types import WeatherError
    def timeout(request):
        raise httpx.ReadTimeout("secret https://api.weatherapi.com?key=secret", request=request)
    for transport, message in [
        (httpx.MockTransport(timeout), "Weather service is unavailable"),
        (httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.example/?key=secret"}, json={})), "Weather service is unavailable"),
        (httpx.MockTransport(lambda request: httpx.Response(200, content=b"{bad")), "Weather response was invalid"),
    ]:
        with pytest.raises(WeatherError, match=message) as caught:
            asyncio.run(search_locations("London", "secret", transport=transport))
        assert "secret" not in str(caught.value)


@pytest.mark.parametrize("raw,expected", [("0", 0), (0, 0), (None, None), ("bad", None), (101, None), (-1, None), (True, None), ("50.5", 50.5)])
def test_probability_values_are_not_clamped(raw, expected):
    from app.weather.types import probability
    assert probability(raw) == expected


def test_all_condition_groups_and_unknown_have_explicit_precipitation():
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    for code, icon, kind in [(1000, "clear-day", "Rain"), (1003, "partly-cloudy", "Rain"), (1009, "cloud", "Rain"), (1012, "fog", "Rain"), (1183, "rain", "Rain"), (1210, "snow", "Snow"), (1276, "thunder", "Rain"), (1279, "thunder-snow", "Snow"), (9999, "unknown", "Rain"), (None, "unknown", "Rain")]:
        inputs.snapshot["days"][0].update(condition_code=code, rain_pct=40, snow_pct=60)
        cell = select_weather(inputs, now, actual_now=now)["cells"][0]
        assert (cell["icon"], cell["precipitation"], cell["probability"]) == (icon, kind, "60%" if kind == "Snow" else "40%")


def test_out_of_range_probability_alone_is_not_useful_coverage():
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    inputs.snapshot["days"] = [{"date": "2026-10-18", "rain_pct": 101, "snow_pct": -1}]
    assert select_weather(inputs, now, actual_now=now)["unavailable"]


def test_normalized_rows_are_sorted_deduplicated_and_partial():
    from app.weather.client import normalize_forecast
    payload = {"location": {"tz_id": "UTC"}, "forecast": {"forecastday": [
        {"date": "2026-10-20", "day": {"maxtemp_c": "nan"}, "hour": [{"time_epoch": 200, "temp_c": 2}, {"time_epoch": 100, "temp_c": 1}, {"time_epoch": "bad"}]},
        {"date": "2026-10-18", "day": {"maxtemp_c": 3}, "hour": [{"time_epoch": 100, "temp_c": 4}]},
        {"date": "2026-10-18", "day": {"maxtemp_c": 5}, "hour": []},
        {"date": "invalid", "day": {}},
    ]}}
    snapshot = normalize_forecast(payload, {"lat": 0, "lon": -0.0}, datetime.now(timezone.utc))
    assert snapshot["location_key"] == "0,0"
    assert [row["date"] for row in snapshot["days"]] == ["2026-10-18", "2026-10-20"]
    assert snapshot["days"][0]["high_c"] == 5
    assert snapshot["days"][1]["high_c"] is None
    assert [row["epoch"] for row in snapshot["hours"]] == [100, 200]
    assert snapshot["hours"][0]["temp_c"] == 4


def test_fallback_duplicate_clock_labels_include_timezone():
    from app.weather.forecast import boundaries, select_weather
    inputs, fetched = weather_inputs("2h", zone="Europe/Berlin")
    effective = datetime(2026, 10, 24, 23, 59, tzinfo=timezone.utc)
    slots = boundaries(effective, ZoneInfo("Europe/Berlin"), 2)
    inputs.snapshot["hours"] = [{"epoch": int(slot.timestamp()), "temp_c": 3} for slot in slots]
    result = select_weather(inputs, effective, actual_now=fetched)
    assert [cell["label"] for cell in result["cells"][:2]] == ["02:00 CEST", "02:00 CET"]
    assert [cell["epoch"] for cell in result["cells"]] == sorted(cell["epoch"] for cell in result["cells"])


def test_oversized_cache_timezone_is_unavailable_without_exception():
    from app.weather.forecast import select_weather
    inputs, now = weather_inputs()
    inputs.snapshot["timezone"] = "x" * 1000
    assert select_weather(inputs, now, actual_now=now)["unavailable"]
