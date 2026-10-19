import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backup import run_backup_loop
from app.calendars import routes as calendar_routes
from app.calendars.sync import run_sync_loop
from app.config import config
from app.dishes import routes as dish_routes
from app.display import preview_routes
from app.display import routes as display_routes
from app.errors import SEE_LOG
from app.ingest import routes as ingest_routes
from app.ingest.worker import run_worker
from app.menu import routes as menu_routes
from app.shopping import routes as shopping_routes
from app.shopping.auto import run_shopping_loop
from app.tags import routes as tag_routes
from app.weather import routes as weather_routes
from app.weather.service import run_weather_loop
from app.web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loops = (run_worker, run_sync_loop, run_backup_loop, run_shopping_loop, run_weather_loop)
    tasks = [asyncio.create_task(c()) for c in loops]
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
config.media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=config.media_dir), name="media")

for module in (
    dish_routes,
    tag_routes,
    menu_routes,
    shopping_routes,
    ingest_routes,
    calendar_routes,
    display_routes,
    preview_routes,
    weather_routes,
):
    app.include_router(module.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/menu", status_code=303)


log = logging.getLogger(__name__)


def _friendly(request: Request, status_code: int, heading: str, message: str):
    """HTMX gets the message as text (base.html shows it); browsers get a page."""
    if request.headers.get("HX-Request"):
        return PlainTextResponse(message, status_code=status_code)
    try:
        return render(request, "error.html", {"heading": heading, "message": message}, status_code=status_code)
    except Exception:  # the page shell itself failed (e.g. the database): plain text still explains
        log.exception("Error page render failed")
        return PlainTextResponse(f"{heading}. {message}", status_code=status_code)


def _for_people(request: Request) -> bool:
    return bool(request.headers.get("HX-Request")) or "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def friendly_http_error(request: Request, exc: StarletteHTTPException):
    if not _for_people(request):
        return await http_exception_handler(request, exc)
    log.info("%s %s -> %s %s", request.method, request.url.path, exc.status_code, exc.detail)
    if exc.status_code == 404:
        return _friendly(request, 404, "Not found", "That no longer exists. It may have been deleted; reload the page to see the latest.")
    return _friendly(request, exc.status_code, "Couldn't do that", "The app couldn't handle that request. Reload the page and try again.")


@app.exception_handler(RequestValidationError)
async def friendly_validation_error(request: Request, exc: RequestValidationError):
    if not _for_people(request):
        return await request_validation_exception_handler(request, exc)
    log.info("%s %s -> 422 %s", request.method, request.url.path, exc.errors())
    return _friendly(request, 422, "Couldn't do that", "Something in that request wasn't valid. Reload the page and try again.")


@app.exception_handler(Exception)
async def friendly_server_error(request: Request, exc: Exception):
    # Starlette re-raises after this handler, so the server log gets the full traceback.
    log.error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    if not _for_people(request):
        return PlainTextResponse("Internal Server Error", status_code=500)
    return _friendly(request, 500, "Something went wrong", SEE_LOG)
