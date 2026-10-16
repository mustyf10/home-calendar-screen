import dataclasses
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock, settings_store
from app.db import get_session
from app.main import app
from app.menu.entries import set_takeaway
from app.models import ShoppingList, utcnow
from app.shopping import service
from app.shopping.service import (
    ShoppingError,
    build_list,
    diff_lists,
    fingerprint,
    menu_fingerprint,
    plain_text,
    week_dinners,
)
from app.web import templates

SAT = date(2026, 10, 17)


def test_week_dinners_skip_takeaway_and_empty_nights(session, make_dish, serve):
    a = make_dish("A", ingredients=["rice"])
    b = make_dish("B", type="side", ingredients=["peas"])
    serve(a, SAT + timedelta(days=1))
    serve(b, SAT + timedelta(days=3))
    serve(a, SAT + timedelta(days=7))  # next week
    set_takeaway(session, SAT + timedelta(days=4))
    assert [(d, x.title) for d, x in week_dinners(session, SAT)] == [
        (SAT + timedelta(days=1), "A"),
        (SAT + timedelta(days=3), "B"),
    ]


def test_fingerprint_ignores_takeaway_but_tracks_menu_dish_and_pantry(session, make_dish, serve):
    a = make_dish("A", ingredients=["rice"])
    b = make_dish("B", ingredients=["fish"])
    serve(a, SAT + timedelta(days=1))
    fp = fingerprint(session, SAT)

    set_takeaway(session, SAT + timedelta(days=2))
    assert fingerprint(session, SAT) == fp

    serve(b, SAT + timedelta(days=1))
    fp_b = fingerprint(session, SAT)
    assert fp_b != fp

    b.ingredients = ["fish", "lemon"]
    session.commit()
    fp_edit = fingerprint(session, SAT)
    assert fp_edit != fp_b

    settings_store.put(session, "pantry", "salt")
    session.commit()
    assert fingerprint(session, SAT) != fp_edit


def test_fingerprint_is_per_week(session, make_dish, serve):
    a = make_dish("A")
    fp = fingerprint(session, SAT)
    serve(a, SAT + timedelta(days=7))
    assert fingerprint(session, SAT) == fp


def test_menu_fingerprint_ignores_pantry_but_tracks_menu(session, make_dish, serve):
    a = make_dish("A", ingredients=["rice"])
    b = make_dish("B", ingredients=["fish"])
    serve(a, SAT + timedelta(days=1))
    mfp, fp = menu_fingerprint(session, SAT), fingerprint(session, SAT)

    settings_store.put(session, "pantry", "salt")
    session.commit()
    assert menu_fingerprint(session, SAT) == mfp
    assert fingerprint(session, SAT) != fp

    serve(b, SAT + timedelta(days=2))
    assert menu_fingerprint(session, SAT) != mfp


def _data(*aisles):
    return {
        "aisles": [{"name": n, "items": [{"item": i, "quantity": q, "for": []} for i, q in items]}
                   for n, items in aisles],
        "check_you_have": ["salt"],
        "no_ingredients": [],
    }


def test_diff_lists_none_old_is_empty():
    assert diff_lists(None, _data(("Produce", [("Onions", "3")]))) == {
        "added": [], "removed": [], "changed": [],
    }


def test_diff_lists_added_removed_changed():
    old = _data(("Produce", [("Onions", "3"), ("Leeks", "2")]), ("Dairy & eggs", [("Milk", "1 l")]))
    new = _data(("Produce", [("onions ", " 3 "), ("Carrots", "4"), ("Apples", "")]),
                ("Dairy & eggs", [("MILK", "2 l")]))
    new["check_you_have"] = ["pepper"]
    assert diff_lists(old, new) == {
        "added": [{"item": "Apples", "quantity": ""}, {"item": "Carrots", "quantity": "4"}],
        "removed": [{"item": "Leeks", "quantity": "2"}],
        "changed": [{"item": "MILK", "old": "1 l", "new": "2 l"}],
    }


def test_diff_lists_quantity_case_and_duplicates():
    old = _data(("Produce", [("Garlic", "1 Bulb")]), ("Other", [("garlic", "9")]))
    new = _data(("Produce", [("garlic", "1 bulb"), ("Garlic", "5")]))
    assert diff_lists(old, new) == {"added": [], "removed": [], "changed": []}


def test_build_without_api_key_raises(session, make_dish, serve):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=1))
    with pytest.raises(ShoppingError, match="Shopping list needs ANTHROPIC_API_KEY"):
        build_list(session, SAT)


