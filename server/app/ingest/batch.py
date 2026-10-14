"""Handwritten/printed batch import: group page photos into recipes, one draft job per recipe."""
import logging

from sqlalchemy.orm import Session

from app.config import config
from app.ingest.images import llm_image_block
from app.ingest.llm import NoToolUse, call_tool
from app.models import ImportBatch, IngestJob

log = logging.getLogger(__name__)

GROUP_SYSTEM = (
    "These are photos of handwritten or printed recipe pages, in upload order. "
    "Group them into individual recipes. A page may contain several recipes — list it under each. "
    "A recipe may continue across consecutive pages. Put pages with no legible recipe in unreadable_pages. "
    "Give each recipe a short English title_hint."
)

GROUP_PAGES_TOOL = {
    "name": "group_pages",
    "description": "Record which pages belong to which recipe.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recipes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pages": {"type": "array", "items": {"type": "integer"}},
                        "title_hint": {"type": "string"},
                    },
                    "required": ["pages", "title_hint"],
                },
            },
            "unreadable_pages": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["recipes", "unreadable_pages"],
    },
}


def _pages(values, n_pages: int) -> list[int]:
    out = set()
    for v in values if isinstance(values, list) else []:
        try:
            p = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= p <= n_pages:
            out.add(p)
    return sorted(out)


def normalize_groups(raw: dict, n_pages: int) -> tuple[list[tuple[list[int], str]], list[int]]:
    """Clean Claude's grouping so that every page is either in a group or listed as unreadable."""
    groups: list[tuple[list[int], str]] = []
    for recipe in raw.get("recipes") or []:
        if not isinstance(recipe, dict):
            continue
        pages = _pages(recipe.get("pages"), n_pages)
        if pages:
            groups.append((pages, str(recipe.get("title_hint") or "").strip()))
    grouped = {p for pages, _ in groups for p in pages}
    unreadable = [p for p in _pages(raw.get("unreadable_pages"), n_pages) if p not in grouped]
    for p in range(1, n_pages + 1):
        if p not in grouped and p not in unreadable:
            groups.append(([p], ""))
    groups.sort(key=lambda g: g[0][0])  # stable: ties keep Claude's order
    return groups, unreadable


def job_text(title_hint: str) -> str:
    target = title_hint or "the recipe on these pages"
    return f"Recipe to extract: {target}. These pages may also contain other recipes — ignore them."


def process_batch(session: Session, batch: ImportBatch) -> None:
    batch.status = "grouping"
    batch.error = None
    session.commit()
    if not config.anthropic_api_key:
        _fail(session, batch, "Handwritten import needs ANTHROPIC_API_KEY")
        return

    import anthropic

    content: list[dict] = []
    for i, rel in enumerate(batch.page_paths, start=1):
        content.append({"type": "text", "text": f"Page {i}"})
        content.append(llm_image_block(config.media_dir / rel, 1568))
    try:
        raw = call_tool(GROUP_SYSTEM, content, GROUP_PAGES_TOOL, timeout=300)
    except (anthropic.APIError, NoToolUse) as e:
        log.warning("batch %s grouping failed: %s", batch.id, e)
        _fail(session, batch, f"Grouping failed: {e}")
        return

    groups, unreadable = normalize_groups(raw, len(batch.page_paths))
    for pages, hint in groups:
        session.add(
            IngestJob(
                batch_id=batch.id,
                platform="paper",
                upload_paths=[batch.page_paths[p - 1] for p in pages],
                status="queued",
                user_text=job_text(hint),
            )
        )
    batch.unreadable_pages = unreadable
    batch.status = "done"
    session.commit()


def _fail(session: Session, batch: ImportBatch, message: str) -> None:
    batch.status = "failed"
    batch.error = message
    session.commit()
