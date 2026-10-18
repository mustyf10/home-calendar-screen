import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.web import templates


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_enable_without_location_keeps_typed_preferences(client):
    response = client.post("/settings/weather", data={"enabled": "on", "mode": "3h", "unit": "fahrenheit"})
    assert response.status_code == 422
    assert 'value="3h" selected' in response.text
    assert 'value="fahrenheit" selected' in response.text
    assert "Select a weather location before enabling weather" in response.text


def test_defaults_and_search_requires_explicit_selection(client, session, monkeypatch):
    from app.weather import routes, service
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key="SECRET"))
    monkeypatch.setattr(service, "config", dataclasses.replace(service.config, weather_api_key="SECRET"))
    async def search(query, key):
        assert query == "London"
        return [{"name": "London<script>", "region": "Ontario", "country": "Canada", "lat": 42.98, "lon": -81.25}, {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1}]
    monkeypatch.setattr(routes, "search_locations", search)
    page = client.get("/settings")
    assert page.status_code == 200
    assert "No weather location selected" in page.text
    assert "SECRET" not in page.text
    response = client.get("/settings/weather/search", params={"q": "London"})
    assert response.status_code == 200
    assert "Ontario" in response.text and "UK" in response.text
    assert "London&lt;script&gt;" in response.text
    assert "London<script>" not in response.text
    assert service.load_weather(session).config["location"] is None
    session.rollback()
    response = client.post("/settings/weather/location", data={"name": "London", "region": "", "country": "UK", "lat": "51.5", "lon": "-0.1"}, follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    saved = service.load_weather(session)
    assert saved.config["location"]["name"] == "London"
    assert not saved.config["enabled"]
    assert "Timezone resolved on first forecast" in client.get("/settings").text


@pytest.mark.parametrize("query", ["", " " * 5, "x" * 101])
def test_invalid_search_is_read_only_422(client, query):
    assert client.get("/settings/weather/search", params={"q": query}).status_code == 422


def test_missing_key_no_matches_and_safe_upstream_failure(client, monkeypatch):
    from app.weather import routes
    from app.weather.types import WeatherError
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key=None))
    response = client.get("/settings/weather/search?q=London")
    assert response.status_code == 422
    assert "WEATHER_API_KEY" in response.text
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key="SECRET"))
    async def empty(query, key):
        return []
    monkeypatch.setattr(routes, "search_locations", empty)
    response = client.get("/settings/weather/search?q=London")
    assert response.status_code == 200
    assert "No matching locations" in response.text
    async def fail(query, key):
        raise WeatherError("Weather service is unavailable")
    monkeypatch.setattr(routes, "search_locations", fail)
    response = client.get("/settings/weather/search?q=London")
    assert response.status_code == 502
    assert "Weather service is unavailable" in response.text
    assert "SECRET" not in response.text


@pytest.mark.parametrize("lat,lon", [("nan", "1"), ("91", "1"), ("1", "181"), ("inf", "0"), ("no", "0")])
def test_invalid_coordinates_do_not_mutate(client, session, lat, lon):
    from app.weather.service import load_weather
    response = client.post("/settings/weather/location", data={"name": "Bad", "region": "", "country": "", "lat": lat, "lon": lon})
    assert response.status_code == 422
    assert load_weather(session).config["location"] is None


def test_enable_disable_prerequisites_and_invalid_enums(client, session, monkeypatch):
    from app.weather import routes, service
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key=None))
    client.post("/settings/weather/location", data={"name": "London", "lat": "51.5", "lon": "-0.1"})
    response = client.post("/settings/weather", data={"enabled": "on", "mode": "2h", "unit": "fahrenheit"})
    assert response.status_code == 422
    assert "Weather API key is missing" in response.text
    assert service.load_weather(session).config["mode"] == "daily"
    session.rollback()
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key="SECRET"))
    assert client.post("/settings/weather", data={"enabled": "on", "mode": "2h", "unit": "fahrenheit"}, follow_redirects=False).status_code == 303
    session.expire_all()
    saved = service.load_weather(session).config
    assert saved["enabled"] and saved["mode"] == "2h" and saved["unit"] == "fahrenheit"
    generation = saved["fetch_generation"]
    session.rollback()
    response = client.post("/settings/weather", data={"enabled": "on", "mode": "bogus", "unit": "fahrenheit"})
    assert response.status_code == 422
    session.rollback()
    monkeypatch.setattr(routes, "config", dataclasses.replace(routes.config, weather_api_key=None))
    assert client.post("/settings/weather", data={"mode": "2h", "unit": "fahrenheit"}, follow_redirects=False).status_code == 303
    session.expire_all()
    saved = service.load_weather(session).config
    assert not saved["enabled"]
    assert saved["fetch_generation"] == generation + 1
