"""Dinner dice: 21-day rule, ANY-tag filter, cycle-through re-rolls, weekly variety."""
import secrets
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import week_start
from app.models import Dish, MenuEntry, Tag
from app.tags.service import CORE_TAGS

WINDOW = timedelta(days=21)


def blocked_dish_ids(session: Session, target: date) -> set[int]:
    return set(
        session.scalars(
            select(MenuEntry.dish_id).where(
                MenuEntry.kind == "dish",
                MenuEntry.date >= target - WINDOW,
                MenuEntry.date <= target + WINDOW,
                MenuEntry.date != target,
            )
        )
    )


def matching_dinners(session: Session, tags: list[str]) -> list[Dish]:
    q = select(Dish).where(Dish.type == "dinner", Dish.archived.is_(False))
    if tags:
        q = q.where(Dish.tags.any(Tag.name.in_(tags)))
    return list(session.scalars(q.order_by(Dish.id)))


def eligible_dishes(session: Session, target: date, tags: list[str]) -> list[Dish]:
    blocked = blocked_dish_ids(session, target)
    return [d for d in matching_dinners(session, tags) if d.id not in blocked]


def _week_core_tags(session: Session, target: date) -> set[str]:
    start = week_start(target)
    dishes = session.scalars(
        select(Dish)
        .join(MenuEntry, MenuEntry.dish_id == Dish.id)
        .where(
            MenuEntry.kind == "dish",
            MenuEntry.date >= start,
            MenuEntry.date <= start + timedelta(days=6),
            MenuEntry.date != target,
        )
    )
    return {n for d in dishes for n in d.tag_names if n in CORE_TAGS}


def roll(
    session: Session, target: date, tags: list[str], exclude_ids: list[int]
) -> tuple[Dish, list[int]] | None:
    eligible = eligible_dishes(session, target, tags)
    if not eligible:
        return None
    excluded = set(exclude_ids)
    fresh = [d for d in eligible if d.id not in excluded]

    if len(tags) != 1:
        used = _week_core_tags(session, target)
        if used:
            preferred = [d for d in fresh if not set(d.tag_names) & used]
            if preferred:
                fresh = preferred

    if fresh:
        picked = secrets.choice(fresh)
        return picked, [*exclude_ids, picked.id]

    last = exclude_ids[-1] if exclude_ids else None
    pool = [d for d in eligible if d.id != last] or eligible
    picked = secrets.choice(pool)
    return picked, [picked.id]


def conflicts(session: Session, dish_id: int, target: date) -> list[date]:
    return list(
        session.scalars(
            select(MenuEntry.date)
            .where(
                MenuEntry.kind == "dish",
                MenuEntry.dish_id == dish_id,
                MenuEntry.date >= target - WINDOW,
                MenuEntry.date <= target + WINDOW,
                MenuEntry.date != target,
            )
            .order_by(MenuEntry.date)
        )
    )
