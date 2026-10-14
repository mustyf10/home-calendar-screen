"""Import pages: review queue, single import, handwritten batches, job review and save."""
import re
import shutil
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.config import config
from app.db import get_session
from app.dishes.forms import DishInput, apply, core_tag_warning, duplicate_url_dish, similar_dishes, validate
from app.ingest import images
from app.ingest.urls import platform_for
from app.models import Dish, ImportBatch, IngestJob
from app.web import render

router = APIRouter()

MB = 1024 * 1024
MAX_IMPORT_FILES, MAX_IMPORT_BYTES = 10, 10 * MB
MAX_BATCH_FILES, MAX_BATCH_BYTES = 40, 15 * MB
ACTIVE = ("queued", "fetching", "transcribing", "extracting")
STATUS_TEXT = {
    "queued": "Waiting in the queue…",
    "fetching": "Fetching the link…",
    "transcribing": "Transcribing the video…",
    "extracting": "Reading the recipe…",
    "grouping": "Sorting the pages into recipes…",
    "ready": "Ready to review",
    "failed": "Failed",
    "discarded": "Discarded",
    "done": "Done",
}
DUP_NOTE = re.compile(r"^Already saved as dish #(\d+)$")


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _job_or_404(session: Session, job_id: int) -> IngestJob:
    job = session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(404, "Import not found")
    return job


def _batch_or_404(session: Session, batch_id: int) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    return batch


def _files(form, name: str = "images") -> list[UploadFile]:
    """Uploaded files, ignoring the empty part browsers send when nothing was chosen."""
    return [f for f in form.getlist(name) if isinstance(f, UploadFile) and f.filename]


async def _read_images(files: list[UploadFile], max_bytes: int) -> tuple[list[tuple[str, bytes]], str | None]:
    """Returns ([(filename, data)], error) — error is '<filename> is too large or not an image.'"""
    out = []
    for f in files:
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


def _save_uploads(job: IngestJob, files: list[tuple[str, bytes]]) -> list[str]:
    """Writes media/imports/<job_id>/upload-N.<ext>, numbering after any existing uploads."""
    paths = []
    start = len(job.upload_paths) + 1
    for n, (filename, data) in enumerate(files, start=start):
        rel = f"imports/{job.id}/upload-{n}.{_ext(filename)}"
        path = config.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        paths.append(rel)
    return paths


def _source_label(job: IngestJob) -> str:
    if job.batch_id:
        return f"Paper batch #{job.batch_id}"
    if job.source_url:
        return (urlsplit(job.source_url).hostname or job.source_url).removeprefix("www.")
    return "Upload"


# --- Queue -------------------------------------------------------------------

@router.get("/imports")
def queue(request: Request, session: Session = Depends(get_session)):
    batches = session.scalars(
        select(ImportBatch).where(ImportBatch.status != "done").order_by(ImportBatch.created_at.desc())
    ).all()
    drafts = session.scalars(
        select(IngestJob)
        .where(IngestJob.status == "ready", IngestJob.dish_id.is_(None))
        .order_by(IngestJob.created_at.desc(), IngestJob.id.desc())
    ).all()
    in_progress = session.scalars(
        select(IngestJob).where(IngestJob.status.in_(ACTIVE)).order_by(IngestJob.created_at)
    ).all()
    failed = session.scalars(
        select(IngestJob).where(IngestJob.status == "failed").order_by(IngestJob.updated_at.desc())
    ).all()
    return render(request, "ingest/queue.html", {
        "batches": batches, "drafts": drafts, "in_progress": in_progress, "failed": failed,
        "source_label": _source_label, "status_text": STATUS_TEXT,
    })


# --- Single import -----------------------------------------------------------

@router.get("/imports/new")
def new_import(request: Request):
    return render(request, "ingest/new.html", {"url": "", "text": "", "error": None})


