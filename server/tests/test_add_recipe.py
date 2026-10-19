"""Add a recipe: immediate save, background enrichment jobs, banners, and the folded-in Import pages."""
import io
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import claude
from app.config import config
from app.db import get_session
from app.ingest import llm, video, web
from app.ingest.jobs import placeholder_title
from app.main import app
from app.models import Base, Dish, ImportBatch, IngestJob
from app.tags.service import set_dish_tags
from app.web import templates

pytestmark = pytest.mark.usefixtures("fixed_clock")

TIKTOK = "https://www.tiktok.com/@cook/video/7400000000000000001"


@pytest.fixture
def db(monkeypatch):
    # Sync routes run on a worker thread, so share one in-memory connection across threads.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    with Session(engine, expire_on_commit=False) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
    app.dependency_overrides.clear()


@pytest.fixture
def client(db):
    return TestClient(app)


@pytest.fixture
def no_network(monkeypatch):
    """Any fetch or Claude call during a request fails the test."""
    def forbidden(*args, **kwargs):
        raise AssertionError("POST /dishes must not do network or Claude work")

    for target, name in ((web, "fetch_page"), (web, "download"), (video, "fetch_video"),
                         (llm, "extract"), (llm, "suggest_tags"), (claude, "call_json"),
                         (httpx.Client, "send")):
        monkeypatch.setattr(target, name, forbidden)


