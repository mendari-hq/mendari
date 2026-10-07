# SPDX-License-Identifier: Apache-2.0
"""`mendari access` (LP-12d): the operator's organizations, bootstrap, invitations and the
preflight before memberships mode, against the server's database (SQLite or Postgres)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mendari import AuditStore
from mendari.cli import main
from tests.conftest import ROOT


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
        org="default", pack_id=None, pack_digest=None, reason="test", set_by="Ada"
    )
    events = store.verify()["events"]
    code, out, err = run(capsys, "access", "check", "--db", db_target)
    report = json.loads(out)
    assert code == 1 and "no record for clinic-b, default" in err
    assert report == [
        {"org": "clinic-b", "api_keys": 1, "recorded": False},
        {"org": "default", "automation_holds": 1, "recorded": False},
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
    for name in ("test_accounts_store.py", "test_access_cli.py", "test_tenancy.py"):
        text = (ROOT / "tests" / name).read_text()
        for address in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text):
            reserved = r"@(example\.(org|com|net)|[\w.-]+\.test)$"
            assert re.search(reserved, address, re.IGNORECASE), (name, address)
