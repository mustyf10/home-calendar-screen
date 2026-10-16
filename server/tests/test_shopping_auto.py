from datetime import date, datetime, timedelta

import pytest

from app import clock, settings_store
from app.menu.entries import clear
from app.models import ShoppingList
from app.shopping import auto
from app.shopping.auto import decide, tick
from app.shopping.service import ShoppingError, menu_fingerprint

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


# --- tick -------------------------------------------------------------------

@pytest.fixture
def today(monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: SAT + timedelta(days=1))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def fake_build(session, calls):
    """Records calls and marks the list rebuilt, like build_list does on success."""
    def _build(s, start):
        calls.append(start)
        row = s.get(ShoppingList, start)
        row.menu_fingerprint = menu_fingerprint(s, start)
        row.pending_fingerprint = row.pending_since = row.last_error = None
        row.failed_attempts = 0
        s.commit()
        return row
    return _build


def _save(session, start):
    session.add(ShoppingList(week_start=start, fingerprint="f", data={"aisles": []},
                             menu_fingerprint=menu_fingerprint(session, start)))
    session.commit()


def test_tick_rebuilds_current_week_after_quiet_period(session, make_dish, serve, today, calls, fake_build):
    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    _save(session, SAT + timedelta(days=7))
    serve(a, SAT + timedelta(days=2))

    assert tick(session, T0, build=fake_build) == []
    assert calls == []
    assert tick(session, m(6), build=fake_build) == [SAT]
    assert calls == [SAT]
    assert tick(session, m(20), build=fake_build) == []
    assert calls == [SAT]


def test_tick_skips_unsaved_next_week_and_past_weeks(session, make_dish, serve, today, calls, fake_build):
    a = make_dish("A", ingredients=["rice"])
    past = SAT - timedelta(days=7)
    _save(session, past)
    serve(a, SAT + timedelta(days=8))
    serve(a, past + timedelta(days=1))
    for t in (T0, m(6), m(30)):
        tick(session, t, build=fake_build)
    assert calls == []


def test_tick_rebuilds_after_weekday_dinner_cleared(session, make_dish, serve, today, calls, fake_build):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=2))
    _save(session, SAT)
    clear(session, SAT + timedelta(days=2))

    tick(session, T0, build=fake_build)
    assert calls == []
    tick(session, m(5), build=fake_build)
    assert calls == [SAT]


def test_tick_ignores_pantry_change(session, make_dish, serve, today, calls, fake_build):
    a = make_dish("A", ingredients=["rice"])
    serve(a, SAT + timedelta(days=2))
    _save(session, SAT)
    settings_store.put(session, "pantry", "salt")
    session.commit()
    for t in (T0, m(6), m(30)):
        tick(session, t, build=fake_build)
    assert calls == []


def test_tick_records_failure_and_retries_once(session, make_dish, serve, today, calls):
    def failing(s, start):
        calls.append(start)
        raise ShoppingError("boom")

    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    serve(a, SAT + timedelta(days=2))

    tick(session, T0, build=failing)
    tick(session, m(5), build=failing)
    row = session.get(ShoppingList, SAT)
    assert (row.failed_attempts, row.last_error, row.last_attempt_at) == (1, "boom", m(5))

    tick(session, m(19), build=failing)
    assert calls == [SAT]
    tick(session, m(20), build=failing)
    assert calls == [SAT, SAT]
    assert session.get(ShoppingList, SAT).failed_attempts == 2
    tick(session, m(120), build=failing)
    assert calls == [SAT, SAT]


def test_tick_generic_exception_message(session, make_dish, serve, today):
    def broken(s, start):
        raise RuntimeError("secret detail")

    a = make_dish("A", ingredients=["rice"])
    _save(session, SAT)
    serve(a, SAT + timedelta(days=2))
    tick(session, T0, build=broken)
    tick(session, m(5), build=broken)
    assert session.get(ShoppingList, SAT).last_error == "Couldn't build the list: RuntimeError"


def test_loop_constants():
    assert auto.QUIET == timedelta(minutes=5)
    assert auto.RETRY_AFTER == timedelta(minutes=15)
    assert auto.TICK_SECONDS == 60
