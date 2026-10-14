import dataclasses
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import backup, clock, settings_store


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    data, dest = tmp_path / "data", tmp_path / "backups"
    (data / "media" / "dishes").mkdir(parents=True)
    dest.mkdir()
    with sqlite3.connect(data / "app.db") as db:
        db.execute("create table t (x)")
        db.execute("insert into t values (42)")
    (data / "media" / "dishes" / "a.jpg").write_bytes(b"jpeg-a")
    monkeypatch.setattr(backup, "config", dataclasses.replace(backup.config, data_dir=data, backup_dir=dest))
    return data, dest


def test_backup_copies_db_and_media_and_prunes(session, dirs):
    data, dest = dirs
    old = dest / (clock.today() - timedelta(days=20)).isoformat()
    recent = dest / (clock.today() - timedelta(days=3)).isoformat()
    for d in (old, recent):
        d.mkdir()
        (d / "app.db").write_bytes(b"old")

    backup.run_backup(session)

    copy = dest / clock.today().isoformat() / "app.db"
    with sqlite3.connect(copy) as db:
        assert db.execute("select x from t").fetchall() == [(42,)]
    assert (dest / "media" / "dishes" / "a.jpg").read_bytes() == b"jpeg-a"
    assert not old.exists()
    assert recent.exists()
    ok_at = datetime.fromisoformat(settings_store.get(session, "backup_last_ok_at"))
    assert ok_at.utcoffset() == timedelta(0)
    assert settings_store.get(session, "backup_last_error") is None


def test_backup_mirrors_changed_media_only(session, dirs):
    data, dest = dirs
    backup.run_backup(session)
    mirrored = dest / "media" / "dishes" / "a.jpg"
    (data / "media" / "dishes" / "a.jpg").write_bytes(b"jpeg-a-edited")
    (data / "media" / "dishes" / "b.jpg").write_bytes(b"jpeg-b")

    backup.run_backup(session)

    assert mirrored.read_bytes() == b"jpeg-a-edited"
    assert (dest / "media" / "dishes" / "b.jpg").read_bytes() == b"jpeg-b"


def test_missing_backup_dir_records_error(session, dirs, monkeypatch):
    data, dest = dirs
    monkeypatch.setattr(backup, "config", dataclasses.replace(backup.config, backup_dir=dest / "nope"))
    backup.run_backup(session)
    assert settings_store.get(session, "backup_last_error") == "Backup dir missing"
    assert settings_store.get(session, "backup_last_ok_at") is None
    assert not (dest / "nope").exists()


def test_failure_records_error(session, dirs):
    data, dest = dirs
    (data / "app.db").unlink()
    (data / "app.db").mkdir()  # unreadable as a database
    backup.run_backup(session)
    assert settings_store.get(session, "backup_last_error")
    assert settings_store.get(session, "backup_last_ok_at") is None


def test_seconds_until_next_run():
    tz = clock.tz()
    assert backup.seconds_until_next_run(datetime(2026, 10, 21, 3, 0, tzinfo=tz)) == 30 * 60
    assert backup.seconds_until_next_run(datetime(2026, 10, 21, 3, 30, tzinfo=tz)) == 24 * 3600
