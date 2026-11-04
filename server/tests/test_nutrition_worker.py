"""Ingredient-based nutrition: eligibility, the Claude call, stale-result protection and the detail-page state."""
import dataclasses
import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.dishes import nutrition
from app.models import Base, Dish, IngestJob

INGREDIENTS = ["200g oats", "500ml milk", "2 bananas"]
ANSWER = {
    "totals": {"calories_kcal": 2000, "protein_g": 80, "carbs_g": 300, "fat_g": 40,
               "fibre_g": 0, "sugar_g": 60, "salt_g": 2},
    "assumptions": [],
    "estimated_servings": None,
}


@pytest.fixture
def Session(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(nutrition, "SessionLocal", factory)
    return factory


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key="k"))


@pytest.fixture
def claude(monkeypatch):
    """Stands in for call_json: records calls, can run code mid-call or raise."""
    class Fake:
        calls = []
        answer = ANSWER
        during = None
        error = None

        def __call__(self, system, content, schema, *, timeout, max_tokens=8192):
            self.calls.append({"system": system, "text": content[0]["text"], "schema": schema})
            if self.during:
                self.during()
            if self.error:
                raise self.error
            return self.answer

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(nutrition, "call_json", fake)
    return fake


def make(Session, title="Porridge", ingredients=INGREDIENTS, servings=4, **kw):
    with Session() as s:
        dish = Dish(title=title, ingredients=list(ingredients), servings=servings, source_platform="manual", **kw)
        s.add(dish)
        s.commit()
        return dish.id


def load(Session, dish_id) -> Dish:
    with Session() as s:
        return s.get(Dish, dish_id)


def edit(Session, dish_id, **fields):
    with Session() as s:
        dish = s.get(Dish, dish_id)
        for k, v in fields.items():
            setattr(dish, k, v)
        s.commit()


def next_id(Session):
    with Session() as s:
        return nutrition.next_dish(s)


def shown(dish) -> dict:
    ctx = nutrition.detail_context(dish)
    return {r["key"]: r["text"] for r in ctx["rows"]}


# --- The estimate ---------------------------------------------------------------

def test_valid_answer_gives_seven_per_serving_values_including_zero(Session, claude):
    dish_id = make(Session, servings=4)

    nutrition.run(dish_id)

    dish = load(Session, dish_id)
    ctx = nutrition.detail_context(dish)
    assert ctx["state"] == "ready" and not ctx["polling"]
    assert shown(dish) == {"calories": "500", "protein": "20", "carbs": "75", "fat": "10",
                           "fibre": "0", "sugar": "15", "salt": "0.5"}
    assert (ctx["servings"], ctx["servings_estimated"], ctx["rough"], ctx["assumptions"]) == (4, False, False, [])
    assert dish.nutrition["totals"]["calories"] == 2000  # whole-recipe totals are what is stored


def test_only_ingredients_are_sent_never_the_title_or_description(Session, claude):
    dish_id = make(Session, title="Secret Famous Porridge", description="Grandma's best", servings=4)

    nutrition.run(dish_id)

    (call,) = claude.calls
    for item in INGREDIENTS:
        assert item in call["text"]
    assert "Secret" not in call["text"] and "Grandma" not in call["text"]
    assert "4" in call["text"]
    assert "Secret" not in call["system"]


def test_missing_servings_are_estimated_and_disclosed(Session, claude):
    claude.answer = dict(ANSWER, estimated_servings=2)
    dish_id = make(Session, servings=None)

    nutrition.run(dish_id)

    dish = load(Session, dish_id)
    ctx = nutrition.detail_context(dish)
    assert dish.servings is None  # the user's blank is never overwritten
    assert (ctx["servings"], ctx["servings_estimated"], ctx["rough"]) == (2, True, True)
    assert shown(dish)["calories"] == "1000"
    assert "not given" in claude.calls[0]["text"]


def test_assumed_quantities_are_shown_and_make_it_rough(Session, claude):
    claude.answer = dict(ANSWER, assumptions=["milk: assumed 500ml", "salt: assumed a pinch"])
    dish_id = make(Session, ingredients=["oats", "milk", "salt"])

    nutrition.run(dish_id)

    ctx = nutrition.detail_context(load(Session, dish_id))
    assert ctx["rough"] is True
    assert ctx["assumptions"] == ["milk: assumed 500ml", "salt: assumed a pinch"]


