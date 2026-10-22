import asyncio
import io
from datetime import datetime

import pytest
from PIL import Image
from starlette.datastructures import QueryParams

from app.display import preview_render, render
from app.display.preview import parse_params
from app.display.geometry import FrameSize, fit_png
from app.display.stamp import stamp
from app.heavy import heavy_lock

pytestmark = pytest.mark.usefixtures("fixed_clock")


def _png(color: int = 255) -> bytes:
    out = io.BytesIO()
    Image.new("L", (800, 480), color).save(out, format="PNG")
    return out.getvalue()


class Rig:
    """Fake monotonic clock; shooting takes `render_s` and sleeping advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.render_s = 0.0
        self.shots: list[str] = []
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def shoot(self, path: str) -> bytes:
        self.shots.append(path)
        self.t += self.render_s
        return _png()

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s

    async def _frame(self, qs, ver):
        return await preview_render.get_preview_frame(
            parse_params(QueryParams(qs)), ver, now=self.now, shoot=self.shoot, sleep=self.sleep
        )

    def frame(self, qs: str, ver: str) -> tuple[bytes, str]:
        return asyncio.run(self._frame(qs, ver))


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(preview_render, "_cache", {})
    monkeypatch.setattr(preview_render, "_last_shot", None)
    return Rig()


def test_first_call_shoots_the_preview_query(rig):
    png, ver = rig.frame("scenario=busy", "v1")
    assert rig.shots == ["/display?scenario=busy"]
    assert ver == "v1"
    assert Image.open(io.BytesIO(png)).mode == "1"


def test_default_params_shoot_plain_display(rig):
    rig.frame("", "v1")
    assert rig.shots == ["/display"]


def test_same_version_is_cached(rig):
    first = rig.frame("scenario=busy", "v1")
    rig.t = 60
    assert rig.frame("scenario=busy", "v1") == first
    assert len(rig.shots) == 1


def test_new_version_is_throttled_to_five_seconds(rig):
    first = rig.frame("scenario=busy", "v1")
    rig.t = 2
    assert rig.frame("scenario=busy", "v2") == first  # old png, old version
    assert len(rig.shots) == 1
    rig.t = 5
    assert rig.frame("scenario=busy", "v2")[1] == "v2"
    assert len(rig.shots) == 2


def test_slow_render_then_version_change_catches_up(rig):
    rig.render_s = 4
    rig.frame("scenario=busy", "v1")  # finishes at t=4
    rig.t = 4.5
    assert rig.frame("scenario=busy", "v2")[1] == "v1"  # throttled: the page sees v1 and retries
    rig.t = 9
    assert rig.frame("scenario=busy", "v2")[1] == "v2"


def test_heavy_lock_serves_cached_png_with_its_old_version(rig):
    first_png, _ = rig.frame("scenario=busy", "v1")
    rig.t = 60

    async def while_locked():
        async with heavy_lock:
            return await rig._frame("scenario=busy", "v2")

    assert asyncio.run(while_locked()) == (first_png, "v1")
    assert len(rig.shots) == 1


def test_at_most_one_shoot_every_two_seconds_across_queries(rig):
    rig.frame("scenario=busy", "v1")  # shot at t=0
    rig.t = 1
    rig.frame("scenario=quiet", "v1")  # nothing cached for this query: waits out the gap
    assert rig.slept == [1] and rig.t == 2 and len(rig.shots) == 2
    rig.t = 7
    rig.frame("scenario=busy", "v2")  # shot at t=7
    rig.t = 8
    assert rig.frame("scenario=quiet", "v2")[1] == "v1"  # cached entry instead of waiting
    assert len(rig.shots) == 3 and rig.slept == [1]


def test_failed_render_still_spaces_out_the_next_attempt(rig):
    async def broken(path):
        rig.shots.append(path)
        raise RuntimeError("chromium crashed")

    rig.shoot = broken
    with pytest.raises(RuntimeError):
        rig.frame("scenario=busy", "v1")
    rig.t = 0.5
    with pytest.raises(RuntimeError):
        rig.frame("scenario=busy", "v1")  # waits out the 2 s gap before retrying
    assert rig.slept == [1.5]


def test_each_query_has_its_own_entry(rig):
    rig.frame("scenario=busy", "v1")
    rig.t = 2
    rig.frame("scenario=quiet", "v1")
    assert len(rig.shots) == 2


def test_cache_keeps_eight_newest(rig):
    for i in range(10):
        rig.t = 2 * i
        rig.frame(f"at=2026-10-{10 + i}T09:00", "v")
    assert len(preview_render._cache) == 8
    assert "at=2026-10-10T09%3A00" not in preview_render._cache


def test_offline_stamp_is_drawn_bottom_left(rig):
    png, _ = rig.frame("offline=07:30", "v1")
    img = Image.open(io.BytesIO(png))
    assert img.getpixel((5, 475)) == 0
    assert img.getpixel((795, 5)) == 255


def test_panel_cache_untouched(rig):
    before = dict(render._cache)
    rig.frame("scenario=busy", "v1")
    assert render._cache == before


def test_weather_overrides_have_isolated_preview_cache_entries_and_leave_panel_untouched(rig):
    before = dict(render._cache)
    sample = rig.frame("scenario=busy&weather=sample", "v1")
    rig.t = 2
    rig.frame("scenario=busy&weather=snow", "v1")
    rig.t = 4
    assert rig.frame("weather=sample&scenario=busy", "v1") == sample
    assert rig.shots == ["/display?scenario=busy&weather=sample", "/display?scenario=busy&weather=snow"]
    assert render._cache == before


@pytest.mark.parametrize("width,height", [(400, 240), (600, 800), (1200, 825)])
def test_target_geometry_uses_canonical_screenshot(rig, width, height):
    png, ver = rig.frame(f"width={width}&height={height}&scenario=busy&weather=snow", "v1")
    assert rig.shots == ["/display?scenario=busy&weather=snow"]
    assert ver == "v1"
    image = Image.open(io.BytesIO(png))
    assert image.size == (width, height)
    assert image.mode == "1"


def test_geometry_has_isolated_cache_entries_even_while_busy(rig):
    canonical = rig.frame("scenario=busy", "v1")
    portrait = rig.frame("scenario=busy&width=600&height=800", "v1")

    async def while_locked():
        async with heavy_lock:
            return await rig._frame("scenario=busy&width=600&height=800", "v2")

    assert asyncio.run(while_locked()) == portrait
    assert rig.frame("scenario=busy", "v1") == canonical
    assert Image.open(io.BytesIO(portrait[0])).size == (600, 800)
    assert rig.shots == ["/display?scenario=busy", "/display?scenario=busy"]


@pytest.mark.parametrize("width,height", [(400, 240), (600, 800)])
def test_preview_stamp_matches_client_stamp_after_fitting(rig, width, height):
    # The asymmetric source catches shifts and changes outside the stamped rectangle.
    source = Image.new("1", (800, 480), 1)
    source.paste(0, (30, 40, 300, 170))
    source.paste(0, (620, 300, 770, 460))
    out = io.BytesIO()
    source.save(out, format="PNG")
    canonical = out.getvalue()

    async def shoot(path):
        rig.shots.append(path)
        return canonical

    rig.shoot = shoot
    preview_png, _ = rig.frame(f"width={width}&height={height}&offline=07:30", "v1")
    fitted = Image.open(io.BytesIO(fit_png(canonical, FrameSize(width, height))))
    client_stamped = stamp(fitted, datetime(2026, 10, 16, 7, 30))
    preview_image = Image.open(io.BytesIO(preview_png))
    assert preview_image.mode == client_stamped.mode == "1"
    assert preview_image.tobytes() == client_stamped.tobytes()
