"""Screenshot /display with headless Chromium and reduce it to a 1-bit PNG."""
import asyncio
import hashlib
import io
import time

from PIL import Image
from playwright.async_api import async_playwright

from app.config import config
from app.heavy import heavy_lock

WIDTH, HEIGHT = 800, 480
MAX_AGE_S = 60

_lock = asyncio.Lock()
_cache: dict = {"png": None, "etag": None, "rendered_at": 0.0}


def to_one_bit(screenshot: bytes) -> bytes:
    img = Image.open(io.BytesIO(screenshot))
    bw = img.convert("L").point(lambda p: 255 if p >= 128 else 0).convert("1", dither=Image.Dither.NONE)
    out = io.BytesIO()
    bw.save(out, format="PNG")
    return out.getvalue()


def etag_for(png: bytes) -> str:
    return '"' + hashlib.sha256(png).hexdigest()[:32] + '"'


async def _screenshot() -> bytes:
    async with heavy_lock, async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
            await page.goto(f"{config.base_url_internal}/display", wait_until="networkidle")
            return await page.screenshot(type="png")
        finally:
            await browser.close()


async def get_frame() -> tuple[bytes, str]:
    """Return (png, etag), re-rendering at most once a minute.

    While a transcription holds the heavy lock, a cached frame is served as-is
    rather than making the e-ink client wait behind it.
    """
    async with _lock:
        stale = _cache["png"] is None or time.monotonic() - _cache["rendered_at"] > MAX_AGE_S
        if stale and not (_cache["png"] is not None and heavy_lock.locked()):
            png = to_one_bit(await _screenshot())
            _cache.update(png=png, etag=etag_for(png), rendered_at=time.monotonic())
        return _cache["png"], _cache["etag"]
