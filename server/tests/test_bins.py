import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session

from app import settings_store
from app.bins import service


@pytest.fixture
def cfg():
    # UKBinCollectionData's public York sample, not a household address.
    return {
        "council": "YorkCouncil",
        "postcode": "YO10 3DD",
        "house_number": "1",
        "uprn": "100050535540",
    }


@pytest.fixture
def rows():
    return [
        {"type": "RECYCLING", "date": "2026-10-22"},
        {"type": "REFUSE", "date": "2026-10-29"},
    ]


def test_parse_result_sorts_deduplicates_and_skips_invalid_rows():
    result = service.parse_result(json.dumps({"bins": [
        {"type": "REFUSE", "collectionDate": "29/10/2026"},
        {"type": " RECYCLING ", "collectionDate": "22/10/2026"},
        {"type": "RECYCLING", "collectionDate": "22/10/2026"},
        {"type": "Garden waste", "collectionDate": "22/10/2026"},
        {"type": "REFUSE", "collectionDate": "Unknown"},
        {"type": "REFUSE", "collectionDate": "31/02/2026"},
        {"type": "REFUSE", "collectionDate": None},
        {"type": "REFUSE"},
        {"type": "  ", "collectionDate": "22/10/2026"},
        {"type": None, "collectionDate": "22/10/2026"},
    ]}))
    assert result == [
        {"type": "Garden waste", "date": "2026-10-22"},
        {"type": "RECYCLING", "date": "2026-10-22"},
        {"type": "REFUSE", "date": "2026-10-29"},
    ]


@pytest.mark.parametrize("stdout", [
    "not json", "null", "[]", "{}", '{"bins": []}',
    '{"bins": null}', '{"bins": {}}',
    '{"bins": [{"type": "REFUSE", "collectionDate": "Unknown"}]}',
    '{"bins": [null]}',
])
def test_parse_result_rejects_missing_or_unusable_dates(stdout):
    with pytest.raises(service.BinsError) as error:
        service.parse_result(stdout)
    assert str(error.value) == service.NO_DATES


PLACE = {"postcode": "YO10 3DD", "district": "York", "code": "E06000014"}


def test_lookup_postcode_returns_canonical_postcode():
    def respond(request):
        assert request.method == "GET"
        assert str(request.url) == service.POSTCODES_URL + "yo10%203dd"
        return httpx.Response(200, json={"result": {
            "postcode": "YO10 3DD", "admin_district": "York",
            "codes": {"admin_district": "E06000014"},
        }})

    assert service.lookup_postcode(
        "yo10 3dd", transport=httpx.MockTransport(respond)
    ) == PLACE


@pytest.mark.parametrize("status, message", [
    (404, "POSTCODE_UNKNOWN"), (429, "POSTCODE_FAILED"), (500, "POSTCODE_FAILED"),
])
def test_lookup_postcode_handles_http_errors(status, message):
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    with pytest.raises(service.BinsError) as error:
        service.lookup_postcode("YO10 3DD", transport=transport)
    assert str(error.value) == getattr(service, message)
    if status != 404:
        assert str(status) in str(error.value.detail)


