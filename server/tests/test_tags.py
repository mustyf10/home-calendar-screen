import pytest
from sqlalchemy import select

from app.models import Tag
from app.tags.service import (
    delete_tag,
    normalize_tag,
    parse_tag_input,
    rename_tag,
    resolve_tag,
)


def test_normalize_tag():
    assert normalize_tag("#Butter Chicken") == "butter-chicken"
    assert normalize_tag("  #  ") is None
    assert normalize_tag("Chana_Masala!") == "chana-masala"


def test_parse_tag_input_dedupes_in_order():
    assert parse_tag_input("#Rice, chicken  #rice eid") == ["rice", "chicken", "eid"]


def test_resolve_tag_prefers_existing_singular_or_plural():
    assert resolve_tag("samosas", {"samosa"}) == "samosa"
    assert resolve_tag("samosa", {"samosas"}) == "samosas"
    assert resolve_tag("tomatoes", {"tomato"}) == "tomato"
    assert resolve_tag("paneer", {"samosa"}) == "paneer"


def test_rename_onto_existing_tag_merges(session, make_dish):
    both = make_dish("Chole", tags=["chana", "chickpea"])
    only = make_dish("Hummus", tags=["chana"])
    chana = session.scalar(select(Tag).where(Tag.name == "chana"))

    rename_tag(session, chana.id, "#Chickpea")
    session.expire_all()

    assert session.scalar(select(Tag).where(Tag.name == "chana")) is None
    assert both.tag_names == ["chickpea"]
    assert only.tag_names == ["chickpea"]


def test_core_tags_cannot_be_renamed_or_deleted(session):
    chicken = session.scalar(select(Tag).where(Tag.name == "chicken"))
    with pytest.raises(ValueError):
        rename_tag(session, chicken.id, "poultry")
    with pytest.raises(ValueError):
        delete_tag(session, chicken.id)
