import json
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from app import settings_store
from app.bins import service
from app.db import get_session
from app.main import app
from app.web import templates


YORK_PLACE = {"postcode": "YO10 3DD", "district": "York", "code": "E06000014"}
YORK_FORM = {
    "postcode": "yo10 3dd",
    "house_number": "1",
    "uprn": "100050535540",
    "council": "YorkCouncil",
}
YORK_CONFIG = {**YORK_FORM, "postcode": "YO10 3DD"}
ROWS = [
    {"type": "RECYCLING", "date": "2026-10-22"},
    {"type": "REFUSE", "date": "2026-10-29"},
]
KEYS = ("bins.config", "bins.collections", "bins.status")


@pytest.fixture
def client(session, monkeypatch, fixed_clock):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)

    def lookup(postcode):
        assert postcode == "YO10 3DD"
        return YORK_PLACE.copy()

    def unexpected_fetch(cfg):
        pytest.fail("This request must not fetch bin collections")

    monkeypatch.setattr(service, "lookup_postcode", lookup)
    monkeypatch.setattr(service, "fetch_collections", unexpected_fetch)
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_first_submit_asks_for_uprn_without_saving(client, session):
    response = client.post(
        "/settings/bins", data={"postcode": "yo10 3dd", "house_number": "1"}
    )

    assert response.status_code == 422
    assert "York also needs your property's UPRN." in unescape(response.text)
    assert 'name="uprn"' in response.text
    assert re.search(
        r'<input\b(?=[^>]*type="hidden")(?=[^>]*name="council")'
        r'(?=[^>]*value="YorkCouncil")[^>]*>',
        response.text,
    )
    assert "Find my UPRN" in response.text
    assert service.FIND_UPRN_URL in response.text
    assert service.load_config(session) is None
    assert all(settings_store.get(session, key) is None for key in KEYS)


