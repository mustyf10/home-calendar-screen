"""Background rebuild of saved shopping lists for this week and next after menu/recipe edits."""
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from app import clock
from app.db import SessionLocal
from app.models import ShoppingList, utcnow
from app.shopping.service import ShoppingError, build_list, menu_fingerprint

log = logging.getLogger(__name__)
QUIET = timedelta(minutes=5)
RETRY_AFTER = timedelta(minutes=15)
TICK_SECONDS = 60


def decide(row: ShoppingList, menu_fp: str, now: datetime) -> Literal["idle", "wait", "rebuild", "gave_up"]:
    """Advance the row's pending state for the current menu fingerprint.

    The quiet period runs from when a new fingerprint is first observed, so menu
    row deletions (which leave no timestamp) still count as changes.
    """
    if menu_fp == row.menu_fingerprint:
        row.pending_fingerprint = None
        row.pending_since = None
        row.failed_attempts = 0
        row.last_error = None
        return "idle"
    if menu_fp != row.pending_fingerprint:
        row.pending_fingerprint = menu_fp
        row.pending_since = now
        row.failed_attempts = 0
        row.last_error = None
        return "wait"
    if now - row.pending_since < QUIET:
        return "wait"
    if row.failed_attempts == 0:
        return "rebuild"
    if row.failed_attempts == 1 and now - row.last_attempt_at >= RETRY_AFTER:
        return "rebuild"
    return "gave_up"


def tick(session: Session, now: datetime, build=build_list) -> list[date]:
    """One pass over this week and next; returns the weeks a rebuild was attempted for."""
    current = clock.week_start(clock.today())
    attempted = []
    for start in (current, current + timedelta(days=7)):
        row = session.get(ShoppingList, start)
        if row is None:
            continue
        action = decide(row, menu_fingerprint(session, start), now)
        session.commit()
        if action != "rebuild":
            continue
        attempted.append(start)
        try:
            build(session, start)
        except Exception as e:
            session.rollback()
            row = session.get(ShoppingList, start)
            row.failed_attempts += 1
            row.last_attempt_at = now
            row.last_error = str(e) if isinstance(e, ShoppingError) else f"Couldn't build the list: {type(e).__name__}"
            session.commit()
            log.warning("Shopping list rebuild for %s failed: %s", start, e)
    return attempted


def _tick_once() -> None:
    with SessionLocal() as session:
        tick(session, utcnow())


async def run_shopping_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            log.exception("Shopping loop failed")
        await asyncio.sleep(TICK_SECONDS)
