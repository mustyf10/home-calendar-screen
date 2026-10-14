from app.dishes.forms import DishInput, validate
from app.ingest.web import parse_page


def test_javascript_source_url_is_rejected(session):
    errors = validate(session, DishInput(title="Curry", source_url="javascript:alert(1)"))
    assert errors["source_url"] == "Links must start with http:// or https://."


def test_page_canonical_with_unsafe_scheme_falls_back_to_page_url():
    html = '<html><head><link rel="canonical" href="javascript:alert(1)"></head><body>x</body></html>'
    page = parse_page(html, "https://example.com/recipe")
    assert page.canonical_url == "https://example.com/recipe"
