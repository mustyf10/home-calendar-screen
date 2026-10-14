from sqlalchemy.orm import Session

from app.models import AppSetting


# Key "pantry": newline-separated staples the shopping list moves to "Check you have".
DEFAULT_PANTRY = "\n".join([
    "salt", "black pepper", "oil", "water", "sugar",
    "cumin seeds", "turmeric", "chilli powder", "garam masala",
])

def get(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row else default


def put(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        row.value = value
    else:
        session.add(AppSetting(key=key, value=value))


def delete(session: Session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row:
        session.delete(row)
