import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.orm import Session

from app import clock
from app.db import get_session
from app.display.data import build_display
from app.display.render import get_frame
from app.web import render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/display")
def display(request: Request, session: Session = Depends(get_session)):
    return render(request, "display.html", {"m": build_display(session, clock.now())})


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
