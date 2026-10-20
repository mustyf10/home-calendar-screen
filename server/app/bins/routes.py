"""Postcode-first bin setup and explicit refresh/removal from Settings."""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.bins import service
from app.calendars.routes import _page
from app.db import get_session
from app.errors import log_failure, user_message

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/settings/bins")
def save(
    request: Request,
    postcode: str = Form(""),
    house_number: str = Form(""),
    uprn: str = Form(""),
    council: str = Form(""),
    council_postcode: str = Form(""),
    council_house_number: str = Form(""),
    session: Session = Depends(get_session),
):
    postcode, house_number, uprn, council = postcode.strip().upper(), house_number.strip(), uprn.strip(), council.strip()
    # A carried council/UPRN belongs to the address that produced the form.
    if council_postcode and (
        "".join(postcode.split()) != "".join(council_postcode.upper().split())
        or house_number != council_house_number.strip()
    ):
        council, uprn = "", ""

    def fail(message, **extra):
        ctx = service.settings_context(session)
        ctx.update(form={"postcode": postcode, "house_number": house_number, "uprn": uprn, "council": council}, error=message)
        ctx.update(extra)
        return _page(request, session, 422, bins=ctx)

    if not postcode or not house_number:
        return fail("Enter your postcode and house number or name.")
    try:
        place = service.lookup_postcode(postcode)
    except service.BinsError as exc:
        log_failure(log, "Bin postcode lookup failed", exc)
        return fail(str(exc))
    postcode = place["postcode"]
    if council in service.councils():
        module, blocked = council, None
    else:
        module, blocked = service.council_for(place)
    if module is None:
        if blocked:
            return fail(f"{blocked['wiki_name']}'s bin lookup needs a web browser, which the kitchen Pi doesn't have.")
        return fail(
            f"Couldn't tell which council covers {place['district']}. Choose it from the list.",
            choose_council=[(module, entry["wiki_name"]) for module, entry in service.councils().items()],
        )
    council = module
    entry = service.councils()[module]
    need_uprn = "uprn" in service.needs(entry)
    details = {"need_uprn": need_uprn, "note": service.note_html(entry), "council_name": entry["wiki_name"]}
    if need_uprn and not uprn:
        return fail(f"{entry['wiki_name']} also needs your property's UPRN.", **details)
    if uprn and not uprn.isdigit():
        return fail("A UPRN is only digits.", **{**details, "need_uprn": True})
    cfg = {"council": module, "postcode": postcode, "house_number": house_number, "uprn": uprn if need_uprn else ""}
    session.rollback()
    try:
        rows = service.fetch_collections(cfg)
    except Exception as exc:
        log_failure(log, f"Bin dates from {module} failed", exc)
        return fail(user_message(service.FAILED, exc), **details)
    service.store(session, cfg, rows)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/bins/refresh")
def refresh(session: Session = Depends(get_session)):
    service.refresh(session)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/bins/delete")
def delete(session: Session = Depends(get_session)):
    service.clear(session)
    session.commit()
    return RedirectResponse("/settings", status_code=303)
