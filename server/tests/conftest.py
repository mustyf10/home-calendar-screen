import dataclasses
import os
import tempfile
from datetime import datetime

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TZ", "Europe/London")
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest
from sqlalchemy.orm import Session

from app import clock
from app.db import make_engine
from app.models import Base, Dish, MenuEntry, Tag
from app.tags.service import CORE_TAGS, set_dish_tags


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        s.add_all(Tag(name=n) for n in CORE_TAGS)
        s.commit()
        yield s


@pytest.fixture
def make_dish(session):
    def _make(title="Dish", type="dinner", tags=(), archived=False, **kw):
        d = Dish(title=title, type=type, archived=archived, source_platform="manual", **kw)
        session.add(d)
        session.flush()
        set_dish_tags(session, d, list(tags))
        session.commit()
        return d

    return _make


@pytest.fixture
def serve(session):
    def _serve(dish, d):
        session.merge(MenuEntry(date=d, kind="dish", dish_id=dish.id))
        session.commit()

    return _serve


FIXED_NOW = "2026-10-16T12:00"  # a Friday


@pytest.fixture
def fixed_clock(monkeypatch):
    """Pins app.clock.now() (and so today()) to FIXED_NOW in the configured zone."""
    now = datetime.fromisoformat(FIXED_NOW).replace(tzinfo=clock.tz())
    monkeypatch.setattr(clock, "now", lambda: now)
    return now


class FakeClaude:
    """Stands in for service._call_claude: records dish titles, can run code mid-call or raise."""

    def __init__(self):
        self.calls = []
        self.during = None
        self.error = None

    def __call__(self, dishes, pantry):
        self.calls.append([d.title for d in dishes])
        if self.during:
            self.during()
        if self.error:
            raise self.error
        items = [{"item": d.title, "quantity": "1", "for": [d.title]} for d in dishes]
        return {"aisles": [{"name": "Produce", "items": items}], "check_you_have": []}


@pytest.fixture
def fake_claude(monkeypatch):
    from app.shopping import service

    fake = FakeClaude()
    monkeypatch.setattr(service, "config", dataclasses.replace(service.config, anthropic_api_key="k"))
    monkeypatch.setattr(service, "_call_claude", fake)
    return fake
