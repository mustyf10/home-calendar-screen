"""Phone Settings controls; provider calls happen only for explicit search."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from app.calendars.routes import _page
from app.config import config
from app.db import get_session
from app.weather import service
from app.weather.client import search_locations
from app.weather.types import WeatherError
from app.web import render

router = APIRouter()


@router.get("/settings/weather/search")
async def search(request: Request, q: str = ""):
    query = q.strip()
    results, error, status = [], None, 200
    if not query or len(query) > 100:
        error, status = "Enter a location search of 1–100 characters", 422
    elif not config.weather_api_key:
        error, status = "Weather API key is missing. Set WEATHER_API_KEY and recreate the container.", 422
    else:
        try:
            results = await search_locations(query, config.weather_api_key)
        except WeatherError as exc:
            error, status = str(exc), 502
    return render(request, "weather/search_results.html", {"results": results, "search_error": error, "query": query}, status_code=status)


@router.post("/settings/weather/location")
def select_location(request: Request, name: str = Form(""), region: str = Form(""), country: str = Form(""), lat: str = Form(""), lon: str = Form(""), session: Session = Depends(get_session)):
    try:
        service.save_location(sessionmaker(bind=session.get_bind(), expire_on_commit=False), {"name": name, "region": region, "country": country, "lat": lat, "lon": lon})
    except WeatherError as exc:
        weather = service.settings_context(session)
        weather["error"] = str(exc)
        return _page(request, session, 422, weather=weather)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/weather")
def save(request: Request, enabled: str = Form(""), mode: str = Form("daily"), unit: str = Form("celsius"), session: Session = Depends(get_session)):
    selected_enabled = enabled.lower() in ("on", "true", "1")
    try:
        service.save_preferences(sessionmaker(bind=session.get_bind(), expire_on_commit=False), enabled=selected_enabled, mode=mode, unit=unit, api_key=config.weather_api_key)
    except WeatherError as exc:
        weather = service.settings_context(session)
        weather["error"] = str(exc)
        weather["config"] = {**weather["config"], "enabled": selected_enabled, "mode": mode, "unit": unit}
        return _page(request, session, 422, weather=weather)
    return RedirectResponse("/settings", status_code=303)