@router.post("/imports")
async def create_import(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    url = (form.get("url") or "").strip()
    text = (form.get("text") or "").strip()
    files = _files(form)

    def fail(error: str | None = None, duplicate: Dish | None = None):
        return render(request, "ingest/new.html",
                      {"url": url, "text": text, "error": error, "duplicate": duplicate}, 422)

    if not url and not text and not files:
        return fail("Provide a link, text, or at least one image.")
    if url and not urlsplit(url).scheme:
        url = "https://" + url
    if url and urlsplit(url).scheme not in ("http", "https"):
        return fail("That doesn't look like a web link.")
    if dup := duplicate_url_dish(session, url):
        return fail(duplicate=dup)
    if len(files) > MAX_IMPORT_FILES:
        return fail(f"Up to {MAX_IMPORT_FILES} images per import.")
    uploads, error = await _read_images(files, MAX_IMPORT_BYTES)
    if error:
        return fail(error)

    job = IngestJob(
        source_url=url or None,
        user_text=text,
        platform=platform_for(url) if url else "upload",
        status="queued",
    )
    session.add(job)
    session.flush()
    job.upload_paths = _save_uploads(job, uploads)
    session.commit()
    return _redirect(f"/imports/{job.id}")


# --- Handwritten batches -----------------------------------------------------

@router.get("/imports/batch")
def new_batch(request: Request):
    return render(request, "ingest/batch_new.html", {"error": None})


@router.post("/imports/batch")
async def create_batch(request: Request, session: Session = Depends(get_session)):
    files = _files(await request.form())

    def fail(error: str):
        return render(request, "ingest/batch_new.html", {"error": error}, 422)

    if not files:
        return fail("Choose at least one photo.")
    if len(files) > MAX_BATCH_FILES:
        return fail(f"Up to {MAX_BATCH_FILES} photos per batch.")
    for f in files:  # cheap checks before reading anything
        if (f.size or 0) > MAX_BATCH_BYTES or (f.content_type and not f.content_type.startswith("image/")):
            return fail(f"{f.filename} is too large or not an image.")

    batch = ImportBatch(status="queued", page_paths=[])
    session.add(batch)
    session.flush()
    batch_dir = f"imports/batch-{batch.id}"
    paths = []
    try:
        for n, f in enumerate(files, start=1):
            data = await f.read()
            if len(data) > MAX_BATCH_BYTES:
                raise ValueError
            paths.append(await run_in_threadpool(images.save_page_image, data, f"{batch_dir}/page-{n:02d}.jpg"))
    except Exception:
        session.rollback()
        shutil.rmtree(config.media_dir / batch_dir, ignore_errors=True)
        return fail(f"{f.filename} is too large or not an image.")
    batch.page_paths = paths
    session.commit()
    return _redirect(f"/imports/batches/{batch.id}")


def _batch_ctx(session: Session, batch: ImportBatch) -> dict:
    jobs = batch.jobs
    next_draft = next((j for j in sorted(jobs, key=lambda j: (j.created_at, j.id))
                       if j.status == "ready" and j.dish_id is None), None)
    polling = batch.status in ("queued", "grouping") or any(j.status in ACTIVE for j in jobs)
    return {"batch": batch, "jobs": jobs, "next_draft": next_draft, "polling": polling,
            "status_text": STATUS_TEXT}


@router.get("/imports/batches/{batch_id}")
def batch_page(batch_id: int, request: Request, session: Session = Depends(get_session)):
    batch = _batch_or_404(session, batch_id)
    return render(request, "ingest/batch.html", _batch_ctx(session, batch))


@router.get("/imports/batches/{batch_id}/status")
def batch_status(batch_id: int, request: Request, session: Session = Depends(get_session)):
    batch = _batch_or_404(session, batch_id)
    return render(request, "ingest/_batch_body.html", _batch_ctx(session, batch))


@router.post("/imports/batches/{batch_id}/retry")
def retry_batch(batch_id: int, session: Session = Depends(get_session)):
    batch = _batch_or_404(session, batch_id)
    if batch.status == "failed":
        batch.status, batch.error = "queued", None
        session.commit()
    return _redirect(f"/imports/batches/{batch.id}")


# --- Job page, review, save --------------------------------------------------

def _review_ctx(session: Session, job: IngestJob, form: DishInput, errors: dict, image_choice: str | None) -> dict:
    ex = job.extracted or {}
    candidates = (job.raw or {}).get("image_candidates") or []
    if image_choice is None:
        image_choice = "" if job.platform == "paper" or not candidates else candidates[0]
    note = (ex.get("note") or "").strip()
    dup_match = DUP_NOTE.match(note)
    return {
        "job": job,
        "form": form,
        "errors": errors,
        "action": f"/imports/{job.id}/save",
        "submit_label": "Save recipe",
        "extra_template": "ingest/_image_picker.html",
        "candidates": candidates,
        "image_choice": image_choice,
        "not_recipe": ex.get("is_recipe") is False,
        "note": None if dup_match else note,
        "dup_note_dish": session.get(Dish, int(dup_match.group(1))) if dup_match else None,
        "core_warning": core_tag_warning(form.type, form.tags),
        "similar": similar_dishes(session, form.title),
        "status_text": STATUS_TEXT,
    }


def _initial_form(job: IngestJob) -> DishInput:
    raw = job.raw or {}
    return DishInput.from_extracted(job.extracted or {}, source_url=raw.get("canonical_url") or job.source_url or "")


@router.get("/imports/{job_id}")
def job_page(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if job.dish_id:
        return _redirect(f"/dishes/{job.dish_id}")
    ctx = {"job": job, "status_text": STATUS_TEXT, "error": None}
    if job.status == "ready":
        ctx = _review_ctx(session, job, _initial_form(job), {}, None)
    return render(request, "ingest/job.html", ctx)


@router.get("/imports/{job_id}/status")
def job_status(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    resp = render(request, "ingest/_status.html", {"job": job, "status_text": STATUS_TEXT})
    if job.status not in ACTIVE:
        resp.headers["HX-Refresh"] = "true"  # terminal: reload to show the review/retry page
    return resp


@router.post("/imports/{job_id}/retry")
async def retry_job(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if job.status != "failed":
        return _redirect(f"/imports/{job.id}")
    form = await request.form()
    text = (form.get("text") or "").strip()
    files = _files(form)
    error = None
    if len(files) > MAX_IMPORT_FILES:
        error = f"Up to {MAX_IMPORT_FILES} images per import."
    else:
        uploads, error = await _read_images(files, MAX_IMPORT_BYTES)
    if error:
        return render(request, "ingest/job.html",
                      {"job": job, "status_text": STATUS_TEXT, "error": error, "text": text}, 422)
    if text:
        job.user_text = f"{job.user_text}\n\n{text}".strip()
    if uploads:
        job.upload_paths = job.upload_paths + _save_uploads(job, uploads)
    job.status, job.error = "queued", None
    session.commit()
    return _redirect(f"/imports/{job.id}")


@router.post("/imports/{job_id}/discard")
def discard_job(job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if job.dish_id is None:
        job.status = "discarded"
        session.commit()
    return _redirect("/imports")


@router.post("/imports/{job_id}/save")
async def save_job(job_id: int, request: Request, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if job.dish_id:
        return _redirect(f"/dishes/{job.dish_id}")
    if job.status != "ready":
        return _redirect(f"/imports/{job.id}")
    form = await request.form()
    inp = DishInput.from_form(form)
    candidates = (job.raw or {}).get("image_candidates") or []
    image_choice = form.get("image") or ""
    if image_choice not in candidates:
        image_choice = ""
    errors = validate(session, inp)
    if errors:
        return render(request, "ingest/job.html", _review_ctx(session, job, inp, errors, image_choice), 422)

    dish = Dish(source_platform=job.platform or "upload")
    if image_choice:
        data = (config.media_dir / image_choice).read_bytes()
        dish.image_path = await run_in_threadpool(images.save_dish_image, data)
    if job.platform == "paper":
        dish.scan_paths = [images.copy_scan(p) for p in job.upload_paths]
    apply(session, dish, inp)
    job.dish_id = dish.id
    session.commit()

    if job.batch_id:
        next_job = session.scalar(
            select(IngestJob)
            .where(IngestJob.batch_id == job.batch_id, IngestJob.status == "ready",
                   IngestJob.dish_id.is_(None), IngestJob.id != job.id)
            .order_by(IngestJob.created_at, IngestJob.id)
            .limit(1)
        )
        if next_job:
            return _redirect(f"/imports/{next_job.id}")
    return _redirect(f"/dishes/{dish.id}")
