"""Dish form parsing/validation shared by manual entry and import review."""
import difflib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.urls import normalize_url, platform_for
from app.models import DISH_TYPES, Dish
from app.tags.service import CORE_TAGS, parse_tag_input, set_dish_tags

CORE_TAG_WARNING = (
    "This dinner has no #chicken, #meat, #fish or #vege tag, "
    "so it won't come up when you roll by those tags."
)


@dataclass
class DishInput:
    title: str = ""
    type: str = "dinner"
    tags_text: str = ""
    description: str = ""
    ingredients_text: str = ""
    steps_text: str = ""
    servings: str = ""
    total_minutes: str = ""
    source_url: str = ""
    notes: str = ""

    @property
    def tags(self) -> list[str]:
        return parse_tag_input(self.tags_text)

    @property
    def ingredients(self) -> list[str]:
        return _lines(self.ingredients_text)

    @property
    def steps(self) -> list[str]:
        return _lines(self.steps_text)

    @classmethod
    def from_form(cls, form) -> "DishInput":
        return cls(**{f: (form.get(f) or "").strip() if f != "type" else (form.get(f) or "dinner")
                      for f in cls.__dataclass_fields__})

    @classmethod
    def from_dish(cls, dish: Dish) -> "DishInput":
        return cls(
            title=dish.title,
            type=dish.type,
            tags_text=" ".join(f"#{n}" for n in dish.tag_names),
            description=dish.description,
            ingredients_text="\n".join(dish.ingredients),
            steps_text="\n".join(dish.steps),
            servings=str(dish.servings or ""),
            total_minutes=str(dish.total_minutes or ""),
            source_url=dish.source_url or "",
            notes=dish.notes,
        )

    @classmethod
    def from_extracted(cls, ex: dict, source_url: str = "") -> "DishInput":
        return cls(
            title=ex.get("title") or "",
            type=ex.get("type") if ex.get("type") in DISH_TYPES else "dinner",
            tags_text=" ".join(f"#{t}" for t in ex.get("tags") or []),
            description=ex.get("description") or "",
            ingredients_text="\n".join(ex.get("ingredients") or []),
            steps_text="\n".join(ex.get("steps") or []),
            servings=str(ex.get("servings") or ""),
            total_minutes=str(ex.get("total_minutes") or ""),
            source_url=source_url,
        )


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _int_or_none(s: str) -> int | None:
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else None


def duplicate_url_dish(session: Session, url: str, exclude_id: int | None = None) -> Dish | None:
    if not url:
        return None
    norm = normalize_url(url)
    q = select(Dish).where(Dish.source_url == norm)
    if exclude_id:
        q = q.where(Dish.id != exclude_id)
    return session.scalar(q)


def validate(session: Session, inp: DishInput, dish_id: int | None = None, require_title: bool = True) -> dict:
    """Returns {field: message}; 'duplicate' holds the clashing Dish if any."""
    errors: dict = {}
    if require_title and not inp.title:
        errors["title"] = "Title is required."
    if inp.type not in DISH_TYPES:
        errors["type"] = "Choose a type."
    if inp.source_url and urlsplit(inp.source_url).scheme.lower() not in ("http", "https"):
        errors["source_url"] = "Links must start with http:// or https://."
        return errors
    dup = duplicate_url_dish(session, inp.source_url, dish_id)
    if dup:
        errors["source_url"] = "Already saved as"
        errors["duplicate"] = dup
    return errors


def apply(session: Session, dish: Dish, inp: DishInput) -> None:
    dish.title = inp.title
    dish.type = inp.type
    dish.description = inp.description
    dish.ingredients = inp.ingredients
    dish.steps = inp.steps
    dish.servings = _int_or_none(inp.servings)
    dish.total_minutes = _int_or_none(inp.total_minutes)
    dish.notes = inp.notes
    if inp.source_url:
        dish.source_url = normalize_url(inp.source_url)
        if dish.source_platform in (None, "manual"):
            dish.source_platform = platform_for(inp.source_url)
    else:
        dish.source_url = None
    session.add(dish)
    session.flush()
    set_dish_tags(session, dish, inp.tags)


def core_tag_warning(dish_type: str, tag_names: list[str]) -> str | None:
    if dish_type == "dinner" and not set(tag_names) & set(CORE_TAGS):
        return CORE_TAG_WARNING
    return None


def _norm_title(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t.lower()).split())


def similar_dishes(session: Session, title: str, exclude_id: int | None = None) -> list[Dish]:
    target = _norm_title(title)
    if not target:
        return []
    out = []
    for d in session.scalars(select(Dish).where(Dish.archived.is_(False))):
        if d.id == exclude_id:
            continue
        if difflib.SequenceMatcher(None, target, _norm_title(d.title)).ratio() >= 0.85:
            out.append(d)
    return out


@dataclass
class FormCtx:
    """Template context for dishes/_form.html."""
    form: DishInput
    errors: dict = field(default_factory=dict)
    action: str = "/dishes"
    submit_label: str = "Save"
