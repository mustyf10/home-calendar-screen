"""Menu rows: effective entry (Saturday takeaway default), upserts, and dish menu history."""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import week_dates
from app.models import Dish, MenuEntry


@dataclass
class EffectiveEntry:
    kind: str  # 'dish' | 'takeaway' | 'empty'
    dish: Dish | None = None
    is_default: bool = False


def effective_entry(d: date, row: MenuEntry | None) -> EffectiveEntry:
    if row is not None:
        if row.kind == "none":
            return EffectiveEntry("empty")
        if row.kind == "dish":
            return EffectiveEntry("dish", row.dish)
        return EffectiveEntry("takeaway")
    if d.weekday() == 5:
        return EffectiveEntry("takeaway", is_default=True)
    return EffectiveEntry("empty")


def get_row(session: Session, d: date) -> MenuEntry | None:
    return session.get(MenuEntry, d)


def week_entries(session: Session, start: date) -> list[tuple[date, EffectiveEntry]]:
    days = week_dates(start)
    rows = {
        r.date: r
        for r in session.scalars(
            select(MenuEntry).where(MenuEntry.date >= days[0], MenuEntry.date <= days[-1])
        )
    }
    return [(d, effective_entry(d, rows.get(d))) for d in days]


def _upsert(session: Session, d: date, kind: str, dish_id: int | None) -> None:
    row = session.get(MenuEntry, d)
    if row is None:
        session.add(MenuEntry(date=d, kind=kind, dish_id=dish_id))
    else:
        row.kind = kind
        row.dish_id = dish_id
    session.commit()


def set_dish(session: Session, d: date, dish_id: int) -> None:
    _upsert(session, d, "dish", dish_id)


def set_takeaway(session: Session, d: date) -> None:
    _upsert(session, d, "takeaway", None)


def clear(session: Session, d: date) -> None:
    if d.weekday() == 5:
        _upsert(session, d, "none", None)
        return
    row = session.get(MenuEntry, d)
    if row is not None:
        session.delete(row)
        session.commit()


@dataclass
class MenuHistory:
    last_on_menu: date | None  # latest past-or-today date
    rollable_from: date | None  # latest date (past or future) + 22 days; None = now


def menu_history(session: Session, dish_ids: list[int], today: date) -> dict[int, MenuHistory]:
    if not dish_ids:
        return {}
    base = select(MenuEntry.dish_id, func.max(MenuEntry.date)).where(
        MenuEntry.kind == "dish", MenuEntry.dish_id.in_(dish_ids)
    ).group_by(MenuEntry.dish_id)
    latest = dict(session.execute(base).all())
    past = dict(session.execute(base.where(MenuEntry.date <= today)).all())
    out = {}
    for i in dish_ids:
        rollable = latest[i] + timedelta(days=22) if i in latest else None
        out[i] = MenuHistory(past.get(i), rollable if rollable and rollable > today else None)
    return out
