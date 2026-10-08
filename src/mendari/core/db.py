# SPDX-License-Identifier: Apache-2.0
"""Database backends shared by the audit store and the registry: SQLite (local) or Postgres
(hosted), running the same SQL written with `?` placeholders.

A store passes its schema (a DDL template with `{real}`, `{serial}` and `{blob}`), its migration
and its Postgres advisory-lock key. Exclusive sessions are transactions serialised with each
other: an IMMEDIATE transaction on SQLite, the advisory lock on Postgres.

`read_only` opens an existing database to read it and nothing else (LP-15: `audit verify`,
`audit export`, `feedback export`, `monitor`): no file or folder is created, no schema written,
nothing migrated, no journal mode changed, and the connection itself refuses writes (SQLite
`mode=ro`; Postgres `READ ONLY` transactions). A target that does not exist is an error.

`ping` runs one statement to tell whether the database answers now (the readiness route,
LP-21), waiting a bounded time and creating nothing: a SQLite file deleted under a running
server stays missing.
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mendari.errors import MendariError

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
    read_only: bool = False

    @abstractmethod
    def session(self, *, exclusive: bool) -> Any:
        """Context manager yielding a Session. Exclusive sessions are transactions serialised
        with every other exclusive session (required for hash-chain appends)."""

    @abstractmethod
    def ping(self, sql: str, timeout_s: float) -> None:
        """Run `sql` once, waiting at most about `timeout_s` for a connection, a lock or the
        answer; raise whatever the database raised. Nothing is created or written."""

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

    def __init__(self, path: str, *, schema: str, migrate: Migrate, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            if path in ("", ":memory:") or not Path(path).is_file():
                raise MendariError(
                    f"no database at {path or '(empty)'}: give the server's --db (a SQLite file "
                    "or a postgresql:// URL)"
                )
            self._memory = None
            self._lock = threading.RLock()
            return
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
        if self.read_only:
            # With no -wal or -shm beside it, no server has the database open and its WAL is
            # checkpointed: read it as a file (`immutable`), so no lock or sidecar file is
            # created. Otherwise a live server owns those files: read through them (`mode=ro`).
            path = Path(self.path).resolve()
            live = any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm"))
            uri = path.as_uri() + ("?mode=ro" if live else "?immutable=1")
            connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        else:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if self.read_only:
                connection.execute("PRAGMA query_only=1")
            else:
                connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def session(self, *, exclusive: bool) -> Iterator[Session]:
        if exclusive and self.read_only:
            raise MendariError("this database was opened read-only: nothing is written to it")
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

    def ping(self, sql: str, timeout_s: float) -> None:
        if self._memory is not None:
            if not self._lock.acquire(timeout=timeout_s):
                raise TimeoutError("the in-memory database stayed busy")
            try:
                self._memory.execute(sql).fetchall()
            finally:
                self._lock.release()
            return
        # `mode=rw` (or `ro`) opens the file only if it is there: a plain connect would create an
        # empty database in its place.
        mode = "ro" if self.read_only else "rw"
        uri = f"{Path(self.path).resolve().as_uri()}?mode={mode}"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout_s, isolation_level=None)
        try:
            connection.execute(sql).fetchall()
        finally:
            connection.close()

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
        read_only: bool = False,
    ):
        self.lock_key = lock_key
        self.read_only = read_only
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        if read_only:
            # Reached first, directly: a wrong URL or a missing database fails now, not after
            # the pool's timeout.
            with psycopg.connect(url, prepare_threshold=None, connect_timeout=15):
                pass

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
            # Read-only: every transaction is READ ONLY, so the server itself refuses a write.
            configure=_read_only_connection if read_only else None,
            open=True,
        )
        if read_only:
            return
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
        if exclusive and self.read_only:
            raise MendariError("this database was opened read-only: nothing is written to it")
        with self._pool.connection() as connection, connection.transaction():
            if exclusive:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_key,))
            yield PostgresSession(connection)

    def ping(self, sql: str, timeout_s: float) -> None:
        # The pool verifies the connection it hands out (`check_connection`); the statement then
        # has the same bound, so a database that accepts connections but stalls still fails.
        milliseconds = max(1, round(timeout_s * 1000))
        with self._pool.connection(timeout=timeout_s) as connection, connection.transaction():
            connection.execute(f"SET LOCAL statement_timeout = {milliseconds}")
            connection.execute(sql.replace("?", "%s"))

    def close(self) -> None:
        self._pool.close()


def connect(
    target: str,
    *,
    schema: str,
    migrate: Migrate,
    lock_key: int,
    migrate_url: str | None = None,
    read_only: bool = False,
) -> Backend:
    """A SQLite path (or ":memory:") or a postgres:// / postgresql:// URL, with `schema` created
    (a DDL template with `{real}`, `{serial}` and `{blob}`) and `migrate` run under the lock.
    `lock_key` is the Postgres advisory lock that serialises schema setup and exclusive sessions.
    `read_only` opens an existing database without writing anything (above). A URL of another
    scheme is refused, never taken for a file name (or printed: it can hold a password)."""
    if target.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(
            target,
            migrate_url,
            schema=schema,
            migrate=migrate,
            lock_key=lock_key,
            read_only=read_only,
        )
    if "://" in target:
        raise MendariError(
            "the database is neither a SQLite path nor a postgres:// or postgresql:// URL "
            "(a driver suffix such as +psycopg is not one)"
        )
    return SqliteBackend(target, schema=schema, migrate=migrate, read_only=read_only)


def _read_only_connection(connection: Any) -> None:
    connection.read_only = True
