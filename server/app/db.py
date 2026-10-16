import json
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import config


def make_engine(url: str) -> Engine:
    memory = url.endswith(":memory:")
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        json_serializer=lambda o: json.dumps(o, ensure_ascii=False),
        # One shared connection so an in-memory DB is visible from every thread.
        poolclass=StaticPool if memory else None,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        if not memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


engine = make_engine(f"sqlite:///{config.data_dir / 'app.db'}")
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
