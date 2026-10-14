from datetime import date, timedelta

import pytest

from app import settings_store
from app.menu.entries import set_takeaway
from app.shopping.service import (
    ShoppingError,
    build_list,
    fingerprint,
    plain_text,
    week_dinners,
)

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


def test_build_without_api_key_raises(session, make_dish, serve):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=1))
    with pytest.raises(ShoppingError, match="Shopping list needs ANTHROPIC_API_KEY"):
        build_list(session, SAT)


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
