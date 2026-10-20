"""Council discovery, isolated scraper calls and last-good collection storage."""
import asyncio
import functools
import importlib.resources
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from markupsafe import Markup, escape

from app import clock, settings_store
from app.db import SessionLocal
from app.errors import UserFacingError, log_failure, user_message

log = logging.getLogger(__name__)
FAILED = "Couldn't get bin dates"
REFRESH_S = 12 * 3600
RETRY_S = 3600
FETCH_TIMEOUT_S = 90
STALE = timedelta(days=2)
POSTCODES_URL = "https://api.postcodes.io/postcodes/"
FIND_UPRN_URL = "https://www.findmyaddress.co.uk/search"
SERVER_DIR = Path(__file__).resolve().parents[2]

TIMEOUT = "The council's website took too long to answer. Try again later."
NO_ANSWER = "The council's website didn't give any bin dates. Check the address, or try again later."
NO_DATES = "The council didn't list any upcoming bin dates."
POSTCODE_UNKNOWN = "That postcode wasn't found. Check it and try again."
POSTCODE_FAILED = "Couldn't check the postcode. Try again in a minute."


class BinsError(UserFacingError):
    """A council or postcode failure safe to show in Settings."""


@functools.cache
def all_councils() -> dict[str, dict]:
    return json.loads(importlib.resources.files("uk_bin_collection").joinpath("tests/input.json").read_text())


def browser_only(entry) -> bool:
    return "web_driver" in entry


def needs(entry) -> list[str]:
    fields = [field for field in ("uprn", "postcode") if field in entry]
    if "house_number" in entry or "paon" in entry:
        fields.append("house_number")
    return fields


def usable(entry) -> bool:
    return bool(
        entry.get("url")
        and not any(key in entry for key in ("web_driver", "wiki_command_url_override", "usrn", "supported_councils"))
        and not entry.get("custom_component_show_url_field")
        and needs(entry)
    )


@functools.cache
def councils() -> dict[str, dict]:
    return dict(sorted(
        ((module, entry) for module, entry in all_councils().items() if usable(entry)),
        key=lambda item: item[1]["wiki_name"],
    ))


def note_html(entry) -> Markup:
    text = escape(entry.get("wiki_note", ""))
    return Markup(re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" rel="noreferrer" target="_blank">\1</a>',
        str(text),
    ))