def test_lookup_postcode_handles_connection_failure():
    def respond(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(service.BinsError) as error:
        service.lookup_postcode("YO10 3DD", transport=httpx.MockTransport(respond))
    assert str(error.value) == service.POSTCODE_FAILED
    assert error.value.detail == "ConnectError"


def test_council_for_york_and_unknown_place():
    assert service.council_for(PLACE) == ("YorkCouncil", None)
    assert service.council_for({"code": "unknown", "district": "No matching council"}) == (None, None)


def test_council_for_falls_back_to_casefolded_name():
    module, entry = next(iter(service.councils().items()))
    place = {"code": "unknown", "district": entry["wiki_name"].swapcase()}
    assert service.council_for(place) == (module, None)


def test_council_for_reports_browser_only_entry():
    entry = next(
        entry for entry in service.all_councils().values()
        if service.browser_only(entry) and entry.get("LAD24CD")
        and not any(
            service.usable(other) and other.get("LAD24CD") == entry["LAD24CD"]
            for other in service.all_councils().values()
        )
    )
    assert service.council_for({
        "code": entry["LAD24CD"], "district": entry["wiki_name"],
    }) == (None, entry)


@pytest.mark.parametrize("district, expected", [
    ("yOrK", "YorkCouncil"), ("Unmatched district", "FirstCouncil"),
])
def test_council_for_prefers_exact_name_then_catalog_order(monkeypatch, district, expected):
    catalog = {
        "BrowserCouncil": {"wiki_name": "York", "LAD24CD": "shared", "web_driver": True},
        "FirstCouncil": {"wiki_name": "First", "LAD24CD": "shared", "url": "https://example.com", "uprn": ""},
        "YorkCouncil": {"wiki_name": "York", "LAD24CD": "shared", "url": "https://example.com", "uprn": ""},
    }
    monkeypatch.setattr(service, "all_councils", lambda: catalog)
    assert service.council_for({"code": "shared", "district": district}) == (expected, None)


@pytest.mark.parametrize("extra", [
    {}, {"web_driver": False}, {"wiki_command_url_override": ""},
    {"usrn": ""}, {"supported_councils": []},
    {"custom_component_show_url_field": True},
])
def test_usable_excludes_unsupported_and_addressless_entries(extra):
    entry = {"url": "https://example.com", **extra}
    if extra:
        entry["uprn"] = ""
    assert not service.usable(entry)
    assert not service.usable({"uprn": ""})


def test_needs_orders_fields_and_accepts_paon():
    assert service.needs({"postcode": "", "paon": "", "uprn": ""}) == [
        "uprn", "postcode", "house_number",
    ]
    assert service.needs({"house_number": "", "paon": ""}) == ["house_number"]
    assert service.needs({}) == []
    assert service.usable({"url": "https://example.com", "postcode": "", "custom_component_show_url_field": False})


def test_councils_are_supported_addressed_and_sorted():
    catalog = service.councils()
    assert "YorkCouncil" in catalog
    assert list(catalog.values()) == sorted(catalog.values(), key=lambda entry: entry["wiki_name"])
    for module, entry in service.all_councils().items():
        assert (module in catalog) == service.usable(entry)
        if service.browser_only(entry) or not service.needs(entry):
            assert module not in catalog


def test_fetch_uses_declared_arguments_and_removes_credentials(cfg, rows, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-ai-key")
    monkeypatch.setenv("WEATHER_API_KEY", "private-weather-key")
    monkeypatch.setenv("BINS_TEST_KEEP", "keep")
    entry = service.councils()["YorkCouncil"]
    expected = [sys.executable, "-m", "app.bins.collect", "YorkCouncil", entry["url"]]
    if entry.get("skip_get_url"):
        expected.append("-s")
    expected.append("-u=100050535540")

    def run(argv, **kwargs):
        assert argv == expected
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == service.FETCH_TIMEOUT_S == 90
        assert Path(kwargs["cwd"]) == Path(__file__).resolve().parents[1]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert "WEATHER_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["BINS_TEST_KEEP"] == "keep"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"bins": [
            {"type": "REFUSE", "collectionDate": "29/10/2026"},
            {"type": "RECYCLING", "collectionDate": "22/10/2026"},
        ]}), stderr="")

    assert service.fetch_collections(cfg, run=run) == rows


def test_args_for_passes_only_declared_fields_and_uses_equals(cfg, monkeypatch):
    monkeypatch.setattr(service, "councils", lambda: {"YorkCouncil": {
        "url": "https://example.com", "postcode": "", "paon": "",
    }})
    assert service.args_for({**cfg, "house_number": "-1"}) == [
        "YorkCouncil", "https://example.com", "-p=YO10 3DD", "-n=-1",
    ]


def test_fetch_failure_redacts_address_and_keeps_only_last_line(cfg):
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=(
            "earlier diagnostic should not survive\n"
            "Failed for 100050535540 at YO10 3DD house 1\n\n"
        ))

    with pytest.raises(service.BinsError) as error:
        service.fetch_collections(cfg, run=run)
    assert str(error.value) == service.NO_ANSWER
    assert error.value.detail == "Failed for … at … house …"
    assert all(value not in error.value.detail for value in (cfg["uprn"], cfg["postcode"], cfg["house_number"]))


def test_stderr_tail_is_bounded_and_ignores_empty_values(cfg):
    assert service._tail("\n\n", cfg) == ""
    assert service._tail("ignored\n" + "x" * 400 + "\n", cfg) == "x" * 300
    assert service._tail("diagnostic", {"uprn": "", "postcode": "", "house_number": ""}) == "diagnostic"


def test_fetch_timeout_is_user_facing(cfg):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(service.BinsError) as error:
        service.fetch_collections(cfg, run=run)
    assert str(error.value) == service.TIMEOUT
    assert error.value.detail == "timed out"


