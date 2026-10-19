import re

import pytest
from fastapi.testclient import TestClient
from jinja2 import UndefinedError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import app
from app.models import DISH_TYPES, Base, Dish
from app.web import templates

ICONS = templates.env.get_template("_icons.html").module
EMPTY_ICON = re.compile(r'<svg class="icon"[^>]*>\s*</svg>')

pytestmark = pytest.mark.usefixtures("fixed_clock")


@pytest.mark.parametrize("name", DISH_TYPES)
def test_every_dish_type_icon_draws_something(name):
    assert not EMPTY_ICON.search(str(ICONS.icon(name)))


def test_unknown_icon_fails_loudly():
    with pytest.raises(UndefinedError):
        ICONS.icon("no-such-icon")


@pytest.fixture
def client(monkeypatch):
    # Sync routes run on a worker thread, so share one in-memory connection across threads.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    with Session(engine) as session:
        session.add_all(Dish(title=f"A {t}", type=t, source_platform="manual") for t in DISH_TYPES)
        session.commit()
        app.dependency_overrides[get_session] = lambda: session
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("url", ["/menu", "/dishes", "/dishes?archived=1", "/dishes/1", "/dishes/new", "/dishes/1/edit",
                                 "/shopping", "/imports/batch", "/tags", "/settings",
                                 "/preview?scenario=busy"])
def test_pages_render_without_empty_icons(client, url):
    r = client.get(url)
    assert r.status_code == 200
    assert '<svg class="icon"' in r.text
    assert not EMPTY_ICON.search(r.text)
