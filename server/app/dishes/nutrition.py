"""Ingredient-based nutrition for a saved recipe: eligibility, estimation, storage and the detail-page state.

Nutrition comes only from the saved ingredient list (never the title) and is stored on `Dish.nutrition`:

    {"status": "ready", "fingerprint": ..., "totals": {7 whole-recipe numbers}, "estimated_servings": int | None,
     "assumptions": [str], "estimated_at": iso}
    {"status": "failed", "fingerprint": ..., "error": str, "estimated_at": iso}

Per-serving values are derived locally from the totals, so a serving-count edit never costs a Claude call.
The rough-estimate label is derived from quantity assumptions and whether the current serving count is estimated.
A failed marker is tied to the ingredient fingerprint it was tried for: it is never retried automatically,
only by an explicit POST or by changing the ingredients.
"""
import hashlib
import json
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.claude import call_json
from app.config import config
from app.db import SessionLocal, get_session
from app.errors import log_failure, user_message
from app.models import Dish, IngestJob, utcnow
from app.web import render

log = logging.getLogger(__name__)

router = APIRouter()

# stored key -> (JSON key, label, unit)
NUTRIENTS = (
    ("calories", "calories_kcal", "Calories", "kcal"),
    ("protein", "protein_g", "Protein", "g"),
    ("carbs", "carbs_g", "Carbs", "g"),
    ("fat", "fat_g", "Fat", "g"),
    ("fibre", "fibre_g", "Fibre", "g"),
    ("sugar", "sugar_g", "Sugar", "g"),
    ("salt", "salt_g", "Salt", "g"),
)
ENRICHMENT_STATUSES = ("queued", "fetching", "transcribing", "extracting")
NO_KEY = "Nutrition estimates need an Anthropic API key. Set ANTHROPIC_API_KEY to enable them."

SYSTEM = """Estimate the nutrition of one recipe from its ingredient list only.

Rules:
- Use only the ingredients listed. Never add ingredients that are not listed, and ignore anything else you may know about the dish.
- Give WHOLE-RECIPE totals (not per serving) for the full quantities listed: calories in kcal; protein, carbs, fat, fibre, sugar and salt in grams.
- Salt is salt in grams, not sodium. Use 0 for a nutrient the ingredients do not contain.
- When an ingredient has no quantity (or an unclear one), assume a sensible quantity and record each such assumption in "assumptions", e.g. "olive oil: assumed 1 tbsp". Use an empty list when every quantity was given.
- When the servings count is not given, estimate how many servings the listed ingredients make and return it in "estimated_servings". When it is given, return null."""

_NUMBER = {"type": "number"}
NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "totals": {
            "type": "object",
            "properties": {json_key: _NUMBER for _, json_key, _, _ in NUTRIENTS},
            "required": [json_key for _, json_key, _, _ in NUTRIENTS],
            "additionalProperties": False,
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "estimated_servings": {"type": ["integer", "null"]},
    },
    "required": ["totals", "assumptions", "estimated_servings"],
    "additionalProperties": False,
}


# --- Pure helpers -------------------------------------------------------------

def _ingredients(ingredients) -> list[str]:
    return [i.strip() for i in (str(v) for v in ingredients or []) if i.strip()]


def fingerprint(ingredients) -> str | None:
    """Hash of the saved ingredients, quantities and order included; None when there are none."""
    items = _ingredients(ingredients)
    if not items:
        return None
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()


def _dict(nutrition) -> dict:
    return nutrition if isinstance(nutrition, dict) else {}


def _count(servings, nutrition: dict) -> tuple[int, bool] | None:
    """(serving count, estimated?) or None when neither the recipe nor the estimate has one."""
    if isinstance(servings, int) and not isinstance(servings, bool) and servings > 0:
        return servings, False
    est = nutrition.get("estimated_servings")
    if isinstance(est, int) and not isinstance(est, bool) and est > 0:
        return est, True
    return None


def _stale(ingredients, servings, nutrition) -> bool:
    """True when saved ingredients exist and no stored result (success or failure) covers them."""
    fp = fingerprint(ingredients)
    if fp is None:
        return False
    nut = _dict(nutrition)
    if nut.get("fingerprint") != fp:
        return True
    return nut.get("status") == "ready" and _count(servings, nut) is None


