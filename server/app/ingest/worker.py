"""Background ingest loop: one queued job or batch at a time, oldest first."""
import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
from sqlalchemy import select, update

from app.config import config
from app.db import SessionLocal
from app.heavy import heavy_lock
from app.ingest import images, llm, video, web
from app.ingest.batch import process_batch
from app.ingest.urls import normalize_url
from app.models import Dish, ImportBatch, IngestJob
from app.tags.service import tag_usage

log = logging.getLogger(__name__)

VIDEO_PLATFORMS = ("instagram", "tiktok", "youtube")
LOCAL_PLATFORMS = ("paper", "upload")
ACTIVE_JOB_STATUSES = ("fetching", "transcribing", "extracting")
FETCH_FAILED = (
    "Couldn't read this link (Instagram often blocks logged-out access). "
    "Paste the caption or upload screenshots and retry."
)


def recover() -> None:
    """Anything mid-flight when the process stopped goes back to the queue."""
    with SessionLocal() as s:
        s.execute(update(IngestJob).where(IngestJob.status.in_(ACTIVE_JOB_STATUSES)).values(status="queued"))
        s.execute(update(ImportBatch).where(ImportBatch.status == "grouping").values(status="queued"))
        s.commit()


def next_item() -> tuple[str, int] | None:
    with SessionLocal() as s:
        job = s.execute(
            select(IngestJob.id, IngestJob.created_at)
            .where(IngestJob.status == "queued")
            .order_by(IngestJob.created_at, IngestJob.id)
            .limit(1)
        ).first()
        batch = s.execute(
            select(ImportBatch.id, ImportBatch.created_at)
            .where(ImportBatch.status == "queued")
            .order_by(ImportBatch.created_at, ImportBatch.id)
            .limit(1)
        ).first()
    if batch and (not job or batch.created_at <= job.created_at):
        return "batch", batch.id
    if job:
        return "job", job.id
    return None


async def run_worker() -> None:
    await asyncio.to_thread(recover)
    while True:
        try:
            item = await asyncio.to_thread(next_item)
            if item is None:
                await asyncio.sleep(2)
            elif item[0] == "batch":
                await asyncio.to_thread(run_batch, item[1])
            else:
                await run_job(item[1])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ingest worker loop error")
            await asyncio.sleep(2)


def _error(e: Exception) -> str:
    return f"Import failed: {type(e).__name__}: {e}"


def run_batch(batch_id: int) -> None:
    with SessionLocal() as s:
        batch = s.get(ImportBatch, batch_id)
        try:
            process_batch(s, batch)
        except Exception as e:
            log.exception("batch %s failed", batch_id)
            s.rollback()
            batch = s.get(ImportBatch, batch_id)
            batch.status, batch.error = "failed", _error(e)
            s.commit()


async def run_job(job_id: int) -> None:
    with SessionLocal() as s:
        job = s.get(IngestJob, job_id)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                await _process(s, job, Path(tmp))
        except Exception as e:
            log.exception("job %s failed", job_id)
            s.rollback()
            job = s.get(IngestJob, job_id)
            job.status, job.error = "failed", _error(e)
            s.commit()


def _set_status(s, job: IngestJob, status: str) -> None:
    job.status = status
    s.commit()


def existing_tag_names(s) -> list[str]:
    return [t.name for t, _count in tag_usage(s)][:500]


