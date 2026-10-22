import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import QueryParams

from app.db import get_session
from app.display.geometry import FrameSize, fit_png, parse_size
from app.display.stamp import fit_bounds
from app.main import app
from app.models import Base


@pytest.fixture
def canonical_png():
    image = Image.new("1", (800, 480), 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 799, 479), outline=0, width=8)
    draw.rectangle((80, 48, 239, 143), fill=0)
    draw.rectangle((560, 288, 719, 431), fill=0)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def client():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        app.dependency_overrides[get_session] = lambda: session
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("query,expected", [
    ("", FrameSize()), ("width=400", FrameSize(400, 480)),
    ("height=800", FrameSize(800, 800)),
    ("width=600&height=800", FrameSize(600, 800)),
    ("width=8192&height=2048", FrameSize(8192, 2048)),
])
def test_parse_size(query, expected):
    assert parse_size(QueryParams(query)) == expected


INVALID = ["width=0", "width=", "width=1.5", "width=no", "width=400&width=400",
           "width=8193", "width=8192&height=8192", "height=-1", "height=", "height=2&height=3"]


@pytest.mark.parametrize("query", INVALID)
def test_invalid_geometry(query):
    with pytest.raises(HTTPException) as error:
        parse_size(QueryParams(query))
    assert error.value.status_code == 422


@pytest.mark.parametrize("size,bounds", [
    (FrameSize(400, 240), (400, 240, 0, 0)),
    (FrameSize(600, 800), (600, 360, 0, 220)),
    (FrameSize(1200, 825), (1200, 720, 0, 52)),
    (FrameSize(1, 1), (1, 1, 0, 0)),
])
def test_fit_preserves_all_content_and_white_bars(canonical_png, size, bounds):
    assert fit_bounds(size.width, size.height) == bounds
    width, height, x, y = bounds
    result = Image.open(io.BytesIO(fit_png(canonical_png, size)))
    source = Image.open(io.BytesIO(canonical_png))
    expected = Image.new("1", (size.width, size.height), 1)
    expected.paste(source.resize((width, height), Image.Resampling.NEAREST), (x, y))
    assert result.mode == "1"
    assert result.size == (size.width, size.height)
    assert result.tobytes() == expected.tobytes()
    if y:
        assert result.crop((0, 0, size.width, y)).getextrema() == (255, 255)
        assert result.crop((0, y + height, size.width, size.height)).getextrema() == (255, 255)


def test_default_png_is_byte_passthrough(canonical_png):
    assert fit_png(canonical_png, FrameSize()) is canonical_png


@pytest.mark.parametrize("query", INVALID)
def test_display_rejects_invalid_geometry(client, query):
    assert client.get("/display?" + query).status_code == 422


def test_display_fits_design_canvas(client):
    response = client.get("/display?scenario=busy&width=600&height=800")
    assert response.status_code == 200
    assert "width: 600px; height: 800px" in response.text
    assert "left: 0px; top: 220px" in response.text
    assert "transform: scale(0.75, 0.75)" in response.text
    assert "+6 more today" in response.text
    default = client.get("/display?scenario=busy")
    assert "width: 800px; height: 480px" in default.text
    assert "left: 0px; top: 0px" in default.text
    assert "transform: scale(1.0, 1.0)" in default.text


@pytest.mark.parametrize("query", INVALID)
def test_frame_validates_before_rendering(client, monkeypatch, query):
    from app.display import routes

    async def forbidden(size):
        pytest.fail("Invalid geometry must not reach the renderer")

    monkeypatch.setattr(routes, "get_sized_frame", forbidden)
    response = client.get("/display/frame.png?" + query)
    assert response.status_code == 422
    assert response.text != "render failed"


