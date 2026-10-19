"""Periodic calendar refresh: each feed's events are replaced wholesale on success."""
import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock
from app.calendars.ics import EventRow, fetch_ics, log_label, owner_email_from_url, parse_events
from app.db import SessionLocal
from app.errors import log_failure, user_message
from app.models import Calendar, CalendarEvent, utcnow

log = logging.getLogger(__name__)
SYNC_INTERVAL_S = 900
FAILED = "Couldn't update this calendar"


def sync_window(today: date) -> tuple[date, date]:
    """Covers the month grid (from the 1st) and the 60-day agenda horizon."""
    return today.replace(day=1) - timedelta(days=7), today + timedelta(days=62)


def load_feed(url: str) -> list[EventRow]:
    start, end = sync_window(clock.today())
    return parse_events(fetch_ics(url), start, end, clock.tz(), owner_email_from_url(url))


def store_events(session: Session, cal: Calendar, rows: list[EventRow]) -> None:
    """Replace the calendar's events and mark it healthy. Caller commits."""
    session.execute(delete(CalendarEvent).where(CalendarEvent.calendar_id == cal.id))
    session.add_all(
        CalendarEvent(
            calendar_id=cal.id,
            uid=r.uid,
            summary=r.summary,
            location=r.location,
            all_day=r.all_day,
            start_at=r.start_at.isoformat() if r.start_at else None,
            end_at=r.end_at.isoformat() if r.end_at else None,
            start_date=r.start_date,
            end_date=r.end_date,
        )
        for r in rows
    )
    cal.last_ok_at = utcnow()
    cal.last_error = None
    cal.last_error_since = None


def sync_calendar(session: Session, cal: Calendar) -> None:
    try:
        rows = load_feed(cal.ics_url)
    except Exception as e:  # network, HTTP or parse failure: keep the old rows
        log_failure(log, f"Calendar {cal.id} ({log_label(cal.ics_url)}) sync failed", e)
        cal.last_error = user_message(FAILED, e)
        if cal.last_error_since is None:
            cal.last_error_since = utcnow()
        session.commit()
        return
    store_events(session, cal, rows)
    session.commit()


def sync_all(session: Session) -> None:
    for cal in session.scalars(select(Calendar).order_by(Calendar.id)).all():
        try:
            sync_calendar(session, cal)
        except Exception:
            log.exception("Calendar %s sync crashed", cal.id)
            session.rollback()


def _sync_all_now() -> None:
    with SessionLocal() as session:
        sync_all(session)


async def run_sync_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_sync_all_now)
        except Exception:
            log.exception("Calendar sync loop failed")
        await asyncio.sleep(SYNC_INTERVAL_S)
