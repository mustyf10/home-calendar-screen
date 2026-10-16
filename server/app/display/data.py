"""Everything the e-ink frame shows, computed from the DB at a given moment."""
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.clock import fmt_day, week_dates, week_start
from app.models import Calendar, CalendarEvent, MenuEntry

AGENDA_LINES = 11
TODAY_MAX_EVENTS = 9
HORIZON_DAYS = 60
CALENDAR_STALE = timedelta(hours=2)
BACKUP_STALE = timedelta(days=2)
BACKUP_GRACE = timedelta(hours=24)
_STARTED = time.monotonic()


@dataclass
class Header:
    weekday: str
    day: int
    month_year: str


@dataclass
class DayCell:
    day: int
    in_month: bool
    is_today: bool
    has_events: bool


@dataclass
class AgendaLine:
    kind: str  # heading | event | note | more
    text: str
    time_text: str = ""


@dataclass
class DinnerRow:
    label: str
    title: str | None
    is_today: bool


@dataclass
class Dinners:
    range_text: str
    rows: list[DinnerRow]


@dataclass
class DisplayModel:
    header: Header
    month_grid: list[list[DayCell]]
    agenda: list[AgendaLine]
    dinners: Dinners
    warning: str | None = None


@dataclass
class Event:
    title: str
    first: date
    last: date  # inclusive
    start: datetime | None  # None for all-day


def _span(ev: CalendarEvent, tz) -> Event:
    if ev.all_day:
        first = ev.start_date
        last = max(first, ev.end_date - timedelta(days=1)) if ev.end_date else first
        return Event(ev.summary, first, last, None)
    start = datetime.fromisoformat(ev.start_at).astimezone(tz)
    end = datetime.fromisoformat(ev.end_at).astimezone(tz) if ev.end_at else start
    last = end.date()
    if end > start and end.time() == datetime.min.time():
        last -= timedelta(days=1)  # ends at midnight: doesn't cover the next day
    return Event(ev.summary, start.date(), max(last, start.date()), start)


def _heading(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return fmt_day(d)


def _event_line(ev: Event, day: date) -> AgendaLine:
    title = ev.title
    if ev.last > ev.first:
        title += f" (until {fmt_day(ev.last)})"
    if ev.start is None or ev.start.date() < day:
        return AgendaLine("event", title, "All day")
    return AgendaLine("event", title, f"{ev.start:%H:%M}")


def _agenda(events: list[Event], today: date) -> list[AgendaLine]:
    horizon_end = today + timedelta(days=HORIZON_DAYS - 1)
    by_day: dict[date, list[Event]] = {}
    for ev in events:
        if ev.last < today or ev.first > horizon_end:
            continue
        by_day.setdefault(max(ev.first, today), []).append(ev)

    def lines_for(day: date) -> list[AgendaLine]:
        evs = by_day[day]
        # All-day (and already-running) events first, then by start time.
        evs.sort(key=lambda e: (1, e.start, e.title) if e.start and e.start.date() == day else (0, None, e.title))
        return [_event_line(e, day) for e in evs]

    lines: list[AgendaLine] = []
    if today in by_day:
        today_lines = lines_for(today)
        if 1 + len(today_lines) > AGENDA_LINES:
            hidden = len(today_lines) - TODAY_MAX_EVENTS
            return [
                AgendaLine("heading", "Today"),
                *today_lines[:TODAY_MAX_EVENTS],
                AgendaLine("more", f"+{hidden} more today"),
            ]
        lines += [AgendaLine("heading", "Today"), *today_lines]
    else:
        lines.append(AgendaLine("note", "Today · nothing planned"))

    hidden = 0
    for day in sorted(d for d in by_day if d > today):
        day_lines = lines_for(day)
        room = AGENDA_LINES - len(lines) - 1
        if room < 1:
            hidden += len(day_lines)
            continue
        lines.append(AgendaLine("heading", _heading(day, today)))
        lines += day_lines[:room]
        hidden += max(0, len(day_lines) - room)

    if hidden:
        if len(lines) >= AGENDA_LINES:
            lines.pop()
            hidden += 1
            if lines[-1].kind == "heading":
                lines.pop()
        lines.append(AgendaLine("more", f"+{hidden} more"))
    return lines


def _month_grid(today: date, events: list[Event]) -> list[list[DayCell]]:
    first = today.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=6 - last.weekday())
    marked: set[date] = set()
    for ev in events:
        d = max(ev.first, grid_start)
        while d <= min(ev.last, grid_end):
            marked.add(d)
            d += timedelta(days=1)
    weeks, d = [], grid_start
    while d <= grid_end:
        weeks.append([
            DayCell(day=(d + timedelta(days=i)).day,
                    in_month=(d + timedelta(days=i)).month == today.month,
                    is_today=d + timedelta(days=i) == today,
                    has_events=d + timedelta(days=i) in marked)
            for i in range(7)
        ])
        d += timedelta(days=7)
    return weeks


