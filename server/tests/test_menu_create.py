"""Create-from-picker: Create offer, exact-title rules, and assigning without extra work."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_session
from app.main import app
from app.models import Dish, IngestJob, MenuEntry
from app.web import templates

pytestmark = pytest.mark.usefixtures("fixed_clock")

DAY = date(2026, 10, 20)  # a Tuesday
ISO = DAY.isoformat()


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def pick(client, q):
    return client.get(f"/menu/{ISO}/pick", params={"q": q}).text


def create(client, title):
    return client.post(f"/menu/{ISO}/create", data={"title": title})


def create_forms(html: str) -> list[str]:
    return re.findall(rf'hx-post="/menu/{ISO}/create".*?name="title" value="([^"]*)"', html, re.S)


def saved(session) -> list[Dish]:
    session.expire_all()
    return session.scalars(select(Dish).order_by(Dish.id)).all()


def jobs(session) -> list[IngestJob]:
    session.expire_all()
    return session.scalars(select(IngestJob)).all()


def on_menu(session):
    session.expire_all()
    row = session.get(MenuEntry, DAY)
    return row.dish_id if row and row.kind == "dish" else None


def test_create_is_offered_for_a_missing_title_alongside_partial_results(client, make_dish):
    make_dish("Chicken curry")
    html = pick(client, "Chicken")

    assert "Chicken curry" in html
    assert create_forms(html) == ["Chicken"]
    assert "Create “Chicken”" in html


def test_no_create_for_empty_input(client):
    assert create_forms(pick(client, "   ")) == []


@pytest.mark.parametrize("typed", ["chicken curry", "  CHICKEN   Curry ", "Chicken curry"])
def test_case_and_whitespace_equivalent_title_suppresses_create(client, make_dish, typed):
    make_dish("Chicken curry")

    assert create_forms(pick(client, typed)) == []


@pytest.mark.parametrize("title,typed", [
    ("Crème brûlée", "CRÈME BRÛLÉE"),
    ("Straße soup", "STRASSE SOUP"),
])
def test_unicode_exact_match_remains_selectable(client, make_dish, title, typed):
    dish = make_dish(title)
    html = pick(client, typed)
    assert create_forms(html) == []
    assert f'"dish_id": "{dish.id}"' in html
    assert title in html


def test_punctuation_difference_alone_is_not_a_duplicate(client, make_dish):
    make_dish("Mac & Cheese")

    assert create_forms(pick(client, "Mac and Cheese")) == ["Mac and Cheese"]


def test_archived_match_links_to_the_recipe_and_offers_no_create(client, make_dish):
    old = make_dish("Daal", archived=True)
    html = pick(client, "daal")

    assert create_forms(html) == []
    assert f'href="/dishes/{old.id}"' in html and "archived" in html


def test_archived_match_cannot_be_created_or_assigned_through_the_endpoint(client, session, make_dish):
    old = make_dish("Daal", archived=True)
    r = create(client, " DAAL ")

    assert r.status_code == 200
    assert [d.id for d in saved(session)] == [old.id] and jobs(session) == []
    assert on_menu(session) is None
    assert saved(session)[0].archived
    assert f'href="/dishes/{old.id}"' in r.text


def test_new_title_saves_one_dinner_with_a_tag_enrichment_job_and_selects_it(client, session):
    r = create(client, "Lentil soup")

    (dish,) = saved(session)
    assert (dish.title, dish.type, dish.source_platform, dish.archived) == ("Lentil soup", "dinner", "manual", False)
    (job,) = jobs(session)
    assert (job.dish_id, job.source_url, job.user_text, job.status) == (dish.id, None, "", "queued")
    assert on_menu(session) == dish.id
    assert r.status_code == 200 and f'id="row-{ISO}"' in r.text
    assert f'href="/dishes/{dish.id}"' in r.text and f'href="/dishes/{dish.id}/edit"' in r.text
    assert "Add ingredients" in r.text


def test_repeated_submission_assigns_the_same_recipe_without_extra_jobs(client, session):
    create(client, "Lentil soup")
    again = create(client, "  lentil SOUP ")

    (dish,) = saved(session)
    assert len(jobs(session)) == 1
    assert on_menu(session) == dish.id
    assert again.status_code == 200 and f'href="/dishes/{dish.id}"' in again.text
    assert "Add ingredients" not in again.text


def test_existing_active_match_is_only_assigned_with_conflict_warning(client, session, make_dish, serve):
    dish = make_dish("Daal")
    serve(dish, date(2026, 10, 25))
    r = create(client, "daal")

    assert [d.id for d in saved(session)] == [dish.id] and jobs(session) == []
    assert on_menu(session) == dish.id
    assert "Also on the menu on" in r.text


def test_active_match_is_chosen_ahead_of_an_archived_duplicate(client, session, make_dish):
    make_dish("Daal", archived=True)
    active = make_dish("daal")
    create(client, "DAAL")

    assert on_menu(session) == active.id and len(saved(session)) == 2


def test_empty_title_creates_nothing(client, session):
    r = create(client, "   ")

    assert r.status_code == 200 and "Type a recipe title first." in r.text
    assert saved(session) == [] and jobs(session) == [] and on_menu(session) is None


@pytest.mark.parametrize("title", ['The "best" <b>soup</b>', "Crème brûlée 🍮", "Pad Thai — 'quick' & easy", "味噌汁"])
def test_quoted_and_non_english_titles_are_created_and_escaped(client, session, title):
    html = pick(client, title)
    assert create_forms(html) == [title.replace("&", "&amp;").replace('"', "&#34;").replace("<", "&lt;")
                                  .replace(">", "&gt;").replace("'", "&#39;")]
    assert "<b>soup</b>" not in html

    r = create(client, title)
    (dish,) = saved(session)
    assert dish.title == title and on_menu(session) == dish.id
    assert "<b>soup</b>" not in r.text
    assert create_forms(pick(client, title)) == []


def test_creating_does_not_queue_nutrition_or_generation(client, session):
    create(client, "Lentil soup")

    (dish,) = saved(session)
    assert dish.nutrition is None and dish.generated_recipe is None
