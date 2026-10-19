from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.tags.service import CORE_TAGS, delete_tag, rename_tag, tag_usage
from app.web import render

router = APIRouter()


def _page(request: Request, session: Session, error: str | None = None, status_code: int = 200):
    if error and request.headers.get("HX-Request"):
        return PlainTextResponse(error, status_code=status_code)  # base.html shows it
    return render(request, "tags/list.html", {
        "rows": tag_usage(session),
        "core_tags": CORE_TAGS,
        "error": error,
    }, status_code=status_code)


def _done(request: Request):
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": "/tags"})
    return RedirectResponse("/tags", status_code=303)


@router.get("/tags")
def list_tags(request: Request, session: Session = Depends(get_session)):
    return _page(request, session)


@router.post("/tags/{tag_id}/rename")
def rename(request: Request, tag_id: int, name: str = Form(""), session: Session = Depends(get_session)):
    try:
        rename_tag(session, tag_id, name)
    except LookupError:
        raise HTTPException(404, "Tag not found")
    except ValueError as e:
        session.rollback()
        return _page(request, session, str(e), 422)
    return _done(request)


@router.post("/tags/{tag_id}/delete")
def delete(request: Request, tag_id: int, session: Session = Depends(get_session)):
    try:
        delete_tag(session, tag_id)
    except LookupError:
        raise HTTPException(404, "Tag not found")
    except ValueError as e:
        session.rollback()
        return _page(request, session, str(e), 422)
    return _done(request)
