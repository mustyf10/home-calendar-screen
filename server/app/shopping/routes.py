from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import clock
from app.db import get_session
from app.shopping.service import (
    NO_KEY_MESSAGE,
    ShoppingError,
    build_list,
    fingerprint,
    get_list,
    plain_text,
    week_dinners,
)
from app.web import render

router = APIRouter()


def _parse_week(week: str) -> date:
    try:
        return clock.week_start(date.fromisoformat(week))
    except ValueError:
        return clock.week_start(clock.today()) + timedelta(days=7)


def _page(request: Request, session: Session, start: date, error: str | None = None, status_code: int = 200):
    saved = get_list(session, start)
    end = start + timedelta(days=6)
    return render(request, "shopping/list.html", {
        "start": start,
        "range_text": f"{clock.fmt_day(start)} – {clock.fmt_day(end)}",
        "prev_week": start - timedelta(days=7),
        "next_week": start + timedelta(days=7),
        "dinners": week_dinners(session, start),
        "saved": saved,
        "out_of_date": saved is not None and saved.fingerprint != fingerprint(session, start),
        "text": plain_text(saved.data) if saved else "",
        "built_at": saved.created_at.replace(tzinfo=timezone.utc).astimezone(clock.tz()) if saved else None,
        "error": error,
    }, status_code=status_code)


@router.get("/shopping")
def shopping(request: Request, week: str = "", session: Session = Depends(get_session)):
    return _page(request, session, _parse_week(week))


@router.post("/shopping/build")
def build(request: Request, week: str = "", session: Session = Depends(get_session)):
    start = _parse_week(week)
    try:
        build_list(session, start)
    except ShoppingError as e:
        session.rollback()
        return _page(request, session, start, str(e), 400 if str(e) == NO_KEY_MESSAGE else 502)
    return RedirectResponse(f"/shopping?week={start.isoformat()}", status_code=303)
