from pathlib import Path

from bs4 import BeautifulSoup

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
