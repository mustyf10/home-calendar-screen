import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models import DISH_TYPES
from app.web import templates


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def category_links(html: str) -> list[tuple[dict, bool]]:
    nav = re.search(r'<nav class="categories"[^>]*>(.*?)</nav>', html, re.S).group(1)
    links = re.findall(r'<a href="([^"]+)"( aria-current="true")?>', nav)
    return [(parse_qs(urlsplit(unescape(href)).query), bool(current)) for href, current in links]


def test_category_links_keep_filters_set_type_and_mark_current(client, make_dish):
    make_dish("Pie", type="baking", tags=["sweet"])
    html = client.get("/dishes", params={"q": "pie", "tag": "sweet", "type": "baking", "archived": "1"}).text
    links = category_links(html)
    assert len(links) == 1 + len(DISH_TYPES)
    for (query, _), expected_type in zip(links, [None, *DISH_TYPES]):
        assert query.get("q") == ["pie"]
        assert query.get("tag") == ["sweet"]
        assert query.get("archived") == ["1"]
        assert query.get("type") == ([expected_type] if expected_type else None)
    assert [current for _, current in links] == [False, *(key == "baking" for key in DISH_TYPES)]


def test_all_is_current_without_a_type(client):
    links = category_links(client.get("/dishes").text)
    assert [current for _, current in links] == [True] + [False] * len(DISH_TYPES)
    assert all(query == {} or set(query) == {"type"} for query, _ in links)


def test_breakfast_filter_excludes_dinners(client, make_dish):
    breakfast = make_dish("Oat pancakes", type="breakfast")
    dinner = make_dish("Lentil curry", type="dinner")
    response = client.get("/dishes", params={"type": "breakfast"})
    assert response.status_code == 200
    assert f'href="/dishes/{breakfast.id}"' in response.text
    assert f'href="/dishes/{dinner.id}"' not in response.text
    assert [current for _, current in category_links(response.text)] == [
        False, True, *([False] * (len(DISH_TYPES) - 1))
    ]