def lookup_postcode(postcode: str, *, transport=None) -> dict:
    try:
        with httpx.Client(timeout=10, transport=transport) as client:
            response = client.get(POSTCODES_URL + quote(postcode, safe=""))
        if response.status_code == 404:
            raise BinsError(POSTCODE_UNKNOWN)
        if response.status_code != 200:
            raise BinsError(POSTCODE_FAILED, detail=str(response.status_code))
        result = response.json()["result"]
        return {"postcode": result["postcode"], "district": result["admin_district"], "code": result["codes"]["admin_district"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # HTTP exception strings can include the postcode in their request URL.
        raise BinsError(POSTCODE_FAILED, detail=type(exc).__name__) from None


def council_for(place: dict) -> tuple[str | None, dict | None]:
    matches = [(module, entry) for module, entry in all_councils().items() if entry.get("LAD24CD") == place["code"]]
    if not matches:
        matches = [(module, entry) for module, entry in all_councils().items() if entry.get("wiki_name", "").casefold() == place["district"].casefold()]
    available = [(module, entry) for module, entry in matches if usable(entry)]
    if available:
        preferred = next((module for module, entry in available if entry["wiki_name"].casefold() == place["district"].casefold()), available[0][0])
        return preferred, None
    return None, next((entry for _, entry in matches if browser_only(entry)), None)


def args_for(cfg) -> list[str]:
    entry = councils()[cfg["council"]]
    args = [cfg["council"], entry["url"]]
    if entry.get("skip_get_url"):
        args.append("-s")
    flags = {"uprn": "-u=", "postcode": "-p=", "house_number": "-n="}
    args.extend(flags[field] + cfg[field] for field in needs(entry))
    return args


def parse_result(stdout) -> list[dict]:
    try:
        bins = json.loads(stdout)["bins"]
        if not isinstance(bins, list):
            raise TypeError
        rows = set()
        for item in bins:
            if not isinstance(item, dict):
                continue
            bin_type = str(item.get("type") or "").strip()
            try:
                day = datetime.strptime(item.get("collectionDate"), "%d/%m/%Y").date()
            except (ValueError, TypeError):
                continue
            if bin_type:
                rows.add((day, bin_type))
        if not rows:
            raise ValueError
        return [{"type": bin_type, "date": day.isoformat()} for day, bin_type in sorted(rows)]
    except (ValueError, KeyError, TypeError):
        raise BinsError(NO_DATES) from None


def _tail(text, cfg):
    line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    values = sorted((cfg.get(field, "") for field in ("uprn", "postcode", "house_number")), key=len, reverse=True)
    for value in values:
        if value:
            line = line.replace(value, "…")
    return line[:300]


def fetch_collections(cfg, *, run=subprocess.run) -> list[dict]:
    env = {key: value for key, value in os.environ.items() if key not in ("ANTHROPIC_API_KEY", "WEATHER_API_KEY")}
    try:
        proc = run(
            [sys.executable, "-m", "app.bins.collect", *args_for(cfg)],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, cwd=SERVER_DIR, env=env,
        )
    except subprocess.TimeoutExpired:
        raise BinsError(TIMEOUT, detail="timed out") from None
    if proc.returncode:
        raise BinsError(NO_ANSWER, detail=_tail(proc.stderr, cfg))
    return parse_result(proc.stdout)


def _load(session, key, default):
    try:
        return json.loads(settings_store.get(session, key) or "null") or default
    except (ValueError, TypeError):
        return default


def load_config(session) -> dict | None:
    cfg = _load(session, "bins.config", None)
    return cfg if isinstance(cfg, dict) and cfg.get("council") in councils() else None


def load_status(session) -> dict:
    defaults = {"last_ok_at": None, "last_error": None, "last_error_since": None}
    status = _load(session, "bins.status", {})
    return {**defaults, **status} if isinstance(status, dict) else defaults


def collections_by_day(session) -> dict[date, list[str]]:
    if load_config(session) is None:
        return {}
    days = {}
    for row in _load(session, "bins.collections", []):
        days.setdefault(date.fromisoformat(row["date"]), []).append(row["type"])
    return days


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store(session, cfg, rows):
    settings_store.put(session, "bins.config", json.dumps(cfg))
    settings_store.put(session, "bins.collections", json.dumps(rows))
    settings_store.put(session, "bins.status", json.dumps({"last_ok_at": _now(), "last_error": None, "last_error_since": None}))


def record_failure(session, message):
    status = load_status(session)
    status["last_error"] = message
    if status["last_error_since"] is None:
        status["last_error_since"] = _now()
    settings_store.put(session, "bins.status", json.dumps(status))


def clear(session):
    for key in ("bins.config", "bins.collections", "bins.status"):
        settings_store.delete(session, key)


def refresh(session) -> bool:
    cfg = load_config(session)
    if cfg is None:
        return True
    session.rollback()  # Do not hold a SQLite read transaction during a scraper call.
    try:
        rows = fetch_collections(cfg)
    except Exception as exc:
        log_failure(log, f"Bin dates from {cfg['council']} failed", exc)
        if load_config(session) != cfg:
            session.rollback()
            return True
        record_failure(session, user_message(FAILED, exc))
        session.commit()
        return False
    if load_config(session) != cfg:
        session.rollback()
        return True
    store(session, cfg, rows)
    session.commit()
    return True


def _refresh_now() -> bool:
    with SessionLocal() as session:
        return refresh(session)


async def run_bins_loop():
    while True:
        ok = False
        try:
            ok = await asyncio.to_thread(_refresh_now)
        except Exception:
            log.exception("Bins loop failed")
        await asyncio.sleep(REFRESH_S if ok else RETRY_S)


def _when(iso):
    if iso is None:
        return ""
    dt = datetime.fromisoformat(iso).astimezone(clock.tz())
    return f"{clock.fmt_day(dt.date())} {dt:%H:%M}"


def settings_context(session) -> dict:
    cfg = load_config(session)
    entry = councils()[cfg["council"]] if cfg else None
    status = load_status(session)
    return {
        "form": {field: cfg.get(field, "") if cfg else "" for field in ("postcode", "house_number", "uprn", "council")},
        "configured": cfg is not None,
        "council_name": entry["wiki_name"] if entry else None,
        "need_uprn": bool(entry and "uprn" in needs(entry)),
        "note": note_html(entry) if entry else None,
        "find_uprn_url": FIND_UPRN_URL,
        "choose_council": None,
        "upcoming": [(clock.fmt_day(day), ", ".join(types)) for day, types in collections_by_day(session).items() if day >= clock.today()],
        "last_ok": _when(status["last_ok_at"]),
        "last_error": status["last_error"],
        "error": None,
    }
