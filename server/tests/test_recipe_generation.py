"""Explicit recipe generation: queue once, review an editable AI draft, approve atomically or discard."""
import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.claude import NoResult
from app.db import get_session
from app.dishes import generation
from app.main import app
from app.models import Dish
from app.web import templates

DRAFT = {"ingredients": ["200 g flour", "2 eggs"], "steps": ["Mix.", "Fry."], "servings": 4}


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def keyed(monkeypatch, session):
    """An API key is configured, the worker's sessions see the test database, and Claude is a stub."""
    monkeypatch.setattr(generation, "config", dataclasses.replace(generation.config, anthropic_api_key="k"))
    monkeypatch.setattr(generation, "SessionLocal", sessionmaker(bind=session.get_bind(), expire_on_commit=False))
    stub = Stub()
    monkeypatch.setattr(generation, "call_json", stub)
    return stub


class Stub:
    def __init__(self):
        self.calls = []
        self.answer = dict(DRAFT)
        self.error = None
        self.during = None

    def __call__(self, system, content, schema, *, timeout, max_tokens=8192):
        self.calls.append(content[0]["text"])
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return self.answer


def fresh(session, dish) -> Dish:
    session.expire_all()
    return session.get(Dish, dish.id)


def saved_fields(dish) -> dict:
    return {c.name: getattr(dish, c.name) for c in Dish.__table__.columns
            if c.name not in ("generated_recipe", "updated_at")}


def current_attempt(session, dish) -> str:
    return (fresh(session, dish).generated_recipe or {}).get("attempt", "")


def act(client, session, dish, action="", attempt=None, **data):
    """A click on what the page shows: its hidden attempt is the current one unless a stale tab passes its own."""
    attempt = current_attempt(session, dish) if attempt is None else attempt
    path = f"/dishes/{dish.id}/generate" + (f"/{action}" if action else "")
    return client.post(path, data={"attempt": attempt, **data}, follow_redirects=False)


def ready(client, session, make_dish, keyed, **kw):
    dish = make_dish("Pancakes", description="Fluffy", notes="n", **kw)
    act(client, session, dish)
    generation.run(dish.id)
    return dish


