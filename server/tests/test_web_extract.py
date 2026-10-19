from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.ingest import web
from app.ingest.urls import normalize_url
from app.ingest.web import extract_jsonld_recipe, iso_minutes, parse_page

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_jsonld_recipe_found_inside_graph_with_list_type():
    r = extract_jsonld_recipe(BeautifulSoup(_html("recipe_graph.html"), "html.parser"))
    assert r["name"] == "Chicken Karahi"
    assert r["description"] == "A quick, peppery karahi."
    assert r["image"] == "https://example.com/karahi-1.jpg"
    assert r["ingredients"] == ["1kg chicken", "4 tomatoes", "2 tsp black pepper"]
    assert r["servings"] == 4
    assert r["total_minutes"] == 75


def test_howto_section_steps_are_flattened():
    r = extract_jsonld_recipe(BeautifulSoup(_html("recipe_graph.html"), "html.parser"))
    assert r["steps"] == [
        "Chop the tomatoes.",
        "Cut the chicken.",
        "Fry the chicken until sealed.",
        "Add tomatoes and pepper; cook down.",
    ]


def test_steps_from_string_and_list_of_strings():
    def soup(instructions: str) -> BeautifulSoup:
        return BeautifulSoup(
            '<script type="application/ld+json">{"@type": "Recipe", "name": "X", '
            f'"recipeInstructions": {instructions}}}</script>',
            "html.parser",
        )

    assert extract_jsonld_recipe(soup('"Mix.\\nBake."'))["steps"] == ["Mix.", "Bake."]
    assert extract_jsonld_recipe(soup('["Mix.", "Bake."]'))["steps"] == ["Mix.", "Bake."]


def test_top_level_list_of_nodes():
    soup = BeautifulSoup(
        '<script type="application/ld+json">[{"@type": "Organization"}, '
        '{"@type": "Recipe", "name": "Toast", "image": {"url": "https://e.com/t.jpg"}}]</script>',
        "html.parser",
    )
    r = extract_jsonld_recipe(soup)
    assert r["name"] == "Toast"
    assert r["image"] == "https://e.com/t.jpg"


def test_no_recipe_returns_none():
    soup = BeautifulSoup('<script type="application/ld+json">{"@type": "WebSite"}</script>', "html.parser")
    assert extract_jsonld_recipe(soup) is None


def test_og_fallback_without_jsonld():
    page = parse_page(_html("recipe_og_only.html"), "https://example.com/daal/")
    assert page.jsonld_recipe is None
    assert page.og_title == "Grandma's Daal"
    assert page.og_description == "Comforting red lentil daal."
    assert page.og_image == "https://example.com/images/daal.jpg"
    assert "Rinse 200g red lentils. Simmer with turmeric." in page.page_text
    assert "color: red" not in page.page_text


def test_tiktok_caption_and_cover_come_from_rehydration_data():
    page = parse_page(_html("tiktok_video.html"), "https://www.tiktok.com/@cook/video/7400000000000000001")
    assert page.og_description.startswith("Crispy garlic chicken wraps")
    assert "500g chicken thighs" in page.og_description
    assert page.og_image == "https://p16-sign.tiktokcdn.com/cover.jpeg"


def test_meta_tags_win_over_rehydration_data():
    html = _html("tiktok_video.html").replace(
        "<title>", '<meta property="og:description" content="From meta"><title>'
    )
    page = parse_page(html, "https://www.tiktok.com/@cook/video/1")
    assert page.og_description == "From meta"
    assert page.og_image == "https://p16-sign.tiktokcdn.com/cover.jpeg"


def test_broken_rehydration_data_is_ignored():
    html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{not json</script>'
    page = parse_page(html, "https://www.tiktok.com/@cook/video/1")
    assert (page.og_description, page.og_image) == ("", None)


def test_tiktok_photo_post_slides_and_title_come_from_rehydration_data():
    page = parse_page(_html("tiktok_slideshow.html"), "https://www.tiktok.com/@cook/video/7500000000000000002")
    assert page.slides == ["https://p16-sign.tiktokcdn.com/slide-1.jpeg", "https://p16-sign.tiktokcdn.com/slide-2.jpeg"]
    assert page.og_title == "butter chicken recipe <3"
    assert page.og_description == "this tasted so good #butterchicken #fyp"


def test_tiktok_videos_have_no_slides():
    assert parse_page(_html("tiktok_video.html"), "https://www.tiktok.com/@cook/video/1").slides == []


@pytest.mark.parametrize("images", ["true", "1", '{"a": 1}', '[null, "x", {"imageURL": {"urlList": [7]}}]'])
def test_odd_slide_data_means_no_slides_not_a_crash(images):
    html = _html("tiktok_slideshow.html").replace('"images":[', f'"images":{images},"was":[')
    page = parse_page(html, "https://www.tiktok.com/@cook/video/1")
    assert (page.slides, page.og_title) == ([], "butter chicken recipe <3")



def test_tiktok_photo_page_is_read_through_its_video_address(monkeypatch):
    """TikTok's /photo/ page is filled in by the browser; the same post under /video/ carries the slides."""
    asked = []

    def fetch_page(url):
        asked.append(url)
        if "/video/" not in url:  # the short link lands on the /photo/ page
            return parse_page("<title>TikTok - Make Your Day</title>",
                              "https://www.tiktok.com/@cook/photo/7500000000000000002?image_index=2")
        return parse_page(_html("tiktok_slideshow.html"), url)

    monkeypatch.setattr(web, "fetch_page", fetch_page)
    page = web.fetch_tiktok_page("https://vm.tiktok.com/ZMslides/")

    assert asked == ["https://vm.tiktok.com/ZMslides/", "https://www.tiktok.com/@cook/video/7500000000000000002"]
    assert len(page.slides) == 2


def test_tiktok_video_page_is_fetched_once(monkeypatch):
    asked = []
    monkeypatch.setattr(web, "fetch_page", lambda url: asked.append(url) or parse_page(_html("tiktok_video.html"), url))
    web.fetch_tiktok_page("https://www.tiktok.com/@cook/video/7400000000000000001")
    assert asked == ["https://www.tiktok.com/@cook/video/7400000000000000001"]


def test_page_text_strips_chrome_and_collapses_whitespace():
    page = parse_page(_html("recipe_graph.html"), "https://example.com/karahi")
    assert "Our favourite weeknight curry." in page.page_text
    for gone in ("Site header", "Copyright", "Home Recipes About", "var x"):
        assert gone not in page.page_text
    assert page.jsonld_recipe["name"] == "Chicken Karahi"


def test_iso_minutes():
    assert iso_minutes("PT1H15M") == 75
    assert iso_minutes("PT45M") == 45
    assert iso_minutes("P0DT2H") == 120
    assert iso_minutes("PT90S") == 2
    assert iso_minutes("P1D") == 1440
    assert iso_minutes("") is None
    assert iso_minutes("soon") is None
    assert iso_minutes(None) is None


def test_normalize_url_keeps_youtube_v_only():
    assert (
        normalize_url("https://www.YouTube.com/watch?v=abc123&t=42&list=PL1#frag")
        == "https://youtube.com/watch?v=abc123"
    )
    assert normalize_url("https://Example.com/recipe/?utm_source=x#top") == "https://example.com/recipe"
    assert normalize_url("https://example.com/watch?v=abc") == "https://example.com/watch"
