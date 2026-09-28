"""Engine / session management for the local SQLite database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from stallion_tally.database.models import AppMeta, Base

SCHEMA_VERSION = 1


def _apply_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def build_engine(url: str) -> Engine:
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        if engine.url.database and engine.url.database != ":memory:":
            Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
        event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


class Database:
    """Owns the engine and hands out transactional sessions."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = build_engine(url)
        self._session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.session() as session:
            row = session.get(AppMeta, "schema_version")
            if row is None:
                session.add(AppMeta(key="schema_version", value=str(SCHEMA_VERSION)))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def is_writable(self) -> bool:
        try:
            with self.session() as session:
                session.execute(select(AppMeta.key).limit(1))
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        self.engine.dispose()
