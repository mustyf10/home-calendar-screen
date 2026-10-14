"""Shared Jinja environment, filters and nav globals."""
from datetime import date
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.clock import fmt_day
from app.db import SessionLocal
from app.models import TYPE_LABELS, IngestJob

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _drafts_count() -> int:
    with SessionLocal() as s:
        return s.scalar(
            select(func.count(IngestJob.id)).where(
                IngestJob.status == "ready", IngestJob.dish_id.is_(None)
            )
        ) or 0


def _fmt_day(d: date | None) -> str:
    return fmt_day(d) if d else ""


templates.env.globals["drafts_count"] = _drafts_count
templates.env.globals["TYPE_LABELS"] = TYPE_LABELS
templates.env.filters["day"] = _fmt_day


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, ctx or {}, status_code=status_code)
