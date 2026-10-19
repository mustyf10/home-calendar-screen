from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app import clock
from app.db import get_session
from app.dishes.forms import DishInput, apply, core_tag_warning, similar_dishes, validate
from app.dishes.search import search_dishes
from app.ingest import images, uploads
from app.ingest.jobs import ACTIVE, STATUS_TEXT, fills_from_source, latest_dish_jobs, placeholder_title, review_banners
from app.ingest.urls import platform_for
from app.menu.entries import menu_history
from app.models import DISH_TYPES, TYPE_LABELS, Dish, IngestJob
from app.tags.service import CORE_TAGS, normalize_tag
from app.web import render

router = APIRouter()

IMAGE_TEMPLATE = "dishes/_image_upload.html"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _dish_or_404(session: Session, dish_id: int) -> Dish:
    dish = session.get(Dish, dish_id)
    if dish is None:
        raise HTTPException(404, "Recipe not found")
    return dish


@router.get("/dishes")
def list_dishes(
    request: Request,
    q: str = "",
    type: str = "",
    tag: str = "",
    archived: bool = False,
    session: Session = Depends(get_session),
):
    type = type if type in DISH_TYPES else ""
    tag = normalize_tag(tag) or ""
    dishes = search_dishes(session, q, type=type or None, tag=tag or None, archived=archived)
    today = clock.today()
    history = menu_history(session, [d.id for d in dishes if d.type == "dinner"], today)
    keep = {"q": q, "archived": "1" if archived else ""}

    def url(**extra: str) -> str:
        return "/dishes?" + urlencode({k: v for k, v in {**keep, **extra}.items() if v})

    clear_tag_url = url(type=type)
    categories = [{"label": "All", "icon": "all", "href": url(tag=tag), "current": not type}] + [
        {"label": TYPE_LABELS[key], "icon": key, "href": url(type=key, tag=tag), "current": type == key}
        for key in DISH_TYPES
    ]
    jobs = latest_dish_jobs(session)
    failed_ids = [dish_id for dish_id, j in jobs.items() if j.status == "failed"]
    return render(request, "dishes/list.html", {
        "clear_tag_url": clear_tag_url,
        "categories": categories,
        "dishes": dishes,
        "history": history,
        "q": q,
        "type": type,
        "tag": tag,
        "archived": archived,
        "core_tags": CORE_TAGS,
        "importing": {dish_id for dish_id, j in jobs.items() if j.status in ACTIVE and fills_from_source(j)},
        "failed_imports": session.scalars(select(Dish).where(Dish.id.in_(failed_ids)).order_by(Dish.title)).all(),
        **review_banners(session),
    })


@router.get("/dishes/new")
def new_dish(request: Request, session: Session = Depends(get_session)):
    return _add_page(request, session, DishInput(), "", {})


def _form_page(request, form, errors, action, submit_label, heading, status_code=200, **extra):
    return render(request, "dishes/edit.html", {
        "form": form,
        "errors": errors,
        "action": action,
        "submit_label": submit_label,
        "heading": heading,
        "extra_template": IMAGE_TEMPLATE,
        **extra,
    }, status_code=status_code)


def _add_page(request, session: Session, form: DishInput, text: str, errors: dict, status_code=200):
    """Add a recipe: a link, caption text and screenshots fill in the recipe in the background."""
    return _form_page(request, form, errors, "/dishes", "Save recipe", "Add a recipe", status_code,
                      import_fields=True, text=text, **review_banners(session))


async def _read_image(form, errors: dict) -> bytes | None:
    upload = form.get("image")
    if not (isinstance(upload, UploadFile) and upload.filename):
        return None
    data = b"" if (upload.size or 0) > MAX_IMAGE_BYTES else await upload.read()
    if len(data) > MAX_IMAGE_BYTES or (upload.size or 0) > MAX_IMAGE_BYTES:
        errors["image"] = "Photos must be 10 MB or smaller."
        return None
    return data or None


def _save_image(data: bytes | None, errors: dict) -> str | None:
    if not data:
        return None
    try:
        return images.save_dish_image(data)
    except Exception:
        errors["image"] = "That file isn't an image we can read."
        return None


async def _validated_input(form, session: Session, dish_id: int | None = None,
                           require_title: bool = True, errors: dict | None = None):
    """Parses and validates the form, then stores any uploaded photo. Returns (input, errors, image_path)."""
    inp = DishInput.from_form(form)
    errors = {**(errors or {}), **validate(session, inp, dish_id, require_title)}
    image = await _read_image(form, errors)
    image_path = None if errors else await run_in_threadpool(_save_image, image, errors)
    return inp, errors, image_path


