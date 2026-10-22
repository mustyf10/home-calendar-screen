from datetime import datetime
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.display.geometry import FrameSize, fit_png
from app.display.stamp import FONT_PATH, FONT_SIZE, fit_bounds, stamp


SINCE = datetime(2026, 10, 22, 7, 30)


def canonical_image():
    image = Image.new("1", (800, 480), 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 143, 200), fill=0)
    draw.rectangle((319, 321, 799, 479), fill=0)
    return image


def fitted(image, width, height):
    output = BytesIO()
    image.save(output, format="PNG")
    result = Image.open(BytesIO(fit_png(output.getvalue(), FrameSize(width, height))))
    result.load()
    return result


def test_canonical_stamp_preserves_original_pixels():
    image = canonical_image()
    original = image.tobytes()
    expected = image.copy()
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        try:
            font = ImageFont.load_default(size=FONT_SIZE)
        except TypeError:
            font = ImageFont.load_default()
    text = f"Offline since {SINCE:%H:%M}"
    draw = ImageDraw.Draw(expected)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width, height = right - left + 20, bottom - top + 20
    y = expected.height - height
    draw.rectangle((0, y, width, expected.height), fill=0)
    draw.text((10 - left, y + 10 - top), text, font=font, fill=255)

    actual = stamp(image, SINCE)
    assert actual.mode == "1"
    assert actual.tobytes() == expected.tobytes()
    assert image.tobytes() == original


@pytest.mark.parametrize("width,height", [(400, 240), (600, 800), (1200, 825), (1, 1)])
def test_stamp_before_and_after_fit_are_identical(width, height):
    image = canonical_image()
    preview = fitted(stamp(image, SINCE), width, height)
    client = stamp(fitted(image, width, height), SINCE)
    assert client.mode == "1"
    assert client.size == (width, height)
    assert client.tobytes() == preview.tobytes()
    fitted_width, fitted_height, x, y = fit_bounds(width, height)
    if y:
        assert client.crop((0, 0, width, y)).getextrema() == (255, 255)
        assert client.crop((0, y + fitted_height, width, height)).getextrema() == (255, 255)
    if x:
        assert client.crop((0, 0, x, height)).getextrema() == (255, 255)
        assert client.crop((x + fitted_width, 0, width, height)).getextrema() == (255, 255)
