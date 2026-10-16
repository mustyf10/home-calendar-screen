"""Live screen preview: the /display HTML and its 1-bit PNG side by side, with "what if" controls."""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app import clock
from app.db import get_session
from app.display.preview import parse_params, preview_model, version
from app.display.render import get_preview_frame
from app.display.scenarios import SCENARIO_LABELS
from app.web import render

log = logging.getLogger(__name__)
router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/preview")
def preview(request: Request, session: Session = Depends(get_session)):
    params = parse_params(request.query_params)
    _, note = preview_model(session, params)
    return render(request, "preview.html", {
        "params": params,
        "query": params.query(),
        "labels": SCENARIO_LABELS,
        "note": note,
        "version": version(session, params),
        "default_at": f"{clock.now():%Y-%m-%dT%H:%M}",
    })


@router.get("/preview/version")
def preview_version(request: Request, session: Session = Depends(get_session)):
    return PlainTextResponse(version(session, parse_params(request.query_params)), headers=NO_STORE)


@router.get("/preview/frame.png")
async def preview_frame(request: Request, session: Session = Depends(get_session)):
    """Accepts an extra, ignored `v` param so the page can bust the browser cache."""
    params = parse_params(request.query_params)
    try:
        png = await get_preview_frame(params, version(session, params))
    except Exception:
        log.exception("Preview render failed")
        return PlainTextResponse("render failed", status_code=503)
    return Response(png, media_type="image/png", headers=NO_STORE)
