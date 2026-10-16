"""Weekly shopping list: the week's dinners -> Claude -> aisles, cached by fingerprint."""
import hashlib
import threading
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings_store
from app.config import config
from app.models import Dish, MenuEntry, ShoppingList, utcnow

AISLES = ("Produce", "Meat & fish", "Dairy & eggs", "Bakery", "Tins & dry goods", "Frozen", "Other")
NO_KEY_MESSAGE = "Shopping list needs ANTHROPIC_API_KEY"
# Serialises builds so a manual rebuild and the background loop never call Claude together.
_build_lock = threading.Lock()

TOOL = {
    "name": "shopping_list",
    "description": "Save the combined shopping list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "aisles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(AISLES)},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "item": {"type": "string"},
                                    "quantity": {"type": "string"},
                                    "for": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["item", "quantity", "for"],
                            },
                        },
                    },
                    "required": ["name", "items"],
                },
            },
            "check_you_have": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["aisles", "check_you_have"],
    },
}


class ShoppingError(Exception):
    pass


def week_dinners(session: Session, start: date) -> list[tuple[date, Dish]]:
    rows = session.scalars(
        select(MenuEntry)
        .where(
            MenuEntry.kind == "dish",
            MenuEntry.date >= start,
            MenuEntry.date <= start + timedelta(days=6),
        )
        .order_by(MenuEntry.date)
    )
    return [(r.date, r.dish) for r in rows]


def pantry_text(session: Session) -> str:
    return settings_store.get(session, "pantry", settings_store.DEFAULT_PANTRY) or ""


def menu_fingerprint(session: Session, start: date) -> str:
    parts = sorted(
        f"{d.isoformat()}|{dish.id}|{dish.updated_at.isoformat()}"
        for d, dish in week_dinners(session, start)
    )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def fingerprint(session: Session, start: date) -> str:
    payload = menu_fingerprint(session, start) + "\n--\n" + pantry_text(session)
    return hashlib.sha256(payload.encode()).hexdigest()


def get_list(session: Session, start: date) -> ShoppingList | None:
    return session.get(ShoppingList, start)


def _system_prompt(pantry: str) -> str:
    staples = ", ".join(ln.strip() for ln in pantry.splitlines() if ln.strip()) or "(none)"
    return (
        "Combine the ingredients of these dinners into one shopping list in English. "
        "Merge duplicate ingredients and add quantities together when the units allow; "
        "keep quantities as written and never invent them (use an empty quantity if none is given). "
        "For each item, list the titles of the dinners that need it in `for`. "
        f"Group items into these aisles only: {', '.join(AISLES)}. "
        "Pantry staples the household normally has go in check_you_have instead of the aisles: "
        f"{staples}."
    )


def _call_claude(dinners: list[Dish], pantry: str) -> dict:
    import anthropic

    text = "\n\n".join(
        f"## {d.title}\n" + "\n".join(f"- {i}" for i in d.ingredients) for d in dinners
    )
    client = anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=120)
    try:
        resp = client.messages.create(
            model=config.llm_model,
            max_tokens=4096,
            system=_system_prompt(pantry),
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "shopping_list"},
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.APIError as e:
        raise ShoppingError(f"Couldn't build the list: {e}") from e
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        raise ShoppingError("Couldn't build the list: no list returned")
    return block.input


def _order_aisles(raw_aisles: list) -> list[dict]:
    merged: dict[str, list] = {name: [] for name in AISLES}
    for aisle in raw_aisles or []:
        name = aisle.get("name") if aisle.get("name") in AISLES else "Other"
        for it in aisle.get("items") or []:
            merged[name].append({
                "item": str(it.get("item") or "").strip(),
                "quantity": str(it.get("quantity") or "").strip(),
                "for": [str(t) for t in it.get("for") or []],
            })
    return [{"name": n, "items": items} for n, items in merged.items() if items]


def build_list(session: Session, start: date, *, only_if_stale: bool = False) -> ShoppingList:
    """Build and save the week's list. only_if_stale skips the Claude call when the saved
    list already matches the menu (e.g. a manual rebuild finished while the caller waited)."""
    with _build_lock:
        if not config.anthropic_api_key:
            raise ShoppingError(NO_KEY_MESSAGE)
        session.expire_all()  # menu/dishes may have changed while waiting for the lock
        row = session.get(ShoppingList, start)
        menu_fp = menu_fingerprint(session, start)
        if only_if_stale and row is not None and row.menu_fingerprint == menu_fp:
            return row
        # Fingerprints describe the state sent to Claude, so edits made during the call keep the list stale.
        full_fp = fingerprint(session, start)
        unique = list(dict.fromkeys(dish for _, dish in week_dinners(session, start)))
        with_ingredients = [d for d in unique if d.ingredients]
        pantry = pantry_text(session)
        data = {
            "aisles": [],
            "check_you_have": [],
            "no_ingredients": [d.title for d in unique if not d.ingredients],
            "dinners": [d.title for d in unique],
        }
        if with_ingredients:
            result = _call_claude(with_ingredients, pantry)
            data["aisles"] = _order_aisles(result.get("aisles"))
            data["check_you_have"] = [str(x) for x in result.get("check_you_have") or []]
        if row is None:
            row = ShoppingList(week_start=start)
        else:
            row.previous_data = row.data
        now = utcnow()
        row.fingerprint = full_fp
        row.menu_fingerprint = menu_fp
        row.data = data
        row.created_at = now
        row.clear_pending()
        row.last_attempt_at = now
        session.add(row)
        session.commit()
        return row


def _items_by_name(data: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for aisle in data.get("aisles") or []:
        for it in aisle.get("items") or []:
            out.setdefault(it["item"].strip().lower(), it)
    return out


def diff_lists(old: dict | None, new: dict) -> dict:
    """Items added, removed, or with a different quantity, matched by name across aisles."""
    diff = {"added": [], "removed": [], "changed": []}
    if old is None:
        return diff
    before, after = _items_by_name(old), _items_by_name(new)
    for key, it in after.items():
        if key not in before:
            diff["added"].append({"item": it["item"], "quantity": it.get("quantity", "")})
        else:
            was, now = (before[key].get("quantity") or "").strip(), (it.get("quantity") or "").strip()
            if was.lower() != now.lower():
                diff["changed"].append({"item": it["item"], "old": was, "new": now})
    for key, it in before.items():
        if key not in after:
            diff["removed"].append({"item": it["item"], "quantity": it.get("quantity", "")})
    for items in diff.values():
        items.sort(key=lambda x: x["item"].strip().lower())
    return diff


def plain_text(data: dict) -> str:
    blocks = []
    for aisle in data.get("aisles") or []:
        lines = [aisle["name"]]
        for it in aisle["items"]:
            qty = f" ({it['quantity']})" if it.get("quantity") else ""
            lines.append(f"- {it['item']}{qty}")
        blocks.append("\n".join(lines))
    if data.get("check_you_have"):
        blocks.append("\n".join(["Check you have", *(f"- {x}" for x in data["check_you_have"])]))
    if data.get("no_ingredients"):
        blocks.append("\n".join(["No ingredients saved", *(f"- {x}" for x in data["no_ingredients"])]))
    return "\n\n".join(blocks)
