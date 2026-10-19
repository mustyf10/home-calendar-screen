"""Shared job vocabulary: statuses, placeholder titles, and the job/draft lookups pages show as banners.

A job with dish_id set when it is created is an enrichment job: it fills in a saved recipe in the
background. A job with dish_id unset is a draft waiting for review on /imports/{id}; saving it sets
dish_id, so "ready with dish_id" always means done.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingest.urls import host, platform_for
from app.models import ImportBatch, IngestJob

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
_PLATFORM_TITLES = {"tiktok": "TikTok recipe", "instagram": "Instagram recipe", "youtube": "YouTube recipe"}


def placeholder_title(source_url: str | None, has_photos: bool) -> str:
    """The title a recipe saved without one gets until the background fill replaces it."""
    if source_url:
        return _PLATFORM_TITLES.get(platform_for(source_url)) or f"Recipe from {host(source_url)}"
    return "Recipe from photos" if has_photos else "Recipe from text"


def is_placeholder_title(title: str, source_url: str | None) -> bool:
    return title in {placeholder_title(source_url, True), placeholder_title(source_url, False)}


def fills_from_source(job: IngestJob) -> bool:
    """False for a tags-only enrichment job: a recipe saved with no link, text or screenshots."""
    return bool(job.source_url or job.user_text.strip() or job.upload_paths)


def latest_dish_jobs(session: Session, dish_ids: list[int] | None = None) -> dict[int, IngestJob]:
    """Each recipe's most recent enrichment (or saved-draft) job, keyed by dish id."""
    newest = select(func.max(IngestJob.id)).where(IngestJob.dish_id.is_not(None)).group_by(IngestJob.dish_id)
    if dish_ids is not None:
        newest = newest.where(IngestJob.dish_id.in_(dish_ids))
    return {j.dish_id: j for j in session.scalars(select(IngestJob).where(IngestJob.id.in_(newest)))}


def review_banners(session: Session) -> dict:
    """Drafts on /imports/{id} (waiting for review, or failed), and handwritten batches not yet sorted."""
    drafts = session.scalars(
        select(IngestJob)
        .where(IngestJob.status.in_(("ready", "failed")), IngestJob.dish_id.is_(None))
        .order_by(IngestJob.created_at, IngestJob.id)
    ).all()
    batches = session.scalars(
        select(ImportBatch).where(ImportBatch.status != "done").order_by(ImportBatch.created_at, ImportBatch.id)
    ).all()
    return {"drafts": drafts, "batches": batches, "status_text": STATUS_TEXT}
