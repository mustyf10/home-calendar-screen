import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker


def test_reenable_fetches_immediately_and_rejects_old_generation(session):
    from app.weather.service import load_weather, run_once, save_location, save_preferences

    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    now = datetime(2026, 10, 18, 9, tzinfo=timezone.utc)
    location = {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1}
    save_location(factory, location)
    save_preferences(factory, enabled=True, mode="daily", unit="celsius", api_key="secret")
    calls = []

    async def provider(place, key, fetched_at):
        calls.append(place)
        if len(calls) == 1:
            save_preferences(factory, enabled=False, mode="daily", unit="celsius", api_key=None)
            save_preferences(factory, enabled=True, mode="daily", unit="celsius", api_key="secret")
        return {"location_key": "51.5,-0.1", "timezone": "Europe/London", "fetched_at": fetched_at.isoformat(), "days": [{"date": "2026-10-18", "high_c": 10, "low_c": 2}], "hours": []}

    assert asyncio.run(run_once(session_factory=factory, now=now, api_key="secret", provider=provider))
    with factory() as check:
        assert load_weather(check).snapshot is None
    assert asyncio.run(run_once(session_factory=factory, now=now + timedelta(seconds=5), api_key="secret", provider=provider))
    with factory() as check:
        assert load_weather(check).snapshot["fetched_at"] == (now + timedelta(seconds=5)).isoformat()
    assert not asyncio.run(run_once(session_factory=factory, now=now + timedelta(minutes=29), api_key="secret", provider=provider))
    assert len(calls) == 2


def test_failure_preserves_last_good_and_restart_schedule_and_preferences(session):
    from app import settings_store
    from app.weather.service import load_weather, run_once, save_location, save_preferences, settings_context
    from app.weather.types import WeatherError
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    now = datetime(2026, 10, 18, 9, tzinfo=timezone.utc)
    save_location(factory, {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1})
    save_preferences(factory, enabled=True, mode="daily", unit="celsius", api_key="secret")
    calls = []
    async def provider(place, key, fetched_at):
        calls.append(fetched_at)
        if len(calls) > 1:
            raise WeatherError("Weather API quota exceeded")
        save_preferences(factory, enabled=True, mode="3h", unit="fahrenheit", api_key="secret")
        return {"location_key": "51.5,-0.1", "timezone": "UTC", "fetched_at": fetched_at.isoformat(), "days": [{"date": "2026-10-18", "high_c": 10}], "hours": []}
    asyncio.run(run_once(session_factory=factory, now=now, api_key="secret", provider=provider))
    with factory() as check:
        first = load_weather(check)
        assert first.config["mode"] == "3h"
        assert first.config["unit"] == "fahrenheit"
        generation = first.config["fetch_generation"]
    save_preferences(factory, enabled=True, mode="2h", unit="celsius", api_key="secret")
    assert not asyncio.run(run_once(session_factory=factory, now=now + timedelta(minutes=29), api_key="secret", provider=provider))
    assert asyncio.run(run_once(session_factory=factory, now=now + timedelta(minutes=30), api_key="secret", provider=provider))
    with factory() as check:
        inputs = load_weather(check)
        assert inputs.snapshot == first.snapshot
        assert inputs.config["fetch_generation"] == generation
        assert settings_context(check)["last_error"] == "Weather API quota exceeded"
    assert not asyncio.run(run_once(session_factory=factory, now=now + timedelta(minutes=31), api_key="secret", provider=provider))


def test_location_aba_discards_inflight_result_and_clears_cache(session):
    from app.weather.service import load_weather, run_once, save_location, save_preferences
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    now = datetime(2026, 10, 18, 9, tzinfo=timezone.utc)
    london = {"name": "London", "region": "", "country": "UK", "lat": 51.5, "lon": -0.1}
    save_location(factory, london)
    save_preferences(factory, enabled=True, mode="daily", unit="celsius", api_key="secret")
    async def provider(place, key, fetched_at):
        save_location(factory, {**london, "lat": 42.98, "lon": -81.25})
        save_location(factory, london)
        return {"location_key": "51.5,-0.1", "timezone": "UTC", "fetched_at": fetched_at.isoformat(), "days": [{"date": "2026-10-18"}], "hours": []}
    asyncio.run(run_once(session_factory=factory, now=now, api_key="secret", provider=provider))
    with factory() as check:
        assert load_weather(check).snapshot is None


def test_corrupt_saved_json_is_safe_and_disabled(session):
    from app import settings_store
    from app.weather.service import load_weather, settings_context
    for raw in ('{bad', '[]', '{"enabled":true}', '{"enabled":true,"mode":[],"unit":"celsius","location":null,"fetch_generation":0}'):
        settings_store.put(session, "weather.config", raw)
        settings_store.put(session, "weather.snapshot", '[]')
        session.commit()
        assert load_weather(session).config["enabled"] is False
        assert load_weather(session).snapshot is None
        assert settings_context(session)["error"]
