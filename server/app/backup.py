"""Nightly backup: dated SQLite snapshots plus a single mirror of media/."""
import asyncio
import logging
import re
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app import clock, settings_store
from app.config import config
from app.db import SessionLocal

log = logging.getLogger(__name__)
RUN_AT = time(3, 30)
KEEP_DAYS = 14
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


def _snapshot_db(dest_dir) -> None:
    dest_dir.mkdir(exist_ok=True)
    tmp = dest_dir / "app.db.partial"
    src = sqlite3.connect(f"file:{config.data_dir / 'app.db'}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(dest_dir / "app.db")


def _mirror_media() -> None:
    src_root, dst_root = config.media_dir, config.backup_dir / "media"
    if not src_root.is_dir():
        return
    for src in src_root.rglob("*"):
        if not src.is_file():
            continue
        dst = dst_root / src.relative_to(src_root)
        st = src.stat()
        if dst.exists():
            dt = dst.stat()
            if dt.st_size == st.st_size and int(dt.st_mtime) == int(st.st_mtime):
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _prune(today: date) -> None:
    cutoff = today - timedelta(days=KEEP_DAYS)
    for d in config.backup_dir.iterdir():
        if d.is_dir() and _DATED.fullmatch(d.name) and date.fromisoformat(d.name) < cutoff:
            shutil.rmtree(d)


def run_backup(session: Session) -> None:
    if not config.backup_dir.is_dir():
        settings_store.put(session, "backup_last_error", "Backup dir missing")
        session.commit()
        return
    try:
        today = clock.today()
        _snapshot_db(config.backup_dir / today.isoformat())
        _mirror_media()
        _prune(today)
    except Exception as e:
        log.exception("Backup failed")
        settings_store.put(session, "backup_last_error", f"{type(e).__name__}: {e}")
        session.commit()
        return
    settings_store.put(session, "backup_last_ok_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    settings_store.delete(session, "backup_last_error")
    session.commit()


def seconds_until_next_run(now: datetime) -> float:
    target = datetime.combine(now.date(), RUN_AT, tzinfo=now.tzinfo)
    if target <= now:
        target = datetime.combine(now.date() + timedelta(days=1), RUN_AT, tzinfo=now.tzinfo)
    # Subtract in UTC so DST changes are accounted for.
    return (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()


def _backup_now() -> None:
    with SessionLocal() as session:
        run_backup(session)


async def run_backup_loop() -> None:
    while True:
        await asyncio.sleep(seconds_until_next_run(clock.now()))
        try:
            await asyncio.to_thread(_backup_now)
        except Exception:
            log.exception("Backup loop failed")
