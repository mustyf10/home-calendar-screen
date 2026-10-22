"""The "Offline since HH:MM" stamp, shared by the server preview and the Pi client.

display-client/stamp.py is a symlink to this file, so keep it to the standard
library and Pillow: no app imports.
"""
from __future__ import annotations

from datetime import datetime

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 22


def fit_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    """Fit the 800×480 design canvas inside the target without cropping."""
    if width * 480 <= height * 800:
        fitted_width, fitted_height = width, max(1, width * 480 // 800)
    else:
        fitted_width, fitted_height = max(1, height * 800 // 480), height
    return (
        fitted_width, fitted_height,
        (width - fitted_width) // 2, (height - fitted_height) // 2,
    )


def stamp(img, since: datetime):
    from PIL import Image, ImageDraw, ImageFont

    overlay = Image.new("1", (800, 480), 1)
    mask = Image.new("1", (800, 480), 0)
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        try:
            font = ImageFont.load_default(size=FONT_SIZE)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    text = f"Offline since {since:%H:%M}"
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    pad = 10
    w, h = right - left + 2 * pad, bottom - top + 2 * pad
    y0 = 480 - h
    draw.rectangle((0, y0, w, 480), fill=0)
    draw.text((pad - left, y0 + pad - top), text, font=font, fill=255)
    ImageDraw.Draw(mask).rectangle((0, y0, w, 480), fill=1)
    fitted_width, fitted_height, x, y = fit_bounds(*img.size)
    if (fitted_width, fitted_height) != overlay.size:
        overlay = overlay.resize((fitted_width, fitted_height), Image.Resampling.NEAREST)
        mask = mask.resize((fitted_width, fitted_height), Image.Resampling.NEAREST)
    img = img.copy()
    img.paste(overlay, (x, y), mask)
    return img
