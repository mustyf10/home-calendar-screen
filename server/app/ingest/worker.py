"""Background ingest loop: one queued job or batch at a time, oldest first.

Draft jobs (no dish) end "ready" for review. Enrichment jobs (dish_id set when queued) fill the blanks of a
saved recipe and end "ready", or "failed" with a reason the recipe page offers to retry.
"""
import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
from sqlalchemy import select, update

from app.config import config
from app.db import SessionLocal
from app.errors import log_failure, user_message
from app.heavy import heavy_lock
from app.ingest import images, llm, video, web
from app.ingest.batch import process_batch
from app.ingest.jobs import fills_from_source, is_placeholder_title
from app.ingest.urls import host, normalize_url, platform_for
from app.models import DISH_TYPES, Dish, ImportBatch, IngestJob
from app.tags.service import existing_tag_names, set_dish_tags

log = logging.getLogger(__name__)

VIDEO_PLATFORMS = ("instagram", "tiktok", "youtube")
LOCAL_PLATFORMS = ("paper", "upload")
ACTIVE_JOB_STATUSES = ("fetching", "transcribing", "extracting")
NOT_A_RECIPE = "This doesn't look like a recipe"
FILLABLE = ("description", "ingredients", "steps", "servings", "total_minutes")
# platform -> (what couldn't be read, why, what to paste instead)
_FETCH_FAILED_SOURCE = {
    "tiktok": ("this TikTok video", "", "the caption"),
    "instagram": ("this Instagram post", " (Instagram often blocks access without logging in)", "the caption"),
    "youtube": ("this YouTube video", "", "the description"),
}
_WEB_FETCH_FAILED = (" (the site may be down or blocking automated access)", "the recipe text")


def fetch_failed_message(platform: str | None, url: str) -> str:
    """What to tell the user when nothing could be read from the link."""
    what, why, paste = _FETCH_FAILED_SOURCE.get(platform or "", (host(url) or "this link", *_WEB_FETCH_FAILED))
    return f"Couldn't read {what}{why}. Paste {paste} or add screenshots, then try again."


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
    return user_message("Couldn't import this recipe", e)


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


def _fail(s, job: IngestJob, error: str) -> None:
    job.status, job.error = "failed", error
    s.commit()


async def _process(s, job: IngestJob, tmp: Path) -> None:
    if job.dish_id is not None and not fills_from_source(job):
        await _suggest_tags(s, job)
        return
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
            _fail(s, job, fetch_failed_message(job.platform, job.source_url))
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
    if job.dish_id is not None:
        await _fill_dish(s, job, extracted, raw, url, ([source_image] if source_image else []) + keyframes)
        return
    job.raw = raw
    if url:
        dup = s.scalar(select(Dish).where(Dish.source_url == normalize_url(url)))
        if dup:
            extracted["note"] = f"Already saved as dish #{dup.id}"
    job.extracted = extracted
    job.status, job.error = "ready", None
    s.commit()


async def _fill_dish(s, job: IngestJob, ex: dict, raw: dict, url: str | None, photos: list[str]) -> None:
    """Fills only what is still empty on the recipe as it is now; typed fields always win.

    Awaited work (the photo) happens while the session holds no pending writes, so the worker never sits
    on SQLite's write lock while a user saves an edit; everything after it is one synchronous transaction.
    """
    canonical = normalize_url(url) if url else None
    dup = s.scalar(select(Dish).where(Dish.source_url == canonical, Dish.id != job.dish_id)) if canonical else None
    photo = None
    if not dup and ex.get("is_recipe") is not False and photos:
        if not s.get(Dish, job.dish_id, populate_existing=True).image_path:
            photo = await asyncio.to_thread(_dish_photo, photos[0])

    # No awaits from here on.
    previous = (job.extracted or {}).get("filled") or {}
    job.raw = raw
    if dup:
        job.extracted = {**ex, "filled": previous, "duplicate_dish_id": dup.id}
        _fail(s, job, f"Already saved as {dup.title}")
        return
    if ex.get("is_recipe") is False:
        job.extracted = {**ex, "filled": previous}
        _fail(s, job, NOT_A_RECIPE)
        return
    dish = s.get(Dish, job.dish_id, populate_existing=True)  # the user may have edited it meanwhile
    if photo and not dish.image_path:
        dish.image_path = photo
    filled = _fill_blanks(dish, job, ex, previous)
    if canonical:
        dish.source_url = canonical
        if dish.source_platform == "manual":
            dish.source_platform = platform_for(canonical)
    set_dish_tags(s, dish, dish.tag_names + list(ex.get("tags") or []))
    job.extracted = {**ex, "filled": {**previous, **filled}}
    if ex.get("fallback"):  # Claude wasn't available: keep what the scrape gave, offer a retry
        _fail(s, job, ex.get("note") or "AI extraction unavailable")
        return
    job.status, job.error = "ready", None
    s.commit()


def _fill_blanks(dish: Dish, job: IngestJob, ex: dict, previous: dict) -> dict:
    """Sets empty fields from ex and returns {field: value} written. A field still holding what an earlier run
    of this job wrote (previous) counts as empty, so a retry after a fallback can replace it; edits win.

    Type has no "empty": while the title is a placeholder the job owns the type the recipe was saved with, and
    keeps owning it only while it still equals the type the job last recorded."""
    def untouched(field: str) -> bool:
        return field in previous and getattr(dish, field) == previous[field]

    filled: dict = {}
    if is_placeholder_title(dish.title, job.source_url) or untouched("title"):
        if ex.get("title"):
            dish.title = filled["title"] = ex["title"]
        if "type" not in previous or untouched("type"):
            if not ex.get("fallback") and ex.get("type") in DISH_TYPES:
                dish.type = ex["type"]
            filled["type"] = dish.type
    for field in FILLABLE:
        value = ex.get(field)
        if value and (not getattr(dish, field) or untouched(field)):
            setattr(dish, field, value)
            filled[field] = value
    return filled


def _dish_photo(rel: str) -> str | None:
    try:
        return images.save_dish_image((config.media_dir / rel).read_bytes())
    except Exception:
        log.exception("couldn't make a dish photo from %s", rel)
        return None


async def _suggest_tags(s, job: IngestJob) -> None:
    """A recipe saved with no link, text or screenshots only gets AI tags added to its own."""
    if config.anthropic_api_key:
        _set_status(s, job, "extracting")
        dish = s.get(Dish, job.dish_id)
        try:
            tags = await asyncio.to_thread(
                llm.suggest_tags, dish.title, dish.type, dish.description, dish.ingredients, existing_tag_names(s)
            )
        except Exception as e:  # any failure: the recipe is saved; say why there are no tags
            log_failure(log, f"Tag suggestion for job {job.id} failed", e)
            _fail(s, job, user_message("Couldn't suggest tags", e))
            return
        dish = s.get(Dish, job.dish_id, populate_existing=True)
        set_dish_tags(s, dish, dish.tag_names + tags)
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
