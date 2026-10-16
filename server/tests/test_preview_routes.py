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
