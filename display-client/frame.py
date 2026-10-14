#!/usr/bin/env python3
"""Pull the pre-rendered kitchen frame and show it on a Waveshare 7.5" V2 e-ink panel.

Run every 5 minutes by a systemd timer. Redraws only when the server's ETag changes.
If the server is unreachable for an hour, the last good image is redrawn with an
"Offline since HH:MM" stamp so a stale panel is obvious.
"""
from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

log = logging.getLogger("frame")

SIZE = (800, 480)
OFFLINE_AFTER = timedelta(minutes=60)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 22

Window = Optional[Tuple[time, time]]


# --- pure decision logic -------------------------------------------------


def parse_quiet_hours(spec: str) -> Window:
    """'23:00-06:00' -> (start, end); 'off' -> None."""
    spec = spec.strip()
    if spec.lower() == "off":
        return None
    try:
        start, end = spec.split("-")
        return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
    except ValueError:
        raise ValueError(f"bad quiet hours {spec!r}; expected HH:MM-HH:MM or 'off'") from None


def in_quiet_hours(t: time, window: Window) -> bool:
    """Start inclusive, end exclusive; a window whose end is before its start wraps midnight."""
    if window is None:
        return False
    start, end = window
    if start <= end:
        return start <= t < end
    return t >= start or t < end


@dataclass
class State:
    etag: Optional[str]
    failing_since: Optional[datetime]
    stamped: bool
    has_last: bool


@dataclass
class Fetched:
    body: bytes
    etag: Optional[str]


@dataclass
class NotModified:
    pass


@dataclass
class FetchFailed:
    reason: str


FetchResult = Union[Fetched, NotModified, FetchFailed]


@dataclass
class Decision:
    action: str  # draw | unchanged | redraw_last | stamp | fail
    failing_since: Optional[datetime]  # value to persist
    exit_code: int


def request_etag(state: State) -> Optional[str]:
    """A stamped panel must be redrawn on recovery, so don't let the server answer 304."""
    return None if state.stamped else state.etag


def decide(now: datetime, state: State, result: FetchResult) -> Decision:
    if isinstance(result, Fetched):
        return Decision("draw", None, 0)
    if isinstance(result, NotModified):
        return Decision("redraw_last" if state.stamped else "unchanged", None, 0)
    since = state.failing_since or now
    if not state.stamped and state.has_last and now - since >= OFFLINE_AFTER:
        return Decision("stamp", since, 1)
    return Decision("fail", since, 1)


# --- state files ---------------------------------------------------------


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip() or None
    except FileNotFoundError:
        return None


def load_state(state_dir: Path) -> State:
    since = _read(state_dir / "failing_since")
    try:
        failing_since = datetime.fromisoformat(since) if since else None
    except ValueError:
        failing_since = None
    return State(
        etag=_read(state_dir / "etag"),
        failing_since=failing_since,
        stamped=(state_dir / "stamped").exists(),
        has_last=(state_dir / "last.png").exists(),
    )


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def save_last(state_dir: Path, img) -> None:
    tmp = state_dir / "last.png.tmp"
    img.save(tmp, format="PNG")
    os.replace(tmp, state_dir / "last.png")


# --- side effects --------------------------------------------------------


def fetch(url: str, etag: Optional[str], ca_file: Optional[str]) -> FetchResult:
    req = urllib.request.Request(url, headers={"If-None-Match": etag} if etag else {})
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            if resp.status != 200:
                return FetchFailed(f"HTTP {resp.status}")
            return Fetched(resp.read(), resp.headers.get("ETag"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return NotModified()
        return FetchFailed(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        return FetchFailed(str(getattr(e, "reason", e)))


def stamp(img, since: datetime):
    from PIL import ImageDraw, ImageFont

    img = img.copy()
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        try:
            font = ImageFont.load_default(size=FONT_SIZE)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    text = f"Offline since {since:%H:%M}"
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    pad = 10
    w, h = right - left + 2 * pad, bottom - top + 2 * pad
    y0 = img.height - h
    draw.rectangle((0, y0, w, img.height), fill=0)
    draw.text((pad - left, y0 + pad - top), text, font=font, fill=255)
    return img


def epd_show(img) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from waveshare_epd import epd7in5_V2

    epd = epd7in5_V2.EPD()
    epd.init()
    epd.display(epd.getbuffer(img))
    epd.sleep()


def file_show(out: str) -> Callable:
    def show(img) -> None:
        img.save(out, format="PNG")

    return show


# --- orchestration -------------------------------------------------------


@dataclass
class Options:
    url: str
    ca_file: Optional[str]
    state_dir: Path
    quiet: Window


def run(opts: Options, now: datetime, fetch: Callable, show: Callable) -> int:
    if in_quiet_hours(now.time(), opts.quiet):
        log.info("quiet hours")
        return 0

    from PIL import Image

    sd = opts.state_dir
    sd.mkdir(parents=True, exist_ok=True)
    state = load_state(sd)
    result = fetch(opts.url, request_etag(state), opts.ca_file)
    d = decide(now, state, result)

    if d.action == "draw":
        try:
            img = Image.open(BytesIO(result.body))
            img.load()
        except (OSError, Image.DecompressionBombError) as e:
            log.error("frame is not a readable image: %s", e)
            return 2
        if img.size != SIZE:
            log.error("unexpected frame size %s, expected %s", img.size, SIZE)
            return 2
        img = img.convert("1")
        show(img)
        save_last(sd, img)
        if result.etag:
            (sd / "etag").write_text(result.etag)
        else:
            _unlink(sd / "etag")
        log.info("displayed new frame (etag %s)", result.etag)
    elif d.action == "redraw_last":
        show(Image.open(sd / "last.png").convert("1"))
        log.info("back online; redrew last frame")
    elif d.action == "unchanged":
        log.info("unchanged")
    else:
        log.error("fetch failed: %s (failing since %s)", result.reason, d.failing_since.isoformat(timespec="minutes"))
        (sd / "failing_since").write_text(d.failing_since.isoformat())
        if d.action == "stamp":
            show(stamp(Image.open(sd / "last.png").convert("1"), d.failing_since))
            (sd / "stamped").write_text(now.isoformat())
            log.info("offline stamp drawn")
        return d.exit_code

    _unlink(sd / "failing_since")
    _unlink(sd / "stamped")
    return d.exit_code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=os.environ.get("FRAME_URL"), help="frame PNG URL (env FRAME_URL)")
    p.add_argument("--ca-file", default=os.environ.get("CA_FILE") or None, help="CA bundle for HTTPS (env CA_FILE)")
    p.add_argument("--state-dir", default="/var/lib/home-calendar-frame", type=Path)
    p.add_argument("--quiet-hours", default=os.environ.get("QUIET_HOURS") or "23:00-06:00",
                   help="HH:MM-HH:MM with no redraws, or 'off' (env QUIET_HOURS)")
    p.add_argument("--dry-run", action="store_true", help="save to --out instead of driving the panel")
    p.add_argument("--out", help="output PNG path for --dry-run")
    args = p.parse_args(argv)
    if not args.url:
        p.error("--url or FRAME_URL is required")
    if args.dry_run and not args.out:
        p.error("--out is required with --dry-run")
    try:
        quiet = parse_quiet_hours(args.quiet_hours)
    except ValueError as e:
        p.error(str(e))

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    opts = Options(url=args.url, ca_file=args.ca_file, state_dir=args.state_dir, quiet=quiet)
    show = file_show(args.out) if args.dry_run else epd_show
    return run(opts, datetime.now(), fetch, show)


if __name__ == "__main__":
    sys.exit(main())
