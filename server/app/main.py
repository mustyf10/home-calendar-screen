import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.backup import run_backup_loop
from app.calendars import routes as calendar_routes
from app.calendars.sync import run_sync_loop
from app.config import config
from app.dishes import routes as dish_routes
from app.display import routes as display_routes
from app.ingest import routes as ingest_routes
from app.ingest.worker import run_worker
from app.menu import routes as menu_routes
from app.shopping import routes as shopping_routes
from app.tags import routes as tag_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(c()) for c in (run_worker, run_sync_loop, run_backup_loop)]
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
):
    app.include_router(module.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/menu", status_code=303)