def test_only_the_explicit_action_queues(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    assert client.get(f"/dishes/{dish.id}/generate").status_code == 200
    assert client.get(f"/dishes/{dish.id}").status_code == 200
    assert fresh(session, dish).generated_recipe in (None, {})
    assert generation.next_dish(session) is None
    r = act(client, session, dish)
    assert r.status_code == 303 and r.headers["location"] == f"/dishes/{dish.id}/generate"
    assert generation.next_dish(session) == dish.id
    assert keyed.calls == []


def test_repeated_clicks_queue_one_job(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    act(client, session, dish)
    first = fresh(session, dish).generated_recipe
    act(client, session, dish)
    act(client, session, dish)
    assert fresh(session, dish).generated_recipe == first
    generation.run(dish.id)
    assert len(keyed.calls) == 1
    act(client, session, dish)  # a ready draft isn't paid for again
    generation.run(dish.id)
    assert len(keyed.calls) == 1
    assert fresh(session, dish).generated_recipe["status"] == "ready"


def test_recipes_with_ingredients_are_not_generated(client, session, make_dish, keyed):
    dish = make_dish("Pancakes", ingredients=["1 egg"])
    act(client, session, dish)
    assert generation.next_dish(session) is None
    assert not generation.detail_context(dish)["visible"]


def test_run_sends_title_and_description_and_keeps_saved_recipe(client, session, make_dish, keyed):
    dish = make_dish("Pancakes", description="Fluffy", tags=["vege"], notes="Mum's", servings=None)
    before = saved_fields(dish)
    act(client, session, dish)
    generation.run(dish.id)
    assert "Pancakes" in keyed.calls[0] and "Fluffy" in keyed.calls[0] and "Mum's" not in keyed.calls[0]
    after = fresh(session, dish)
    assert after.generated_recipe["status"] == "ready"
    assert after.generated_recipe["draft"] == DRAFT
    assert saved_fields(after) == before
    assert generation.next_dish(session) is None


def test_ready_draft_survives_navigation_and_is_editable(client, session, make_dish, keyed):
    dish = ready(client, session, make_dish, keyed)
    for _ in range(2):
        html = client.get(f"/dishes/{dish.id}/generate").text
        assert "AI-generated" in html and "200 g flour" in html and "Approve" in html and "Discard" in html
    assert fresh(session, dish).ingredients == []


def test_active_review_polls_and_ready_does_not(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    act(client, session, dish)
    assert "hx-get" in client.get(f"/dishes/{dish.id}/generate").text
    assert 'hx-get="/dishes/%d/generate/status"' % dish.id in client.get(f"/dishes/{dish.id}/generate/status").text
    generation.run(dish.id)
    assert client.get(f"/dishes/{dish.id}/generate/status").headers.get("HX-Refresh") == "true"
    html = client.get(f"/dishes/{dish.id}/generate").text
    assert "hx-get" not in html and "<textarea" in html


def test_approval_saves_reviewed_fields_atomically_and_keeps_the_rest(client, session, make_dish, keyed):
    dish = make_dish("Pancakes", description="Fluffy", tags=["vege"], notes="Mum's", total_minutes=20,
                     source_url="https://example.com/p", image_path="a.jpg")
    before = saved_fields(dish)
    act(client, session, dish)
    generation.run(dish.id)
    r = act(client, session, dish, "approve", **{"ingredients_text": "300 g flour\n1 egg\n", "steps_text": "Whisk\nCook", "servings": "6 people"})
    assert r.status_code == 303
    after = fresh(session, dish)
    assert (after.ingredients, after.steps, after.servings) == (["300 g flour", "1 egg"], ["Whisk", "Cook"], 6)
    assert after.generated_recipe in (None, {})
    assert saved_fields(after) == {**before, "ingredients": ["300 g flour", "1 egg"],
                                   "steps": ["Whisk", "Cook"], "servings": 6}
    assert after.tag_names == ["vege"]


def test_invalid_approval_changes_nothing_and_keeps_edits(client, session, make_dish, keyed):
    dish = ready(client, session, make_dish, keyed)
    r = act(client, session, dish, "approve", **{"ingredients_text": " \n", "steps_text": "Mine", "servings": "x"})
    assert r.status_code == 422
    assert "Add at least one ingredient" in r.text and "Servings must be" in r.text and "Mine" in r.text
    after = fresh(session, dish)
    assert after.ingredients == [] and after.generated_recipe["status"] == "ready"


def test_discard_changes_no_saved_field(client, session, make_dish, keyed):
    dish = ready(client, session, make_dish, keyed)
    before = saved_fields(fresh(session, dish))
    r = act(client, session, dish, "discard")
    assert r.status_code == 303
    after = fresh(session, dish)
    assert after.generated_recipe in (None, {})
    assert saved_fields(after) == before
    assert generation.next_dish(session) is None


def test_failure_is_visible_and_retryable_by_hand_only(client, session, make_dish, keyed):
    keyed.error = NoResult("bad")
    dish = make_dish("Pancakes")
    act(client, session, dish)
    generation.run(dish.id)
    gen = fresh(session, dish).generated_recipe
    assert gen["status"] == "failed" and gen["error"].startswith("Couldn't generate a recipe")
    assert generation.next_dish(session) is None
    assert "Couldn&#39;t generate a recipe" in client.get(f"/dishes/{dish.id}/generate").text
    assert "Try again" in client.get(f"/dishes/{dish.id}/generate").text
    generation.run(dish.id)
    assert len(keyed.calls) == 1  # nothing retries by itself
    keyed.error = None
    act(client, session, dish)
    assert generation.next_dish(session) == dish.id
    generation.run(dish.id)
    assert fresh(session, dish).generated_recipe["status"] == "ready"
    assert len(keyed.calls) == 2


@pytest.mark.parametrize("answer", [
    {"ingredients": [], "steps": ["x"], "servings": 2},
    {"ingredients": ["1 egg"], "steps": [], "servings": 0},
    {"ingredients": ["1 egg"], "steps": [], "servings": 2.5},
    {"ingredients": "egg", "steps": [], "servings": 2},
])
def test_unusable_drafts_fail_without_being_saved(client, session, make_dish, keyed, answer):
    keyed.answer = answer
    dish = make_dish("Pancakes")
    act(client, session, dish)
    generation.run(dish.id)
    after = fresh(session, dish)
    assert after.generated_recipe["status"] == "failed" and not after.generated_recipe.get("draft")
    assert after.ingredients == []


def test_missing_credentials_explain_and_make_no_call(client, session, make_dish, monkeypatch):
    monkeypatch.setattr(generation, "config", dataclasses.replace(generation.config, anthropic_api_key=""))
    called = []
    monkeypatch.setattr(generation, "call_json", lambda *a, **k: called.append(1))
    dish = make_dish("Pancakes")
    assert "disabled" in client.get(f"/dishes/{dish.id}/generate").text
    act(client, session, dish)
    gen = fresh(session, dish).generated_recipe
    assert gen["status"] == "failed" and "ANTHROPIC_API_KEY" in gen["error"]
    assert generation.next_dish(session) is None and not called
    assert "ANTHROPIC_API_KEY" in client.get(f"/dishes/{dish.id}/generate").text
    assert fresh(session, dish).ingredients == []


def test_key_removed_after_queueing_fails_the_job_without_a_call(client, session, make_dish, keyed, monkeypatch):
    dish = make_dish("Pancakes")
    act(client, session, dish)
    monkeypatch.setattr(generation, "config", dataclasses.replace(generation.config, anthropic_api_key=""))
    generation.run(dish.id)
    assert keyed.calls == []
    assert fresh(session, dish).generated_recipe["status"] == "failed"


def test_restart_recovers_running_generation_once(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    act(client, session, dish)
    seen = {}
    keyed.during = lambda: seen.setdefault("status", fresh(session, dish).generated_recipe["status"])
    generation.run(dish.id)
    assert seen["status"] == "running"
    # Simulate the process dying mid-call.
    d = fresh(session, dish)
    d.generated_recipe = {**d.generated_recipe, "status": "running", "draft": None}
    session.commit()
    assert generation.next_dish(session) is None
    generation.recover(session)
    assert fresh(session, dish).generated_recipe["status"] == "queued"
    assert generation.next_dish(session) == dish.id
    generation.run(dish.id)
    assert fresh(session, dish).generated_recipe["status"] == "ready"


def test_recover_leaves_other_states_alone(client, session, make_dish, keyed):
    done = ready(client, session, make_dish, keyed)
    failed = make_dish("Soup")
    failed.generated_recipe = {"status": "failed", "attempt": 1, "error": "x"}
    session.commit()
    generation.recover(session)
    assert fresh(session, done).generated_recipe["status"] == "ready"
    assert fresh(session, failed).generated_recipe["status"] == "failed"


def test_next_dish_is_oldest_queued_first(client, session, make_dish, keyed):
    a, b = make_dish("A"), make_dish("B")
    act(client, session, b)
    act(client, session, a)
    assert generation.next_dish(session) == b.id


def test_edit_during_generation_blocks_the_commit(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    act(client, session, dish)

    def edit():
        d = session.get(Dish, dish.id)
        d.ingredients = ["my own egg"]
        session.commit()

    keyed.during = edit
    generation.run(dish.id)
    after = fresh(session, dish)
    assert after.ingredients == ["my own egg"]
    assert after.generated_recipe["status"] == "failed" and after.generated_recipe["draft"] is None
    assert "changed" in after.generated_recipe["error"]


def test_discard_while_running_drops_the_late_answer(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    act(client, session, dish)
    keyed.during = lambda: act(client, session, dish, "discard")
    generation.run(dish.id)
    assert fresh(session, dish).generated_recipe in (None, {})


def test_stale_approval_is_blocked_and_overwrites_nothing(client, session, make_dish, keyed):
    dish = ready(client, session, make_dish, keyed)
    d = fresh(session, dish)
    d.steps = ["Their own step"]
    d.servings = 2
    session.commit()
    html = client.get(f"/dishes/{dish.id}/generate").text
    assert "changed after this draft" in html
    r = act(client, session, dish, "approve", **{"ingredients_text": "x", "steps_text": "y", "servings": "9"})
    assert r.status_code == 409 and "changed after this draft" in r.text
    assert "200 g flour" not in r.text and ">x<" in r.text  # the edits in the form are kept
    after = fresh(session, dish)
    assert (after.ingredients, after.steps, after.servings) == ([], ["Their own step"], 2)
    assert after.generated_recipe["status"] == "ready"
    act(client, session, dish, "discard")
    assert fresh(session, dish).generated_recipe in (None, {})


def test_approving_without_a_ready_draft_does_nothing(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    r = act(client, session, dish, "approve", **{"ingredients_text": "x"})
    assert r.status_code == 422
    assert fresh(session, dish).ingredients == []
    act(client, session, dish)  # queued, not ready
    r = act(client, session, dish, "approve", **{"ingredients_text": "x"})
    assert r.status_code == 422
    assert fresh(session, dish).ingredients == []


def test_detail_fragment_states(client, session, make_dish, keyed):
    def fragment(dish):
        d = fresh(session, dish)
        return templates.get_template("dishes/_generation.html").render(
            dish=d, generation=generation.detail_context(d))

    dish = make_dish("Pancakes")
    assert f'action="/dishes/{dish.id}/generate"' in fragment(dish) and "Generate recipe" in fragment(dish)
    act(client, session, dish)
    assert "queued" in fragment(dish) and "hx-trigger" in fragment(dish)
    generation.run(dish.id)
    html = fragment(dish)
    assert "Review draft" in html and "hx-trigger" not in html
    act(client, session, dish, "approve", **{"ingredients_text": "1 egg"})
    assert fragment(dish).strip() == ""


def test_unknown_recipe_is_404(client):
    assert client.get("/dishes/999/generate").status_code == 404
    assert client.post("/dishes/999/generate").status_code == 404
    assert client.post("/dishes/999/generate/discard").status_code == 404


def test_stale_tab_cannot_approve_or_discard_a_replacement_draft(client, session, make_dish, keyed):
    dish = ready(client, session, make_dish, keyed)
    old = current_attempt(session, dish)
    act(client, session, dish, "discard")
    act(client, session, dish)  # a new draft B
    generation.run(dish.id)
    assert current_attempt(session, dish) != old
    r = act(client, session, dish, "approve", attempt=old, ingredients_text="stale tab", steps_text="", servings="2")
    assert r.status_code == 409 and "out of date" in r.text and "stale tab" not in r.text
    assert fresh(session, dish).ingredients == []
    act(client, session, dish, "discard", attempt=old)
    assert fresh(session, dish).generated_recipe["status"] == "ready"
    r = act(client, session, dish, "approve", ingredients_text="fine", steps_text="", servings="")
    assert r.status_code == 303 and fresh(session, dish).ingredients == ["fine"]


def test_requests_without_the_attempt_token_do_nothing(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    client.post(f"/dishes/{dish.id}/generate")
    assert fresh(session, dish).generated_recipe in (None, {})
    act(client, session, dish)
    client.post(f"/dishes/{dish.id}/generate/discard")
    assert fresh(session, dish).generated_recipe["status"] == "queued"


def test_stale_tab_retry_does_not_requeue(client, session, make_dish, keyed):
    keyed.error = NoResult("bad")
    dish = make_dish("Pancakes")
    act(client, session, dish)
    generation.run(dish.id)
    act(client, session, dish, attempt="an-older-token")
    assert fresh(session, dish).generated_recipe["status"] == "failed"


def test_draft_state_changes_never_move_updated_at_but_approval_does(client, session, make_dish, keyed):
    dish = make_dish("Pancakes")
    stamp = fresh(session, dish).updated_at
    act(client, session, dish)
    assert fresh(session, dish).updated_at == stamp
    generation.run(dish.id)
    assert fresh(session, dish).updated_at == stamp
    generation.recover(session)
    act(client, session, dish, "discard")
    assert fresh(session, dish).updated_at == stamp
    act(client, session, dish)
    generation.run(dish.id)
    act(client, session, dish, "approve", ingredients_text="1 egg", steps_text="", servings="")
    assert fresh(session, dish).updated_at > stamp
