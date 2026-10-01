"""Database backends shared by the audit store and the registry: SQLite (local) or Postgres
(hosted), running the same SQL written with `?` placeholders.

A store passes its schema (a DDL template with `{real}`, `{serial}` and `{blob}`), its migration
and its Postgres advisory-lock key. Exclusive sessions are transactions serialised with each
other: an IMMEDIATE transaction on SQLite, the advisory lock on Postgres.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

Migrate = Callable[["Session"], None]


class Session(ABC):
    @abstractmethod
    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a statement and return its rows as dicts (empty for statements without rows)."""

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    @abstractmethod
    def columns(self, table: str) -> set[str]:
        """The column names a table has now."""


class Backend(ABC):
    kind: str

    @abstractmethod
    def session(self, *, exclusive: bool) -> Any:
        """Context manager yielding a Session. Exclusive sessions are transactions serialised
        with every other exclusive session (required for hash-chain appends)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class SqliteSession(Session):
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()]

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.all(f"PRAGMA table_info({table})")}


class SqliteBackend(Backend):
    kind = "sqlite"

    def __init__(self, path: str, *, schema: str, migrate: Migrate):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = (
            sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            if path == ":memory:"
            else None
        )
        self._lock = threading.RLock()  # serialises access to the shared in-memory connection
        with self._connect() as connection:
            connection.executescript(
                schema.format(real="REAL", serial="INTEGER PRIMARY KEY AUTOINCREMENT", blob="BLOB")
            )
        with self.session(exclusive=True) as db:  # IMMEDIATE: one process migrates at a time
            migrate(db)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._lock:
                self._memory.row_factory = sqlite3.Row
                yield self._memory
            return
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def session(self, *, exclusive: bool) -> Iterator[Session]:
        with self._connect() as connection:
            if not exclusive:
                yield SqliteSession(connection)
                return
            # IMMEDIATE takes the write lock up front so hash-chain appends are serialised.
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield SqliteSession(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def close(self) -> None:
        if self._memory is not None:
            self._memory.close()


class PostgresSession(Session):
    def __init__(self, connection: Any):
        self.connection = connection

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql.replace("?", "%s"), tuple(params))
        return list(cursor.fetchall()) if cursor.description else []

    def columns(self, table: str) -> set[str]:
        rows = self.all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        )
        return {row["column_name"] for row in rows}


class PostgresBackend(Backend):
    kind = "postgres"

    def __init__(
        self,
        url: str,
        migrate_url: str | None = None,
        *,
        schema: str,
        migrate: Migrate,
        lock_key: int,
    ):
        self.lock_key = lock_key
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None: prepared statements break behind transaction-mode poolers
        # (Neon's pooled endpoint, PgBouncer).
        # check_connection: Neon suspends idle compute and the server closes pooled connections,
        # so every connection is verified (one round trip) before use and replaced if dead.
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=4,
            max_idle=60,
            check=ConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "application_name": "mendari",
            },
            open=True,
        )
        # Schema setup goes over the direct (unpooled) connection when one is given, as Neon
        # recommends for DDL; the advisory lock serialises concurrent cold starts.
        ddl = schema.format(
            real="DOUBLE PRECISION",
            serial="BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
            blob="BYTEA",
        )
        with (
            psycopg.connect(
                migrate_url or url, prepare_threshold=None, row_factory=dict_row
            ) as connection,
            connection.transaction(),
        ):
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            for statement in ddl.split(";"):
                if statement.strip():
                    connection.execute(statement)
            migrate(PostgresSession(connection))

    @contextmanager
    def session(self, *, exclusive: bool) -> Iterator[Session]:
        with self._pool.connection() as connection, connection.transaction():
            if exclusive:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_key,))
            yield PostgresSession(connection)

    def close(self) -> None:
        self._pool.close()


def connect(
    target: str,
    *,
    schema: str,
    migrate: Migrate,
    lock_key: int,
    migrate_url: str | None = None,
) -> Backend:
    """A SQLite path (or ":memory:") or a postgres:// / postgresql:// URL, with `schema` created
    (a DDL template with `{real}`, `{serial}` and `{blob}`) and `migrate` run under the lock.
    `lock_key` is the Postgres advisory lock that serialises schema setup and exclusive sessions."""
    if target.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(
            target, migrate_url, schema=schema, migrate=migrate, lock_key=lock_key
        )
    return SqliteBackend(target, schema=schema, migrate=migrate)
