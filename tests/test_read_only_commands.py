# SPDX-License-Identifier: Apache-2.0
"""`audit verify`, `audit export`, `feedback export` and `monitor` open the server's database
read-only (LP-15): a mistyped --db is one line and exit 1, never a new empty database that
"verifies"; another application's database is refused, untouched; the server's database is left
exactly as it was; and a database written by an older Mendari is verified as it is."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.pack import Pack
from mendari.errors import MendariError

COMMANDS = {
    "audit verify": ["audit", "verify"],
    "audit verify --notes": ["audit", "verify", "--notes"],
    "audit export": ["audit", "export"],
    "feedback export": ["feedback", "export", "mendari/patient-inbox-router", "--out", "{out}"],
    "monitor": ["monitor", "--pack", "mendari/patient-inbox-router"],
}


def run(command: str, db: str, tmp_path: Path) -> int:
    argv = [part.format(out=tmp_path / "feedback.jsonl") for part in COMMANDS[command]]
    return main([*argv, "--db", db])


def used_database(path: Path, pack: Pack) -> Path:
    """A server's SQLite database with a decision in it, in rollback-journal mode (not WAL), so
    any write would show in its bytes or its journal mode."""
    with AuditStore(path) as store:
        Decision(pack, store=store)({"message": {"body": "Can I get a refill of my lisinopril?"}})
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    return path


@pytest.mark.parametrize("command", sorted(COMMANDS))
@pytest.mark.parametrize("where", ["missing.db", "no/such/folder/missing.db", ""])
def test_a_missing_database_is_one_line_and_never_created(
    command: str, where: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = str(tmp_path / where) if where else ""
    assert run(command, target, tmp_path) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith("error: no database at")
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # no file, no folder


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_another_applications_database_is_refused_untouched(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    before = other.read_bytes()
    assert run(command, str(other), tmp_path) == 1
    assert "not a Mendari database" in capsys.readouterr().err
    assert other.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.db"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_servers_database_is_left_exactly_as_it_was(
    command: str, tmp_path: Path, inbox_pack: Pack
) -> None:
    db = used_database(tmp_path / "mendari.db", inbox_pack)
    before = db.read_bytes()
    assert run(command, str(db), tmp_path) in (0, 1)  # monitor may alert; it never writes
    assert db.read_bytes() == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_a_read_only_file_verifies(tmp_path: Path, inbox_pack: Pack) -> None:
    db = used_database(tmp_path / "mendari.db", inbox_pack)
    db.chmod(0o444)
    try:
        assert main(["audit", "verify", "--db", str(db)]) == 0
    finally:
        db.chmod(0o644)


def test_an_older_database_is_verified_as_it_is_and_never_migrated(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before a release's migration has run on it, a database lacks the tables and columns that
    release adds: `verify` reads only the chain and the records, which every release keeps, and
    the commands that need the rest say what to do instead of migrating it."""
    db = used_database(tmp_path / "mendari.db", inbox_pack)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE settings")
        connection.execute("ALTER TABLE feedback DROP COLUMN superseded_by")
    before = db.read_bytes()
    assert main(["audit", "verify", "--db", str(db)]) == 0
    assert main(["audit", "verify", "--notes", "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["monitor", "--db", str(db)]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert "written by an older Mendari" in line and "start the server on it once" in line
    assert db.read_bytes() == before  # still unmigrated


def test_a_read_only_store_refuses_any_write(db_target: str, inbox_pack: Pack) -> None:
    with AuditStore(db_target) as store:  # the server's: created and used
        Decision(inbox_pack, store=store)({"message": {"body": "Refill please."}})
    with AuditStore(db_target, read_only=True) as store:
        assert store.verify()["ok"]
        with pytest.raises(MendariError, match="opened read-only"):
            store.hold_automation(
                org=None, pack_id=None, pack_digest=None, reason="drill", set_by="test"
            )


def test_an_empty_postgres_schema_is_refused_and_left_empty(db_target: str) -> None:
    if not db_target.startswith("postgresql"):
        pytest.skip("Postgres: a schema with no tables")
    with pytest.raises(MendariError, match="not a Mendari database"):
        AuditStore(db_target, read_only=True)
    import psycopg

    with psycopg.connect(db_target) as connection:
        tables = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchone()
    assert tables == (0,)
