# SPDX-License-Identifier: Apache-2.0
"""`audit verify`, `audit export`, `feedback export` and `monitor` open the server's database
read-only (LP-15): a mistyped --db is one line and exit 1, never a new empty database that
"verifies"; another application's database is refused, untouched; and the server's database is
left exactly as it was (a pre-release one is refused: tests/test_schema_baseline.py). A command
that reads only a pack (`scoreboard arms`, LP-17c) takes no database and writes nothing, and
neither does checking a signed scoreboard result (`scoreboard verify`, LP-17e)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mendari import Decision
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.checkpoint import public_text
from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.evals import signing
from tests.test_scoreboard_signing import keys_file, result

COMMANDS = {
    "audit verify": ["audit", "verify"],
    "audit verify --notes": ["audit", "verify", "--notes"],
    # Hidden evaluation sets (LP-11e): checked (each unchecked without keys), marking nothing.
    "audit verify --evalsets": ["audit", "verify", "--evalsets"],
    # Their keys' check (LP-11f; without keys it refuses once the database is read) and the
    # sealed export (no key needed).
    "evalset rotate --check": ["evalset", "rotate", "--check"],
    "evalset export": ["evalset", "export", "--out", "{out}"],
    # A set's labelling progress (LP-11h): counts, no key needed (a set not there is refused).
    "evalset progress": ["evalset", "progress", "6b1c4f0e-2d6a-4c41-9a8e-3f1d2c5b7a90"],
    # A set's state, commitment, runs and stored aggregates (LP-11i): no key needed.
    "evalset show": ["evalset", "show", "6b1c4f0e-2d6a-4c41-9a8e-3f1d2c5b7a90"],
    "audit export": ["audit", "export"],
    "feedback export": ["feedback", "export", "mendari/patient-inbox-router", "--out", "{out}"],
    "monitor": ["monitor", "--agent", "mendari/patient-inbox-router"],
}
# Commands that read a pack's files and nothing else: no --db, nothing written anywhere.
PACK_ONLY = {
    # A pack's configurations by arm (LP-17c), from its manifest.
    "scoreboard arms": ["scoreboard", "arms", "mendari/patient-inbox-router"],
    "scoreboard arms --json": ["scoreboard", "arms", "mendari/patient-inbox-router", "--json"],
}
# Commands that read the files they are given and nothing else: no --db, nothing written. A
# signed scoreboard result checked against a keys file, or one public key (LP-17e).
FILES_ONLY = {
    "scoreboard verify --keys": ["scoreboard", "verify", "{result}", "--keys", "{keys}"],
    "scoreboard verify --public-key": ["scoreboard", "verify", "{result}", "--public-key", "{key}"],
}


def run(command: str, db: str, tmp_path: Path) -> int:
    argv = [part.format(out=tmp_path / "feedback.jsonl") for part in COMMANDS[command]]
    return main([*argv, "--db", db])


def used_database(path: Path, pack: Pack) -> Path:
    """A server's SQLite database with a decision in it, in rollback-journal mode (not WAL), so
    any write would show in its bytes or its journal mode."""
    with AuditStore(path) as store:
        Decision(pack, store=store)({"message": {"body": "Can I get a refill of my lisinopril?"}})
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
    command: str, tmp_path: Path, inbox_pack: Pack
) -> None:
    db = used_database(tmp_path / "mendari.db", inbox_pack)
    before = db.read_bytes()
    assert run(command, str(db), tmp_path) in (0, 1)  # monitor may alert; it never writes
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("command", sorted(PACK_ONLY))
def test_a_command_that_reads_only_a_pack_writes_nothing(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack
) -> None:
    files = sorted(path for path in inbox_pack.root.rglob("*") if path.is_file())
    before = [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files]
    monkeypatch.chdir(tmp_path)
    assert main(PACK_ONLY[command]) == 0
    # Nothing here, nor in the Hub's config and cache folders, which conftest puts here.
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert sorted(path for path in inbox_pack.root.rglob("*") if path.is_file()) == files
    assert [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files] == before
    assert main([*PACK_ONLY[command], "--db", str(tmp_path / "mendari.db")]) == 1  # takes none
    assert sorted(p.name for p in tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", sorted(FILES_ONLY))
def test_a_command_that_reads_only_its_files_writes_nothing(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Ed25519PrivateKey.generate()
    given = tmp_path / "given"
    given.mkdir()
    (given / "result.json").write_text(signing.dump(signing.sign(result(), key)), encoding="utf-8")
    (given / "keys.json").write_text(json.dumps(keys_file(key)), encoding="utf-8")
    files = sorted(given.iterdir())
    before = [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files]
    monkeypatch.chdir(tmp_path)
    places = {
        "result": str(given / "result.json"),
        "keys": str(given / "keys.json"),
        "key": public_text(key.public_key()),
    }
    argv = [part.format(**places) for part in FILES_ONLY[command]]
    assert main(argv) == 0
    # Nothing here but what it was given, nor in the Hub's config folder (its pins are read).
    assert sorted(p.name for p in tmp_path.iterdir()) == ["given"]
    assert sorted(given.iterdir()) == files
    assert [(path, path.stat().st_mtime_ns, path.read_bytes()) for path in files] == before
    assert main([*argv, "--db", str(tmp_path / "mendari.db")]) == 1  # takes none
    assert sorted(p.name for p in tmp_path.iterdir()) == ["given"]


def test_a_read_only_file_verifies(tmp_path: Path, inbox_pack: Pack) -> None:
    db = used_database(tmp_path / "mendari.db", inbox_pack)
    db.chmod(0o444)
    try:
        assert main(["audit", "verify", "--db", str(db)]) == 0
    finally:
        db.chmod(0o644)


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


def test_a_wal_database_no_server_has_open_gains_no_files(tmp_path: Path, inbox_pack: Pack) -> None:
    """A server leaves its SQLite database in WAL mode, the -wal and -shm files gone once it
    closes: reading it then creates no sidecar file a server might later be unable to write."""
    folder = tmp_path / "var"
    folder.mkdir()
    db = folder / "mendari.db"
    with AuditStore(db) as store:  # WAL, as a server leaves it
        Decision(inbox_pack, store=store)({"message": {"body": "Refill please."}})
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