def test_build_records_menu_fingerprint_clears_pending_and_keeps_previous(session, make_dish, serve, monkeypatch):
    monkeypatch.setattr(service, "config", dataclasses.replace(service.config, anthropic_api_key="k"))
    monkeypatch.setattr(service, "_call_claude", lambda dishes, pantry: {
        "aisles": [{"name": "Produce", "items": [{"item": "Rice", "quantity": "1", "for": ["A"]}]}],
        "check_you_have": [],
    })
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=1))
    first = build_list(session, SAT)
    assert first.previous_data is None
    assert first.menu_fingerprint == menu_fingerprint(session, SAT)
    first_data = first.data

    first.pending_fingerprint, first.pending_since = "x", utcnow()
    first.failed_attempts, first.last_error = 1, "boom"
    session.commit()
    row = build_list(session, SAT)
    assert isinstance(row, ShoppingList)
    assert row.previous_data == first_data
    assert (row.pending_fingerprint, row.pending_since, row.failed_attempts, row.last_error) == (None, None, 0, None)
    assert row.last_attempt_at is not None


def test_plain_text_lists_aisles_check_and_missing():
    data = {
        "aisles": [
            {"name": "Produce", "items": [{"item": "Onions", "quantity": "3", "for": ["A"]}]},
            {"name": "Frozen", "items": [{"item": "Peas", "quantity": "", "for": ["B"]}]},
        ],
        "check_you_have": ["salt"],
        "no_ingredients": ["C"],
    }
    assert plain_text(data) == (
        "Produce\n- Onions (3)\n\nFrozen\n- Peas\n\n"
        "Check you have\n- salt\n\nNo ingredients saved\n- C"
    )


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: SAT + timedelta(days=1))
    # The nav badge reads the app's own DB; these tests only care about the shopping page.
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _saved(session, **kw):
    row = ShoppingList(
        week_start=SAT,
        fingerprint=fingerprint(session, SAT),
        menu_fingerprint=menu_fingerprint(session, SAT),
        data=_data(("Produce", [("Onions", "3")])),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def test_page_updating_after_menu_change(session, make_dish, serve, client):
    _saved(session)
    serve(make_dish("A", ingredients=["rice"]), SAT + timedelta(days=1))
    html = client.get(f"/shopping?week={SAT}").text
    assert "Updating shortly…" in html
    assert 'hx-trigger="every 10s"' in html
    assert "Out of date" not in html


def test_page_failed_shows_error(session, make_dish, serve, client):
    _saved(session, last_error="boom", failed_attempts=1)
    serve(make_dish("A", ingredients=["rice"]), SAT + timedelta(days=1))
    html = client.get(f"/shopping?week={SAT}").text
    assert "Couldn't update: boom" in html
    assert "Updating shortly" not in html
    assert "every 10s" not in html


def test_page_pantry_change_shows_out_of_date(session, client):
    _saved(session)
    settings_store.put(session, "pantry", "salt")
    session.commit()
    html = client.get(f"/shopping?week={SAT}").text
    assert "Out of date — the menu or pantry changed since this list was built." in html
    assert "Updating shortly" not in html


def test_page_menu_change_outside_auto_weeks_shows_out_of_date(session, make_dish, serve, client, monkeypatch):
    _saved(session)
    serve(make_dish("A", ingredients=["rice"]), SAT + timedelta(days=1))
    monkeypatch.setattr(clock, "today", lambda: SAT + timedelta(days=30))
    html = client.get(f"/shopping?week={SAT}").text
    assert "Out of date" in html
    assert "Updating shortly" not in html


def test_page_current_has_no_banner_and_shows_diff(session, client):
    _saved(session, previous_data=_data(("Produce", [("Onions", "2"), ("Leeks", "1")]), ("Other", [("Foil", "")])))
    row = session.get(ShoppingList, SAT)
    row.data = _data(("Produce", [("Onions", "3"), ("Carrots", "4")]), ("Other", [("Foil", "")]))
    session.commit()
    html = client.get(f"/shopping?week={SAT}").text
    assert "Changed since last list" in html
    assert "Carrots (4)" in html
    assert "Leeks" in html
    assert "Onions 2 → 3" in html
    assert "Out of date" not in html and "Updating shortly" not in html
    assert "every 10s" not in html


def test_page_no_diff_section_when_unchanged(session, client):
    _saved(session)
    html = client.get(f"/shopping?week={SAT}").text
    assert "Changed since last list" not in html