def _range_text(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start:%a} {start.day} – {end:%a} {end.day} {end:%b}"
    return f"{fmt_day(start)} – {fmt_day(end)}"


def _menu(session: Session, today: date) -> dict[date, str | None]:
    """Dinner title per date of today's menu week: a dish title, "Takeaway" or None."""
    days = week_dates(week_start(today))
    rows = {
        e.date: e
        for e in session.scalars(select(MenuEntry).where(MenuEntry.date.between(days[0], days[-1])))
    }
    menu: dict[date, str | None] = {}
    for d in days:
        entry = rows.get(d)
        if entry is None:
            menu[d] = "Takeaway" if d.weekday() == 5 else None
        elif entry.kind == "dish":
            menu[d] = entry.dish.title
        elif entry.kind == "takeaway":
            menu[d] = "Takeaway"
        else:
            menu[d] = None
    return menu


def _dinners(menu: dict[date, str | None], today: date) -> Dinners:
    days = week_dates(week_start(today))
    rows = [DinnerRow(label=f"{d:%a} {d.day}", title=menu.get(d), is_today=d == today) for d in days]
    return Dinners(range_text=_range_text(days[0], days[-1]), rows=rows)


def _utc_naive_to_local(dt: datetime, tz) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def warning(session: Session, now: datetime, uptime: timedelta | None = None) -> str | None:
    if uptime is None:
        uptime = timedelta(seconds=time.monotonic() - _STARTED)
    warnings = []
    calendars = session.scalars(select(Calendar)).all()
    if not calendars:
        warnings.append("No calendars set up")
    failing = [
        c for c in calendars
        if c.last_error_since and now - _utc_naive_to_local(c.last_error_since, now.tzinfo) > CALENDAR_STALE
    ]
    if failing:
        oldest = min(failing, key=lambda c: c.last_error_since)
        since = _utc_naive_to_local(oldest.last_error_since, now.tzinfo)
        warnings.append(f"Calendar '{oldest.name}' not updating since {since:%H:%M}")
    last_ok = settings_store.get(session, "backup_last_ok_at")
    if last_ok:
        ok_at = datetime.fromisoformat(last_ok).astimezone(now.tzinfo)
        if now - ok_at > BACKUP_STALE:
            warnings.append(f"Backup not run since {fmt_day(ok_at.date())}")
    elif uptime > BACKUP_GRACE:
        warnings.append("Backup never run")
    return " · ".join(warnings) or None


@dataclass
class DisplayInputs:
    events: list[Event]
    menu: dict[date, str | None]
    warning: str | None


def load_inputs(session: Session, now: datetime, uptime: timedelta | None = None) -> DisplayInputs:
    return DisplayInputs(
        events=[_span(ev, now.tzinfo) for ev in session.scalars(select(CalendarEvent))],
        menu=_menu(session, now.date()),
        warning=warning(session, now, uptime),
    )


def compose(inputs: DisplayInputs, now: datetime) -> DisplayModel:
    today = now.date()
    return DisplayModel(
        header=Header(weekday=f"{today:%A}".upper(), day=today.day, month_year=f"{today:%B %Y}"),
        month_grid=_month_grid(today, inputs.events),
        agenda=_agenda(inputs.events, today),
        dinners=_dinners(inputs.menu, today),
        warning=inputs.warning,
    )