async def _process(s, job: IngestJob, tmp: Path) -> None:
    job_rel = f"imports/{job.id}"
    job_dir = config.media_dir / job_rel
    raw = {
        "caption": "",
        "page_text": "",
        "transcript": "",
        "jsonld_recipe": None,
        "canonical_url": None,
        "og_title": "",
        "source_url": job.source_url,
        "image_candidates": list(job.upload_paths),
    }
    source_image: str | None = None
    keyframes: list[str] = []
    media: Path | None = None

    # 1. fetching
    if job.platform not in LOCAL_PLATFORMS and job.source_url:
        _set_status(s, job, "fetching")
        media, source_image = await _fetch(job, raw, tmp, job_rel)
        got_something = any((raw["caption"], raw["page_text"], raw["jsonld_recipe"], media))
        if not got_something and not job.user_text.strip() and not job.upload_paths:
            job.raw = raw
            job.status, job.error = "failed", FETCH_FAILED
            s.commit()
            return
        if source_image:
            raw["image_candidates"].append(source_image)

    # 2. transcribing (non-fatal)
    if media:
        _set_status(s, job, "transcribing")
        raw["transcript"], keyframes = await _transcribe(media, tmp, job_dir, job_rel)
        raw["image_candidates"] += keyframes

    # 3. extracting
    _set_status(s, job, "extracting")
    llm_order = list(job.upload_paths) + keyframes + ([source_image] if source_image else [])
    extracted = await asyncio.to_thread(
        llm.extract,
        raw,
        job.user_text,
        [config.media_dir / p for p in llm_order],
        job.platform or "upload",
        existing_tag_names(s),
    )
    url = raw["canonical_url"] or job.source_url
    if url:
        dup = s.scalar(select(Dish).where(Dish.source_url == normalize_url(url)))
        if dup:
            extracted["note"] = f"Already saved as dish #{dup.id}"
    job.raw = raw
    job.extracted = extracted
    job.status, job.error = "ready", None
    s.commit()


async def _fetch(job: IngestJob, raw: dict, tmp: Path, job_rel: str) -> tuple[Path | None, str | None]:
    """Fills raw in place; returns (downloaded media, saved source image rel path)."""
    media = None
    image_url = None
    fetched_video = False
    if job.platform in VIDEO_PLATFORMS:
        import yt_dlp

        try:
            v = await asyncio.to_thread(video.fetch_video, job.source_url, tmp)
            raw["caption"], raw["canonical_url"] = v.caption, v.canonical_url
            media, image_url = v.media_path, v.thumbnail_url
            fetched_video = True
        except yt_dlp.utils.DownloadError as e:
            log.info("yt-dlp couldn't fetch %s (%s); trying the web page", job.source_url, e)
    if not fetched_video:
        try:
            page = await asyncio.to_thread(web.fetch_page, job.source_url)
        except (httpx.HTTPError, ValueError) as e:
            log.info("web fetch failed for %s: %s", job.source_url, e)
        else:
            jsonld = page.jsonld_recipe
            raw["page_text"] = page.page_text
            raw["jsonld_recipe"] = jsonld
            raw["og_title"] = page.og_title
            raw["caption"] = raw["caption"] or page.og_description
            raw["canonical_url"] = raw["canonical_url"] or page.canonical_url
            image_url = (jsonld or {}).get("image") or page.og_image
    source_image = await _download_image(image_url, f"{job_rel}/source.jpg") if image_url else None
    return media, source_image


async def _download_image(url: str, rel: str) -> str | None:
    try:
        data = await asyncio.to_thread(web.download, url)
        return await asyncio.to_thread(images.save_jpeg, data, rel, 1600, 88)
    except Exception as e:
        log.info("couldn't download image %s: %s", url, e)
        return None


async def _transcribe(media: Path, tmp: Path, job_dir: Path, job_rel: str) -> tuple[str, list[str]]:
    transcript = ""
    try:
        audio = await asyncio.to_thread(video.extract_audio, media, tmp)
        async with heavy_lock:
            transcript = await asyncio.to_thread(video.transcribe, audio)
    except Exception:
        log.exception("transcription failed for %s", media)
    frames: list[str] = []
    try:
        paths = await asyncio.to_thread(video.keyframes, media, job_dir)
        frames = [f"{job_rel}/{p.name}" for p in paths]
    except Exception:
        log.exception("keyframes failed for %s", media)
    return transcript, frames
