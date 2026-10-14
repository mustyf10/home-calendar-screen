import re

from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.orm import Session

from app.models import Dish, Tag, dish_tags

CORE_TAGS = ("chicken", "meat", "fish", "vege")


def normalize_tag(raw: str) -> str | None:
    s = raw.strip().lstrip("#").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s).strip("-")
    s = s[:32].strip("-")
    return s or None


def parse_tag_input(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,\s]+", text or ""):
        name = normalize_tag(part)
        if name and name not in out:
            out.append(name)
    return out


def resolve_tag(name: str, existing: set[str]) -> str:
    candidates = [name]
    if name.endswith("es"):
        candidates.append(name[:-2])
    if name.endswith("s"):
        candidates.append(name[:-1])
    candidates += [name + "s", name + "es"]
    for c in candidates:
        if c in existing:
            return c
    return name


def all_tag_names(session: Session) -> set[str]:
    return set(session.scalars(select(Tag.name)))


def resolve_names(session: Session, names: list[str]) -> list[str]:
    existing = all_tag_names(session)
    out: list[str] = []
    for raw in names:
        n = normalize_tag(raw)
        if n:
            n = resolve_tag(n, existing)
            if n not in out:
                out.append(n)
    return out


def set_dish_tags(session: Session, dish: Dish, names: list[str]) -> None:
    resolved = resolve_names(session, names)
    by_name = {t.name: t for t in session.scalars(select(Tag).where(Tag.name.in_(resolved)))}
    tags = []
    for n in resolved:
        tag = by_name.get(n)
        if tag is None:
            tag = Tag(name=n)
            session.add(tag)
        tags.append(tag)
    dish.tags = tags
    session.flush()


def rename_tag(session: Session, tag_id: int, new_raw: str) -> None:
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise LookupError("Tag not found")
    if tag.name in CORE_TAGS:
        raise ValueError("Core tags can't be renamed")
    new = normalize_tag(new_raw)
    if not new:
        raise ValueError("Tag name can't be empty")
    if new == tag.name:
        return
    target = session.scalar(select(Tag).where(Tag.name == new))
    if target is None:
        tag.name = new
    else:
        session.execute(
            insert(dish_tags)
            .prefix_with("OR IGNORE")
            .from_select(
                ["dish_id", "tag_id"],
                select(dish_tags.c.dish_id, literal(target.id)).where(
                    dish_tags.c.tag_id == tag.id
                ),
            )
        )
        session.execute(delete(dish_tags).where(dish_tags.c.tag_id == tag.id))
        session.delete(tag)
    session.commit()


def delete_tag(session: Session, tag_id: int) -> None:
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise LookupError("Tag not found")
    if tag.name in CORE_TAGS:
        raise ValueError("Core tags can't be deleted")
    session.delete(tag)
    session.commit()


def tag_usage(session: Session, *, dinners_only: bool = False) -> list[tuple[Tag, int]]:
    """Tags with counts of non-archived dishes; core tags first, then by count desc, name."""
    join_cond = dish_tags.c.tag_id == Tag.id
    count = func.count(Dish.id)
    dish_cond = (Dish.id == dish_tags.c.dish_id) & (Dish.archived.is_(False))
    if dinners_only:
        dish_cond = dish_cond & (Dish.type == "dinner")
    rows = session.execute(
        select(Tag, count)
        .outerjoin(dish_tags, join_cond)
        .outerjoin(Dish, dish_cond)
        .group_by(Tag.id)
    ).all()
    rows.sort(key=lambda r: (_core_rank(r[0].name), -r[1], r[0].name))
    return [(t, c) for t, c in rows]


def _core_rank(name: str) -> int:
    return CORE_TAGS.index(name) if name in CORE_TAGS else len(CORE_TAGS)


def dinner_tag_names(session: Session) -> list[str]:
    return [t.name for t, c in tag_usage(session, dinners_only=True) if c > 0 or t.name in CORE_TAGS]