def test_note_html_escapes_markup_but_allows_safe_links():
    note = str(service.note_html({"wiki_note": (
        '<script>alert("x")</script> [Help](https://example.com/?a=1&b=2) '
        '[Bad](javascript:alert)'
    )}))
    assert "<script>" not in note
    assert "&lt;script&gt;" in note
    assert '<a href="https://example.com/?a=1&amp;b=2" rel="noreferrer" target="_blank">Help</a>' in note
    assert '[Bad](javascript:alert)' in note
    assert service.note_html({}) == ""


def test_refresh_failure_preserves_good_rows_and_first_failure_time(session, cfg, rows, monkeypatch):
    service.store(session, cfg, rows)
    session.commit()
    original = service.load_status(session)

    def fail(config):
        assert config == cfg
        assert not session.in_transaction()
        raise service.BinsError(service.NO_ANSWER)

    monkeypatch.setattr(service, "fetch_collections", fail)
    assert service.refresh(session) is False
    first = service.load_status(session)
    assert first["last_error"].startswith("Couldn't get bin dates.")
    assert first["last_ok_at"] == original["last_ok_at"]
    assert datetime.fromisoformat(first["last_error_since"]).tzinfo == timezone.utc
    assert json.loads(settings_store.get(session, "bins.collections")) == rows

    # A sentinel earlier timestamp proves the second failure cannot reset it,
    # even when both refreshes happen within the same wall-clock second.
    first["last_error_since"] = "2026-10-18T12:00:00+00:00"
    settings_store.put(session, "bins.status", json.dumps(first))
    session.commit()
    assert service.refresh(session) is False
    assert service.load_status(session) == first
    assert json.loads(settings_store.get(session, "bins.collections")) == rows


def test_refresh_success_replaces_rows_and_clears_failure(session, cfg, rows, monkeypatch):
    service.store(session, cfg, rows[:1])
    service.record_failure(session, "Previous failure")
    session.commit()
    monkeypatch.setattr(service, "fetch_collections", lambda config: rows)
    assert service.refresh(session) is True
    assert json.loads(settings_store.get(session, "bins.collections")) == rows
    status = service.load_status(session)
    assert status["last_error"] is None
    assert status["last_error_since"] is None
    assert datetime.fromisoformat(status["last_ok_at"]).tzinfo == timezone.utc


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("removed", [False, True])
def test_refresh_discards_inflight_success_or_failure_after_config_change(
    session, cfg, rows, monkeypatch, fails, removed,
):
    service.store(session, cfg, rows)
    session.commit()
    replacement = {**cfg, "uprn": ""}

    def fetch(config):
        assert config == cfg
        assert not session.in_transaction()
        with Session(session.get_bind()) as writer:
            if removed:
                service.clear(writer)
            else:
                service.store(writer, replacement, rows[:1])
            writer.commit()
        if fails:
            raise service.BinsError(service.NO_ANSWER)
        return [{"type": "Stale in-flight result", "date": "2026-10-30"}]

    monkeypatch.setattr(service, "fetch_collections", fetch)
    assert service.refresh(session) is True
    assert service.load_config(session) == (None if removed else replacement)
    if removed:
        assert settings_store.get(session, "bins.collections") is None
        assert settings_store.get(session, "bins.status") is None
    else:
        assert json.loads(settings_store.get(session, "bins.collections")) == rows[:1]
        assert service.load_status(session)["last_error"] is None


def test_unconfigured_refresh_does_not_fetch_and_ignores_cached_rows(session, rows, monkeypatch):
    settings_store.put(session, "bins.collections", json.dumps(rows))
    session.commit()

    def unexpected_fetch(config):
        pytest.fail("An unconfigured service must not fetch")

    monkeypatch.setattr(service, "fetch_collections", unexpected_fetch)
    assert service.refresh(session) is True
    assert service.collections_by_day(session) == {}


def test_collections_by_day_groups_types_in_stored_order(session, cfg):
    service.store(session, cfg, [
        {"type": "Garden waste", "date": "2026-10-22"},
        {"type": "RECYCLING", "date": "2026-10-22"},
        {"type": "REFUSE", "date": "2026-10-29"},
    ])
    session.commit()
    assert service.collections_by_day(session) == {
        date(2026, 10, 22): ["Garden waste", "RECYCLING"],
        date(2026, 10, 29): ["REFUSE"],
    }
