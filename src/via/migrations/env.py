from alembic import context
from sqlalchemy import create_engine, pool

from via.db import models  # noqa: F401
from via.db.base import Base

config = context.config


def run_migrations() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,  # SQLite can't ALTER most things
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