def test_a_given_serving_count_ignores_a_model_estimate(Session, claude):
    claude.answer = dict(ANSWER, estimated_servings=9)
    dish_id = make(Session, servings=4)

    nutrition.run(dish_id)

    ctx = nutrition.detail_context(load(Session, dish_id))
    assert (ctx["servings"], ctx["servings_estimated"]) == (4, False)


def test_serving_edit_recalculates_without_another_call(Session, claude):
    dish_id = make(Session, servings=4)
    nutrition.run(dish_id)

    edit(Session, dish_id, servings=2)

    dish = load(Session, dish_id)
    assert shown(dish)["calories"] == "1000"
    assert next_id(Session) is None
    nutrition.run(dish_id)
    assert len(claude.calls) == 1


def test_clearing_servings_asks_again_for_an_estimate(Session, claude):
    dish_id = make(Session, servings=4)
    nutrition.run(dish_id)

    edit(Session, dish_id, servings=None)

    assert next_id(Session) == dish_id
    assert nutrition.detail_context(load(Session, dish_id))["state"] == "pending"


# --- Eligibility ----------------------------------------------------------------

def test_no_ingredients_means_no_call(Session, claude):
    dish_id = make(Session, ingredients=[], title="Pancakes")
    blank = make(Session, ingredients=["", "  "])

    assert next_id(Session) is None
    nutrition.run(dish_id)
    nutrition.run(blank)

    assert claude.calls == []
    ctx = nutrition.detail_context(load(Session, dish_id))
    assert ctx["state"] == "missing_ingredients" and ctx["rows"] == [] and not ctx["polling"]
    assert "Add ingredients" in ctx["message"]


def test_new_and_existing_eligible_dishes_are_processed_oldest_first(Session, claude):
    first = make(Session, title="A")
    second = make(Session, title="B", ingredients=["x"])

    assert next_id(Session) == first
    nutrition.run(first)
    assert next_id(Session) == second
    nutrition.run(second)
    assert next_id(Session) is None


def test_archived_dishes_are_skipped_but_keep_their_estimate(Session, claude):
    archived = make(Session, archived=True)
    kept = make(Session, title="Kept")
    nutrition.run(kept)
    edit(Session, kept, archived=True)

    assert next_id(Session) is None
    nutrition.run(archived)

    assert len(claude.calls) == 1  # only the call for "Kept"
    assert nutrition.detail_context(load(Session, kept))["state"] == "ready"
    ctx = nutrition.detail_context(load(Session, archived))
    assert ctx["state"] == "archived" and not ctx["polling"]


def test_restoring_makes_an_archived_dish_eligible(Session, claude):
    dish_id = make(Session, archived=True)
    assert next_id(Session) is None

    edit(Session, dish_id, archived=False)

    assert next_id(Session) == dish_id


@pytest.mark.parametrize("status", ["queued", "fetching", "transcribing", "extracting"])
def test_dishes_with_active_enrichment_wait(Session, claude, status):
    dish_id = make(Session)
    with Session() as s:
        s.add(IngestJob(dish_id=dish_id, status=status))
        s.commit()

    assert next_id(Session) is None
    nutrition.run(dish_id)

    assert claude.calls == []


def test_finished_enrichment_does_not_block(Session, claude):
    dish_id = make(Session)
    with Session() as s:
        s.add_all([IngestJob(dish_id=dish_id, status="ready"), IngestJob(dish_id=dish_id, status="failed")])
        s.commit()

    assert next_id(Session) == dish_id


def test_without_credentials_nothing_is_called_and_the_page_says_why(Session, claude, monkeypatch):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key=None))
    dish_id = make(Session)

    assert next_id(Session) is None
    nutrition.run(dish_id)

    assert claude.calls == []
    assert load(Session, dish_id).nutrition is None
    ctx = nutrition.detail_context(load(Session, dish_id))
    assert ctx["state"] == "unavailable" and not ctx["polling"] and "ANTHROPIC_API_KEY" in ctx["message"]


