from datetime import date

from app.clock import week_start
from app.menu.entries import clear, effective_entry, get_row, set_dish, set_takeaway, week_entries

SAT = date(2026, 10, 17)
WED = date(2026, 10, 21)


def eff(session, d):
    return effective_entry(d, get_row(session, d))


def test_week_start_is_saturday():
    assert week_start(date(2026, 10, 23)) == SAT
    assert week_start(SAT) == SAT


def test_saturday_without_row_defaults_to_takeaway(session):
    e = eff(session, SAT)
    assert e.kind == "takeaway"
    assert e.is_default


def test_cleared_saturday_is_empty(session):
    clear(session, SAT)
    assert eff(session, SAT).kind == "empty"


def test_weekday_without_row_is_empty(session):
    assert eff(session, WED).kind == "empty"


def test_set_takeaway_on_wednesday(session):
    set_takeaway(session, WED)
    e = eff(session, WED)
    assert e.kind == "takeaway"
    assert not e.is_default


def test_set_dish_then_clear_weekday_deletes_row(session, make_dish):
    a = make_dish("A")
    set_dish(session, WED, a.id)
    e = eff(session, WED)
    assert e.kind == "dish" and e.dish.id == a.id
    clear(session, WED)
    assert get_row(session, WED) is None


def test_set_dish_overwrites_saturday_takeaway(session, make_dish):
    a = make_dish("A")
    set_dish(session, SAT, a.id)
    assert eff(session, SAT).dish.id == a.id
    set_takeaway(session, SAT)
    assert eff(session, SAT).kind == "takeaway"


def test_week_entries_returns_seven_effective_days(session, make_dish):
    a = make_dish("A")
    set_dish(session, WED, a.id)
    rows = week_entries(session, SAT)
    assert [d for d, _ in rows] == [date(2026, 10, 17 + i) for i in range(7)]
    kinds = [e.kind for _, e in rows]
    assert kinds == ["takeaway", "empty", "empty", "empty", "dish", "empty", "empty"]


def test_menu_history_last_on_menu_and_rollable_from(session, make_dish, serve):
    from datetime import timedelta

    from app.menu.entries import menu_history

    a = make_dish("A")
    b = make_dish("B")
    c = make_dish("C")
    serve(a, WED - timedelta(days=5))
    serve(a, WED + timedelta(days=7))
    serve(b, WED - timedelta(days=30))
    h = menu_history(session, [a.id, b.id, c.id], WED)
    assert h[a.id].last_on_menu == WED - timedelta(days=5)
    assert h[a.id].rollable_from == WED + timedelta(days=29)
    assert h[b.id].last_on_menu == WED - timedelta(days=30)
    assert h[b.id].rollable_from is None  # 22 days after is already past: "now"
    assert h[c.id].last_on_menu is None and h[c.id].rollable_from is None