def test_second_submit_saves_canonical_postcode_and_shows_dates(client, session, monkeypatch):
    fetched = []

    def fetch(cfg):
        fetched.append(cfg.copy())
        return ROWS

    monkeypatch.setattr(service, "fetch_collections", fetch)
    response = client.post("/settings/bins", data=YORK_FORM, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert fetched == [YORK_CONFIG]
    session.expire_all()
    assert service.load_config(session) == YORK_CONFIG
    assert json.loads(settings_store.get(session, "bins.collections")) == ROWS
    status = service.load_status(session)
    assert status["last_ok_at"] is not None
    assert status["last_error"] is None
    assert status["last_error_since"] is None
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Council: <strong>York</strong>" in page.text
    assert "Thu 22 Oct" in page.text
    assert "RECYCLING" in page.text


def test_council_without_uprn_saves_on_first_submit(client, session, monkeypatch):
    module = next(
        module for module, entry in service.councils().items()
        if service.needs(entry) == ["postcode", "house_number"]
    )
    monkeypatch.setattr(service, "council_for", lambda place: (module, None))
    fetched = []

    def fetch(cfg):
        fetched.append(cfg.copy())
        return ROWS

    monkeypatch.setattr(service, "fetch_collections", fetch)
    response = client.post(
        "/settings/bins",
        data={"postcode": "yo10 3dd", "house_number": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    expected = {**YORK_CONFIG, "council": module, "uprn": ""}
    assert fetched == [expected]
    session.expire_all()
    assert service.load_config(session) == expected


def test_browser_only_council_is_rejected(client, session, monkeypatch):
    entry = next(
        entry for entry in service.all_councils().values()
        if service.browser_only(entry)
    )
    monkeypatch.setattr(service, "council_for", lambda place: (None, entry))
    response = client.post(
        "/settings/bins", data={"postcode": "yo10 3dd", "house_number": "1"}
    )

    assert response.status_code == 422
    assert (
        f"{entry['wiki_name']}'s bin lookup needs a web browser, "
        "which the kitchen Pi doesn't have."
    ) in unescape(response.text)
    assert service.load_config(session) is None


def test_unknown_council_offers_selection_and_selected_council_asks_for_uprn(
    client, session, monkeypatch
):
    monkeypatch.setattr(service, "council_for", lambda place: (None, None))
    response = client.post(
        "/settings/bins", data={"postcode": "yo10 3dd", "house_number": "1"}
    )

    assert response.status_code == 422
    assert "Couldn't tell which council covers York. Choose it from the list." in unescape(response.text)
    assert re.search(r'<select\b[^>]*name="council"', response.text)
    assert "Choose your council" in response.text
    assert 'value="YorkCouncil"' in response.text
    assert service.load_config(session) is None

    selected = client.post(
        "/settings/bins",
        data={"postcode": "yo10 3dd", "house_number": "1", "council": "YorkCouncil"},
    )
    assert selected.status_code == 422
    assert "York also needs your property's UPRN." in unescape(selected.text)
    assert 'name="uprn"' in selected.text
    assert service.load_config(session) is None


@pytest.mark.parametrize("missing", ["postcode", "house_number"])
def test_missing_address_field_is_read_only(client, session, monkeypatch, missing):
    def unexpected_lookup(postcode):
        pytest.fail("Incomplete addresses must not trigger a postcode lookup")

    monkeypatch.setattr(service, "lookup_postcode", unexpected_lookup)
    response = client.post("/settings/bins", data={**YORK_FORM, missing: "   "})

    assert response.status_code == 422
    assert "Enter your postcode and house number or name." in response.text
    assert service.load_config(session) is None


def test_nondigit_uprn_is_read_only(client, session):
    response = client.post("/settings/bins", data={**YORK_FORM, "uprn": "12a"})

    assert response.status_code == 422
    assert "A UPRN is only digits." in response.text
    assert 'name="uprn"' in response.text
    assert 'value="12a"' in response.text
    assert service.load_config(session) is None


def test_unknown_postcode_is_shown_without_saving(client, session, monkeypatch):
    def lookup(postcode):
        raise service.BinsError(service.POSTCODE_UNKNOWN)

    monkeypatch.setattr(service, "lookup_postcode", lookup)
    response = client.post("/settings/bins", data=YORK_FORM)

    assert response.status_code == 422
    assert service.POSTCODE_UNKNOWN in unescape(response.text)
    assert service.load_config(session) is None


def test_fetch_failure_preserves_form_without_saving(client, session, monkeypatch):
    def fetch(cfg):
        raise service.BinsError(service.NO_ANSWER, detail="private upstream diagnostics")

    monkeypatch.setattr(service, "fetch_collections", fetch)
    response = client.post("/settings/bins", data=YORK_FORM)

    assert response.status_code == 422
    assert f"Couldn't get bin dates. {service.NO_ANSWER}" in unescape(response.text)
    assert "private upstream diagnostics" not in response.text
    assert 'name="uprn"' in response.text
    assert 'value="100050535540"' in response.text
    assert 'value="YO10 3DD"' in response.text
    assert 'value="1"' in response.text
    assert service.load_config(session) is None
    assert all(settings_store.get(session, key) is None for key in KEYS)


def test_refresh_updates_collections_and_clears_error(client, session, monkeypatch):
    service.store(session, YORK_CONFIG, ROWS[:1])
    service.record_failure(session, "Previous failure")
    session.commit()
    fetched = []

    def fetch(cfg):
        fetched.append(cfg.copy())
        return ROWS

    monkeypatch.setattr(service, "fetch_collections", fetch)
    response = client.post("/settings/bins/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert fetched == [YORK_CONFIG]
    session.expire_all()
    assert json.loads(settings_store.get(session, "bins.collections")) == ROWS
    assert service.load_config(session) == YORK_CONFIG
    status = service.load_status(session)
    assert status["last_ok_at"] is not None
    assert status["last_error"] is None
    assert status["last_error_since"] is None


def test_delete_removes_all_bin_settings(client, session):
    service.store(session, YORK_CONFIG, ROWS)
    service.record_failure(session, "Previous failure")
    session.commit()
    assert all(settings_store.get(session, key) is not None for key in KEYS)

    response = client.post("/settings/bins/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    session.expire_all()
    assert all(settings_store.get(session, key) is None for key in KEYS)
    assert service.load_config(session) is None
    page = client.get("/settings")
    assert page.status_code == 200
    assert "RECYCLING" not in page.text


def bin_form(html):
    from bs4 import BeautifulSoup

    form = BeautifulSoup(html, "html.parser").select_one('#bins form[action="/settings/bins"]')
    return {field["name"]: field.get("value", "") for field in form.select("input[name]")}


def test_changed_postcode_discards_carried_council_and_uprn(client, session, monkeypatch):
    service.store(session, YORK_CONFIG, ROWS)
    session.commit()
    form = bin_form(client.get("/settings").text)
    module = next(module for module, entry in service.councils().items() if service.needs(entry) == ["postcode", "house_number"])
    # Deliberately synthetic postcode: no additional real address fixture.
    monkeypatch.setattr(service, "lookup_postcode", lambda postcode: {**YORK_PLACE, "postcode": postcode})
    monkeypatch.setattr(service, "council_for", lambda place: (module, None))
    monkeypatch.setattr(service, "fetch_collections", lambda cfg: ROWS)
    response = client.post("/settings/bins", data={**form, "postcode": "changed postcode"}, follow_redirects=False)
    assert response.status_code == 303
    saved = service.load_config(session)
    assert saved["council"] == module
    assert saved["postcode"] == "CHANGED POSTCODE"
    assert saved["uprn"] == ""


def test_changed_house_requests_a_new_uprn_instead_of_fetching_old_property(client, session):
    service.store(session, YORK_CONFIG, ROWS)
    session.commit()
    form = bin_form(client.get("/settings").text)
    response = client.post("/settings/bins", data={**form, "house_number": "-1"})
    assert response.status_code == 422
    assert "York also needs your property's UPRN." in unescape(response.text)
    assert bin_form(response.text)["uprn"] == ""
    assert service.load_config(session) == YORK_CONFIG


def test_no_uprn_fallback_selection_survives_fetch_failure_and_retry(client, session, monkeypatch):
    module = next(module for module, entry in service.councils().items() if service.needs(entry) == ["postcode", "house_number"])
    monkeypatch.setattr(service, "council_for", lambda place: (None, None))

    def fail(cfg):
        raise service.BinsError(service.TIMEOUT)

    monkeypatch.setattr(service, "fetch_collections", fail)
    failed = client.post("/settings/bins", data={**YORK_FORM, "council": module, "uprn": ""})
    assert failed.status_code == 422
    assert service.TIMEOUT in unescape(failed.text)
    monkeypatch.setattr(service, "fetch_collections", lambda cfg: ROWS)
    retry = client.post("/settings/bins", data=bin_form(failed.text), follow_redirects=False)
    assert retry.status_code == 303
    assert service.load_config(session)["council"] == module
