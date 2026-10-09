# SPDX-License-Identifier: Apache-2.0
"""The grading key and the hidden-set permissions (LP-11d, ADR-0011). A grading key is the only
credential that holds `grade`: the operator mints it on the command line (never over HTTP), for
at most 365 days, chained with why; it holds nothing else; an administrator lists and revokes
it, never rotates it. No person, administrator, other key or open mode holds `grade`; `label`
and `curate` are people's, and open mode holds none of the three. A grading route refuses
everyone else with its own reason. No route needs these yet. All keys are made per test."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.cli import main
from mendari.server import auth as auth_module
from mendari.server import security_log
from mendari.server.app import create_app
from mendari.server.auth import Auth, AuthSettings, Principal
from tests.conftest import encrypt_mode, encrypted_store, security_lines, signed_in
from tests.test_api_keys import chained
from tests.test_auth import CLIENT, IDP, attempt
from tests.test_hosted_data import sign_in_env


def minted(capsys: pytest.CaptureFixture[str], db: Path, *extra: str) -> tuple[int, Any]:
    """`mendari access grader-key`'s exit code, and the key it printed (or what it said)."""
    code = main(["access", "grader-key", "--reason", "LP-17's runner", "--db", str(db), *extra])
    printed = capsys.readouterr()
    return code, (json.loads(printed.out) if code == 0 else printed.err)


def test_the_command_line_mints_a_grading_key_and_chains_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    AuditStore(db).close()  # a Mendari database, as the server's
    code, key = minted(capsys, db, "--org", "default")
    assert code == 0
    assert (key["role"], key["org"], key["name"], key["scope"]) == (
        "grading",
        "default",
        "grading",
        None,
    )
    assert key["key"].startswith("ohk_") and key["reason"] == "LP-17's runner"
    with AuditStore(db) as store:
        (created,) = [e for e in chained(store, key["id"]) if e["event"] == "key.created"]
        assert created["role"] == "grading" and created["reason"] == "LP-17's runner"
        assert key["key"] not in json.dumps(created)  # the secret is printed once, never chained
        assert store.verify()["ok"]
        found = store.api_key(key["key"])
        assert found is not None and found["role"] == "grading"
    assert minted(capsys, db, "--org", "default", "--days", "365")[0] == 0
    for days in ("0", "366"):
        code, said = minted(capsys, db, "--org", "default", "--days", days)
        assert code == 1 and "a key lives 1 to 365 days" in said


def test_a_grading_key_needs_a_recorded_organization_where_there_are_some(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    with AuditStore(db) as store:
        store.create_organization(org="clinic-a", name="Clinic A", created_by="t")
    code, said = minted(capsys, db, "--org", "clinic-b")
    assert code == 1 and "no organization clinic-b" in said
    assert minted(capsys, db, "--org", "clinic-a")[0] == 0


def test_a_grading_key_holds_grade_and_nothing_else() -> None:
    def key(roles: set[str], scope: Any = None) -> Principal:
        return Principal(
            kind="key", name="k", roles=frozenset(roles), grant=scope, id="key:k", actor="k"
        )

    assert key({"grading"}).permissions() == {"grade"}
    assert key({"grading"}).is_grading_key and not key({"service"}).is_grading_key
    assert "grade" not in key({"service"}).permissions()
    person = Principal(
        kind="user", name="p", roles=frozenset({"admin", "grading"}), id="u", actor="u"
    )
    assert "grade" not in person.permissions() and "curate" in person.permissions()
    assert "label" not in person.permissions()  # an administrator labels nothing
    labeller = Principal(kind="user", name="l", roles=frozenset({"labeller"}), id="l", actor="l")
    assert labeller.permissions() == {"label"}
    as_key = key({"labeller", "admin"})
    assert {"label", "curate", "keys", "grade"}.isdisjoint(as_key.permissions())  # people's
    for permission in ("grade", "label", "curate"):
        assert not auth_module.OPEN.may(permission), permission
    assert auth_module.OPEN.may("admin") and auth_module.OPEN.may("keys")


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AuditStore]:
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")
    return client, encrypted_store(tmp_path / "a.db")


def test_an_administrator_lists_and_revokes_it_but_never_rotates_or_mints_one(
    server: tuple[TestClient, AuditStore],
) -> None:
    client, store = server
    admin = signed_in(store, role="admin")
    key = store.create_grading_key(org="default", created_by="operator", reason="runner")
    bearer = {"authorization": f"Bearer {key['key']}"}
    listed = {k["id"]: k for k in client.get("/api/auth/keys", headers=admin).json()}
    assert listed[key["id"]]["role"] == "grading" and "key" not in listed[key["id"]]
    rotated = client.post(f"/api/auth/keys/{key['id']}/rotate", json={}, headers=admin)
    assert rotated.status_code == 409
    for body in ({"name": "g", "role": "grading"}, {"name": "l", "role": "labeller"}):
        assert client.post("/api/auth/keys", json=body, headers=admin).status_code == 422
    assert client.get("/api/audit", headers=bearer).status_code == 403  # it reads nothing
    assert client.get("/api/packs", headers=bearer).status_code == 403
    health = client.get("/api/health", headers=bearer).json()
    assert set(health) == {"status", "version", "sign_in"}  # it may not read: three fields
    assert client.delete(f"/api/auth/keys/{key['id']}", headers=admin).status_code == 204
    assert client.get("/api/health/ready", headers=bearer).status_code in (200, 503)  # public
    assert client.get("/api/audit", headers=bearer).status_code == 401  # revoked: no credential


def test_a_grading_route_lets_the_grading_key_through_and_refuses_everyone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No route needs `grade` yet (11l adds one): a path mapped to it here shows the guard."""
    probe = "/api/grading-probe"
    real = Auth.permission
    monkeypatch.setattr(
        Auth,
        "permission",
        lambda self, method, path: "grade" if path == probe else real(self, method, path),
    )
    security_log.configure()
    settings = AuthSettings(
        issuer=IDP, client_id=CLIENT, client_secret=None, role_claim="roles", role_map={},
        org_claim=None, public_url=None,
    )  # fmt: skip
    store = AuditStore(tmp_path / "a.db")
    guard = Auth(settings, store)
    key = store.create_grading_key(org="default", created_by="operator", reason="runner")
    service = store.create_api_key(name="s", role="service", org="default", created_by="t")
    assert attempt(guard, "GET", probe, {"authorization": f"Bearer {key['key']}"})[0] == 200
    capsys.readouterr()
    for headers in (signed_in(store, role="admin"), {"authorization": f"Bearer {service['key']}"}):
        assert attempt(guard, "GET", probe, headers)[0] == 403
    reasons = [line.get("reason") for line in security_lines(capsys.readouterr().out)]
    assert reasons.count("grading_only") == 2
    # A `grading` claim makes no one a grader: claims mode knows no such role, so it signs no
    # one in.
    assert attempt(guard, "GET", probe, signed_in(store, role="grading"))[0] == 401
