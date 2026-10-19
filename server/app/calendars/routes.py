"""The /settings page: calendars, pantry staples, backup status."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, settings_store
from app.backup import run_backup
from app.calendars.ics import log_label, masked_url
from app.calendars.sync import load_feed, store_events, sync_all
from app.db import get_session
from app.errors import log_failure, user_message
from app.models import Calendar
from app.web import render
from app.weather.service import settings_context

log = logging.getLogger(__name__)
router = APIRouter()

HELP = (
    "In Google Calendar → Settings → (calendar) → Integrate calendar, copy "
    "'Secret address in iCal format'. Changes in Google can take a few hours to appear."
)


def _local(dt: datetime | None) -> str:
    """Naive-UTC or aware datetime → 'Wed 21 Oct 14:05' local."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(clock.tz())
    return f"{clock.fmt_day(dt.date())} {dt:%H:%M}"


def _page(request: Request, session: Session, status_code: int = 200, **ctx):
    calendars = session.scalars(select(Calendar).order_by(Calendar.name, Calendar.id)).all()
    last_ok = settings_store.get(session, "backup_last_ok_at")
    return render(request, "settings/index.html", {
        "help": HELP,
        "calendars": [
            {
                "cal": c,
                "masked": masked_url(c.ics_url),
                "last_ok": _local(c.last_ok_at),
                "error_since": _local(c.last_error_since),
            }
            for c in calendars
        ],
        "pantry": settings_store.get(session, "pantry", settings_store.DEFAULT_PANTRY),
        "backup_last_ok": _local(datetime.fromisoformat(last_ok)) if last_ok else None,
        "backup_last_error": settings_store.get(session, "backup_last_error"),
        "form": {"name": "", "url": ""},
        "error": None,
        "weather": settings_context(session),
        **ctx,
    }, status_code=status_code)


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@router.post("/settings/calendars")
def add_calendar(
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    session: Session = Depends(get_session),
):
    name, url = name.strip(), url.strip()
    form = {"name": name, "url": url}

    def fail(message: str):
        return _page(request, session, 422, form=form, error=message)

    if not name or not url:
        return fail("Name and URL are both required.")
    if session.scalar(select(Calendar).where(Calendar.ics_url == url)):
        return fail("That calendar is already added.")
    try:
        rows = load_feed(url)
    except Exception as e:
        log_failure(log, f"Adding calendar {log_label(url)} failed", e)
        return fail(user_message("Couldn't add that calendar", e))
    cal = Calendar(name=name, ics_url=url)
    session.add(cal)
    session.flush()
    store_events(session, cal, rows)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/calendars/{cal_id}/delete")
def delete_calendar(cal_id: int, session: Session = Depends(get_session)):
    cal = session.get(Calendar, cal_id)
    if cal is None:
        raise HTTPException(404)
    session.delete(cal)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sync")
def sync_now(session: Session = Depends(get_session)):
    sync_all(session)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/pantry")
def save_pantry(pantry: str = Form(""), session: Session = Depends(get_session)):
    lines = [line.strip() for line in pantry.splitlines()]
    settings_store.put(session, "pantry", "\n".join(line for line in lines if line))
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/backup")
def backup_now(session: Session = Depends(get_session)):
    run_backup(session)
    return RedirectResponse("/settings", status_code=303)
