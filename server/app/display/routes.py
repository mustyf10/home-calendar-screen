import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.calendars.routes import _page
from app.db import get_session
from app.display.preview import parse_params, preview_model
from app.display.render import get_frame
from app.display.settings import load_policy, save_policy
from app.errors import UserFacingError, log_failure, user_message
from app.web import render

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/display")
def display(request: Request, session: Session = Depends(get_session)):
    """The panel's layout; preview query params (see preview.parse_params) only change what it shows."""
    model, _ = preview_model(session, parse_params(request.query_params))
    return render(request, "display.html", {"m": model})


@router.get("/display/frame.png")
async def frame(request: Request, session: Session = Depends(get_session)):
    headers = {"X-Display-Policy": json.dumps(load_policy(session), separators=(",", ":"))}
    try:
        png, etag = await get_frame()
    except Exception:
        log.exception("Frame render failed")
        return PlainTextResponse("render failed", status_code=503, headers=headers)
    headers.update({"ETag": etag, "Cache-Control": "no-cache"})
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(png, media_type="image/png", headers=headers)


@router.post("/settings/display")
def update_display(
    request: Request,
    night_enabled: str = Form(""),
    night_start: str = Form("23:00"),
    night_end: str = Form("06:00"),
    session: Session = Depends(get_session),
):
    enabled = night_enabled == "on"
    try:
        save_policy(session, night_enabled=enabled, night_start=night_start, night_end=night_end)
        session.commit()
    except Exception as exc:
        session.rollback()
        if isinstance(exc, UserFacingError):
            message = str(exc)
        else:
            log_failure(log, "Saving display settings failed", exc)
            message = user_message("Couldn't save display settings", exc)
        ctx = {
            "config": {
                "night_enabled": enabled, "night_start": night_start,
                "night_end": night_end, "timezone": load_policy(session)["timezone"],
            },
            "error": message,
        }
        return _page(request, session, 422, display=ctx)
    return RedirectResponse("/settings", status_code=303)
