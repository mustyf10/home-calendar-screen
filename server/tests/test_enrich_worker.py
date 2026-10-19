"""The background fill of a saved recipe: fetch → extract → fill only what is still empty."""
import asyncio
import dataclasses
import io

import pytest
import yt_dlp
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import config
from app.db import make_engine
from app.ingest import llm, video, web, worker
from app.ingest.web import PageData
from app.models import Base, Dish, IngestJob
from app.tags.service import set_dish_tags

TIKTOK = "https://vm.tiktok.com/ZMabc/"
CANONICAL = "https://www.tiktok.com/@cook/video/7400000000000000001"


def jpeg_bytes(colour="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), colour).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def Session(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    return factory


@pytest.fixture
def fetched(monkeypatch):
    """yt-dlp returns a caption, canonical URL and thumbnail; no media, so nothing to transcribe."""
    calls = {"downloads": []}

    def fetch_video(url, tmp):
        calls["video"] = url
        return video.VideoData(caption="Crispy garlic chicken wraps. 4 wraps.", canonical_url=CANONICAL,
                               thumbnail_url="https://cdn.example/cover.jpg", media_path=None)

    def download(url):
        calls["downloads"].append(url)
        return jpeg_bytes("green")

    monkeypatch.setattr(video, "fetch_video", fetch_video)
    monkeypatch.setattr(web, "download", download)
    return calls


EXTRACTED = {
    "title": "Crispy garlic chicken wraps",
    "type": "snack",
    "tags": ["chicken", "wrap"],
    "description": "Quick wraps.",
    "ingredients": ["4 wraps", "500g chicken"],
    "steps": ["Fry.", "Wrap."],
    "servings": 4,
    "total_minutes": 25,
    "is_recipe": True,
    "note": "",
}


@pytest.fixture
def extract(monkeypatch):
    seen = {}

    def fake(raw, user_text, image_paths, platform, existing_tags):
        seen.update(raw=raw, user_text=user_text, image_paths=image_paths, platform=platform)
        return dict(seen.get("result", EXTRACTED), tags=list(seen.get("result", EXTRACTED)["tags"]))

    monkeypatch.setattr(llm, "extract", fake)
    return seen


def make(Session, title="TikTok recipe", tags=(), job=None, **dish_kw):
    with Session() as s:
        dish = Dish(title=title, source_platform=dish_kw.pop("source_platform", "tiktok"), **dish_kw)
        s.add(dish)
        s.flush()
        set_dish_tags(s, dish, list(tags))
        j = IngestJob(dish_id=dish.id, status="queued", **({"source_url": TIKTOK, "platform": "tiktok"} | (job or {})))
        s.add(j)
        s.commit()
        return dish.id, j.id


def run(Session, job_id):
    asyncio.run(worker.run_job(job_id))
    s = Session()
    job = s.get(IngestJob, job_id)
    return s, job, s.get(Dish, job.dish_id)


def test_link_only_recipe_is_filled_in(Session, fetched, extract):
    dish_id, job_id = make(Session, source_url="https://tiktok.com/ZMabc")

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("ready", None)
    assert dish.title == "Crispy garlic chicken wraps"
    assert dish.type == "snack"
    assert dish.description == "Quick wraps."
    assert dish.ingredients == ["4 wraps", "500g chicken"] and dish.steps == ["Fry.", "Wrap."]
    assert (dish.servings, dish.total_minutes) == (4, 25)
    assert dish.tag_names == ["chicken", "wrap"]
    assert dish.source_url == "https://tiktok.com/@cook/video/7400000000000000001"
    assert dish.image_path and dish.image_path.startswith("dishes/")
    assert (config.media_dir / dish.image_path).is_file()
    assert fetched["downloads"] == ["https://cdn.example/cover.jpg"]


def test_typed_fields_win_and_tags_merge(Session, fetched, extract):
    photo = "dishes/mine.jpg"
    dish_id, job_id = make(Session, title="Gran's wraps", type="dinner", tags=["vege", "family"],
                           description="My note", servings=2, image_path=photo)

    s, job, dish = run(Session, job_id)

    assert job.status == "ready"
    assert (dish.title, dish.type, dish.description, dish.servings) == ("Gran's wraps", "dinner", "My note", 2)
    assert dish.ingredients == ["4 wraps", "500g chicken"] and dish.total_minutes == 25
    assert dish.tag_names == ["chicken", "family", "vege", "wrap"]
    assert dish.image_path == photo
    assert fetched["downloads"] == ["https://cdn.example/cover.jpg"]


def test_fill_reads_the_dish_fresh_after_fetching(Session, monkeypatch, extract):
    dish_id, job_id = make(Session)

    def fetch_video(url, tmp):  # the user edits the recipe while the link is being read
        with Session() as other:
            d = other.get(Dish, dish_id)
            d.title, d.steps = "Edited meanwhile", ["My step"]
            other.commit()
        return video.VideoData(caption="caption", canonical_url=None, thumbnail_url=None, media_path=None)

    monkeypatch.setattr(video, "fetch_video", fetch_video)
    s, job, dish = run(Session, job_id)

    assert (dish.title, dish.type, dish.steps) == ("Edited meanwhile", "dinner", ["My step"])
    assert dish.ingredients == ["4 wraps", "500g chicken"]


def test_screenshots_are_sent_but_never_become_the_photo(Session, monkeypatch, extract):
    rel = "imports/shots/upload-1.jpg"
    (config.media_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (config.media_dir / rel).write_bytes(jpeg_bytes())
    dish_id, job_id = make(Session, title="Recipe from photos", source_platform="manual",
                           job={"source_url": None, "platform": "upload", "upload_paths": [rel]})

    s, job, dish = run(Session, job_id)

    assert extract["image_paths"] == [config.media_dir / rel]
    assert dish.title == "Crispy garlic chicken wraps"
    assert dish.image_path is None
    assert dish.source_url is None


def test_fetch_failure_without_text_fails_and_leaves_the_dish(Session, monkeypatch, extract):
    def refuse(url, tmp):
        raise yt_dlp.utils.DownloadError("Unexpected response from webpage request")

    def no_page(url):
        raise ValueError("blocked")

    monkeypatch.setattr(video, "fetch_video", refuse)
    monkeypatch.setattr(web, "fetch_page", no_page)
    dish_id, job_id = make(Session)

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("failed", worker.FETCH_FAILED)
    assert dish.title == "TikTok recipe" and dish.ingredients == []
    assert "raw" not in extract


def test_tiktok_page_fallback_feeds_the_caption(Session, monkeypatch, extract):
    def refuse(url, tmp):
        raise yt_dlp.utils.DownloadError("nope")

    page = PageData(url=CANONICAL, canonical_url=CANONICAL, og_title="", og_description="Wraps caption",
                    og_image=None, page_text="", jsonld_recipe=None)
    monkeypatch.setattr(video, "fetch_video", refuse)
    monkeypatch.setattr(web, "fetch_page", lambda url: page)
    dish_id, job_id = make(Session)

    s, job, dish = run(Session, job_id)

    assert extract["raw"]["caption"] == "Wraps caption"
    assert job.status == "ready" and dish.title == "Crispy garlic chicken wraps"


def test_not_a_recipe_fails_without_filling(Session, fetched, extract):
    extract["result"] = dict(EXTRACTED, is_recipe=False)
    dish_id, job_id = make(Session)

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("failed", "This doesn't look like a recipe")
    assert dish.title == "TikTok recipe" and dish.ingredients == [] and dish.image_path is None


def test_canonical_url_of_another_recipe_fails_as_duplicate(Session, fetched, extract):
    with Session() as s:
        other = Dish(title="Old wraps", source_platform="tiktok",
                     source_url="https://tiktok.com/@cook/video/7400000000000000001")
        s.add(other)
        s.commit()
    dish_id, job_id = make(Session, source_url="https://tiktok.com/ZMabc")

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("failed", "Already saved as Old wraps")
    assert job.extracted["duplicate_dish_id"] == other.id
    assert dish.title == "TikTok recipe" and dish.source_url == "https://tiktok.com/ZMabc"
    assert dish.ingredients == []


def test_claude_unavailable_fills_from_the_scrape_then_fails_for_retry(Session, fetched, monkeypatch):
    monkeypatch.setattr(llm, "config", dataclasses.replace(llm.config, anthropic_api_key=None))
    dish_id, job_id = make(Session)

    s, job, dish = run(Session, job_id)

    assert job.status == "failed"
    assert job.error == "AI extraction unavailable (ANTHROPIC_API_KEY not set)"
    assert job.extracted["fallback"] is True
    assert dish.title == "Crispy garlic chicken wraps. 4 wraps."
    assert dish.description == "Crispy garlic chicken wraps. 4 wraps."
    assert dish.type == "dinner"
    assert dish.image_path is not None


def test_claude_error_note_is_not_mistaken_for_a_fallback(Session, fetched, extract):
    extract["result"] = dict(EXTRACTED, note="AI extraction failed: the caption was short")
    dish_id, job_id = make(Session)

    s, job, dish = run(Session, job_id)

    assert job.status == "ready"


def test_tags_only_job_merges_suggested_tags(Session, monkeypatch):
    seen = {}

    def suggest(title, type, description, ingredients, existing):
        seen.update(title=title, type=type, description=description, ingredients=ingredients)
        return ["daal", "vege"]

    monkeypatch.setattr(worker, "config", dataclasses.replace(worker.config, anthropic_api_key="k"))
    monkeypatch.setattr(llm, "suggest_tags", suggest)
    monkeypatch.setattr(llm, "extract", lambda *a: pytest.fail("tags-only jobs don't extract"))
    dish_id, job_id = make(Session, title="Daal", type="side", tags=["lentil"], description="Red lentils",
                           ingredients=["lentils"], source_platform="manual",
                           job={"source_url": None, "platform": "upload"})

    s, job, dish = run(Session, job_id)

    assert seen == {"title": "Daal", "type": "side", "description": "Red lentils", "ingredients": ["lentils"]}
    assert job.status == "ready"
    assert dish.tag_names == ["daal", "lentil", "vege"]


def test_tags_only_job_without_api_key_just_finishes(Session, monkeypatch):
    monkeypatch.setattr(worker, "config", dataclasses.replace(worker.config, anthropic_api_key=None))
    monkeypatch.setattr(llm, "suggest_tags", lambda *a: pytest.fail("no key, no call"))
    dish_id, job_id = make(Session, title="Daal", tags=["vege"], source_platform="manual",
                           job={"source_url": None, "platform": "upload"})

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error, dish.tag_names) == ("ready", None, ["vege"])


def test_tags_only_failure_is_reported(Session, monkeypatch):
    def boom(*a):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(worker, "config", dataclasses.replace(worker.config, anthropic_api_key="k"))
    monkeypatch.setattr(llm, "suggest_tags", boom)
    dish_id, job_id = make(Session, title="Daal", source_platform="manual", job={"source_url": None, "platform": "upload"})

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("failed", "Couldn't suggest tags: overloaded")


def test_review_drafts_still_wait_for_review(Session, fetched, extract):
    with Session() as s:
        job = IngestJob(source_url=TIKTOK, platform="tiktok", status="queued")
        s.add(job)
        s.commit()

    asyncio.run(worker.run_job(job.id))

    with Session() as s:
        job = s.get(IngestJob, job.id)
        assert (job.status, job.dish_id, job.extracted["title"]) == ("ready", None, "Crispy garlic chicken wraps")
        assert s.query(Dish).count() == 0


# --- No awaits while holding SQLite's write lock ------------------------------

@pytest.fixture
def FileSession(monkeypatch, tmp_path):
    """A file-backed DB (WAL, like the Pi), so a second connection really competes for the write lock."""
    url = f"sqlite:///{tmp_path / 'app.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    return factory, url


def test_user_edit_while_the_photo_is_made_is_not_blocked_and_wins(FileSession, fetched, extract, monkeypatch):
    Session, url = FileSession
    dish_id, job_id = make(Session)
    other = sessionmaker(bind=create_engine(url, connect_args={"timeout": 1}), expire_on_commit=False)
    real_photo = worker._dish_photo

    def photo_while_user_edits(rel):
        with other() as s2:  # the user saves an edit while the worker is busy with the photo
            d = s2.get(Dish, dish_id)
            d.description = "Typed while importing"
            s2.commit()
        return real_photo(rel)

    monkeypatch.setattr(worker, "_dish_photo", photo_while_user_edits)

    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("ready", None)
    assert dish.description == "Typed while importing"
    assert dish.ingredients == ["4 wraps", "500g chicken"]
    assert dish.image_path and dish.image_path.startswith("dishes/")


# --- A retry may replace what an earlier fill wrote, never what the user typed --

def requeue(Session, job_id):
    with Session() as s:
        s.get(IngestJob, job_id).status = "queued"
        s.commit()


def real_extraction(monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a: dict(EXTRACTED, tags=list(EXTRACTED["tags"])))


def test_retry_after_fallback_replaces_what_the_fallback_wrote(Session, fetched, monkeypatch):
    monkeypatch.setattr(llm, "config", dataclasses.replace(llm.config, anthropic_api_key=None))
    dish_id, job_id = make(Session)
    s, job, dish = run(Session, job_id)
    assert job.status == "failed" and dish.title == "Crispy garlic chicken wraps. 4 wraps."

    requeue(Session, job_id)
    real_extraction(monkeypatch)
    s, job, dish = run(Session, job_id)

    assert (job.status, job.error) == ("ready", None)
    assert (dish.title, dish.type, dish.description) == ("Crispy garlic chicken wraps", "snack", "Quick wraps.")
    assert dish.ingredients == ["4 wraps", "500g chicken"]


def test_retry_keeps_fields_the_user_edited_after_the_fallback(Session, fetched, monkeypatch):
    monkeypatch.setattr(llm, "config", dataclasses.replace(llm.config, anthropic_api_key=None))
    dish_id, job_id = make(Session)
    run(Session, job_id)
    with Session() as s:
        d = s.get(Dish, dish_id)
        d.title, d.description = "My wraps", "My description"
        s.commit()

    requeue(Session, job_id)
    real_extraction(monkeypatch)
    s, job, dish = run(Session, job_id)

    assert job.status == "ready"
    assert (dish.title, dish.type, dish.description) == ("My wraps", "dinner", "My description")
    assert dish.steps == ["Fry.", "Wrap."]


def test_retry_keeps_a_type_the_user_chose_after_the_fallback(Session, fetched, monkeypatch):
    monkeypatch.setattr(llm, "config", dataclasses.replace(llm.config, anthropic_api_key=None))
    dish_id, job_id = make(Session)
    run(Session, job_id)
    with Session() as s:
        s.get(Dish, dish_id).type = "side"
        s.commit()

    requeue(Session, job_id)
    real_extraction(monkeypatch)
    s, job, dish = run(Session, job_id)

    assert job.status == "ready"
    assert (dish.title, dish.type, dish.description) == ("Crispy garlic chicken wraps", "side", "Quick wraps.")
