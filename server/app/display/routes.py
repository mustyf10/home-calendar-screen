import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.display.preview import parse_params, preview_model
from app.display.render import get_frame
from app.web import render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/display")
def display(request: Request, session: Session = Depends(get_session)):
    """The panel's layout; preview query params (see preview.parse_params) only change what it shows."""
    model, _ = preview_model(session, parse_params(request.query_params))
    return render(request, "display.html", {"m": model})


@router.get("/display/frame.png")
async def frame(request: Request):
    try:
        png, etag = await get_frame()
    except Exception:
        log.exception("Frame render failed")
        return PlainTextResponse("render failed", status_code=503)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(png, media_type="image/png", headers=headers)