def test_estimate_nutrition_itself_refuses_without_a_key(monkeypatch, claude):
    monkeypatch.setattr(nutrition, "config", dataclasses.replace(nutrition.config, anthropic_api_key=None))

    with pytest.raises(RuntimeError):
        nutrition.estimate_nutrition(INGREDIENTS, 4)
    assert claude.calls == []


# --- Invalidation -----------------------------------------------------------------

def test_ingredient_edits_invalidate_but_other_edits_do_not(Session, claude):
    dish_id = make(Session)
    nutrition.run(dish_id)

    edit(Session, dish_id, title="New name", description="d", notes="n", steps=["Stir."], type="breakfast")
    assert next_id(Session) is None
    assert nutrition.detail_context(load(Session, dish_id))["state"] == "ready"

    edit(Session, dish_id, ingredients=INGREDIENTS + ["honey"])
    assert next_id(Session) == dish_id


def test_quantity_and_order_changes_count_as_ingredient_changes(Session, claude):
    dish_id = make(Session)
    nutrition.run(dish_id)

    edit(Session, dish_id, ingredients=["250g oats", "500ml milk", "2 bananas"])
    assert next_id(Session) == dish_id
    nutrition.run(dish_id)
    edit(Session, dish_id, ingredients=["500ml milk", "250g oats", "2 bananas"])
    assert next_id(Session) == dish_id


def test_stale_values_are_marked_outdated_and_keep_polling_state(Session, claude):
    dish_id = make(Session)
    nutrition.run(dish_id)

    edit(Session, dish_id, ingredients=["1 apple"])

    ctx = nutrition.detail_context(load(Session, dish_id))
    assert ctx["state"] == "outdated" and ctx["outdated"] and ctx["polling"]
    assert ctx["rows"]  # the old numbers stay visible, flagged as old


def test_a_fresh_dish_is_pending_and_polls(Session, claude):
    ctx = nutrition.detail_context(load(Session, make(Session)))

    assert ctx["state"] == "pending" and ctx["polling"] and ctx["rows"] == []


# --- Failures ---------------------------------------------------------------------

def test_failure_is_visible_and_not_retried_automatically(Session, claude):
    claude.error = RuntimeError("boom")
    dish_id = make(Session)

    nutrition.run(dish_id)

    dish = load(Session, dish_id)
    assert dish.nutrition["status"] == "failed" and "totals" not in dish.nutrition
    ctx = nutrition.detail_context(dish)
    assert ctx["state"] == "error" and ctx["rows"] == [] and not ctx["polling"] and ctx["error"]
    assert next_id(Session) is None
    nutrition.run(dish_id)
    assert len(claude.calls) == 1


def test_changed_ingredients_make_a_failed_dish_eligible_again(Session, claude):
    claude.error = RuntimeError("boom")
    dish_id = make(Session)
    nutrition.run(dish_id)

    edit(Session, dish_id, ingredients=["1 egg"])

    assert next_id(Session) == dish_id
    claude.error = None
    nutrition.run(dish_id)
    assert nutrition.detail_context(load(Session, dish_id))["state"] == "ready"


