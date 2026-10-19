"""Screenshot/photo uploads attached to an import job (Add a recipe, retries, draft retries)."""
import re

from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.config import config
from app.ingest import images
from app.models import IngestJob

MB = 1024 * 1024
MAX_IMPORT_FILES, MAX_IMPORT_BYTES = 10, 10 * MB


def files(form, name: str = "images") -> list[UploadFile]:
    """Uploaded files, ignoring the empty part browsers send when nothing was chosen."""
    return [f for f in form.getlist(name) if isinstance(f, UploadFile) and f.filename]


async def read_images(uploads: list[UploadFile], max_bytes: int = MAX_IMPORT_BYTES,
                      max_files: int = MAX_IMPORT_FILES) -> tuple[list[tuple[str, bytes]], str | None]:
    """Returns ([(filename, data)], error) — e.g. '<filename> is too large or not an image.'"""
    if len(uploads) > max_files:
        return [], f"Up to {max_files} images at a time."
    out = []
    for f in uploads:
        bad = f"{f.filename} is too large or not an image."
        if (f.size or 0) > max_bytes or (f.content_type and not f.content_type.startswith("image/")):
            return [], bad
        data = await f.read()
        if len(data) > max_bytes or not await run_in_threadpool(images.is_image, data):
            return [], bad
        out.append((f.filename, data))
    return out, None


def _ext(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else "jpg"


def save_uploads(job: IngestJob, uploads: list[tuple[str, bytes]]) -> list[str]:
    """Writes media/imports/<job_id>/upload-N.<ext>, numbering after any existing uploads; job needs an id."""
    paths = []
    for n, (filename, data) in enumerate(uploads, start=len(job.upload_paths) + 1):
        rel = f"imports/{job.id}/upload-{n}.{_ext(filename)}"
        path = config.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        paths.append(rel)
    return paths


def append_to_job(job: IngestJob, text: str, uploads: list[tuple[str, bytes]]) -> None:
    """Adds retry material and puts the job back in the queue."""
    if text:
        job.user_text = f"{job.user_text}\n\n{text}".strip()
    if uploads:
        job.upload_paths = job.upload_paths + save_uploads(job, uploads)
    job.status, job.error = "queued", None
