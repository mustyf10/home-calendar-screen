import dataclasses
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import clock, settings_store
from app.db import get_session
from app.display import routes, settings
from app.main import app
from app.web import templates


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def rendered_frame(monkeypatch):
    async def fake_frame(size):
        return b"offline-test-png", '"unchanged-image"'
    monkeypatch.setattr(routes, "get_sized_frame", fake_frame)


def display_section(response):
    return response.text.split('<section id="display">', 1)[1].split("</section>", 1)[0]


def test_default_policy_and_accessible_form(client, session, monkeypatch):
    monkeypatch.setattr(clock, "config", dataclasses.replace(clock.config, tz="America/New_York"))
    assert settings.load_policy(session) == {
        "night_enabled": True, "night_start": "23:00", "night_end": "06:00",
        "timezone": "America/New_York",
    }
    response = client.get("/settings")
    assert response.status_code == 200
    section = display_section(response)
    assert re.search(r'name="night_enabled"[^>]*checked', section)
    assert 'name="night_start" value="23:00"' in section
    assert 'name="night_end" value="06:00"' in section
    assert 'for="display-night-start"' in section and 'for="display-night-end"' in section
    assert "America/New_York" in section
    assert settings_store.get(session, "display.policy") is None


def test_edit_disable_and_timezone_is_derived(client, session, monkeypatch):
    response = client.post("/settings/display", data={
        "night_enabled": "on", "night_start": " 22:15 ", "night_end": "07:30",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    session.expire_all()
    saved = json.loads(settings_store.get(session, "display.policy"))
    assert saved == {"night_enabled": True, "night_start": "22:15", "night_end": "07:30"}
    section = display_section(client.get("/settings"))
    assert 'name="night_start" value="22:15"' in section
    assert 'name="night_end" value="07:30"' in section
    response = client.post("/settings/display", data={
        "night_start": "08:00", "night_end": "08:00",
    }, follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    assert settings.load_policy(session)["night_enabled"] is False
    section = display_section(client.get("/settings"))
    assert not re.search(r'name="night_enabled"[^>]*checked', section)
    monkeypatch.setattr(clock, "config", dataclasses.replace(clock.config, tz="Asia/Tokyo"))
    assert settings.load_policy(session)["timezone"] == "Asia/Tokyo"
    assert "timezone" not in json.loads(settings_store.get(session, "display.policy"))


@pytest.mark.parametrize("enabled,start,end", [
    ("on", "24:00", "06:00"), ("on", "23:00", "06:60"),
    ("on", "7:00", "06:00"), ("on", "23:00:00", "06:00"),
    ("on", "2026-10-20T23:00", "06:00"), ("on", "09:00", "09:00"),
    ("", "invalid", "06:00"), ("", "23:00", "invalid"),
])
def test_invalid_values_preserve_typed_form_and_saved_policy(client, session, enabled, start, end):
    settings.save_policy(session, night_enabled=True, night_start="22:00", night_end="07:00")
    session.commit()
    before = settings_store.get(session, "display.policy")
    response = client.post("/settings/display", data={
        "night_enabled": enabled, "night_start": start, "night_end": end,
    })
    assert response.status_code == 422
    section = display_section(response)
    assert 'role="alert"' in section
    assert f'name="night_start" value="{start}"' in section
    assert f'name="night_end" value="{end}"' in section
    assert bool(re.search(r'name="night_enabled"[^>]*checked', section)) == bool(enabled)
    session.expire_all()
    assert settings_store.get(session, "display.policy") == before


def test_invalid_initial_submission_does_not_create_policy(client, session):
    response = client.post("/settings/display", data={
        "night_enabled": "on", "night_start": "09:00", "night_end": "09:00",
    })
    assert response.status_code == 422
    assert settings_store.get(session, "display.policy") is None


def test_write_failure_rolls_back_and_keeps_typed_values(client, session, monkeypatch):
    settings.save_policy(session, night_enabled=True, night_start="23:00", night_end="06:00")
    session.commit()
    before = settings_store.get(session, "display.policy")

    def read_only():
        raise PermissionError("read-only settings")

    monkeypatch.setattr(session, "commit", read_only)
    response = client.post("/settings/display", data={"night_start": "21:30", "night_end": "08:15"})
    assert response.status_code == 422
    section = display_section(response)
    assert 'role="alert"' in section
    assert 'name="night_start" value="21:30"' in section
    assert 'name="night_end" value="08:15"' in section
    assert not re.search(r'name="night_enabled"[^>]*checked', section)
    session.expire_all()
    assert settings_store.get(session, "display.policy") == before


@pytest.mark.parametrize("raw", ["not json", "[]", '{"night_enabled":"false"}',
    '{"night_enabled":true,"night_start":"24:00","night_end":"06:00"}',
    '{"night_enabled":true,"night_start":"06:00","night_end":"06:00"}',
])
def test_malformed_stored_policy_uses_valid_defaults(session, raw):
    settings_store.put(session, "display.policy", raw)
    session.commit()
    assert settings.load_policy(session) == {**settings.DEFAULTS, "timezone": clock.tz().key}


def test_frame_policy_changes_reach_200_and_304_without_changing_etag(client, rendered_frame):
    first = client.get("/display/frame.png")
    assert first.status_code == 200
    initial_policy = json.loads(first.headers["X-Display-Policy"])
    assert initial_policy == {**settings.DEFAULTS, "timezone": clock.tz().key}
    etag = first.headers["etag"]
    assert client.post("/settings/display", data={
        "night_start": "21:00", "night_end": "08:00",
    }, follow_redirects=False).status_code == 303
    unchanged = client.get("/display/frame.png", headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    updated = {"night_enabled": False, "night_start": "21:00", "night_end": "08:00", "timezone": clock.tz().key}
    assert json.loads(unchanged.headers["X-Display-Policy"]) == updated
    assert unchanged.headers["etag"] == etag
    fresh = client.get("/display/frame.png")
    assert fresh.status_code == 200
    assert fresh.headers["etag"] == etag
    assert json.loads(fresh.headers["X-Display-Policy"]) == updated


def test_render_failure_still_delivers_updated_policy(client, monkeypatch):
    async def fail_frame(size):
        raise RuntimeError("renderer offline")
    monkeypatch.setattr(routes, "get_sized_frame", fail_frame)
    response = client.get("/display/frame.png")
    assert response.status_code == 503
    assert json.loads(response.headers["X-Display-Policy"])["night_enabled"] is True
    assert client.post("/settings/display", data={
        "night_start": "20:00", "night_end": "07:00",
    }, follow_redirects=False).status_code == 303
    response = client.get("/display/frame.png", headers={"If-None-Match": '"old"'})
    assert response.status_code == 503
    assert response.text == "render failed"
    assert json.loads(response.headers["X-Display-Policy"]) == {
        "night_enabled": False, "night_start": "20:00", "night_end": "07:00", "timezone": clock.tz().key,
    }