def per_serving(dish: Dish) -> dict[str, float] | None:
    """The seven per-serving values from the stored totals, or None when there are none or no serving basis."""
    nut = _dict(dish.nutrition)
    basis = _count(dish.servings, nut)
    totals = nut.get("totals")
    if nut.get("status") != "ready" or basis is None or not isinstance(totals, dict):
        return None
    return {key: totals[key] / basis[0] for key, *_ in NUTRIENTS}


def _text(value: float, unit: str) -> str:
    return f"{round(value):d}" if unit == "kcal" else f"{round(value, 1):g}"


def validate_estimate(out: dict, servings: int | None) -> dict:
    """The model's answer as stored fields. Raises ValueError on anything malformed; nothing is clamped."""
    totals_in = out.get("totals")
    if not isinstance(totals_in, dict):
        raise ValueError("totals missing")
    totals = {}
    for key, json_key, _, _ in NUTRIENTS:
        v = totals_in.get(json_key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"bad value for {json_key}: {v!r}")
        totals[key] = float(v)
    est = out.get("estimated_servings")
    if servings is None:
        if isinstance(est, bool) or not isinstance(est, int) or est <= 0:
            raise ValueError(f"bad estimated servings: {est!r}")
    else:
        est = None
    assumptions = out.get("assumptions")
    if not isinstance(assumptions, list):
        raise ValueError("assumptions missing")
    return {
        "totals": totals,
        "estimated_servings": est,
        "assumptions": [a.strip() for a in assumptions if isinstance(a, str) and a.strip()],
    }


def estimate_nutrition(ingredients: list[str], servings: int | None) -> dict:
    """One Claude call from the ingredient list alone. Raises on a missing key, API error or unusable answer."""
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    # Legacy/manual recipes can contain zero or negative counts. Treat them as
    # missing consistently for both the prompt and response validation.
    if not isinstance(servings, int) or isinstance(servings, bool) or servings <= 0:
        servings = None
    items = _ingredients(ingredients)
    text = "INGREDIENTS:\n" + "\n".join(items) + "\n\nSERVINGS: " + (
        str(servings) if servings else "not given - estimate how many the ingredients make")
    out = call_json(SYSTEM, [{"type": "text", "text": text}], NUTRITION_SCHEMA, timeout=60, max_tokens=4096)
    return validate_estimate(out, servings)


# --- Worker side --------------------------------------------------------------

def _enriching(dish_id):
    return exists().where(IngestJob.dish_id == dish_id, IngestJob.status.in_(ENRICHMENT_STATUSES))


def next_dish(session: Session) -> int | None:
    """The oldest non-archived recipe whose saved ingredients have no estimate or failure marker, if we may call."""
    if not config.anthropic_api_key:
        return None
    rows = session.execute(
        select(Dish.id, Dish.ingredients, Dish.servings, Dish.nutrition)
        .where(Dish.archived.is_(False), ~_enriching(Dish.id))
        .order_by(Dish.id)
    )
    for dish_id, ingredients, servings, nutrition in rows:
        if _stale(ingredients, servings, nutrition):
            return dish_id
    return None


def _eligible(s: Session, dish: Dish) -> bool:
    return (not dish.archived and _stale(dish.ingredients, dish.servings, dish.nutrition)
            and not s.scalar(select(_enriching(dish.id))))


def run(dish_id: int) -> None:
    """Estimate one recipe. Blocking: the worker runs it in a thread. No DB transaction is open during the call."""
    if not config.anthropic_api_key:
        return
    with SessionLocal() as s:
        dish = s.get(Dish, dish_id)
        if dish is None or not _eligible(s, dish):
            return
        fp = fingerprint(dish.ingredients)
        if fp is None:
            return
        ingredients, servings = _ingredients(dish.ingredients), dish.servings

    result, error = None, None
    try:
        result = estimate_nutrition(ingredients, servings)
    except Exception as e:  # any failure becomes a visible marker for this ingredient snapshot
        log_failure(log, f"Nutrition estimate for dish {dish_id} failed", e)
        error = user_message("Couldn't estimate nutrition", e)

    stamp = utcnow().isoformat(timespec="seconds")
    if result is None:
        stored = {"status": "failed", "fingerprint": fp, "error": error, "estimated_at": stamp}
    else:
        stored = {
            "status": "ready",
            "fingerprint": fp,
            **result,
            "estimated_at": stamp,
        }
    _store(dish_id, fp, stored)


