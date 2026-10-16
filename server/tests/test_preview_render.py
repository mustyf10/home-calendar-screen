import asyncio
import io

import pytest
from PIL import Image
from starlette.datastructures import QueryParams

from app.display import render
from app.display.preview import parse_params


def _png(color: int = 255) -> bytes:
    out = io.BytesIO()
    Image.new("L", (800, 480), color).save(out, format="PNG")
    return out.getvalue()


class Rig:
    def __init__(self):
        self.t = 0.0
        self.shots: list[str] = []

    def now(self) -> float:
        return self.t

    async def shoot(self, path: str) -> bytes:
        self.shots.append(path)
        return _png()

    def frame(self, qs: str, ver: str) -> bytes:
        return asyncio.run(render.get_preview_frame(parse_params(QueryParams(qs)), ver, now=self.now, shoot=self.shoot))


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(render, "_preview_cache", {})
    return Rig()


def test_first_call_shoots_the_preview_query(rig):
    png = rig.frame("scenario=busy", "v1")
    assert rig.shots == ["/display?scenario=busy"]
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
    assert rig.frame("scenario=busy", "v2") == first
    assert len(rig.shots) == 1
    rig.t = 5
    rig.frame("scenario=busy", "v2")
    assert len(rig.shots) == 2


def test_each_query_has_its_own_entry(rig):
    rig.frame("scenario=busy", "v1")
    rig.frame("scenario=quiet", "v1")
    assert len(rig.shots) == 2


def test_cache_keeps_eight_newest(rig):
    for i in range(10):
        rig.t = i
        rig.frame(f"at=2026-10-{10 + i}T09:00", "v")
    assert len(render._preview_cache) == 8
    assert "at=2026-10-10T09%3A00" not in render._preview_cache


def test_offline_stamp_is_drawn_bottom_left(rig):
    img = Image.open(io.BytesIO(rig.frame("offline=07:30", "v1")))
    assert img.getpixel((5, 475)) == 0
    assert img.getpixel((795, 5)) == 255


def test_panel_cache_untouched(rig):
    before = dict(render._cache)
    rig.frame("scenario=busy", "v1")
    assert render._cache == before
