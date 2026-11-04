from alembic import context

from app.db import engine
from app.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with engine.connect() as connection:
        # SQLite batch migrations replace tables referenced by other tables.
        # Disable enforcement before the migration transaction, then validate
        # every relationship before committing and restore it on this connection.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise RuntimeError("Migration left broken foreign-key references")
            connection.commit()
        finally:
            connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


run_migrations_online()
