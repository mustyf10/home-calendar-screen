"""Image normalisation: dish photos, batch page scans, and re-encoding for the LLM."""
import base64
import io
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from app.config import config


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def _jpeg(img: Image.Image, max_px: int, quality: int) -> bytes:
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _write(rel: str, data: bytes) -> str:
    path = config.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def is_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def save_jpeg(data: bytes, rel: str, max_px: int, quality: int) -> str:
    """Normalise any image to an RGB JPEG at media/<rel>; returns rel."""
    return _write(rel, _jpeg(_open(data), max_px, quality))


def save_dish_image(data: bytes) -> str:
    return save_jpeg(data, f"dishes/{uuid.uuid4()}.jpg", 1200, 85)


def save_page_image(data: bytes, dest: str) -> str:
    """dest is relative to the media dir, e.g. imports/batch-3/page-01.jpg."""
    return save_jpeg(data, dest, 2000, 90)


def copy_scan(rel: str) -> str:
    """Copy a page photo to media/scans/<uuid4>.jpg so it outlives the import."""
    dest = f"scans/{uuid.uuid4()}.jpg"
    (config.media_dir / "scans").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.media_dir / rel, config.media_dir / dest)
    return dest


def llm_image_block(path: Path, max_px: int) -> dict:
    """Anthropic image content block: the image re-encoded to fit max_px at JPEG quality 85."""
    data = _jpeg(_open(path.read_bytes()), max_px, 85)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()},
    }
