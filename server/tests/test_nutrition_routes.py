"""The nutrition fragment and its two routes: no Claude call or waiting on GET, polling only while work is pending."""
import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_session
from app.dishes import nutrition
from app.models import Dish

TOTALS = {"calories": 2000, "protein": 80, "carbs": 300, "fat": 40, "fibre": 0, "sugar": 60, "salt": 2}
INGREDIENTS = ["200g oats", "2 bananas"]


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key="k"))


@pytest.fixture
def no_calls(monkeypatch):
    def refuse(*a, **kw):
        pytest.fail("a route must never call Claude")

    monkeypatch.setattr(nutrition, "call_json", refuse)


@pytest.fixture
def client(session, no_calls):
    app = FastAPI()
    app.include_router(nutrition.router)

    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def ready(**kw):
    stored = {"status": "ready", "fingerprint": nutrition.fingerprint(INGREDIENTS), "totals": TOTALS,
              "estimated_servings": None, "assumptions": [], "rough": False, "estimated_at": "2026-11-04T10:00:00"}
    return {**stored, **kw}


def fragment(client, dish):
    resp = client.get(f"/dishes/{dish.id}/nutrition/status")
    assert resp.status_code == 200
    return resp.text


def test_ready_shows_seven_values_with_zero_and_stops_polling(client, make_dish):
    dish = make_dish(ingredients=INGREDIENTS, servings=4, nutrition=ready())

    html = fragment(client, dish)

    for label, text in [("Calories", "500 kcal"), ("Protein", "20 g"), ("Carbs", "75 g"), ("Fat", "10 g"),
                        ("Fibre", "0 g"), ("Sugar", "15 g"), ("Salt", "0.5 g")]:
        assert f"<th scope=\"row\">{label}</th><td>{text}</td>" in html
    assert "hx-get" not in html and "Based on 4 servings" in html


def test_pending_polls_the_status_route(client, make_dish):
    dish = make_dish(ingredients=INGREDIENTS, servings=4)

    html = fragment(client, dish)

    assert f'hx-get="/dishes/{dish.id}/nutrition/status"' in html and "Estimating nutrition" in html
    assert "<table" not in html


def test_outdated_values_are_flagged_and_still_polling(client, make_dish):
    dish = make_dish(ingredients=["1 apple"], servings=4, nutrition=ready())

    html = fragment(client, dish)

    assert "hx-get" in html and "old ingredients" in html and "500 kcal" in html


def test_estimated_servings_and_assumptions_are_disclosed(client, make_dish):
    dish = make_dish(ingredients=INGREDIENTS, nutrition=ready(
        estimated_servings=2, rough=True, assumptions=["oats: assumed <b>100g</b>"]))

    html = fragment(client, dish)

    assert "Based on 2 servings (estimated" in html and "Rough estimate" in html
    assert "oats: assumed &lt;b&gt;100g&lt;/b&gt;" in html and "<b>100g</b>" not in html


def test_entered_servings_replace_the_estimated_basis(client, make_dish, session):
    dish = make_dish(ingredients=INGREDIENTS, nutrition=ready(estimated_servings=2, rough=True))
    assert "1000 kcal" in fragment(client, dish)
    dish.servings = 4
    session.commit()
    html = fragment(client, dish)
    assert "500 kcal" in html
    assert "Rough estimate" not in html
    assert "(estimated" not in html


@pytest.mark.parametrize("stored, text", [
    ({"status": "failed", "fingerprint": nutrition.fingerprint(INGREDIENTS), "error": "Couldn't estimate nutrition. Try later."},
     "Couldn&#39;t estimate nutrition. Try later."),
])
def test_failure_shows_the_reason_with_retry_and_no_polling(client, make_dish, stored, text):
    dish = make_dish(ingredients=INGREDIENTS, servings=4, nutrition=stored)

    html = fragment(client, dish)

    assert text in html and f'action="/dishes/{dish.id}/nutrition"' in html
    assert "hx-get" not in html and "<table" not in html


def test_missing_ingredients_shows_the_prompt_and_no_numbers(client, make_dish):
    dish = make_dish(ingredients=[])

    html = fragment(client, dish)

    assert "Add ingredients to estimate nutrition" in html
    assert "hx-get" not in html and "<table" not in html


def test_missing_key_shows_configuration_and_stops_polling(client, make_dish, monkeypatch):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key=None))
    dish = make_dish(ingredients=INGREDIENTS, servings=4)

    html = fragment(client, dish)

    assert "ANTHROPIC_API_KEY" in html and "hx-get" not in html


def test_archived_stops_polling(client, make_dish):
    dish = make_dish(ingredients=INGREDIENTS, servings=4, archived=True)

    html = fragment(client, dish)

    assert "hx-get" not in html and "archived" in html


def test_completion_swap_has_no_poll_trigger(client, make_dish, session):
    dish = make_dish(ingredients=INGREDIENTS, servings=4)
    assert "hx-trigger" in fragment(client, dish)

    dish.nutrition = ready()
    session.commit()

    assert "hx-trigger" not in fragment(client, dish)


def test_status_for_an_unknown_dish_is_404(client):
    assert client.get("/dishes/999/nutrition/status").status_code == 404
    assert client.post("/dishes/999/nutrition").status_code == 404


def test_retry_post_clears_only_a_failed_marker(client, make_dish, session):
    failed = make_dish(ingredients=INGREDIENTS, nutrition={
        "status": "failed", "fingerprint": nutrition.fingerprint(INGREDIENTS), "error": "x"})
    good = make_dish(ingredients=INGREDIENTS, nutrition=ready())

    for dish in (failed, good):
        resp = client.post(f"/dishes/{dish.id}/nutrition", follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, f"/dishes/{dish.id}")

    session.refresh(failed)
    session.refresh(good)
    assert failed.nutrition is None
    assert good.nutrition["status"] == "ready"
    assert nutrition.next_dish(session) == failed.id


def test_retry_without_a_key_still_clears_and_shows_configuration(client, make_dish, session, monkeypatch):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key=None))
    dish = make_dish(ingredients=INGREDIENTS, servings=4, nutrition={
        "status": "failed", "fingerprint": nutrition.fingerprint(INGREDIENTS), "error": "x"})

    client.post(f"/dishes/{dish.id}/nutrition")

    assert nutrition.next_dish(session) is None
    assert "ANTHROPIC_API_KEY" in fragment(client, dish)
