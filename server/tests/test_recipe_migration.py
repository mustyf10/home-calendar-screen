"""Exercise the real Alembic entry point on isolated household data."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


SERVER = Path(__file__).resolve().parents[1]


def migrate(tmp_path, revision, direction="upgrade"):
    env = {**os.environ, "DATA_DIR": str(tmp_path), "BACKUP_DIR": str(tmp_path)}
    subprocess.run([sys.executable, "-m", "alembic", direction, revision], cwd=SERVER, env=env, check=True, capture_output=True, text=True)


def test_upgrade_preserves_linked_recipes_and_accepts_breakfast(tmp_path):
    migrate(tmp_path, "0002")
    db = sqlite3.connect(tmp_path / "app.db")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("""INSERT INTO dishes
        (id,title,type,description,ingredients,steps,source_platform,scan_paths,notes,archived,created_at,updated_at)
        VALUES (1,'Daal','dinner','','[\"200g lentils\"]','[]','manual','[]','',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),
               (2,'Old dish','side','','[]','[]','manual','[]','',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
    db.execute("INSERT INTO dish_tags VALUES (1,1)")
    db.execute("INSERT INTO menu_entries VALUES ('2026-11-04','dish',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    db.execute("""INSERT INTO ingest_jobs
        (dish_id,user_text,upload_paths,status,created_at,updated_at)
        VALUES (1,'','[]','ready',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
    db.commit()
    migrate(tmp_path, "head")
    assert db.execute("SELECT id,title,archived,nutrition,generated_recipe FROM dishes ORDER BY id").fetchall() == [(1, "Daal", 0, None, None), (2, "Old dish", 1, None, None)]
    assert db.execute("SELECT * FROM dish_tags").fetchall() == [(1, 1)]
    assert db.execute("SELECT dish_id FROM menu_entries").fetchall() == [(1,)]
    assert db.execute("SELECT dish_id FROM ingest_jobs").fetchall() == [(1,)]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.execute("UPDATE dishes SET type='breakfast' WHERE id=1")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE dishes SET type='invalid' WHERE id=1")
    db.rollback()
    migrate(tmp_path, "0002", "downgrade")
    assert db.execute("SELECT type FROM dishes WHERE id=1").fetchone() == ("dinner",)
    assert db.execute("SELECT * FROM dish_tags").fetchall() == [(1, 1)]
    migrate(tmp_path, "head")
    db.close()


def test_fresh_database_reaches_new_recipe_schema(tmp_path):
    migrate(tmp_path, "head")
    with sqlite3.connect(tmp_path / "app.db") as db:
        names = {row[1] for row in db.execute("PRAGMA table_info(dishes)")}
        assert {"nutrition", "generated_recipe"} <= names
