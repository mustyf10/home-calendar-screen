"""Screens get plain-language failures; raw exception text stays out of them."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.calendars import ics
from app.calendars import routes as calendar_routes
from app.db import get_session
from app.errors import SEE_LOG, UserFacingError, user_message
from app.main import app
from app.models import Calendar
from app.web import templates

SIGN_IN_PAGE = b'<!doctype html><html lang="en-US"><head><base href="https://accounts.google.com/v3/signin/">'
SHARE_LINK = "https://calendar.google.com/calendar/u/0/r?cid=ZmFtaWx5QGdyb3VwLmNhbGVuZGFyLmdvb2dsZS5jb20"


@pytest.fixture
def serve_http(monkeypatch):
    """Route the calendar fetcher's HTTP through a handler instead of the network."""
    real_client = httpx.Client

    def use(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(ics.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))

    return use


def google_share_link(request):
    if request.url.host == "calendar.google.com":
        return httpx.Response(302, headers={"location": "https://accounts.google.com/v3/signin/identifier"})
    return httpx.Response(200, content=SIGN_IN_PAGE, headers={"content-type": "text/html"})


@pytest.fixture
def client(session, monkeypatch):
    monkeypatch.setitem(templates.env.globals, "drafts_count", lambda: 0)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_unexpected_failures_point_to_the_log_without_their_text():
    message = user_message("Couldn't add that calendar", RuntimeError("Content line could not be parsed: <!doctype html>"))
    assert message == f"Couldn't add that calendar. {SEE_LOG}"


def test_known_failures_say_what_to_do():
    assert user_message("Couldn't add that calendar", httpx.ConnectTimeout("timed out")) == (
        "Couldn't add that calendar. The other site took too long to answer. Try again in a minute."
    )
    assert user_message("Backup failed", UserFacingError("The disk is full.", detail="ENOSPC")) == "Backup failed. The disk is full."


def test_google_share_link_explains_which_address_to_copy(serve_http):
    serve_http(google_share_link)
    with pytest.raises(ics.FeedError) as caught:
        ics.fetch_ics(SHARE_LINK)
    assert str(caught.value) == ics.NOT_A_FEED_GOOGLE
    assert "<!doctype" in caught.value.detail  # the log still sees what came back


def test_other_web_pages_are_not_mistaken_for_feeds(serve_http):
    serve_http(lambda request: httpx.Response(200, content=b"<html><body>Hello</body></html>"))
    with pytest.raises(ics.FeedError, match="opened a web page"):
        ics.fetch_ics("https://example.com/calendar")


@pytest.mark.parametrize(("status", "words"), [(404, "No calendar was found"), (403, "refused access"), (503, "HTTP 503")])
def test_http_failures_are_explained(serve_http, status, words):
    serve_http(lambda request: httpx.Response(status))
    with pytest.raises(ics.FeedError, match=words) as caught:
        ics.fetch_ics("https://example.com/feed.ics")
    assert caught.value.detail == f"HTTP {status}"


def test_a_feed_with_a_byte_order_mark_still_loads(serve_http):
    serve_http(lambda request: httpx.Response(200, content=b"\xef\xbb\xbfBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"))
    assert ics.fetch_ics("webcal://example.com/feed.ics").startswith(b"\xef\xbb\xbfBEGIN:VCALENDAR")


def test_adding_a_share_link_shows_advice_not_the_sign_in_page(client, session, serve_http, caplog):
    serve_http(google_share_link)
    resp = client.post("/settings/calendars", data={"name": "Family", "url": SHARE_LINK})
    assert resp.status_code == 422
    assert "Secret address in iCal format" in resp.text
    assert "accounts.google.com/v3/signin" not in resp.text
    assert session.query(Calendar).count() == 0
    logged = caplog.text
    assert "FeedError" in logged and "doctype" in logged
    assert "cid=" not in logged and "Z2xl" not in logged  # no part of the address's query reaches the log


def test_feed_tokens_never_reach_the_log(client, serve_http, caplog):
    serve_http(lambda request: httpx.Response(404))
    client.post("/settings/calendars", data={"name": "Work", "url": "https://example.com/feed.ics?token=abc123"})
    assert "example.com #" in caplog.text
    assert "abc123" not in caplog.text


def test_a_malformed_address_is_a_form_error_not_a_crash(client):
    resp = client.post("/settings/calendars", data={"name": "Work", "url": "https://[broken"})
    assert resp.status_code == 422
    assert ics.INVALID_ADDRESS.replace("'", "&#39;") in resp.text
    assert 'value="https://[broken"' in resp.text  # what was typed is kept for fixing


def test_missing_pages_get_a_page_not_json(client):
    resp = client.get("/dishes/999999", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert "It may have been deleted" in resp.text
    assert client.get("/dishes/999999", headers={"accept": "application/json"}).json() == {"detail": "Recipe not found"}


def test_htmx_requests_get_the_message_as_text(client, session):
    resp = client.post("/menu/2026-10-21", data={"dish_id": "999999"}, headers={"HX-Request": "true"})
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.startswith("That no longer exists.")


def test_invalid_htmx_requests_get_text_not_validation_json(client):
    resp = client.get("/menu/not-a-date/row", headers={"HX-Request": "true"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("text/plain")
    assert "wasn't valid" in resp.text
    assert client.get("/menu/not-a-date/row").json()["detail"][0]["loc"] == ["path", "d"]  # API callers keep the detail


def test_crashes_show_a_friendly_page_and_hide_the_exception(client, monkeypatch):
    def broken(session):
        raise RuntimeError("database file /data/app.db is locked")

    monkeypatch.setattr(calendar_routes, "settings_context", broken)
    resp = client.get("/settings", headers={"accept": "text/html"})
    assert resp.status_code == 500
    assert SEE_LOG in resp.text
    assert "locked" not in resp.text
