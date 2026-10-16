from datetime import date, datetime, timedelta

import pytest

from app import clock, settings_store
from app.menu.entries import clear
from app.models import ShoppingList
from app.shopping.auto import decide, tick
from app.shopping.service import ShoppingError, fingerprint, menu_fingerprint

SAT = date(2026, 10, 17)
T0 = datetime(2026, 10, 18, 9, 0)


def m(minutes, seconds=0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def _row(menu_fp="old"):
    return ShoppingList(week_start=SAT, fingerprint="f", data={}, menu_fingerprint=menu_fp, failed_attempts=0)


def _fail(row, now):
    row.failed_attempts += 1
    row.last_attempt_at = now
    row.last_error = "boom"


# --- decide -----------------------------------------------------------------

def test_decide_matching_fingerprint_is_idle_and_clears_pending():
    row = _row()
    row.pending_fingerprint, row.pending_since, row.failed_attempts, row.last_error = "new", T0, 1, "boom"
    assert decide(row, "old", m(10)) == "idle"
    assert (row.pending_fingerprint, row.pending_since, row.failed_attempts, row.last_error) == (None, None, 0, None)


def test_decide_waits_out_the_quiet_period():
    row = _row()
    assert decide(row, "new", T0) == "wait"
    assert (row.pending_fingerprint, row.pending_since) == ("new", T0)
    assert decide(row, "new", m(4, 59)) == "wait"
    assert decide(row, "new", m(5)) == "rebuild"


def test_decide_new_change_restarts_timer():
    row = _row()
    assert decide(row, "new", T0) == "wait"
    assert decide(row, "newer", m(3)) == "wait"
    assert decide(row, "newer", m(7, 59)) == "wait"
    assert decide(row, "newer", m(8)) == "rebuild"


def test_decide_retries_once_after_failure_then_gives_up_until_change():
    row = _row()
    decide(row, "new", T0)
    assert decide(row, "new", m(5)) == "rebuild"
    _fail(row, m(5))
    assert decide(row, "new", m(19)) != "rebuild"
    assert decide(row, "new", m(20)) == "rebuild"
    _fail(row, m(20))
    assert decide(row, "new", m(40)) == "gave_up"
    assert decide(row, "new", m(600)) == "gave_up"
    assert row.last_error == "boom"

    assert decide(row, "newer", m(601)) == "wait"
    assert (row.failed_attempts, row.last_error) == (0, None)
    assert decide(row, "newer", m(606)) == "rebuild"


# --- tick (real build_list, Claude faked at the boundary) ------------------

@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: SAT + timedelta(days=1))


def _save(session, start, **kw):
    kw.setdefault("menu_fingerprint", menu_fingerprint(session, start))
    session.add(ShoppingList(week_start=start, fingerprint=fingerprint(session, start), data={"aisles": []}, **kw))
    session.commit()


def test_tick_rebuilds_current_week_after_quiet_period(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    _save(session, SAT + timedelta(days=7))
    serve(a, SAT + timedelta(days=2))

    assert tick(session, T0) == []
    assert fake_claude.calls == []
    assert tick(session, m(6)) == [SAT]
    assert fake_claude.calls == [["A"]]
    assert session.get(ShoppingList, SAT).menu_fingerprint == menu_fingerprint(session, SAT)
    assert tick(session, m(20)) == []
    assert fake_claude.calls == [["A"]]


def test_tick_skips_unsaved_next_week_and_past_weeks(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    past = SAT - timedelta(days=7)
    _save(session, past)
    serve(a, SAT + timedelta(days=8))
    serve(a, past + timedelta(days=1))
    for t in (T0, m(6), m(30)):
        tick(session, t)
    assert fake_claude.calls == []


def test_tick_rebuilds_after_weekday_dinner_cleared(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    b = make_dish("B", ingredients=["fish"])
    serve(a, SAT + timedelta(days=2))
    serve(b, SAT + timedelta(days=3))
    _save(session, SAT)
    clear(session, SAT + timedelta(days=2))

    tick(session, T0)
    assert fake_claude.calls == []
    tick(session, m(5))
    assert fake_claude.calls == [["B"]]


def test_tick_ignores_pantry_change(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=2))
    _save(session, SAT)
    settings_store.put(session, "pantry", "salt")
    session.commit()
    for t in (T0, m(6), m(30)):
        tick(session, t)
    assert fake_claude.calls == []


def test_tick_adopts_menu_fingerprint_for_current_pre_migration_list(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=2))
    _save(session, SAT, menu_fingerprint=None)
    for t in (T0, m(6), m(30)):
        tick(session, t)
    assert fake_claude.calls == []
    assert session.get(ShoppingList, SAT).menu_fingerprint == menu_fingerprint(session, SAT)


def test_tick_rebuilds_out_of_date_pre_migration_list(session, make_dish, serve, today, fake_claude):
    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT, menu_fingerprint=None)
    serve(a, SAT + timedelta(days=2))
    tick(session, T0)
    assert fake_claude.calls == []
    assert session.get(ShoppingList, SAT).menu_fingerprint is None
    tick(session, m(5))
    assert fake_claude.calls == [["A"]]


def test_tick_records_failure_and_retries_once(session, make_dish, serve, today, fake_claude):
    fake_claude.error = ShoppingError("boom")
    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    serve(a, SAT + timedelta(days=2))

    tick(session, T0)
    tick(session, m(5))
    row = session.get(ShoppingList, SAT)
    assert (row.failed_attempts, row.last_error, row.last_attempt_at) == (1, "boom", m(5))

    tick(session, m(19))
    assert len(fake_claude.calls) == 1
    tick(session, m(20))
    assert len(fake_claude.calls) == 2
    assert session.get(ShoppingList, SAT).failed_attempts == 2
    tick(session, m(120))
    assert len(fake_claude.calls) == 2


def test_tick_generic_exception_message(session, make_dish, serve, today, fake_claude):
    fake_claude.error = RuntimeError("secret detail")
    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    serve(a, SAT + timedelta(days=2))
    tick(session, T0)
    tick(session, m(5))
    assert session.get(ShoppingList, SAT).last_error == "Couldn't build the list: RuntimeError"