def test_explicit_retry_clears_the_marker_and_permits_another_attempt(Session, claude):
    claude.error = RuntimeError("boom")
    dish_id = make(Session)
    nutrition.run(dish_id)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.db import get_session

    app = FastAPI()
    app.include_router(nutrition.router)
    with Session() as s:
        app.dependency_overrides[get_session] = lambda: s
        resp = TestClient(app).post(f"/dishes/{dish_id}/nutrition", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/dishes/{dish_id}"

    assert next_id(Session) == dish_id
    claude.error = None
    nutrition.run(dish_id)
    assert nutrition.detail_context(load(Session, dish_id))["state"] == "ready"


BAD_ANSWERS = {
    "negative": dict(ANSWER, totals=dict(ANSWER["totals"], fat_g=-1)),
    "nan": dict(ANSWER, totals=dict(ANSWER["totals"], fat_g=math.nan)),
    "inf": dict(ANSWER, totals=dict(ANSWER["totals"], fat_g=math.inf)),
    "string": dict(ANSWER, totals=dict(ANSWER["totals"], fat_g="10")),
    "bool": dict(ANSWER, totals=dict(ANSWER["totals"], fat_g=True)),
    "missing nutrient": dict(ANSWER, totals={k: v for k, v in ANSWER["totals"].items() if k != "salt_g"}),
    "no totals": {"assumptions": [], "estimated_servings": None},
    "no assumptions list": dict(ANSWER, assumptions=None),
    "servings needed but absent": dict(ANSWER, estimated_servings=None),
    "zero servings": dict(ANSWER, estimated_servings=0),
    "negative servings": dict(ANSWER, estimated_servings=-3),
    "fractional servings": dict(ANSWER, estimated_servings=2.5),
}


@pytest.mark.parametrize("name", BAD_ANSWERS)
def test_malformed_answers_fail_instead_of_being_clamped(Session, claude, name):
    claude.answer = BAD_ANSWERS[name]
    dish_id = make(Session, servings=None)

    nutrition.run(dish_id)

    dish = load(Session, dish_id)
    assert dish.nutrition["status"] == "failed" and "totals" not in dish.nutrition
    assert nutrition.detail_context(dish)["rows"] == []
    assert next_id(Session) is None


# --- Races ------------------------------------------------------------------------

def test_ingredient_edit_during_the_call_discards_the_result(Session, claude):
    dish_id = make(Session)
    claude.during = lambda: edit(Session, dish_id, ingredients=["1 egg"])

    nutrition.run(dish_id)

    assert load(Session, dish_id).nutrition is None
    assert next_id(Session) == dish_id  # the new ingredients are still to be estimated


def test_ingredient_edit_during_a_failing_call_discards_the_failure_too(Session, claude):
    dish_id = make(Session)
    claude.error = RuntimeError("boom")
    claude.during = lambda: edit(Session, dish_id, ingredients=["1 egg"])

    nutrition.run(dish_id)

    assert load(Session, dish_id).nutrition is None


def test_archiving_during_the_call_discards_the_result(Session, claude):
    dish_id = make(Session)
    claude.during = lambda: edit(Session, dish_id, archived=True)

    nutrition.run(dish_id)

    assert load(Session, dish_id).nutrition is None


def test_title_edit_during_the_call_does_not_discard(Session, claude):
    dish_id = make(Session)
    claude.during = lambda: edit(Session, dish_id, title="Renamed")

    nutrition.run(dish_id)

    assert nutrition.detail_context(load(Session, dish_id))["state"] == "ready"


def test_deleted_dish_during_the_call_is_ignored(Session, claude):
    dish_id = make(Session)

    def delete():
        with Session() as s:
            s.delete(s.get(Dish, dish_id))
            s.commit()

    claude.during = delete

    nutrition.run(dish_id)  # must not raise


def test_ingredients_gaining_active_enrichment_before_the_call_wait(Session, claude):
    dish_id = make(Session)
    with Session() as s:
        s.add(IngestJob(dish_id=dish_id, status="extracting"))
        s.commit()

    nutrition.run(dish_id)

    assert claude.calls == []


# --- No write lock during the call ----------------------------------------------

def test_no_transaction_is_held_while_claude_answers(monkeypatch, tmp_path, claude):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(nutrition, "SessionLocal", Session)
    dish_id = make(Session)
    other = sessionmaker(bind=create_engine(url, connect_args={"timeout": 1}), expire_on_commit=False)

    def user_saves_a_note():
        with other() as s2:  # would raise "database is locked" if run() held a write transaction
            s2.get(Dish, dish_id).notes = "typed meanwhile"
            s2.commit()

    claude.during = user_saves_a_note

    nutrition.run(dish_id)

    dish = load(Session, dish_id)
    assert dish.notes == "typed meanwhile" and dish.nutrition["status"] == "ready"


def test_storing_a_result_is_not_a_recipe_edit(Session, claude):
    dish_id = make(Session)
    before = load(Session, dish_id).updated_at

    nutrition.run(dish_id)

    assert load(Session, dish_id).nutrition["status"] == "ready"
    assert load(Session, dish_id).updated_at == before  # the shopping list keys on updated_at
