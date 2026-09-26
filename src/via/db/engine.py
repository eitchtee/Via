from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    event.listen(engine.sync_engine, "connect", _set_pragmas)
    return engine


SessionMaker = async_sessionmaker[AsyncSession]


def make_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def upgrade(db_path: Path) -> None:
    """Bring the database schema up to date (synchronous; run in a thread)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), "head")
