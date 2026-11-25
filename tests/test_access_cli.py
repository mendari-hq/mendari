# SPDX-License-Identifier: Apache-2.0
"""`mendari access` (LP-12d): the operator's organizations, bootstrap, invitations and the
preflight before memberships mode, against the server's database (SQLite or Postgres)."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mendari import Agent, AuditStore, Decision
from mendari.cli import main
from mendari.core.phi import PhiPolicy
from tests.conftest import HASH_KEY_B64, INPUT_HASH_KEY, ROOT


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_the_operator_creates_organizations_bootstraps_and_invites(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    AuditStore(db_target)  # the server's database, with its tables
    code, out, _ = run(
        capsys, "access", "org", "create", "clinic-a", "--name", "Clinic A", "--db", db_target
    )
    assert code == 0 and json.loads(out)["id"] == "clinic-a"
    assert main(["access", "org", "create", "default", "--name", "X", "--db", db_target]) == 1
    code, out, err = run(
        capsys, "access", "bootstrap", "--email", "owner@example.org", "--db", db_target
    )
    assert code == 0 and json.loads(out)["membership"]["roles"] == ["admin"]
    code, _, err = run(
        capsys, "access", "bootstrap", "--email", "owner@example.org", "--db", db_target
    )
    assert code == 0 and "already bootstrapped" in err
    code, out, err = run(
        capsys, "access", "invite", "--org", "clinic-a", "--email", "ada@example.org",
        "--role", "reviewer", "--role", "viewer", "--db", db_target,
    )  # fmt: skip
    made = json.loads(out)
    assert code == 0 and made["roles"] == ["reviewer", "viewer"] and "token" not in made
    token = re.search(r"/invite#(ohi_\S+)", err)
    assert token is not None and token.group(1) not in out  # shown once, on its own line
    code, out, _ = run(capsys, "access", "org", "list", "--db", db_target)
    assert [o["id"] for o in json.loads(out)] == ["clinic-a", "default"]
    code, out, _ = run(capsys, "access", "members", "--org", "default", "--db", db_target)
    assert [m["pending_email"] for m in json.loads(out)] == ["owner@example.org"]
    code, out, _ = run(capsys, "access", "requests", "--db", db_target)
    assert json.loads(out) == []
    assert main(["access", "invite", "--org", "nowhere", "--email", "x@example.org",
                 "--role", "viewer", "--db", db_target]) == 1  # fmt: skip


def test_the_preflight_names_each_organization_in_use_without_a_record(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = AuditStore(db_target)
    store.create_api_key(name="k", role="viewer", org="clinic-b", created_by="Ada")
    store.hold_automation(
        org="default", agent_id=None, agent_digest=None, reason="test", set_by="Ada"
    )
    with store._db.session(exclusive=True) as db:  # an evaluation report from before tenancy
        db.all(
            "INSERT INTO eval_reports (id, created_at, agent_id, dataset, report) "
            "VALUES ('r1', '2026-10-07T00:00:00+00:00', 'p', 'd', '{}')"
        )
    events = store.verify()["events"]
    code, out, err = run(capsys, "access", "check", "--db", db_target)
    report = json.loads(out)
    assert code == 1 and "no record for clinic-b, default" in err
    assert report == [
        {"org": "clinic-b", "api_keys": 1, "recorded": False, "recordable": True},
        {
            "org": "default",
            "automation_holds": 1,
            "eval_reports": 1,
            "recorded": False,
            "recordable": True,
        },
    ]  # ids and counts only
    assert store.verify()["events"] == events and store.organizations() == []  # wrote nothing
    store.create_organization(org="clinic-b", name="Clinic B", created_by="cli")
    store.bootstrap(email="owner@example.org", created_by="cli")
    code, _, err = run(capsys, "access", "check", "--db", db_target)
    assert code == 0 and "every organization in use has a record" in err


def test_without_a_database_the_commands_say_how_to_give_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["access", "org", "list"]) == 1
    assert "give --db" in capsys.readouterr().err
    assert main(["access", "check", "--db", str(tmp_path / "missing.db")]) == 1


def test_the_account_tests_use_reserved_addresses_only() -> None:
    """No real address in a test or fixture (RFC 2606/6761 domains only)."""
    for name in (
        "test_accounts_store.py",
        "test_access_cli.py",
        "test_tenancy.py",
        "test_memberships.py",
    ):
        text = (ROOT / "tests" / name).read_text()
        for address in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text):
            reserved = r"@(example\.(org|com|net)|[\w.-]+\.test)$"
            assert re.search(reserved, address, re.IGNORECASE), (name, address)


def test_the_preflight_names_an_organization_no_record_can_have(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A claim may have named an organization with an id no record takes: it is named apart, so
    nobody switches to memberships mode and strands its keys."""
    store = AuditStore(db_target)
    store.create_api_key(name="k", role="viewer", org="Clinic_C", created_by="Ada")
    code, out, err = run(capsys, "access", "check", "--db", db_target)
    assert code == 1 and json.loads(out) == [
        {"org": "Clinic_C", "api_keys": 1, "recorded": False, "recordable": False}
    ]
    assert "Clinic_C cannot have a record" in err and "the owner decides" in err


def test_a_protected_database_takes_only_keyed_address_digests(
    db_target: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with AuditStore(db_target, phi=PhiPolicy(mode="hash", hash_key=INPUT_HASH_KEY)):
        pass  # kept under PHI mode hash: protected
    monkeypatch.delenv("MENDARI_PHI_MODE", raising=False)
    monkeypatch.delenv("MENDARI_INPUT_HASH_KEY", raising=False)
    argv = ["access", "bootstrap", "--email", "owner@example.org", "--db", db_target]
    assert main(argv) == 1
    assert "MENDARI_INPUT_HASH_KEY" in capsys.readouterr().err
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    assert main(argv) == 0
    store = AuditStore(db_target, read_only=True)
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT payload FROM audit_events WHERE event = 'membership.created'")
    payloads = [json.loads(r["payload"]) for r in rows]
    assert [p["email_digest"].split(":")[0] for p in payloads] == ["hmac-sha256"]


def test_the_read_commands_leave_the_database_as_it_was(
    tmp_path: Path, triage_agent: Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    """`org list`, `members`, `requests`, `keys`, `clients` and `check` open the database
    read-only: no sidecar file, nothing written; another application's database is refused."""
    folder = tmp_path / "var"
    folder.mkdir()
    db = folder / "mendari.db"
    with AuditStore(db) as store:
        Decision(triage_agent, store=store)({"message": {"body": "Refill please."}})
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")  # so any write shows in its bytes
    before = db.read_bytes()
    reads = (
        ["access", "org", "list"],
        ["access", "members", "--org", "default"],
        ["access", "requests"],
        ["access", "keys"],
        ["access", "clients", "--org", "default"],
    )
    for argv in reads:
        assert main([*argv, "--db", str(db)]) == 0
        assert json.loads(capsys.readouterr().out) == []
    assert main(["access", "check", "--db", str(db)]) == 1
    assert json.loads(capsys.readouterr().out) == [
        {"org": "default", "decisions": 1, "recorded": False, "recordable": True}
    ]
    assert db.read_bytes() == before and sorted(p.name for p in folder.iterdir()) == ["mendari.db"]
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as connection, connection:
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    for argv in (*reads, ["access", "check"]):
        assert main([*argv, "--db", str(other)]) == 1
        assert "not a Mendari database" in capsys.readouterr().err
