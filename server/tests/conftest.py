import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TZ", "Europe/London")
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest
from sqlalchemy.orm import Session

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
