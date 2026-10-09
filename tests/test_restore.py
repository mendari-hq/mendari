# SPDX-License-Identifier: Apache-2.0
"""Backup and restore, as the incident runbook says (LP-21m): a database backed up with the
runbook's own commands (SQLite's `.backup`, Postgres's `pg_dump` and `pg_restore`) and restored
verifies whole against the last signed checkpoint from before the backup, and a chain rewritten
after it does not. In CI on SQLite and Postgres."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Pack
from mendari.cli import main
from mendari.core.checkpoint import KEY_ENV, public_text
from tests.conftest import POSTGRES_URL
from tests.test_checkpoints import decide, rewrite_from
from tests.test_signed_checkpoint import SEED, key


def signed_before_backup(db: str, pack: Pack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Decisions, the day's signed checkpoint, then more decisions: what a backup holds."""
    monkeypatch.setenv(KEY_ENV, base64.b64encode(SEED).decode())
    with AuditStore(db) as store:
        decide(store, pack, 3)
        statement = store.record_checkpoint(signer=key())["signed"]
        decide(store, pack, 2)
    assert statement is not None
    return dict(statement)


def verified_against(db: str, statement: dict[str, Any], tmp_path: Path) -> int:
    """`mendari audit verify --against` a copy kept as the runbook says: the published answer of
    `/api/audit/checkpoint`, as `curl` saves it."""
    public = public_text(key().public_key())
    kept = tmp_path / f"checkpoint-{uuid.uuid4().hex}.json"
    published = {"statement": statement, "public_key": public, "key_id": statement["key_id"]}
    kept.write_text(json.dumps(published))
    return main(["audit", "verify", "--db", db, "--against", str(kept), "--public-key", public])


def events_in(db: Path) -> int:
    with closing(sqlite3.connect(db)) as connection:
        (count,) = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()
    return int(count)


# A server that crashed mid-work: decisions made while a connection stays open, then the process
# dies without closing it, so the write-ahead log is never folded into the file (`-wal`, `-shm`).
CRASHING = """
import os, sqlite3, sys
from mendari import AuditStore, Decision, load_pack
held = sqlite3.connect(sys.argv[1])
held.execute("PRAGMA journal_mode=WAL").fetchone()
held.execute("SELECT COUNT(*) FROM audit_events").fetchone()
store = AuditStore(sys.argv[1])
decision = Decision(load_pack(sys.argv[2]), store=store)
for n in range(5):
    decision({"message": {"body": f"After the backup, number {n}."}})
os._exit(0)
"""


def test_a_sqlite_backup_restores_and_verifies_against_the_signed_checkpoint(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the runbook says: a backup while it runs, a crash that leaves `-wal` and `-shm`, then
    the three files moved aside (kept) and the backup copied into place."""
    var = tmp_path / "var"
    var.mkdir()
    live = var / "mendari.db"
    statement = signed_before_backup(str(live), inbox_pack, monkeypatch)
    backup = tmp_path / "mendari-backup.db"
    sqlite3_cli = shutil.which("sqlite3")
    if sqlite3_cli is not None:  # the runbook's command
        subprocess.run([sqlite3_cli, str(live), f".backup '{backup}'"], check=True)
    else:  # the same online backup, through Python's sqlite3
        with closing(sqlite3.connect(live)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
    backed_up = events_in(backup)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    crash = [sys.executable, "-c", CRASHING, str(live), str(inbox_pack.root)]
    subprocess.run(crash, check=True, env=env)
    left = [live.with_name(live.name + suffix) for suffix in ("-wal", "-shm")]
    assert all(path.exists() for path in left)  # the crash left them: the case that matters
    kept = tmp_path / "incident"
    kept.mkdir()
    for path in (live, *left):  # moved aside together, and kept
        path.rename(kept / path.name)
    shutil.copyfile(backup, live)
    assert verified_against(str(live), statement, tmp_path) == 0
    assert events_in(live) == backed_up  # the backup, not the backup with the crash's log on it
    assert events_in(kept / "mendari.db") > backed_up  # the incident's, with its log: kept
    restored = live
    assert main(["audit", "checkpoint", "--db", str(restored)]) == 0
    with AuditStore(str(restored)) as store:  # a restore someone rewrote: caught
        rewrite_from(store, 1)
        with store._db.session(exclusive=True) as db:
            db.all("DELETE FROM audit_checkpoints")
            db.all("DELETE FROM checkpoint_statements")
    assert verified_against(str(restored), statement, tmp_path) == 1


@contextmanager
def a_new_database() -> Iterator[str]:
    import psycopg

    name = f"restore_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    base = re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", POSTGRES_URL, count=1)
    try:
        yield base
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_a_postgres_dump_restores_and_verifies_against_the_signed_checkpoint(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not POSTGRES_URL:
        pytest.skip("set MENDARI_TEST_DATABASE_URL to run the Postgres backend tests")
    dump, restore = shutil.which("pg_dump"), shutil.which("pg_restore")
    if dump is None or restore is None:
        if os.environ.get("CI"):
            pytest.fail("CI runs the restore test: pg_dump and pg_restore must be installed")
        pytest.skip("pg_dump and pg_restore are not installed")
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_URL else "?"
    live = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    try:
        statement = signed_before_backup(live, inbox_pack, monkeypatch)
        archive = tmp_path / "mendari.dump"
        subprocess.run(  # the runbook's commands, the schema the test runs in
            [dump, "--format=custom", f"--schema={schema}", f"--file={archive}", POSTGRES_URL],
            check=True,
        )
        with a_new_database() as target:
            subprocess.run([restore, "--no-owner", f"--dbname={target}", str(archive)], check=True)
            restored = f"{target}{separator}options=-csearch_path%3D{schema}"
            assert verified_against(restored, statement, tmp_path) == 0
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_the_runbook_shows_the_commands_the_restore_test_runs() -> None:
    """What the restore test runs is what the runbook says to run."""
    from tests.conftest import ROOT

    runbook = (ROOT / "docs/operations/incident-runbook.md").read_text()
    for command in (
        "sqlite3 var/mendari.db \".backup 'mendari-backup.db'\"",
        "move `var/mendari.db`, `var/mendari.db-wal` and `var/mendari.db-shm` aside together",
        "create a **new branch** from a point in time",
        'mendari automation hold --all-orgs --reason "restore, incident 7" --db "$RESTORE_URL"',
        "mendari access end-sessions --kind all",
        "mendari audit export --administrative --since",
        'mendari phi purge --db "$RESTORE_URL"',
        'pg_dump --format=custom --file mendari.dump "$DATABASE_URL"',
        'pg_restore --no-owner --dbname "$RESTORE_URL" mendari.dump',
        'mendari audit verify --db "$RESTORE_URL" --against',
        'mendari audit checkpoint --db "$RESTORE_URL"',
    ):
        assert command in runbook, command
