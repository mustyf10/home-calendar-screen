"""Preview-only "what if" parameters shared by /display, the preview PNG and the version poll."""
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app import clock
from app.calendars.sync import sync_window
from app.clock import fmt_day
from app.display.data import DisplayModel, compose, load_inputs
from app.display.scenarios import SCENARIOS, scenario_now

_APP = Path(__file__).resolve().parent.parent
DISPLAY_TEMPLATE = _APP / "templates" / "display.html"
APP_CSS = _APP / "static" / "app.css"
_PROCESS_ID = uuid.uuid4().hex  # changes on every `uvicorn --reload` restart

FORCED_WARNINGS = {
    "no-calendars": lambda today: "No calendars set up",
    "calendar-failing": lambda today: "Calendar 'Family' not updating since 07:15",
    "backup": lambda today: f"Backup not run since {fmt_day(today - timedelta(days=3))}",
}


@dataclass(frozen=True)
class PreviewParams:
    at: datetime | None
    scenario: str
    warn: tuple[str, ...]
    offline: time | None

    def query(self) -> str:
        """Canonical query string: keys sorted, defaults and empty values omitted."""
        pairs = []
        if self.at:
            pairs.append(("at", f"{self.at:%Y-%m-%dT%H:%M}"))
        if self.offline:
            pairs.append(("offline", f"{self.offline:%H:%M}"))
        if self.scenario != "real":
            pairs.append(("scenario", self.scenario))
        pairs += [("warn", w) for w in self.warn]
        return urlencode(pairs)


def _parse(value: str | None, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(value or "", fmt)
    except ValueError:
        return None


def parse_params(query) -> PreviewParams:
    """query: a multi-dict such as Starlette's QueryParams. Invalid values fall back silently."""
    at = _parse(query.get("at"), "%Y-%m-%dT%H:%M")
    offline = _parse(query.get("offline"), "%H:%M")
    scenario = query.get("scenario")
    warn = set(query.getlist("warn"))
    return PreviewParams(
        at=at.replace(tzinfo=clock.tz()) if at else None,
        scenario=scenario if scenario in SCENARIOS else "real",
        warn=tuple(w for w in FORCED_WARNINGS if w in warn),
        offline=offline.time() if offline else None,
    )


def preview_now(params: PreviewParams) -> datetime:
    return scenario_now(params.scenario, params.at or clock.now())


def preview_model(session: Session, params: PreviewParams) -> tuple[DisplayModel, str | None]:
    """The display model for these params, plus a note when a pretend date is outside the synced data."""
    now = preview_now(params)
    if params.scenario == "real":
        inputs = load_inputs(session, now)
    else:
        inputs = SCENARIOS[params.scenario](now)
    if params.warn:
        inputs.warning = " · ".join(FORCED_WARNINGS[w](now.date()) for w in params.warn)
    note = None
    if params.scenario == "real" and params.at:
        start, end = sync_window(clock.today())
        if not start <= params.at.date() <= end:
            note = f"Calendar data only covers {fmt_day(start)}–{fmt_day(end)}"
    return compose(inputs, now), note


def version(session: Session, params: PreviewParams) -> str:
    """Changes whenever what the preview shows could change: data, params, layout files or a reload."""
    h = hashlib.sha256()
    h.update(repr(preview_model(session, params)[0]).encode())
    h.update(params.query().encode())
    for path in (DISPLAY_TEMPLATE, APP_CSS):
        h.update(str(path.stat().st_mtime_ns).encode())
    h.update(_PROCESS_ID.encode())
    return h.hexdigest()[:16]
