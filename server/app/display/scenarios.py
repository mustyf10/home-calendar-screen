"""Canned display inputs for the preview's "what if" scenarios; no DB access."""
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from app.clock import week_dates, week_start
from app.display.data import DisplayInputs, _Event

SCENARIO_LABELS = {
    "real": "Real data",
    "quiet": "Quiet week",
    "busy": "Busy day",
    "long-names": "Long names",
    "nothing": "Nothing planned",
    "trip": "Trip under way",
    "month-boundary": "Month boundary",
}

_DISHES = ["Butter chicken", "Spaghetti bolognese", "Fish pie", "Veggie chilli",
           "Roast chicken", "Pad thai", "Shepherd's pie"]


def _timed(now: datetime, day: date, hour: int, minute: int, title: str) -> _Event:
    start = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
    return _Event(title, day, day, start)


def _all_day(first: date, last: date, title: str) -> _Event:
    return _Event(title, first, last, None)


def _menu(today: date, titles: list[str | None]) -> dict[date, str | None]:
    """titles run Sat–Fri; a None Saturday falls back to the Takeaway default."""
    days = week_dates(week_start(today))
    return {d: t if t is not None or d.weekday() != 5 else "Takeaway" for d, t in zip(days, titles)}


def _quiet(now: datetime) -> DisplayInputs:
    today = now.date()
    events = [
        _timed(now, today, 18, 0, "Swimming lesson"),
        _timed(now, today + timedelta(days=3), 10, 30, "Dentist"),
        _all_day(today + timedelta(days=8), today + timedelta(days=8), "Bin day"),
    ]
    return DisplayInputs(events, _menu(today, [None, *_DISHES[:6]]), None)


def _busy(now: datetime) -> DisplayInputs:
    today = now.date()
    events = [
        _timed(now, today, 8 + i // 2, 30 * (i % 2), f"Meeting {i + 1}")
        for i in range(15)
    ]
    tomorrow = today + timedelta(days=1)
    events += [
        _timed(now, tomorrow, 9, 0, "School run"),
        _timed(now, tomorrow, 13, 0, "Lunch with Sam"),
        _timed(now, tomorrow, 19, 30, "Book club"),
    ]
    return DisplayInputs(events, _menu(today, _DISHES), None)


def _long_names(now: datetime) -> DisplayInputs:
    today = now.date()
    long = "Parents' evening with the year six teachers about the residential trip and the "
    events = [
        _timed(now, today, 9, 0, long + "forms"),
        _timed(now, today, 16, 45, "Extremely long-winded orthodontist follow-up appointment at the new clinic"),
        _timed(now, today + timedelta(days=1), 11, 0, "Neighbourhood association annual general meeting and bring-and-share"),
        _all_day(today + timedelta(days=2), today + timedelta(days=2),
                 "Grandma and Grandad visiting from Aberdeen for the long weekend celebrations"),
    ]
    dish = "Slow-cooked Moroccan lamb tagine with apricots and couscous"
    return DisplayInputs(events, _menu(today, [dish] * 7), None)


def _nothing(now: datetime) -> DisplayInputs:
    return DisplayInputs([], _menu(now.date(), [None] * 7), None)


def _trip(now: datetime) -> DisplayInputs:
    today = now.date()
    events = [
        _all_day(today - timedelta(days=1), today + timedelta(days=3), "Family trip to Cornwall"),
        _timed(now, today, 19, 0, "Fish and chips on the harbour"),
        _timed(now, today + timedelta(days=1), 10, 0, "Eden Project"),
    ]
    return DisplayInputs(events, _menu(today, ["Pasties", "Cream tea", None, None, None, None, None]), None)


def _month_boundary(now: datetime) -> DisplayInputs:
    today = now.date()
    days = week_dates(week_start(today))
    last = next((d for d in days if (d + timedelta(days=1)).day == 1), days[-1])
    events = [
        _timed(now, today, 18, 0, "Football"),
        _timed(now, last, 19, 0, "Month-end drinks"),
        _all_day(last + timedelta(days=1), last + timedelta(days=1), "Rent due"),
    ]
    return DisplayInputs(events, _menu(today, _DISHES), None)


SCENARIOS: dict[str, Callable[[datetime], DisplayInputs]] = {
    "quiet": _quiet,
    "busy": _busy,
    "long-names": _long_names,
    "nothing": _nothing,
    "trip": _trip,
    "month-boundary": _month_boundary,
}


def _boundary_now(now: datetime) -> datetime:
    """08:00 on the Thursday of the Sat–Fri menu week that straddles the end of now's month.

    A month ending on a Friday has no such week, so the next month end is used instead.
    """
    first = now.date().replace(day=1)
    while True:
        nxt = (first + timedelta(days=32)).replace(day=1)
        last = nxt - timedelta(days=1)
        if last.weekday() != 4:
            break
        first = nxt
    thursday = week_start(last) + timedelta(days=5)
    return datetime.combine(thursday, time(8, 0), tzinfo=now.tzinfo)


def scenario_now(name: str, now: datetime) -> datetime:
    return _boundary_now(now) if name == "month-boundary" else now
