from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session

from app.models import Dish, Tag
from app.tags.service import normalize_tag


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_dishes(
    session: Session,
    q: str = "",
    type: str | None = None,
    tag: str | None = None,
    archived: bool = False,
) -> list[Dish]:
    words = [w.lstrip("#") for w in (q or "").split()]
    words = [w for w in words if w]
    stmt = select(Dish).where(Dish.archived.is_(archived))
    for w in words:
        pat = _like(w)
        stmt = stmt.where(
            or_(
                Dish.title.ilike(pat, escape="\\"),
                Dish.description.ilike(pat, escape="\\"),
                Dish.notes.ilike(pat, escape="\\"),
                cast(Dish.ingredients, String).ilike(pat, escape="\\"),
                Dish.tags.any(Tag.name.ilike(pat, escape="\\")),
            )
        )
    if type:
        stmt = stmt.where(Dish.type == type)
    if tag:
        stmt = stmt.where(Dish.tags.any(Tag.name == (normalize_tag(tag) or "")))
    dishes = list(session.scalars(stmt))
    lowered = [w.lower() for w in words]

    def key(d: Dish):
        t = d.title.lower()
        return (not all(w in t for w in lowered), t)

    return sorted(dishes, key=key)