def png(colour="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buf, "PNG")
    return buf.getvalue()


def add_dish(db, title="Wraps", **kw) -> Dish:
    tags = kw.pop("tags", [])
    dish = Dish(title=title, source_platform=kw.pop("source_platform", "manual"), **kw)
    db.add(dish)
    db.flush()
    set_dish_tags(db, dish, tags)
    db.commit()
    return dish


def add_job(db, dish=None, status="queued", **kw) -> IngestJob:
    job = IngestJob(dish_id=dish.id if dish else None, status=status, **kw)
    db.add(job)
    db.commit()
    return job


def only_job(db) -> IngestJob:
    db.expire_all()
    jobs = db.scalars(select(IngestJob)).all()
    assert len(jobs) == 1
    return jobs[0]


# --- Placeholder titles -------------------------------------------------------

@pytest.mark.parametrize("url, photos, expected", [
    (TIKTOK, False, "TikTok recipe"),
    ("https://vm.tiktok.com/ZMabc/", True, "TikTok recipe"),
    ("https://www.instagram.com/reel/abc/", False, "Instagram recipe"),
    ("https://youtu.be/abc", False, "YouTube recipe"),
    ("https://www.bbcgoodfood.com/recipes/daal", True, "Recipe from bbcgoodfood.com"),
    ("", True, "Recipe from photos"),
    ("", False, "Recipe from text"),
])
def test_placeholder_title(url, photos, expected):
    assert placeholder_title(url, photos) == expected


# --- POST /dishes -------------------------------------------------------------

def test_link_only_save_is_immediate_with_placeholder_and_one_job(client, db, no_network):
    r = client.post("/dishes", data={"title": "", "type": "dinner", "source_url": TIKTOK}, follow_redirects=False)

    assert r.status_code == 303
    dish = db.scalars(select(Dish)).one()
    assert r.headers["location"] == f"/dishes/{dish.id}?created=1"
    assert dish.title == "TikTok recipe"
    assert dish.source_platform == "tiktok"
    job = only_job(db)
    assert (job.dish_id, job.source_url, job.platform, job.status) == (dish.id, TIKTOK, "tiktok", "queued")
    assert (job.user_text, job.upload_paths) == ("", [])


def test_caption_and_screenshots_go_on_the_job_not_the_dish(client, db, no_network):
    r = client.post(
        "/dishes",
        data={"title": "", "type": "dinner", "text": "  Garlic wraps: 4 wraps, 1 chicken  "},
        files=[("images", ("one.png", png(), "image/png")), ("images", ("two.png", png("blue"), "image/png"))],
        follow_redirects=False,
    )

    assert r.status_code == 303
    dish = db.scalars(select(Dish)).one()
    assert dish.title == "Recipe from photos"
    assert dish.image_path is None
    job = only_job(db)
    assert job.user_text == "Garlic wraps: 4 wraps, 1 chicken"
    assert job.source_url is None and job.platform == "upload"
    assert job.upload_paths == [f"imports/{job.id}/upload-1.png", f"imports/{job.id}/upload-2.png"]
    assert all((config.media_dir / p).is_file() for p in job.upload_paths)


def test_typed_title_is_kept_and_typed_only_save_queues_tags_only_job(client, db, no_network):
    r = client.post("/dishes", data={"title": "Daal", "type": "side", "tags_text": "#vege"}, follow_redirects=False)

    assert r.status_code == 303
    dish = db.scalars(select(Dish)).one()
    assert (dish.title, dish.type, dish.tag_names) == ("Daal", "side", ["vege"])
    job = only_job(db)
    assert (job.dish_id, job.source_url, job.user_text, job.upload_paths) == (dish.id, None, "", [])


def test_title_is_required_without_link_text_or_screenshots(client, db, no_network):
    r = client.post("/dishes", data={"title": "", "type": "dinner"})

    assert r.status_code == 422
    assert "Title is required." in r.text
    assert db.scalars(select(Dish)).all() == [] and db.scalars(select(IngestJob)).all() == []


def test_non_image_screenshot_is_rejected_without_saving(client, db, no_network):
    r = client.post("/dishes", data={"title": "", "type": "dinner"},
                    files=[("images", ("notes.txt", b"hello", "text/plain"))])

    assert r.status_code == 422
    assert "notes.txt is too large or not an image." in r.text
    assert db.scalars(select(Dish)).all() == []


def test_duplicate_link_is_still_refused(client, db, no_network):
    other = add_dish(db, "Old wraps", source_url="https://tiktok.com/@cook/video/7400000000000000001")
    r = client.post("/dishes", data={"title": "", "type": "dinner", "source_url": TIKTOK})

    assert r.status_code == 422
    assert f'Already saved as <a href="/dishes/{other.id}">Old wraps</a>' in r.text


def test_add_page_has_link_caption_screenshots_and_batch_link(client):
    html = client.get("/dishes/new").text
    form = re.search(r'<form method="post" action="/dishes".*?</form>', html, re.S).group(0)
    assert 'name="source_url"' in form and 'name="text"' in form
    assert re.search(r'<input[^>]*name="images"[^>]*multiple', form)
    assert re.search(r'<input name="title"(?![^>]*required)', form)
    assert form.count('name="source_url"') == 1
    assert 'href="/imports/batch"' in html


# --- Recipe page banners ------------------------------------------------------

def test_active_job_shows_polling_importing_banner(client, db):
    dish = add_dish(db, "TikTok recipe", source_url=TIKTOK)
    add_job(db, dish, status="fetching", source_url=TIKTOK, platform="tiktok")

    html = client.get(f"/dishes/{dish.id}?created=1").text

    assert re.search(rf'hx-get="/dishes/{dish.id}/import/status"[^>]*hx-trigger="every 2s"', html)
    assert "Importing…" in html and "fetching the link" in html
    assert "This dinner has no" not in html  # tags are still on their way
    assert "Similar recipe" not in html


def test_status_partial_refreshes_once_the_job_finishes(client, db):
    dish = add_dish(db, "TikTok recipe")
    job = add_job(db, dish, status="extracting", source_url=TIKTOK, platform="tiktok")

    r = client.get(f"/dishes/{dish.id}/import/status")
    assert "HX-Refresh" not in r.headers and "reading the recipe" in r.text

    job.status = "ready"
    db.commit()
    assert client.get(f"/dishes/{dish.id}/import/status").headers["HX-Refresh"] == "true"


def test_failed_job_shows_retry_banner_with_dismiss(client, db):
    dish = add_dish(db, "TikTok recipe", source_url=TIKTOK)
    add_job(db, dish, status="failed", source_url=TIKTOK, platform="tiktok", error="Couldn't read this link")

    html = client.get(f"/dishes/{dish.id}").text

    assert "Couldn't read the link" in html and "Couldn&#39;t read this link" in html
    banner = re.search(rf'<form method="post" action="/dishes/{dish.id}/import/retry".*?</form>', html, re.S).group(0)
    assert 'name="text"' in banner and 'name="images"' in banner and "Try again" in banner
    assert f'formaction="/dishes/{dish.id}/import/dismiss"' in banner
    assert "hx-get" not in html


def test_duplicate_failure_links_to_the_other_recipe(client, db):
    other = add_dish(db, "Old wraps")
    dish = add_dish(db, "TikTok recipe")
    add_job(db, dish, status="failed", source_url=TIKTOK, error="Already saved as Old wraps",
            extracted={"duplicate_dish_id": other.id})

    html = client.get(f"/dishes/{dish.id}").text
    assert f'Already saved as <a href="/dishes/{other.id}">Old wraps</a>' in html


def test_retry_appends_text_and_screenshots_and_requeues(client, db):
    dish = add_dish(db, "TikTok recipe")
    job = add_job(db, dish, status="failed", source_url=TIKTOK, platform="tiktok", user_text="first",
                  error="nope")

    r = client.post(f"/dishes/{dish.id}/import/retry", data={"text": "the caption"},
                    files=[("images", ("s.png", png(), "image/png"))], follow_redirects=False)

    assert (r.status_code, r.headers["location"]) == (303, f"/dishes/{dish.id}")
    job = only_job(db)
    assert (job.status, job.error, job.user_text) == ("queued", None, "first\n\nthe caption")
    assert job.upload_paths == [f"imports/{job.id}/upload-1.png"]


def test_dismiss_discards_the_job_and_hides_the_banner(client, db):
    dish = add_dish(db, "TikTok recipe")
    add_job(db, dish, status="failed", source_url=TIKTOK, error="nope")

    r = client.post(f"/dishes/{dish.id}/import/dismiss", follow_redirects=False)

    assert r.status_code == 303
    assert only_job(db).status == "discarded"
    assert "Couldn't read the link" not in client.get(f"/dishes/{dish.id}").text
    assert "Couldn't fill in" not in client.get("/dishes").text


def test_fill_from_link_button_starts_a_job_once(client, db):
    dish = add_dish(db, "Crispy garlic chicken wraps", source_url=TIKTOK, source_platform="tiktok")
    html = client.get(f"/dishes/{dish.id}").text
    assert re.search(rf'<form method="post" action="/dishes/{dish.id}/import">.*?Fill in from the link', html, re.S)

    client.post(f"/dishes/{dish.id}/import")
    client.post(f"/dishes/{dish.id}/import")  # a second click while it runs does nothing

    job = only_job(db)
    assert (job.dish_id, job.source_url, job.platform, job.status) == (dish.id, TIKTOK, "tiktok", "queued")
    assert "Fill in from the link" not in client.get(f"/dishes/{dish.id}").text


def test_no_fill_button_when_the_recipe_has_ingredients_or_no_link(client, db):
    full = add_dish(db, "Full", source_url=TIKTOK, ingredients=["1 egg"])
    bare = add_dish(db, "Bare")
    assert "Fill in from the link" not in client.get(f"/dishes/{full.id}").text
    assert "Fill in from the link" not in client.get(f"/dishes/{bare.id}").text


# --- Recipes list and drafts --------------------------------------------------

def test_recipes_list_shows_importing_pill_and_failed_banner(client, db):
    busy = add_dish(db, "Busy one")
    broken = add_dish(db, "Broken one")
    calm = add_dish(db, "Calm one")
    add_job(db, busy, status="transcribing", source_url=TIKTOK)
    add_job(db, broken, status="failed", source_url="https://example.com/x", error="nope")
    add_job(db, calm, status="ready")

    html = client.get("/dishes").text

    cards = dict(re.findall(r'<h3><a href="/dishes/(\d+)">.*?</a></h3>(.*?)</article>', html, re.S))
    assert "Importing…" in cards[str(busy.id)]
    assert "Importing…" not in cards[str(calm.id)] and "Importing…" not in cards[str(broken.id)]
    banner = re.search(r'<div class="warn banner"[^>]*>.*?</div>', html, re.S).group(0)
    assert f'<a href="/dishes/{broken.id}">Broken one</a>' in banner
    assert "Calm one" not in banner


def test_only_the_latest_job_of_a_recipe_counts(client, db):
    dish = add_dish(db, "Retried")
    add_job(db, dish, status="failed", error="old")
    add_job(db, dish, status="ready")
    assert "Couldn't fill in" not in client.get("/dishes").text
    assert "Couldn't read the link" not in client.get(f"/dishes/{dish.id}").text


def test_drafts_waiting_for_review_are_listed_on_add_and_recipes(client, db):
    draft = add_job(db, status="ready", platform="paper", extracted={"title": "Gran's scones"})
    saved = add_job(db, add_dish(db, "Saved one"), status="ready", platform="paper", extracted={"title": "Saved one"})
    batch = ImportBatch(status="grouping", page_paths=["a.jpg", "b.jpg"])
    done = ImportBatch(status="done", page_paths=["c.jpg"])
    db.add_all([batch, done])
    db.commit()

    for url in ("/dishes/new", "/dishes"):
        html = client.get(url).text
        assert f'<a href="/imports/{draft.id}">Gran&#39;s scones</a>' in html
        assert f'href="/imports/batches/{batch.id}"' in html
        assert f'href="/imports/{saved.id}"' not in html
        assert f'href="/imports/batches/{done.id}"' not in html


def test_drafts_badge_sits_on_the_add_button(client, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 3)
    html = client.get("/dishes").text
    add = re.search(r'<a href="/dishes/new" class="add-recipe".*?</a>', html, re.S).group(0)
    assert '<span class="badge">3</span>' in add


# --- Import folded in ---------------------------------------------------------

@pytest.mark.parametrize("url", ["/imports", "/imports/new"])
def test_old_import_pages_redirect_to_add(client, url):
    r = client.get(url, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/dishes/new")


def test_sidebar_has_no_import_item(client):
    nav = re.search(r'<nav id="main-nav".*?</nav>', client.get("/menu").text, re.S).group(0)
    assert "/imports" not in nav and ">Import<" not in nav
    assert 'href="/tags"' in nav and 'href="/settings"' in nav


def test_batch_import_page_still_works(client):
    r = client.get("/imports/batch")
    assert r.status_code == 200 and 'action="/imports/batch"' in r.text
