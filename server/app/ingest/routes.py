"""Import pages: handwritten batches and draft review. Links, text and screenshots go through Add a recipe."""
import re
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from PIL import Image

from app.config import config
from app.db import get_session
from app.dishes.forms import DishInput, apply, core_tag_warning, similar_dishes, validate
from app.ingest import images, uploads
from app.ingest.jobs import ACTIVE, STATUS_TEXT
from app.models import Dish, ImportBatch, IngestJob
from app.web import render

router = APIRouter()

MAX_BATCH_FILES, MAX_BATCH_BYTES = 40, 15 * uploads.MB
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


@router.get("/imports")
@router.get("/imports/new")
def add_a_recipe():
    """Import folded into Add a recipe."""
    return _redirect("/dishes/new")


# --- Handwritten batches -----------------------------------------------------

@router.get("/imports/batch")
def new_batch(request: Request):
    return render(request, "ingest/batch_new.html", {"error": None})


@router.post("/imports/batch")
async def create_batch(request: Request, session: Session = Depends(get_session)):
    files = uploads.files(await request.form())

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
    except (ValueError, Image.UnidentifiedImageError, Image.DecompressionBombError):
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
    if image_choice is None and "photo" in ex:  # a photo post: the slide Claude picked, or none
        image_choice = ex["photo"] if ex["photo"] in candidates else ""
    elif image_choice is None:
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
    added, error = await uploads.read_images(uploads.files(form))
    if error:
        return render(request, "ingest/job.html",
                      {"job": job, "status_text": STATUS_TEXT, "error": error, "text": text}, 422)
    uploads.append_to_job(job, text, added)
    session.commit()
    return _redirect(f"/imports/{job.id}")


@router.post("/imports/{job_id}/discard")
def discard_job(job_id: int, session: Session = Depends(get_session)):
    job = _job_or_404(session, job_id)
    if job.dish_id is None:
        job.status = "discarded"
        session.commit()
    return _redirect("/dishes/new")


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