def test_mixed_geometry_conditional_responses(client, monkeypatch, canonical_png):
    from app.display import render

    async def canonical():
        return canonical_png, render.etag_for(canonical_png)

    monkeypatch.setattr(render, "get_frame", canonical)
    monkeypatch.setattr(render, "_sized_cache", {})
    default = client.get("/display/frame.png")
    small = client.get("/display/frame.png?width=400&height=240")
    portrait = client.get("/display/frame.png?width=600&height=800")
    assert default.content == canonical_png
    assert len({r.headers["etag"] for r in (default, small, portrait)}) == 3
    assert Image.open(io.BytesIO(small.content)).size == (400, 240)
    assert Image.open(io.BytesIO(portrait.content)).size == (600, 800)
    for path, etag in [
        ("/display/frame.png", default.headers["etag"]),
        ("/display/frame.png?width=400&height=240", small.headers["etag"]),
        ("/display/frame.png?width=600&height=800", portrait.headers["etag"]),
    ]:
        same = client.get(path, headers={"If-None-Match": etag})
        assert same.status_code == 304 and same.content == b""
        assert same.headers["etag"] == etag
        assert same.headers["cache-control"] == "no-cache"
        assert "x-display-policy" in same.headers
    other = client.get("/display/frame.png?width=400&height=240",
                       headers={"If-None-Match": portrait.headers["etag"]})
    assert other.status_code == 200
    assert other.content == small.content


def test_derived_cache_reuses_pixels_and_evicts_oldest(monkeypatch, canonical_png):
    import asyncio
    from app.display import render

    tag = render.etag_for(canonical_png)

    async def canonical():
        return canonical_png, tag

    calls = []
    original_fit = render.fit_png

    def fitted(png, size):
        calls.append(size)
        return original_fit(png, size)

    monkeypatch.setattr(render, "get_frame", canonical)
    monkeypatch.setattr(render, "fit_png", fitted)
    monkeypatch.setattr(render, "_sized_cache", {})

    async def exercise():
        first = await render.get_sized_frame(FrameSize(400, 240))
        assert await render.get_sized_frame(FrameSize(400, 240)) is first
        assert calls == [FrameSize(400, 240)]
        for width in range(10, 18):
            await render.get_sized_frame(FrameSize(width, 10))
        assert len(render._sized_cache) == 8
        assert (tag, 400, 240) not in render._sized_cache
        assert await render.get_sized_frame(FrameSize()) == (canonical_png, tag)
        old = render._sized_cache[(tag, 10, 10)]
        replacement = Image.new("1", (800, 480), 0)
        output = io.BytesIO()
        replacement.save(output, format="PNG")

        async def changed():
            png = output.getvalue()
            return png, render.etag_for(png)

        monkeypatch.setattr(render, "get_frame", changed)
        new = await render.get_sized_frame(FrameSize(10, 10))
        assert new != old
        assert Image.open(io.BytesIO(new[0])).getpixel((0, 2)) == 0

    asyncio.run(exercise())


def test_geometry_changes_reuse_canonical_render_during_contention(monkeypatch, canonical_png):
    import asyncio
    from app.display import render

    shots = []

    async def screenshot():
        shots.append("/display")
        return canonical_png

    monkeypatch.setattr(render, "screenshot", screenshot)
    monkeypatch.setattr(render, "_cache", {"png": None, "etag": None, "rendered_at": 0.0})
    monkeypatch.setattr(render, "_sized_cache", {})
    monkeypatch.setattr(render, "_lock", asyncio.Lock())
    monkeypatch.setattr(render, "heavy_lock", asyncio.Lock())

    async def exercise():
        await render.get_sized_frame(FrameSize(400, 240))
        await render.get_sized_frame(FrameSize(600, 800))
        assert shots == ["/display"]
        render._cache["rendered_at"] = -render.MAX_AGE_S
        async with render.heavy_lock:
            png, _ = await render.get_sized_frame(FrameSize(1200, 825))
        assert Image.open(io.BytesIO(png)).size == (1200, 825)
        assert shots == ["/display"]

    asyncio.run(exercise())
