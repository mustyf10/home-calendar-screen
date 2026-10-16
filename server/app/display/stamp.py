"""The "Offline since HH:MM" stamp, shared by the server preview and the Pi client.

display-client/stamp.py is a symlink to this file, so keep it to the standard
library and Pillow: no app imports.
"""
from __future__ import annotations

from datetime import datetime

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 22


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
