"""Validated output dimensions and fitting for the canonical display canvas."""
import io
import re
from dataclasses import dataclass

from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import QueryParams

from app.display.stamp import fit_bounds


@dataclass(frozen=True)
class FrameSize:
    width: int = 800
    height: int = 480

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        return fit_bounds(self.width, self.height)


def parse_size(query: QueryParams) -> FrameSize:
    dimensions = {}
    for name, default in (("width", 800), ("height", 480)):
        values = query.getlist(name)
        if not values:
            dimensions[name] = default
            continue
        if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
            raise HTTPException(status_code=422, detail=f"{name} must be a single integer from 1 to 8192")
        # Bound the text before integer conversion as well as image allocation.
        value = values[0].lstrip("0")
        if len(value) > 4 or not value or not 1 <= int(value) <= 8192:
            raise HTTPException(status_code=422, detail=f"{name} must be from 1 to 8192")
        dimensions[name] = int(value)
    size = FrameSize(**dimensions)
    if size.width * size.height > 16_777_216:
        raise HTTPException(status_code=422, detail="Frame must contain at most 16777216 pixels")
    return size


def fit_png(png: bytes, size: FrameSize) -> bytes:
    """Scale monochrome design pixels uniformly and letterbox in white."""
    if size == FrameSize():
        return png
    width, height, x, y = size.bounds
    with Image.open(io.BytesIO(png)) as source:
        fitted = source.convert("1", dither=Image.Dither.NONE).resize(
            (width, height), Image.Resampling.NEAREST,
        )
    canvas = Image.new("1", (size.width, size.height), 1)
    canvas.paste(fitted, (x, y))
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
