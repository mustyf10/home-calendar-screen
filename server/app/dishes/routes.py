from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app import clock
from app.config import config
from app.db import get_session
from app.dishes.forms import DishInput, apply, core_tag_warning, similar_dishes, validate
from app.dishes.search import search_dishes
from app.menu.entries import menu_history
from app.models import DISH_TYPES, TYPE_LABELS, Dish
from app.tags.service import CORE_TAGS, existing_tag_names, normalize_tag
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
    })


@router.get("/dishes/new")
def new_dish(request: Request):
    return _form_page(request, DishInput(), {}, "/dishes", "Save recipe", "Add a recipe")


def _form_page(request, form, errors, action, submit_label, heading, status_code=200):
    return render(request, "dishes/edit.html", {
        "form": form,
        "errors": errors,
        "action": action,
        "submit_label": submit_label,
        "heading": heading,
        "extra_template": IMAGE_TEMPLATE,
    }, status_code=status_code)


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
    from app.ingest.images import save_dish_image

    try:
        return save_dish_image(data)
    except Exception:
        errors["image"] = "That file isn't an image we can read."
        return None


async def _validated_input(form, session: Session, dish_id: int | None = None):
    """Parses and validates the form, then stores any uploaded photo. Returns (input, errors, image_path)."""
    inp = DishInput.from_form(form)
    errors = validate(session, inp, dish_id)
    image = await _read_image(form, errors)
    image_path = None if errors else await run_in_threadpool(_save_image, image, errors)
    return inp, errors, image_path


async def _suggested_tags(session: Session, inp: DishInput) -> tuple[list[str], str]:
    """AI tags for a new recipe; never blocks saving. Returns (tags, error message)."""
    if not config.anthropic_api_key:
        return [], ""
    from app.ingest.llm import suggest_tags

    existing = existing_tag_names(session)
    try:
        tags = await run_in_threadpool(
            suggest_tags, inp.title, inp.type, inp.description, inp.ingredients, existing
        )
        return tags, ""
    except Exception as e:  # any failure: save anyway and tell the user
        return [], f"Couldn't suggest tags: {e}"


@router.post("/dishes")
async def create_dish(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    inp, errors, image_path = await _validated_input(form, session)
    if errors:
        return _form_page(request, inp, errors, "/dishes", "Save recipe", "Add a recipe", 422)
    extra, tag_error = await _suggested_tags(session, inp)
    dish = Dish(source_platform="manual", image_path=image_path)
    apply(session, dish, inp, extra_tags=extra)
    session.commit()
    params = {"created": 1}
    if tag_error:
        params["tag_error"] = tag_error
    return RedirectResponse(f"/dishes/{dish.id}?{urlencode(params)}", status_code=303)


@router.get("/dishes/{dish_id}")
def show_dish(
    request: Request,
    dish_id: int,
    created: bool = False,
    tag_error: str = "",
    session: Session = Depends(get_session),
):
    dish = _dish_or_404(session, dish_id)
    history = menu_history(session, [dish.id], clock.today()).get(dish.id)
    return render(request, "dishes/detail.html", {
        "dish": dish,
        "history": history if dish.type == "dinner" else None,
        "core_warning": core_tag_warning(dish.type, dish.tag_names),
        "similar": similar_dishes(session, dish.title, exclude_id=dish.id) if created else [],
        "flash": tag_error or None,
        "core_tags": CORE_TAGS,
    })


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