@router.post("/dishes")
async def create_dish(request: Request, session: Session = Depends(get_session)):
    """Saves at once and queues one background job; this request never fetches or calls Claude."""
    form = await request.form()
    text = (form.get("text") or "").strip()
    shots = uploads.files(form)
    screenshots, shots_error = await uploads.read_images(shots)
    has_source = bool((form.get("source_url") or "").strip() or text or shots)
    inp, errors, image_path = await _validated_input(
        form, session, require_title=not has_source, errors={"images": shots_error} if shots_error else None
    )
    if errors:
        return _add_page(request, session, inp, text, errors, 422)
    inp.title = inp.title or placeholder_title(inp.source_url, bool(screenshots))
    dish = Dish(source_platform="manual", image_path=image_path)
    apply(session, dish, inp)
    job = IngestJob(
        dish_id=dish.id,
        source_url=inp.source_url or None,
        user_text=text,
        platform=platform_for(inp.source_url) if inp.source_url else "upload",
        status="queued",
    )
    session.add(job)
    session.flush()
    job.upload_paths = uploads.save_uploads(job, screenshots)
    session.commit()
    return RedirectResponse(f"/dishes/{dish.id}?created=1", status_code=303)


def _detail_ctx(session: Session, dish: Dish, created: bool = False, **extra) -> dict:
    job = _latest_job(session, dish)
    active = job is not None and job.status in ACTIVE
    failed = job if job is not None and job.status == "failed" else None
    duplicate_id = (failed.extracted or {}).get("duplicate_dish_id") if failed else None
    history = menu_history(session, [dish.id], clock.today()).get(dish.id)
    return {
        "dish": dish,
        "history": history if dish.type == "dinner" else None,
        "core_warning": None if active else core_tag_warning(dish.type, dish.tag_names),
        "similar": similar_dishes(session, dish.title, exclude_id=dish.id) if created and not active else [],
        "core_tags": CORE_TAGS,
        "import_job": job if active else None,
        "failed_job": failed,
        "duplicate": session.get(Dish, duplicate_id) if duplicate_id else None,
        "can_fill": bool(dish.source_url and not dish.ingredients and not dish.steps and not active and not failed),
        "status_text": STATUS_TEXT,
        **extra,
    }


@router.get("/dishes/{dish_id}")
def show_dish(request: Request, dish_id: int, created: bool = False, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    return render(request, "dishes/detail.html", _detail_ctx(session, dish, created))


# --- Background fill ---------------------------------------------------------

def _latest_job(session: Session, dish: Dish) -> IngestJob | None:
    return latest_dish_jobs(session, [dish.id]).get(dish.id)


@router.get("/dishes/{dish_id}/import/status")
def import_status(request: Request, dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    job = _latest_job(session, dish)
    if job is None or job.status not in ACTIVE:
        return Response(headers={"HX-Refresh": "true"})  # finished: reload to show the filled recipe
    return render(request, "dishes/_import_status.html", {"dish": dish, "import_job": job, "status_text": STATUS_TEXT})


@router.post("/dishes/{dish_id}/import")
def start_import(dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    job = _latest_job(session, dish)
    if dish.source_url and (job is None or job.status not in ACTIVE):
        session.add(IngestJob(dish_id=dish.id, source_url=dish.source_url,
                              platform=platform_for(dish.source_url), status="queued"))
        session.commit()
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)


@router.post("/dishes/{dish_id}/import/retry")
async def retry_import(request: Request, dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    job = _latest_job(session, dish)
    if job is None or job.status != "failed":
        return RedirectResponse(f"/dishes/{dish.id}", status_code=303)
    form = await request.form()
    text = (form.get("text") or "").strip()
    added, error = await uploads.read_images(uploads.files(form))
    if error:
        return render(request, "dishes/detail.html",
                      _detail_ctx(session, dish, retry_error=error, retry_text=text), status_code=422)
    uploads.append_to_job(job, text, added)
    session.commit()
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)


@router.post("/dishes/{dish_id}/import/dismiss")
def dismiss_import(dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    job = _latest_job(session, dish)
    if job is not None and job.status == "failed":
        job.status = "discarded"
        session.commit()
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)


@router.get("/dishes/{dish_id}/edit")
def edit_dish(request: Request, dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    return _form_page(
        request, DishInput.from_dish(dish), {}, f"/dishes/{dish.id}", "Save changes", f"Edit {dish.title}"
    )


@router.post("/dishes/{dish_id}")
async def update_dish(request: Request, dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    form = await request.form()
    inp, errors, image_path = await _validated_input(form, session, dish.id)
    if errors:
        return _form_page(
            request, inp, errors, f"/dishes/{dish.id}", "Save changes", f"Edit {dish.title}", 422
        )
    if image_path:
        dish.image_path = image_path
    apply(session, dish, inp)
    session.commit()
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)


def _set_archived(session: Session, dish_id: int, archived: bool) -> RedirectResponse:
    dish = _dish_or_404(session, dish_id)
    dish.archived = archived
    session.commit()
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)


@router.post("/dishes/{dish_id}/archive")
def archive_dish(dish_id: int, session: Session = Depends(get_session)):
    return _set_archived(session, dish_id, True)


@router.post("/dishes/{dish_id}/restore")
def restore_dish(dish_id: int, session: Session = Depends(get_session)):
    return _set_archived(session, dish_id, False)