def _store(dish_id: int, fp: str, stored: dict) -> None:
    """Attach the result only if the recipe still has the snapshot's ingredients and isn't archived.

    The write is a conditional UPDATE on the row version just read, so an edit landing between read and write
    makes it match nothing; we then re-read and decide again. updated_at is left alone: nutrition is derived data
    and must not look like a recipe edit (the shopping list keys on it).
    """
    for _ in range(5):
        with SessionLocal() as s:
            row = s.execute(select(Dish.updated_at, Dish.ingredients, Dish.archived).where(Dish.id == dish_id)).first()
        if row is None or row.archived or fingerprint(row.ingredients) != fp:
            return  # changed or archived while Claude worked: this answer belongs to other inputs
        with SessionLocal() as s:  # a new transaction: a read snapshot upgraded to a write can fail as stale
            done = s.execute(
                update(Dish)
                .where(Dish.id == dish_id, Dish.archived.is_(False), Dish.updated_at == row.updated_at)
                .values(nutrition=stored, updated_at=row.updated_at)
                .execution_options(synchronize_session=False)
            ).rowcount
            s.commit()
            if done:
                return
    log.warning("Nutrition for dish %s kept changing; result dropped", dish_id)


# --- Detail page ----------------------------------------------------------------

def detail_context(dish: Dish) -> dict:
    """What the nutrition fragment shows. Pure: never calls Claude or touches the database.

    state: ready | pending | outdated | error | unavailable | missing_ingredients | archived.
    `polling` is true only while an estimate is expected to arrive on its own.
    """
    nut = _dict(dish.nutrition)
    fp = fingerprint(dish.ingredients)
    ctx: dict = {"state": None, "polling": False, "rows": [], "outdated": False, "rough": False, "servings": None,
           "servings_estimated": False, "assumptions": [], "error": None, "message": None}

    def with_values(outdated: bool) -> None:
        values = per_serving(dish)
        basis = _count(dish.servings, nut)
        if values is None or basis is None:
            return
        ctx.update(
            rows=[{"key": key, "label": label, "unit": unit, "text": _text(values[key], unit)}
                  for key, _, label, unit in NUTRIENTS],
            outdated=outdated, rough=bool(nut.get("assumptions")) or basis[1], servings=basis[0], servings_estimated=basis[1],
            assumptions=list(nut.get("assumptions") or []),
        )

    if fp is None:
        ctx.update(state="missing_ingredients", message="Add ingredients to estimate nutrition")
        return ctx
    current = nut.get("fingerprint") == fp
    if current and nut.get("status") == "ready" and _count(dish.servings, nut) is not None:
        with_values(False)
        ctx["state"] = "ready"
        return ctx
    if dish.archived:
        with_values(True)
        ctx.update(state="archived", message="Nutrition isn't estimated for archived recipes. Restore it to estimate.")
        return ctx
    if current and nut.get("status") == "failed":
        ctx.update(state="error", error=nut.get("error") or "The estimate failed.")
        return ctx
    if not config.anthropic_api_key:
        with_values(True)
        ctx.update(state="unavailable", message=NO_KEY)
        return ctx
    with_values(True)
    ctx.update(state="outdated" if ctx["rows"] else "pending", polling=True)
    return ctx


# --- Routes ----------------------------------------------------------------------

def _dish_or_404(session: Session, dish_id: int) -> Dish:
    dish = session.get(Dish, dish_id)
    if dish is None:
        raise HTTPException(404, "Recipe not found")
    return dish


@router.get("/dishes/{dish_id}/nutrition/status")
def nutrition_status(request: Request, dish_id: int, session: Session = Depends(get_session)):
    dish = _dish_or_404(session, dish_id)
    return render(request, "dishes/_nutrition.html", {"dish": dish, "nutrition": detail_context(dish)})


@router.post("/dishes/{dish_id}/nutrition")
def retry_nutrition(dish_id: int, session: Session = Depends(get_session)):
    """Explicit retry: forget the failed marker so the worker tries these ingredients again."""
    dish = _dish_or_404(session, dish_id)
    if _dict(dish.nutrition).get("status") == "failed":
        session.execute(update(Dish).where(Dish.id == dish.id).values(nutrition=None, updated_at=Dish.updated_at))
        session.commit()  # updated_at kept: clearing a marker is not a recipe edit
    return RedirectResponse(f"/dishes/{dish.id}", status_code=303)
