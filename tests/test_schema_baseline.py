# SPDX-License-Identifier: Apache-2.0
"""The storage baseline (`core/db.py`): each store's schema is the first release's, marked in
the transaction that makes it (`mendari-engine/1` in `settings`, `mendari-registry/1` in
`hub_meta`), and every open reads the mark before anything else. A database a pre-release
Mendari made (the store's anchor table without a mark) is refused, read-only opens included,
with nothing in it read or changed; so is a mark naming a newer schema, or one no Mendari writes.
The engine and the registry share one database deployed: each checks only its own anchor and
mark. The pre-release databases are made here, in the shape an earlier release left, never
committed. SQLite and Postgres."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

import pytest

from mendari.cli import main
from mendari.core import db as db_module
from mendari.core.audit import _CHAIN_LOCK, _TABLES, ENGINE_SCHEMA, AuditStore
from mendari.core.db import PRE_RELEASE, Baseline, Session
from mendari.errors import MendariError
from mendari.hub.registry import REGISTRY_SCHEMA, RegistryStore

STORES: dict[str, tuple[Callable[..., Any], Baseline]] = {
    "engine": (AuditStore, ENGINE_SCHEMA),
    "registry": (RegistryStore, REGISTRY_SCHEMA),
}
# The command that reads each store read-only, as an operator runs it.
VERIFY = {"engine": ["audit", "verify"], "registry": ["registry", "verify"]}
GENESIS = "0" * 64
WHEN = "2026-11-07T09:00:00.000+00:00"
# What a pre-release Mendari left, by store: its tables (the anchor, and the mark's table with an
# older store's own rows, or without it), and no mark.
PRE_RELEASE_SHAPES: dict[str, dict[str, tuple[str, ...]]] = {
    "engine": {
        "marked by an older store": (
            "CREATE TABLE decisions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "pack_id TEXT NOT NULL, pack_version TEXT NOT NULL, outcome TEXT NOT NULL, "
            "record TEXT NOT NULL)",
            "CREATE TABLE audit_events (seq BIGINT PRIMARY KEY, decision_id TEXT NOT NULL, "
            "event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
            "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"INSERT INTO settings (name, value) VALUES ('org_bound', '{WHEN}')",
            "INSERT INTO settings (name, value) VALUES ('phi_tokens_v2', '1')",
            "INSERT INTO audit_events VALUES "
            f"(1, 'd-1', 'decision.created', '{{}}', '{WHEN}', '{GENESIS}', '{'a' * 64}')",
        ),
        "from before its settings": (
            "CREATE TABLE audit_events (seq BIGINT PRIMARY KEY, decision_id TEXT NOT NULL, "
            "event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
        ),
    },
    "registry": {
        "marked by an older store": (
            "CREATE TABLE hub_events (seq BIGINT PRIMARY KEY, subject TEXT NOT NULL, "
            "event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
            "CREATE TABLE hub_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            f"INSERT INTO hub_meta (key, value) VALUES ('chain', '{WHEN}')",
            "CREATE TABLE hub_tokens (token_sha256 TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
            "created_at TEXT NOT NULL)",
        ),
        "from before its meta table": (
            "CREATE TABLE hub_events (seq BIGINT PRIMARY KEY, subject TEXT NOT NULL, "
            "event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "prev_hash TEXT NOT NULL, hash TEXT NOT NULL)",
        ),
    },
}
SHAPES = [(store, shape) for store, shapes in PRE_RELEASE_SHAPES.items() for shape in shapes]


def postgres(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


def execute(target: str, *statements: str) -> None:
    """Statements run past the stores, as an earlier release or another program would."""
    if postgres(target):
        import psycopg

        with psycopg.connect(target, autocommit=True) as served:
            for statement in statements:
                served.execute(statement)
        return
    with closing(sqlite3.connect(target)) as local, local:
        for statement in statements:
            local.execute(statement)


def snapshot(target: str) -> dict[str, list[str]]:
    """Every table of the database (the test's schema, on Postgres) with its rows: what an open
    that changes nothing leaves as it was."""
    if postgres(target):
        import psycopg

        with psycopg.connect(target) as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            ]
            return {
                name: sorted(repr(row) for row in connection.execute(f'SELECT * FROM "{name}"'))
                for name in names
            }
    with closing(sqlite3.connect(target)) as connection:
        names = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            name: sorted(repr(row) for row in connection.execute(f'SELECT * FROM "{name}"'))
            for name in names
        }


def marks(target: str, baseline: Baseline) -> list[str]:
    """The store's mark rows, as `snapshot` shows them (one, once its schema is made)."""
    rows = snapshot(target).get(baseline.table, [])
    return [row for row in rows if row.startswith("('schema',")]


def remark(target: str, baseline: Baseline, value: str) -> None:
    execute(
        target,
        f"UPDATE {baseline.table} SET value = '{value}' WHERE {baseline.key} = 'schema'",
    )


def opened(kind: str, target: str, **options: Any) -> None:
    store, _ = STORES[kind]
    with store(target, **options):
        pass


# -- a fresh database ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(STORES))
def test_a_store_marks_the_schema_it_makes_and_opens_it_again(kind: str, db_target: str) -> None:
    store, baseline = STORES[kind]
    opened(kind, db_target)
    assert marks(db_target, baseline) == [repr(("schema", baseline.mark))]
    made = snapshot(db_target)
    opened(kind, db_target)  # a second start finds its own schema: nothing changes
    assert snapshot(db_target) == made
    with store(db_target, read_only=True) as reading:
        assert reading.verify()["ok"]
    assert snapshot(db_target) == made


def test_the_mark_names_each_store_and_this_release() -> None:
    assert (ENGINE_SCHEMA.mark, REGISTRY_SCHEMA.mark) == ("mendari-engine/1", "mendari-registry/1")
    assert (ENGINE_SCHEMA.anchor, ENGINE_SCHEMA.table) == ("audit_events", "settings")
    assert (REGISTRY_SCHEMA.anchor, REGISTRY_SCHEMA.table) == ("hub_events", "hub_meta")


def test_a_schema_and_its_mark_are_made_in_one_transaction(db_target: str) -> None:
    """A start that fails after making the schema leaves nothing: no table without its mark,
    which every later open would refuse as a pre-release database."""

    def failing(_: Session) -> None:
        raise RuntimeError("the start stops after the schema is made")

    with pytest.raises(RuntimeError, match="the start stops"):
        db_module.connect(
            db_target,
            schema=_TABLES,
            migrate=failing,
            lock_key=_CHAIN_LOCK,
            baseline=ENGINE_SCHEMA,
        )
    assert snapshot(db_target) == {}
    opened("engine", db_target)
    assert marks(db_target, ENGINE_SCHEMA) == [repr(("schema", "mendari-engine/1"))]


def test_each_chain_is_unique_from_the_first_release_on(db_target: str) -> None:
    """The unique `prev_hash` indexes that keep each chain from forking are in the schemas."""
    with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert "audit_events_prev_hash" in db.indexes("audit_events")
    with RegistryStore(db_target) as registry, registry._db.session(exclusive=False) as db:
        assert "hub_events_prev_hash" in db.indexes("hub_events")


# -- what is refused, before anything is read ----------------------------------------------------


@pytest.mark.parametrize("read_only", [False, True], ids=["write", "read-only"])
@pytest.mark.parametrize(("kind", "shape"), SHAPES)
def test_a_pre_release_database_is_refused_and_left_as_it_was(
    kind: str, shape: str, read_only: bool, db_target: str
) -> None:
    execute(db_target, *PRE_RELEASE_SHAPES[kind][shape])
    before = snapshot(db_target)
    with pytest.raises(MendariError) as refused:
        opened(kind, db_target, read_only=read_only)
    assert str(refused.value) == PRE_RELEASE
    assert "docs/operations/incident-runbook.md, 'Starting over'" in PRE_RELEASE
    assert snapshot(db_target) == before


@pytest.mark.parametrize("kind", sorted(STORES))
def test_the_command_line_refuses_a_pre_release_database_in_one_line(
    kind: str, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    shape = next(iter(PRE_RELEASE_SHAPES[kind]))
    execute(db_target, *PRE_RELEASE_SHAPES[kind][shape])
    before = snapshot(db_target)
    assert main([*VERIFY[kind], "--db", db_target]) == 1
    assert capsys.readouterr().err.splitlines() == [f"error: {PRE_RELEASE}"]
    assert snapshot(db_target) == before


@pytest.mark.parametrize("read_only", [False, True], ids=["write", "read-only"])
@pytest.mark.parametrize("kind", sorted(STORES))
def test_a_schema_a_newer_mendari_made_is_refused(
    kind: str, read_only: bool, db_target: str
) -> None:
    _, baseline = STORES[kind]
    opened(kind, db_target)
    newer = f"{baseline.name}/{baseline.version + 1}"
    remark(db_target, baseline, newer)
    before = snapshot(db_target)
    with pytest.raises(MendariError, match="made by a newer Mendari") as refused:
        opened(kind, db_target, read_only=read_only)
    assert f"schema {newer}; this release reads {baseline.mark}" in str(refused.value)
    assert snapshot(db_target) == before


@pytest.mark.parametrize("value", ["", "mendari-engine", "mendari-registry/1", "x/0", "x/1.0"])
@pytest.mark.parametrize("kind", sorted(STORES))
def test_a_mark_no_mendari_writes_is_refused(kind: str, value: str, db_target: str) -> None:
    _, baseline = STORES[kind]
    opened(kind, db_target)
    written = value.replace("x", baseline.name)
    if written == baseline.mark:  # the other store's mark, in this store's table
        written = REGISTRY_SCHEMA.mark if kind == "engine" else ENGINE_SCHEMA.mark
    remark(db_target, baseline, written)
    for read_only in (False, True):
        with pytest.raises(MendariError, match="marked with something no Mendari writes"):
            opened(kind, db_target, read_only=read_only)


@pytest.mark.parametrize("kind", sorted(STORES))
def test_a_read_only_open_of_a_database_without_the_store_is_refused(
    kind: str, db_target: str
) -> None:
    """A read-only open makes nothing: a database without the store's schema (another
    application's, then also holding the other store's) is refused and left as it was."""
    _, baseline = STORES[kind]
    execute(db_target, "CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    for other in (None, "registry" if kind == "engine" else "engine"):
        if other is not None:
            opened(other, db_target)
        before = snapshot(db_target)
        with pytest.raises(MendariError) as refused:
            opened(kind, db_target, read_only=True)
        assert str(refused.value) == baseline.missing
        assert snapshot(db_target) == before


# -- one database, two stores --------------------------------------------------------------------


def test_the_engine_and_the_registry_share_one_database(db_target: str) -> None:
    for _ in range(2):  # each start finds the other's schema and its own
        opened("engine", db_target)
        opened("registry", db_target)
    assert marks(db_target, ENGINE_SCHEMA) == [repr(("schema", "mendari-engine/1"))]
    assert marks(db_target, REGISTRY_SCHEMA) == [repr(("schema", "mendari-registry/1"))]
    with RegistryStore(db_target) as registry:
        registry.create_namespace("acme", "community", "Acme")
        assert registry.verify()["ok"]
    with AuditStore(db_target) as store:
        store.record_run("monitor", "ok")
        assert store.verify()["ok"]
    made = snapshot(db_target)
    assert "rate_limits" in made  # one table, the two stores' buckets
    with AuditStore(db_target, read_only=True) as store:
        assert store.verify()["ok"] and store.runs()["monitor"]["outcome"] == "ok"
    with RegistryStore(db_target, read_only=True) as registry:
        assert registry.verify()["events"] == 1
    assert snapshot(db_target) == made


@pytest.mark.parametrize("old", sorted(STORES))
def test_each_store_checks_only_its_own_anchor_and_mark(old: str, db_target: str) -> None:
    """Beside a pre-release store, the other one is made and opens as usual; the pre-release one
    is refused every time, and left as it was."""
    new = "registry" if old == "engine" else "engine"
    execute(db_target, *next(iter(PRE_RELEASE_SHAPES[old].values())))
    for _ in range(2):
        opened(new, db_target)
        before = snapshot(db_target)
        for read_only in (False, True):
            with pytest.raises(MendariError) as refused:
                opened(old, db_target, read_only=read_only)
            assert str(refused.value) == PRE_RELEASE
        assert snapshot(db_target) == before
    opened(new, db_target, read_only=True)
    _, baseline = STORES[new]
    assert marks(db_target, baseline) == [repr(("schema", baseline.mark))]
