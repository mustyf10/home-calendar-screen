import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import settings_store
from app.display.data import DisplayInputs, compose, load_inputs
from app.display.preview import parse_params, preview_model
from app.weather.scenarios import sample_weather
from app.web import templates
from starlette.datastructures import QueryParams

LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 3, 22, 23, 20, tzinfo=LONDON)
pytestmark = pytest.mark.usefixtures("fixed_clock")


def screen(model):
    return templates.env.get_template("display.html").render(m=model)


def test_disabled_weather_preserves_display_layout(session):
    inputs = load_inputs(session, NOW)
    model = compose(inputs, NOW, actual_now=NOW)
    assert model.weather is None
    html = screen(model)
    assert '<section class="left">' in html
    assert '<section class="weather-strip"' not in html
    assert len(model.month_grid) == 6
    assert len(model.dinners.rows) == 7


@pytest.mark.parametrize("mode,count", [("daily", 3), ("2h", 4), ("3h", 4)])
@pytest.mark.parametrize("unit,label", [("celsius", "°C"), ("fahrenheit", "°F")])
def test_cached_weather_composes_modes_units_without_changing_calendar(session, mode, count, unit, label):
    weather = sample_weather(NOW, NOW, "sample")
    weather.config.update(mode=mode, unit=unit)
    settings_store.put(session, "weather.config", json.dumps(weather.config))
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    model = compose(load_inputs(session, NOW), NOW, actual_now=NOW)
    assert model.weather["unit_label"] == label
    assert len(model.weather["cells"]) == count
    assert len(model.month_grid) == 6
    assert model.header.day == 22
    html = screen(model)
    assert 'class="left weather-enabled"' in html
    assert html.index("WeatherAPI.com") < html.index('class="weather-cells"')
    assert 'href="https://www.weatherapi.com/"' in html
    assert '<svg class="weather-icon"' in html
    assert "Rain " in html
    if mode != "daily":
        assert "Mon 23" in html
    elif unit == "fahrenheit":
        assert "104°/-40°" in html
        assert 'weather-temperature-long' in html


@pytest.mark.parametrize("state,text", [("snow", "Snow 60%"), ("missing", "Rain —"),
                                        ("stale", "Stale"), ("unavailable", "Weather unavailable")])
def test_preview_weather_state_is_visible(state, text):
    params = parse_params(QueryParams(f"scenario=quiet&weather={state}&at=2026-03-22T23:20"))
    model, _ = preview_model(None, params)
    assert text in screen(model)


def test_weather_actual_age_is_independent_of_pretend_display_time():
    weather = sample_weather(NOW, NOW, "sample")
    inputs = DisplayInputs([], {}, None, weather=weather)
    model = compose(inputs, NOW + timedelta(days=1), actual_now=NOW)
    assert model.weather["unavailable"] is False
    assert model.weather["stale"] is False
    expired = compose(inputs, NOW, actual_now=NOW + timedelta(hours=6))
    assert expired.weather["unavailable"] is True
    outside = compose(inputs, NOW + timedelta(days=90), actual_now=NOW)
    assert outside.weather["unavailable"] is True


def test_weather_off_override_does_not_mutate_saved_preferences(session):
    weather = sample_weather(NOW, NOW, "sample")
    value = json.dumps(weather.config)
    settings_store.put(session, "weather.config", value)
    session.commit()
    model, _ = preview_model(session, parse_params(QueryParams("weather=off")))
    assert model.weather is None
    assert settings_store.get(session, "weather.config") == value


@pytest.mark.parametrize("kind", ["clear-day", "clear-night", "partly-cloudy", "cloud", "fog", "rain", "snow", "thunder", "thunder-snow", "unknown"])
def test_all_weather_icons_are_local_monochrome_svg(kind):
    icon = templates.env.get_template("weather/icons.html").module.weather_icon(kind)
    assert '<svg ' in icon and 'stroke="#000"' in icon
    assert '<path ' in icon
    assert '<image' not in icon and 'http' not in icon


def test_weather_timezone_does_not_change_calendar_date_or_menu():
    weather = sample_weather(NOW, NOW, "sample")
    weather.snapshot["timezone"] = "Asia/Tokyo"
    model = compose(DisplayInputs([], {}, None, weather=weather), NOW, actual_now=NOW)
    assert model.header.day == 22
    assert model.weather["timezone_label"] == "JST"
    assert model.weather["cells"][0]["label"] == "Mon 23"
    assert "JST" in screen(model)


def test_duplicate_clock_label_keeps_abbreviation_inside_weather_label():
    weather = sample_weather(NOW, NOW, "sample")
    model = compose(DisplayInputs([], {}, None, weather=weather), NOW, actual_now=NOW)
    model.weather["cells"] = [{
        "label": "02:00 CEST", "day_label": "Sun 25", "temperature": "-2°",
        "icon": "clear-night", "precipitation": "Rain", "probability": "0%",
    }] * 4
    html = screen(model)
    assert 'class="weather-label weather-label-long"' in html
    assert "02:00 CEST" in html and "Sun 25" in html


def test_rendering_and_version_polling_never_contact_the_provider(session, monkeypatch):
    import httpx
    from app.display.preview import version

    def forbidden(*args, **kwargs):
        raise AssertionError("render must not make upstream requests")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    weather = sample_weather(NOW, NOW, "sample")
    settings_store.put(session, "weather.config", json.dumps(weather.config))
    settings_store.put(session, "weather.snapshot", json.dumps(weather.snapshot))
    session.commit()
    screen(compose(load_inputs(session, NOW), NOW, actual_now=NOW))
    version(session, parse_params(QueryParams("")))
    version(session, parse_params(QueryParams("scenario=busy&weather=snow")))
