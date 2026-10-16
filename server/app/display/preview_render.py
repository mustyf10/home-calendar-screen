"""1-bit PNGs for the preview page, cached apart from the panel's frame and ETag."""
import asyncio
import io
import time
from dataclasses import dataclass
from datetime import datetime

from PIL import Image

from app.display.preview import PreviewParams, preview_now
from app.display.render import screenshot, to_one_bit
from app.display.stamp import stamp
from app.heavy import heavy_lock

MIN_INTERVAL_S = 5  # per query: between a render finishing and the next one for a new version
GLOBAL_MIN_INTERVAL_S = 2  # across all queries: between any two renders
CACHE_SIZE = 8


@dataclass
class _Entry:
    ver: str  # the version the png was rendered for
    png: bytes
    at: float  # monotonic time the render finished


_lock = asyncio.Lock()
_cache: dict[str, _Entry] = {}  # params.query() -> entry
_last_shot: float | None = None


def _with_stamp(png: bytes, since: datetime) -> bytes:
    out = io.BytesIO()
    stamp(Image.open(io.BytesIO(png)), since).save(out, format="PNG")
    return out.getvalue()


async def get_preview_frame(
    params: PreviewParams, ver: str, *, now=time.monotonic, shoot=screenshot, sleep=asyncio.sleep
) -> tuple[bytes, str]:
    """Return (png, rendered_ver) for these params.

    Re-renders only when ver changed, at most every 5 s per query and every 2 s overall.
    Otherwise the cached png comes back with the older version it was rendered for, so
    the page can tell it is stale and ask again.
    """
    global _last_shot
    key = params.query()
    async with _lock:
        hit = _cache.get(key)
        if hit and (hit.ver == ver or now() - hit.at < MIN_INTERVAL_S or heavy_lock.locked()):
            return hit.png, hit.ver
        if _last_shot is not None and (wait := _last_shot + GLOBAL_MIN_INTERVAL_S - now()) > 0:
            if hit:
                return hit.png, hit.ver
            await sleep(wait)
        try:
            png = to_one_bit(await shoot(f"/display?{key}" if key else "/display"))
        finally:
            _last_shot = now()  # failed renders count too, so retries can't relaunch Chromium every second
        if params.offline:
            png = _with_stamp(png, datetime.combine(preview_now(params).date(), params.offline))
        _cache[key] = _Entry(ver, png, _last_shot)
        while len(_cache) > CACHE_SIZE:
            del _cache[min(_cache, key=lambda k: _cache[k].at)]
        return png, ver
