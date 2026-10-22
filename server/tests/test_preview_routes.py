import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import app
from app.models import Base
from app.web import templates

pytestmark = pytest.mark.usefixtures("fixed_clock")

@pytest.fixture
def client(monkeypatch):
    # Sync routes run on a worker thread, so share one in-memory connection across threads.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    with Session(engine) as session:
        app.dependency_overrides[get_session] = lambda: session
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_preview_page_has_both_panes(client):
    r = client.get("/preview?scenario=busy")
    assert r.status_code == 200
    assert re.search(r'<iframe [^>]*src="/display\?scenario=busy"', r.text)
    assert re.search(r'<img [^>]*src="/preview/frame\.png\?scenario=busy&v=[0-9a-f]{16}"', r.text)
    for label in ("No calendars", "Calendar failing", "Backup stale"):
        assert label in r.text


def test_preview_page_shows_range_note(client):
    r = client.get("/preview?at=2027-06-01T09:00")
    assert "Calendar data only covers" in r.text


def test_version_is_sixteen_hex(client):
    r = client.get("/preview/version?scenario=busy")
    assert r.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{16}", r.text)
    assert r.headers["cache-control"] == "no-store"


def test_display_accepts_scenario(client):
    assert "+6 more today" in client.get("/display?scenario=busy").text


def test_preview_frame_render_failure_is_503(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("no chromium")

    monkeypatch.setattr("app.display.preview_routes.get_preview_frame", boom)
    r = client.get("/preview/frame.png?scenario=busy&v=abc")
    assert (r.status_code, r.text) == (503, "render failed")


def test_preview_frame_png(client, monkeypatch):
    seen = []

    async def fake(params, ver):
        seen.append((params.query(), ver))
        return b"\x89PNG fake", "0123456789abcdef"

    monkeypatch.setattr("app.display.preview_routes.get_preview_frame", fake)
    r = client.get("/preview/frame.png?scenario=busy&v=ignored")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-preview-version"] == "0123456789abcdef"
    assert seen[0][0] == "scenario=busy" and len(seen[0][1]) == 16


def test_weather_preview_and_version_do_not_make_upstream_requests(client, monkeypatch):
    import httpx

    async def forbidden(*args, **kwargs):
        raise AssertionError("Display and preview must only read cached weather")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    for query in ("scenario=quiet&weather=sample", "scenario=busy&weather=snow", "weather=unavailable", "weather=off", ""):
        assert client.get("/display?" + query).status_code == 200
        assert client.get("/preview/version?" + query).status_code == 200


def test_preview_unknown_weather_is_not_reflected_in_version_urls(client):
    response = client.get("/preview?scenario=quiet&weather=weather-test-secret")
    assert response.status_code == 200
    assert "weather-test-secret" not in response.text
    assert "weather-test-secret" not in client.get("/preview/version?weather=weather-test-secret").text


@pytest.mark.parametrize("endpoint", ["/preview", "/preview/version", "/preview/frame.png", "/display"])
@pytest.mark.parametrize("query", [
    "width=0", "width=", "width=abc", "width=400&width=400",
    "width=8193", "width=8192&height=8192", "height=", "height=480&height=480",
])
def test_invalid_geometry_is_validation_error(client, monkeypatch, endpoint, query):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid geometry must not invoke rendering")

    monkeypatch.setattr("app.display.preview_routes.get_preview_frame", forbidden)
    response = client.get(f"{endpoint}?{query}")
    assert response.status_code == 422
    assert "render failed" not in response.text


def test_preview_geometry_reaches_controls_and_both_panes(client):
    response = client.get("/preview?scenario=busy&width=600&height=800")
    assert response.status_code == 200
    assert re.search(r'name="width" value="600"', response.text)
    assert re.search(r'name="height" value="800"', response.text)
    assert re.search(r'<iframe [^>]*width="600" height="800"', response.text)
    assert re.search(r'<img [^>]*width="600" height="800"', response.text)
    assert 'height=800&amp;scenario=busy&amp;width=600' in response.text
    assert re.search(r'name="zoom" value="mm" disabled', response.text)
    assert re.search(r'name="physical_width_mm"[^>]*value=""', response.text)


def test_default_preview_retains_physical_width(client):
    response = client.get("/preview?scenario=busy")
    assert re.search(r'name="physical_width_mm"[^>]*value="163.2"', response.text)
    assert not re.search(r'name="zoom" value="mm" disabled', response.text)


def test_frame_geometry_reaches_renderer_and_ignores_browser_calibration(client, monkeypatch):
    seen = []

    async def fake(params, ver):
        seen.append(params)
        return b"png", ver

    monkeypatch.setattr("app.display.preview_routes.get_preview_frame", fake)
    response = client.get("/preview/frame.png?width=600&height=800&physical_width_mm=100")
    assert response.status_code == 200
    assert (seen[0].width, seen[0].height) == (600, 800)
    assert seen[0].query() == "height=800&width=600"
