"""Engine, session factory, ``Base`` and migration helper. DB lives at ``DATA_DIR/app.db`` (SQLite, WAL)."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from treinvertraging.config import get_settings
from treinvertraging.models import Base

__all__ = [
    "Base",
    "dispose_engines",
    "get_engine",
    "get_session_factory",
    "init_db",
    "make_engine",
    "session_scope",
]

_lock = threading.Lock()
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}


def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
    finally:
        cur.close()


def make_engine(url: str) -> Engine:
    """Build an engine for ``url`` with the SQLite pragmas applied (no caching)."""
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    """Engine for the current ``DATA_DIR`` (cached per URL, so tests with a tmp DATA_DIR work)."""
    settings = get_settings()
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = settings.db_url
    with _lock:
        if url not in _engines:
            _engines[url] = make_engine(url)
        return _engines[url]


def get_session_factory() -> sessionmaker[Session]:
    engine = get_engine()
    key = str(engine.url)
    with _lock:
        if key not in _factories:
            _factories[key] = sessionmaker(bind=engine, expire_on_commit=False)
        return _factories[key]


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engines() -> None:
    """Close and forget all cached engines (used by tests)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
        _factories.clear()


def _alembic_dir() -> Path:
    """Locate the ``alembic`` directory (override with ``ALEMBIC_DIR``)."""
    override = os.environ.get("ALEMBIC_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "alembic" / "env.py").exists():
            return parent / "alembic"
    for candidate in (Path.cwd() / "alembic", Path("/app/alembic")):
        if (candidate / "env.py").exists():
            return candidate
    raise FileNotFoundError("alembic directory not found; set ALEMBIC_DIR")


def init_db() -> None:
    """Run ``alembic upgrade head`` against ``DATA_DIR/app.db`` (creates the file if needed)."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(_alembic_dir()))
    cfg.set_main_option("sqlalchemy.url", settings.db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
