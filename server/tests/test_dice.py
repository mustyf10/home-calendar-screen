from datetime import date, timedelta

from app.menu.dice import (
    blocked_dish_ids,
    conflicts,
    eligible_dishes,
    matching_dinners,
    roll,
)
from app.models import MenuEntry

T = date(2026, 10, 21)  # a Wednesday; week Sat 17 – Fri 23 Oct


def ids(dishes):
    return {d.id for d in dishes}


def test_served_21_days_before_is_blocked_22_is_not(session, make_dish, serve):
    a = make_dish("A")
    b = make_dish("B")
    serve(a, T - timedelta(days=21))
    serve(b, T - timedelta(days=22))
    assert blocked_dish_ids(session, T) == {a.id}
    assert ids(eligible_dishes(session, T, [])) == {b.id}


def test_planned_after_target_blocks(session, make_dish, serve):
    a = make_dish("A")
    serve(a, T + timedelta(days=10))
    assert a.id in blocked_dish_ids(session, T)


def test_target_own_entry_does_not_block(session, make_dish, serve):
    a = make_dish("A")
    serve(a, T)
    assert blocked_dish_ids(session, T) == set()


def test_takeaway_rows_do_not_block(session, make_dish):
    a = make_dish("A")
    session.add(MenuEntry(date=T - timedelta(days=1), kind="takeaway"))
    session.commit()
    assert blocked_dish_ids(session, T) == set()
    assert ids(eligible_dishes(session, T, [])) == {a.id}


def test_only_unarchived_dinners_are_rolled(session, make_dish):
    make_dish("Samosa", type="snack", tags=["chicken"])
    make_dish("Old", tags=["chicken"], archived=True)
    x = make_dish("X", tags=["chicken"])
    for _ in range(10):
        assert roll(session, T, ["chicken"], [])[0].id == x.id


def test_tag_filter_is_any(session, make_dish):
    x = make_dish("X", tags=["chicken"])
    y = make_dish("Y", tags=["fish"])
    assert ids(matching_dinners(session, ["chicken", "fish"])) == {x.id, y.id}
    assert ids(matching_dinners(session, ["chicken"])) == {x.id}
    assert ids(matching_dinners(session, [])) == {x.id, y.id}
    assert matching_dinners(session, ["nonexistent"]) == []
    assert roll(session, T, ["nonexistent"], []) is None
    seen = {roll(session, T, ["chicken", "fish"], [])[0].id for _ in range(50)}
    assert seen == {x.id, y.id}


def test_cycle_covers_every_dish_before_repeating(session, make_dish):
    for n in "ABCD":
        make_dish(n)
    exclude: list[int] = []
    picked = []
    for _ in range(4):
        dish, exclude = roll(session, T, [], exclude)
        picked.append(dish.id)
    assert len(set(picked)) == 4
    assert exclude == picked
    dish, nxt = roll(session, T, [], exclude)
    assert dish.id != picked[-1]
    assert nxt == [dish.id]


def test_single_eligible_dish_repeats(session, make_dish):
    a = make_dish("A")
    exclude: list[int] = []
    for _ in range(5):
        dish, exclude = roll(session, T, [], exclude)
        assert dish.id == a.id


def test_empty_eligible_returns_none(session, make_dish, serve):
    assert roll(session, T, [], []) is None
    a = make_dish("A")
    serve(a, T - timedelta(days=3))
    assert roll(session, T, [], []) is None


def test_variety_prefers_other_core_tags_with_no_tags(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=2))
    make_dish("Chicken 2", tags=["chicken"])
    fish = make_dish("Fish", tags=["fish"])
    for _ in range(30):
        assert roll(session, T, [], [])[0].id == fish.id


def test_variety_applies_to_multiple_tags(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=1))
    make_dish("Chicken 2", tags=["chicken"])
    fish = make_dish("Fish", tags=["fish"])
    for _ in range(30):
        assert roll(session, T, ["chicken", "fish"], [])[0].id == fish.id


def test_variety_falls_back_when_only_same_core_eligible(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=2))
    other = make_dish("Chicken 2", tags=["chicken"])
    assert roll(session, T, [], [])[0].id == other.id


def test_variety_ignored_with_single_tag(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=2))
    c2 = make_dish("Chicken 2", tags=["chicken", "rice"])
    c3 = make_dish("Chicken 3", tags=["chicken", "rice"])
    seen = {roll(session, T, ["rice"], [])[0].id for _ in range(50)}
    assert seen == {c2.id, c3.id}


def test_variety_ignores_dishes_outside_the_week(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=5))  # Fri 16 Oct: previous week
    c2 = make_dish("Chicken 2", tags=["chicken"])
    fish = make_dish("Fish", tags=["fish"])
    seen = {roll(session, T, [], [])[0].id for _ in range(50)}
    assert seen == {c2.id, fish.id}


def test_variety_respects_cycle_exclusion(session, make_dish, serve):
    served = make_dish("Served", tags=["chicken"])
    serve(served, T - timedelta(days=1))
    c2 = make_dish("Chicken 2", tags=["chicken"])
    fish = make_dish("Fish", tags=["fish"])
    dish, nxt = roll(session, T, [], [])
    assert dish.id == fish.id
    dish, nxt = roll(session, T, [], nxt)
    assert dish.id == c2.id
    assert nxt == [fish.id, c2.id]


def test_conflicts_lists_other_dates_within_window(session, make_dish, serve):
    a = make_dish("A")
    serve(a, T - timedelta(days=4))
    serve(a, T + timedelta(days=30))
    serve(a, T)
    assert conflicts(session, a.id, T) == [T - timedelta(days=4)]
