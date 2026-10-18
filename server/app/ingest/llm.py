"""Claude calls: recipe extraction and tag suggestion."""
import json
import logging
from pathlib import Path

from app.claude import NoResult, call_json
from app.config import config
from app.ingest.images import llm_image_block
from app.models import DISH_TYPES
from app.tags.service import normalize_tag, resolve_tag

log = logging.getLogger(__name__)

TAG_RULES = """Tagging rules:
- Give 3 to 8 tags: lowercase, singular, multi-word tags joined with "-" (e.g. "butter-chicken").
- Include exactly one core tag: "chicken" for any poultry, "fish" for fish or seafood, "meat" for any other meat, "vege" when there is no meat or fish.
- Then add the main ingredients (at most 4), the dish name or style, and the cuisine.
- Reuse an existing tag whenever one means the same thing. Existing tags (most used first): {existing}"""

EXTRACT_SYSTEM = """Extract one recipe from the material provided.
Classify type as one of:
- dinner: a main meal
- snack: snacks and savouries such as samosas or pakoras
- dessert
- baking: cakes, breads, biscuits
- side
Write everything in English, translating if needed.
Keep ingredient quantities exactly as given, and never invent them.
Keep steps concise.
Set is_recipe to false if the material isn't actually a recipe.

{tag_rules}"""

PAPER_NOTE = (
    "The images are photos of handwritten or printed recipe pages. "
    "Transcribe carefully and write [illegible] for anything unreadable."
)

RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "type": {"type": "string", "enum": list(DISH_TYPES)},
        "tags": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "servings": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "total_minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "is_recipe": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["title", "type", "tags", "description", "ingredients", "steps", "is_recipe"],
    "additionalProperties": False,
}

TAGS_SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["tags"],
    "additionalProperties": False,
}

MAX_IMAGES = 8
MAX_PAPER_PAGES = 20


def tag_rules(existing_tags: list[str]) -> str:
    return TAG_RULES.format(existing=", ".join(existing_tags) or "(none yet)")


def clean_tags(tags, existing_tags: list[str]) -> list[str]:
    existing = set(existing_tags)
    out: list[str] = []
    for raw in tags or []:
        name = normalize_tag(str(raw))
        if name:
            name = resolve_tag(name, existing)
            if name not in out:
                out.append(name)
    return out


def _str_list(value) -> list[str]:
    return [s.strip() for s in (str(v) for v in value or []) if s.strip()]


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def fallback_extracted(raw: dict, user_text: str, note: str) -> dict:
    """Best-effort draft built from scraped data when Claude isn't available."""
    jsonld = raw.get("jsonld_recipe") or {}
    caption = raw.get("caption") or ""
    text = caption or user_text or ""
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return {
        "title": jsonld.get("name") or raw.get("og_title") or first_line[:120],
        "type": "dinner",
        "tags": [],
        "description": text,
        "ingredients": jsonld.get("ingredients") or [],
        "steps": jsonld.get("steps") or [],
        "servings": jsonld.get("servings"),
        "total_minutes": jsonld.get("total_minutes"),
        "is_recipe": True,
        "note": note,
    }


def _source_text(raw: dict, user_text: str) -> str:
    jsonld = raw.get("jsonld_recipe")
    sections = [
        ("SOURCE URL", raw.get("canonical_url") or raw.get("source_url") or ""),
        ("CAPTION", raw.get("caption") or ""),
        ("PAGE TEXT", raw.get("page_text") or ""),
        ("TRANSCRIPT", raw.get("transcript") or ""),
        ("JSON-LD RECIPE", _jsonld_text(jsonld) if jsonld else ""),
        ("USER NOTES", user_text or ""),
    ]
    body = "\n\n".join(f"{label}:\n{value.strip()}" for label, value in sections if value and value.strip())
    return body or "(No text. Use the images.)"


def _jsonld_text(jsonld: dict) -> str:
    return json.dumps(jsonld, ensure_ascii=False, indent=1)


def extract(raw: dict, user_text: str, image_paths: list[Path], platform: str, existing_tags: list[str]) -> dict:
    """image_paths are absolute and already ordered (uploads → keyframes → thumbnail; or paper pages)."""
    if not config.anthropic_api_key:
        result = fallback_extracted(raw, user_text, "AI extraction unavailable (ANTHROPIC_API_KEY not set)")
    else:
        result = _extract_with_claude(raw, user_text, image_paths, platform, existing_tags)
    result["tags"] = clean_tags(result.get("tags"), existing_tags)
    return result


def _extract_with_claude(raw, user_text, image_paths, platform, existing_tags) -> dict:
    import anthropic

    system = EXTRACT_SYSTEM.format(tag_rules=tag_rules(existing_tags))
    extra_note = ""
    if platform == "paper":
        system += "\n\n" + PAPER_NOTE
        if len(image_paths) > MAX_PAPER_PAGES:
            extra_note = "Only the first 20 pages were read"
        blocks = [llm_image_block(p, 1568) for p in image_paths[:MAX_PAPER_PAGES]]
    else:
        blocks = [llm_image_block(p, 1024) for p in image_paths[:MAX_IMAGES]]
    content = [{"type": "text", "text": _source_text(raw, user_text)}, *blocks]
    try:
        out = call_json(system, content, RECIPE_SCHEMA, timeout=180)
    except (anthropic.APIError, NoResult) as e:
        log.warning("extraction failed: %s", e)
        return fallback_extracted(raw, user_text, f"AI extraction failed: {e}")
    note = str(out.get("note") or "").strip()
    if extra_note:
        note = f"{note} {extra_note}." if note else f"{extra_note}."
    return {
        "title": str(out.get("title") or "").strip(),
        "type": out.get("type") if out.get("type") in DISH_TYPES else "dinner",
        "tags": out.get("tags") or [],
        "description": str(out.get("description") or "").strip(),
        "ingredients": _str_list(out.get("ingredients")),
        "steps": _str_list(out.get("steps")),
        "servings": _int_or_none(out.get("servings")),
        "total_minutes": _int_or_none(out.get("total_minutes")),
        "is_recipe": out.get("is_recipe") is not False,
        "note": note,
    }


def suggest_tags(title: str, type: str, description: str, ingredients: list[str], existing_tags: list[str]) -> list[str]:
    """Raises on a missing key, an API error, or an unusable answer; the caller flashes the message."""
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    text = "\n\n".join(
        f"{label}:\n{value}"
        for label, value in (
            ("TITLE", title),
            ("TYPE", type),
            ("DESCRIPTION", description),
            ("INGREDIENTS", "\n".join(ingredients)),
        )
        if value
    )
    out = call_json(tag_rules(existing_tags), [{"type": "text", "text": text}], TAGS_SCHEMA, timeout=60, max_tokens=4096)
    return clean_tags(out.get("tags"), existing_tags)
