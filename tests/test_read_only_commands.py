# SPDX-License-Identifier: Apache-2.0
"""`audit verify`, `audit export`, `feedback export` and `monitor` open the server's database
read-only (LP-15): a mistyped --db is one line and exit 1, never a new empty database that
"verifies"; another application's database is refused, untouched; and the server's database is
left exactly as it was (a pre-release one is refused: tests/test_schema_baseline.py)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core.agent import Agent
from mendari.core.audit import AuditStore
from mendari.errors import MendariError

AGENT = "mendari-test/triage"  # the agent these tests decide with
COMMANDS = {
    "audit verify": ["audit", "verify"],
    "audit verify --notes": ["audit", "verify", "--notes"],
    "audit export": ["audit", "export"],
    "feedback export": ["feedback", "export", AGENT, "--out", "{out}"],
    "monitor": ["monitor", "--agent", AGENT],
}


def run(command: str, db: str, tmp_path: Path) -> int:
    argv = [part.format(out=tmp_path / "feedback.jsonl") for part in COMMANDS[command]]
    return main([*argv, "--db", db])


def used_database(path: Path, agent: Agent) -> Path:
    """A server's SQLite database with a decision in it, in rollback-journal mode (not WAL), so
    any write would show in its bytes or its journal mode."""
    with AuditStore(path) as store:
        Decision(agent, store=store)({"message": {"body": "Can I get a refill of my lisinopril?"}})
    with closing(sqlite3.connect(path)) as connection, connection:
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
    with closing(sqlite3.connect(other)) as connection, connection:
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    before = other.read_bytes()
    assert run(command, str(other), tmp_path) == 1
    assert "not a Mendari database" in capsys.readouterr().err
    assert other.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.db"]


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_the_servers_database_is_left_exactly_as_it_was(
    command: str, tmp_path: Path, triage_agent: Agent
) -> None:
    db = used_database(tmp_path / "mendari.db", triage_agent)
    before = db.read_bytes()
    assert run(command, str(db), tmp_path) in (0, 1)  # monitor may alert; it never writes
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_a_read_only_file_verifies(tmp_path: Path, triage_agent: Agent) -> None:
    db = used_database(tmp_path / "mendari.db", triage_agent)
    db.chmod(0o444)
    try:
        assert main(["audit", "verify", "--db", str(db)]) == 0
    finally:
        db.chmod(0o644)


def test_a_read_only_store_refuses_any_write(db_target: str, triage_agent: Agent) -> None:
    with AuditStore(db_target) as store:  # the server's: created and used
        Decision(triage_agent, store=store)({"message": {"body": "Refill please."}})
    with AuditStore(db_target, read_only=True) as store:
        assert store.verify()["ok"]
        with pytest.raises(MendariError, match="opened read-only"):
            store.hold_automation(
                org=None, agent_id=None, agent_digest=None, reason="drill", set_by="test"
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


def test_a_wal_database_no_server_has_open_gains_no_files(
    tmp_path: Path, triage_agent: Agent
) -> None:
    """A server leaves its SQLite database in WAL mode, the -wal and -shm files gone once it
    closes: reading it then creates no sidecar file a server might later be unable to write."""
    folder = tmp_path / "var"
    folder.mkdir()
    db = folder / "mendari.db"
    with AuditStore(db) as store:  # WAL, as a server leaves it
        Decision(triage_agent, store=store)({"message": {"body": "Refill please."}})
    assert sorted(p.name for p in folder.iterdir()) == ["mendari.db"]
    for command in sorted(COMMANDS):
        assert run(command, str(db), tmp_path) in (0, 1)
        assert sorted(p.name for p in folder.iterdir()) == ["mendari.db"], command
    folder.chmod(0o555)  # a folder the command line cannot write
    try:
        assert main(["audit", "verify", "--db", str(db)]) == 0
    finally:
        folder.chmod(0o755)


@pytest.mark.parametrize(
    "target",
    ["postgresql+psycopg://mendari:S3cretPW@localhost/db", "POSTGRESQL://mendari:S3cretPW@x/db"],
)
def test_a_url_of_another_scheme_is_refused_without_its_password(
    target: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for command in ("audit verify", "monitor"):
        assert run(command, target, tmp_path) == 1
        err = capsys.readouterr().err
        assert "S3cretPW" not in err and "neither a SQLite path nor a postgres" in err
