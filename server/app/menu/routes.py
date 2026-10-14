from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import clock
from app.db import get_session
from app.dishes.search import search_dishes
from app.menu import entries
from app.menu.dice import blocked_dish_ids, conflicts, matching_dinners, roll
from app.models import Dish
from app.tags.service import CORE_TAGS, dinner_tag_names, parse_tag_input, resolve_names
from app.web import render

router = APIRouter()


def _parse_week(week: str) -> date:
    try:
        d = date.fromisoformat(week) if week else clock.today()
    except ValueError:
        d = clock.today()
    return clock.week_start(d)


def _range_text(start: date) -> str:
    end = start + timedelta(days=6)
    return f"{clock.fmt_day(start)} – {clock.fmt_day(end)} {end.year}"


def _row_ctx(session: Session, d: date, **extra) -> dict:
    ctx = {
        "d": d,
        "entry": entries.effective_entry(d, entries.get_row(session, d)),
        "is_today": d == clock.today(),
        "state": "view",
        "picked_tags": [],
        "more_tags": "",
        "message": None,
        "warning": None,
    }
    ctx.update(extra)
    return ctx


def _row(request: Request, session: Session, d: date, **extra):
    ctx = _row_ctx(session, d, **extra)
    return render(request, "menu/_row.html", {"r": ctx, "core_tags": CORE_TAGS})


@router.get("/menu")
def week_page(request: Request, week: str = "", session: Session = Depends(get_session)):
    start = _parse_week(week)
    return render(request, "menu/week.html", {
        "start": start,
        "range_text": _range_text(start),
        "prev_week": start - timedelta(days=7),
        "next_week": start + timedelta(days=7),
        "this_week": clock.week_start(clock.today()),
        "rows": [_row_ctx(session, d) for d in clock.week_dates(start)],
        "datalist": dinner_tag_names(session),
        "core_tags": CORE_TAGS,
    })


@router.get("/menu/{d}/row")
def row(request: Request, d: date, session: Session = Depends(get_session)):
    return _row(request, session, d)


def _parse_ids(text: str) -> list[int]:
    return [int(p) for p in (text or "").split(",") if p.strip().isdigit()]


def _fmt_tags(tags: list[str]) -> str:
    return " or ".join(f"#{t}" for t in tags)


@router.post("/menu/{d}/roll")
async def roll_dish(request: Request, d: date, session: Session = Depends(get_session)):
    form = await request.form()
    tags = resolve_names(session, form.getlist("tags") + parse_tag_input(form.get("more_tags") or ""))
    keep = {
        "picked_tags": tags,
        "more_tags": " ".join(f"#{t}" for t in tags if t not in CORE_TAGS),
    }
    result = roll(session, d, tags, _parse_ids(form.get("exclude") or ""))
    if result is None:
        if tags and not matching_dinners(session, tags):
            message = f"No dinners are tagged {_fmt_tags(tags)}."
        elif tags:
            message = (
                f"No dinners tagged {_fmt_tags(tags)} available — "
                "everything matching was on the menu within 21 days."
            )
        else:
            message = "No dinners available — everything was on the menu within 21 days."
        return _row(request, session, d, message=message, **keep)
    dish, next_exclude = result
    return _row(
        request, session, d,
        state="suggest", suggestion=dish, tags=tags,
        next_exclude=",".join(str(i) for i in next_exclude), **keep,
    )


@router.post("/menu/{d}")
async def choose_dish(request: Request, d: date, session: Session = Depends(get_session)):
    form = await request.form()
    raw = (form.get("dish_id") or "").strip()
    dish = session.get(Dish, int(raw)) if raw.isdigit() else None
    if dish is None or dish.archived:
        raise HTTPException(404, "Recipe not found")
    entries.set_dish(session, d, dish.id)
    clash = conflicts(session, dish.id, d)
    warning = None
    if clash:
        warning = f"Also on the menu on {', '.join(clock.fmt_day(c) for c in clash)} (within 21 days)"
    return _row(request, session, d, warning=warning)


@router.post("/menu/{d}/takeaway")
def takeaway(request: Request, d: date, session: Session = Depends(get_session)):
    entries.set_takeaway(session, d)
    return _row(request, session, d)


@router.post("/menu/{d}/clear")
def clear(request: Request, d: date, session: Session = Depends(get_session)):
    entries.clear(session, d)
    return _row(request, session, d)


@router.get("/menu/{d}/pick")
def pick(request: Request, d: date, q: str = "", session: Session = Depends(get_session)):
    found = search_dishes(session, q)
    found.sort(key=lambda x: x.type != "dinner")  # stable: dinners first, search order kept
    return _row(
        request, session, d,
        state="pick", q=q, results=found, blocked=blocked_dish_ids(session, d),
    )
