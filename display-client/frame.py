#!/usr/bin/env python3
"""Fetch the kitchen frame every five minutes; Settings controls the night pause.

Refresh spacing and optional cached-frame maintenance follow the selected adapter.
Use a separate state directory for each physical screen.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import logging
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stamp import stamp  # noqa: E402
from adapters import Adapter, PanelSpec, POLL_INTERVAL, validate_size  # noqa: E402

log = logging.getLogger("frame")
OFFLINE_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class Policy:
    night_enabled: bool
    night_start: str
    night_end: str
    timezone: str

    @classmethod
    def parse(cls, value: str | None) -> Policy | None:
        try:
            data = json.loads(value)
            if not isinstance(data, dict) or type(data.get("night_enabled")) is not bool:
                return None
            for key in ("night_start", "night_end"):
                if not isinstance(data.get(key), str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", data[key]):
                    return None
                time.fromisoformat(data[key])
            if data["night_enabled"] and data["night_start"] == data["night_end"]:
                return None
            ZoneInfo(data["timezone"])
            return cls(**{key: data[key] for key in cls.__dataclass_fields__})
        except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError):
            return None

    def paused(self, now: datetime) -> bool:
        if not self.night_enabled:
            return False
        local = now.astimezone(ZoneInfo(self.timezone)).strftime("%H:%M")
        if self.night_start < self.night_end:
            return self.night_start <= local < self.night_end
        return local >= self.night_start or local < self.night_end

    def preventive(self, now: datetime, completed: datetime | None,
                   max_interval: timedelta | None) -> bool:
        soon = now + POLL_INTERVAL
        if max_interval is None or completed is None or self.paused(now) or not self.paused(soon):
            return False
        # Walk in UTC: a DST fold or gap must not change elapsed-time safety.
        end = soon.replace(second=0, microsecond=0)
        while self.paused(end):
            end += timedelta(minutes=1)
        return completed + max_interval <= end


@dataclass
class Fetched:
    body: bytes
    etag: str | None
    policy: Policy | None = None


@dataclass
class NotModified:
    policy: Policy | None = None


@dataclass
class FetchFailed:
    reason: str
    policy: Policy | None = None


FetchResult = Fetched | NotModified | FetchFailed


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock and persisted timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass
class State:
    etag: str | None = None
    failing_since: datetime | None = None
    stamped: bool = False
    policy: Policy | None = None
    last_attempt: datetime | None = None
    last_completed: datetime | None = None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def load_state(state_dir: Path) -> State:
    raw = _read(state_dir / "state.json")
    if raw is None and not (state_dir / "state.json").exists():
        # One-time migration of the previous client's visible image metadata.
        since = _read(state_dir / "failing_since")
        old_since = datetime.fromisoformat(since) if since else None
        if old_since is not None and old_since.tzinfo is None:
            old_since = old_since.astimezone()
        return State(etag=_read(state_dir / "etag"), failing_since=old_since,
                     stamped=(state_dir / "stamped").exists())
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(State.__dataclass_fields__):
        raise ValueError("invalid persisted refresh state; refusing an untracked draw")
    for key in ("failing_since", "last_attempt", "last_completed"):
        data[key] = utc(datetime.fromisoformat(data[key])) if data[key] is not None else None
    data["policy"] = Policy.parse(json.dumps(data.get("policy")))
    return State(**data)


def save_state(state_dir: Path, state: State) -> None:
    data = asdict(state)
    for key in ("failing_since", "last_attempt", "last_completed"):
        data[key] = data[key].isoformat() if data[key] else None
    tmp = state_dir / "state.json.tmp"
    with tmp.open("w") as out:
        json.dump(data, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, state_dir / "state.json")
    fd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    for name in ("etag", "failing_since", "stamped"):
        (state_dir / name).unlink(missing_ok=True)


def save_last(state_dir: Path, img) -> None:
    tmp = state_dir / "last.png.tmp"
    with tmp.open("wb") as out:
        img.save(out, format="PNG")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, state_dir / "last.png")


def fetch(url: str, etag: str | None, ca_file: str | None) -> FetchResult:
    req = urllib.request.Request(url, headers={"If-None-Match": etag} if etag else {})
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
    policy = None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            policy = Policy.parse(resp.headers.get("X-Display-Policy"))
            if resp.status != 200:
                return FetchFailed(f"HTTP {resp.status}", policy)
            return Fetched(resp.read(), resp.headers.get("ETag"), policy)
    except urllib.error.HTTPError as e:
        policy = Policy.parse(e.headers.get("X-Display-Policy")) if e.headers else None
        if e.code == 304:
            return NotModified(policy)
        return FetchFailed(f"HTTP {e.code}", policy)
    except (urllib.error.URLError, OSError, HTTPException) as e:
        return FetchFailed(str(getattr(e, "reason", e)), policy)


def load_adapter(driver: str) -> Adapter:
    try:
        module, factory = driver.split(":")
        adapter = getattr(importlib.import_module(module), factory)()
        if not isinstance(adapter.spec, PanelSpec):
            raise ValueError("adapter.spec must be a PanelSpec")
        adapter.spec.__post_init__()
        if not callable(adapter.show):
            raise ValueError("adapter.show must be callable")
        return adapter
    except Exception as exc:
        raise ValueError(f"invalid driver {driver!r}: {exc}") from exc


def sized_url(url: str, size: tuple[int, int]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    for name, expected in zip(("width", "height"), size):
        values = [value for key, value in query if key == name]
        if values and (len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]) or
                       int(values[0]) != expected):
            raise ValueError(f"URL {name} must occur once and match {expected}")
    query = [(key, value) for key, value in query if key not in ("width", "height")]
    query.extend(zip(("width", "height"), map(str, size)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def file_show(out: str) -> Callable:
    def show(img) -> None:
        img.save(out, format="PNG")
    return show


@dataclass
class Options:
    url: str
    ca_file: str | None
    state_dir: Path
    panel: PanelSpec


def run(opts: Options, clock: Callable[[], datetime], fetch: Callable, show: Callable) -> int:
    from PIL import Image

    sd = opts.state_dir
    sd.mkdir(parents=True, exist_ok=True)
    # Hold the lock through networking, decisions, driver calls and persistence.
    with (sd / "refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(sd)
        cached = None
        try:
            with Image.open(sd / "last.png") as image:
                if image.size == opts.panel.size:
                    cached = image.convert("1")
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
        result = fetch(opts.url, None if state.stamped or cached is None else state.etag, opts.ca_file)
        now = utc(clock())  # Never measure the guard before a potentially slow HTTP call.
        if result.policy is not None:
            state.policy = result.policy
        clean = None
        invalid = False
        if isinstance(result, Fetched):
            try:
                clean = Image.open(BytesIO(result.body))
                if clean.size != opts.panel.size:
                    raise ValueError(f"unexpected frame size {clean.size}, expected {opts.panel.size}")
                clean = clean.convert("1")
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                log.error("frame is not a readable image: %s", e)
                result = FetchFailed(str(e), result.policy)
                invalid = True
                clean = None
        failed = isinstance(result, FetchFailed)
        exit_code = 2 if invalid else int(failed)
        if failed:
            state.failing_since = state.failing_since or now
            log.error("fetch failed: %s", result.reason)
        elif not state.stamped:
            state.failing_since = None
        has_last = cached is not None
        # Legacy caches have no physical history. Observe a full guard interval
        # before the first safety redraw rather than guessing when they were drawn.
        if has_last and state.last_attempt is None and state.last_completed is None:
            state.last_attempt = now
        maintenance = bool(has_last and opts.panel.maintenance_after is not None and
                           (state.last_completed is None or
                            now - state.last_completed >= opts.panel.maintenance_after))
        preventive = bool(has_last and state.policy and
                          state.policy.preventive(now, state.last_completed, opts.panel.max_interval))
        wants_stamp = bool(failed and has_last and not state.stamped and
                           now - state.failing_since >= OFFLINE_AFTER)
        needs_draw = (isinstance(result, Fetched) or
                      (not failed and state.stamped and has_last) or
                      wants_stamp or maintenance or preventive)
        history = [t for t in (state.last_attempt, state.last_completed) if t is not None]
        allowed = not history or now - max(history) >= opts.panel.min_interval
        scheduled = bool(state.policy and not state.policy.paused(now))
        # Only cached safety maintenance may draw before learning a valid policy.
        if not needs_draw or not allowed or not (scheduled or maintenance):
            save_state(sd, state)
            return exit_code
        if state.policy is None and isinstance(result, Fetched):
            clean = None
            result = NotModified()  # Only redraw the cached image without a known policy.
        if clean is None:
            if not has_last:
                save_state(sd, state)
                return exit_code
            clean = cached
        offline = failed and (state.stamped or wants_stamp)
        stamp_time = state.failing_since
        if stamp_time is not None and state.policy is not None:
            stamp_time = stamp_time.astimezone(ZoneInfo(state.policy.timezone))
        visible = stamp(clean, stamp_time) if offline else clean
        state.last_attempt = now
        # Conservatively retain warning state if the driver is interrupted.
        if offline:
            state.stamped = True
        save_state(sd, state)  # A failed write must stop us BEFORE invoking hardware.
        show(visible)
        completed = utc(clock())
        state.last_completed = completed
        if isinstance(result, Fetched):
            save_last(sd, clean)
            state.etag = result.etag
        if not failed:
            state.stamped = False
            state.failing_since = None
        save_state(sd, state)
        log.info("displayed %s frame", "offline" if offline else "clean")
        return exit_code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=os.environ.get("FRAME_URL"), help="frame PNG URL (env FRAME_URL)")
    p.add_argument("--ca-file", default=os.environ.get("CA_FILE") or None, help="CA bundle for HTTPS (env CA_FILE)")
    p.add_argument("--state-dir", default="/var/lib/home-calendar-frame", type=Path)
    p.add_argument("--dry-run", action="store_true", help="save to --out instead of driving the panel")
    p.add_argument("--out", help="output PNG path for --dry-run")
    p.add_argument("--driver", default=os.environ.get("FRAME_DRIVER", "adapters.waveshare7in5_v2:create"),
                   help="trusted local module:factory adapter (env FRAME_DRIVER)")
    p.add_argument("--width", default=os.environ.get("FRAME_WIDTH"), type=int)
    p.add_argument("--height", default=os.environ.get("FRAME_HEIGHT"), type=int)
    args = p.parse_args(argv)
    if not args.url:
        p.error("--url or FRAME_URL is required")
    if args.dry_run and not args.out:
        p.error("--out is required with --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.dry_run:
            size = (args.width if args.width is not None else 800,
                    args.height if args.height is not None else 480)
            panel = PanelSpec(size, timedelta(seconds=180),
                              timedelta(hours=23, minutes=45), timedelta(hours=24))
            show = file_show(args.out)
        else:
            adapter = load_adapter(args.driver)
            panel = adapter.spec
            size = (args.width if args.width is not None else panel.size[0],
                    args.height if args.height is not None else panel.size[1])
            validate_size(size)
            if size != panel.size:
                raise ValueError("explicit dimensions must match the selected adapter")
            show = adapter.show
        url = sized_url(args.url, size)
    except ValueError as exc:
        p.error(str(exc))
    opts = Options(url=url, ca_file=args.ca_file, state_dir=args.state_dir, panel=panel)
    return run(opts, lambda: datetime.now(timezone.utc), fetch, show)


if __name__ == "__main__":
    sys.exit(main())
