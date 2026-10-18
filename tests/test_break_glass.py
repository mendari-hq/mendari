# SPDX-License-Identifier: Apache-2.0
"""The break-glass for access (LP-21l): `mendari access revoke` and `mendari access end-sessions`
write straight to the server's database, for any organization, so they work with the server
down; each is chained with who and why, and takes effect on the server's next request. On SQLite
and Postgres (`db_target`)."""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core.audit import SESSION_GROUPS, break_glass_reason
from mendari.server import auth as auth_module
from mendari.standards import audit_fhir, fhir_structure
from tests.conftest import ROOT
from tests.test_auth import FakeIdp, key_for, sign_in, signing_in_with
from tests.test_chain_orgs import chained, jwks

REASON = "leaked in a support ticket"
SCOPE = "https://mendari.co/fhir/scope/decide"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else None, err


def events(store: AuditStore, kind: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in chained(store) if event["event"] == kind]


def test_the_operator_revokes_any_organization_s_key_and_client_with_a_reason(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = AuditStore(db_target)
    key = store.create_api_key(name="k", role="service", org="clinic-a", created_by="Ada")
    client = store.register_client(
        name="c", role="service", org="clinic-b", created_by="Ada", scopes=SCOPE, jwks=jwks()
    )
    token, _ = store.issue_token(client["client_id"], SCOPE, 300)
    assert store.api_key(key["key"]) is not None and store.access_token(token) is not None
    code, out, err = run(
        capsys, "access", "revoke", "key", key["id"], "--reason", REASON, "--db", db_target
    )
    assert code == 0 and out == {"keys": [key["id"]], "clients": []} and "revoked in" in err
    assert store.api_key(key["key"]) is None  # refused from the next request on
    code, out, _ = run(
        capsys, "access", "revoke", "client", client["client_id"],
        "--reason", " leaked in\na  support\tticket ", "--db", db_target,
    )  # fmt: skip
    assert code == 0 and out == {"keys": [], "clients": [client["client_id"]]}
    assert store.client(client["client_id"]) is None and store.access_token(token) is None
    [revoked_key] = events(store, "key.revoked")
    [revoked_client] = events(store, "client.revoked")
    for revoked in (revoked_key, revoked_client):
        assert revoked["reason"] == REASON  # one line
        assert revoked["revoked_by"].startswith("command line (")
    assert store.verify()["ok"]
    # Again, or an id the database does not have: refused, nothing chained.
    code, _, err = run(
        capsys, "access", "revoke", "key", key["id"], "--reason", REASON, "--db", db_target
    )
    assert code == 1 and f"no key {key['id']} in force" in err
    code, _, err = run(
        capsys, "access", "revoke", "client", "nope", "--reason", REASON, "--db", db_target
    )
    assert code == 1 and "no client nope in force" in err
    code, listed, _ = run(capsys, "access", "keys", "--db", db_target)
    assert [(k["id"], k["status"]) for k in listed] == [(key["id"], "revoked")]
    assert key["key"] not in json.dumps(listed)  # never a secret
    code, listed, _ = run(capsys, "access", "clients", "--org", "clinic-b", "--db", db_target)
    assert [c["client_id"] for c in listed] == [client["client_id"]]
    assert listed[0]["revoked_by"].startswith("command line (")


def test_revoking_all_of_an_organization_leaves_every_other_s(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every key and client of clinic-a still in force, a rotated key in its overlap too (it
    still works); not one already revoked or expired; nothing of clinic-b's."""
    store = AuditStore(db_target)
    mine = [
        store.create_api_key(name=f"k{n}", role="service", org="clinic-a", created_by="Ada")
        for n in range(4)
    ]
    rotated = store.rotate_api_key(mine[2]["id"], rotated_by="Ada", overlap_hours=24)
    assert store.api_key(mine[2]["key"]) is not None  # replaced, in its overlap: it works
    with store._db.session(exclusive=True) as db:  # expired: works no more
        db.all("UPDATE api_keys SET expires_at = '2020-01-01' WHERE id = ?", (mine[3]["id"],))
    theirs = store.create_api_key(name="k", role="service", org="clinic-b", created_by="Ada")
    client = store.register_client(
        name="c", role="service", org="clinic-a", created_by="Ada", scopes=SCOPE, jwks=jwks()
    )
    other = store.register_client(
        name="c", role="service", org="clinic-b", created_by="Ada", scopes=SCOPE, jwks=jwks()
    )
    token, _ = store.issue_token(other["client_id"], SCOPE, 300)
    store.revoke_api_key(mine[1]["id"], revoked_by="Ada")  # already revoked: left as it was
    code, out, _ = run(
        capsys, "access", "revoke", "all", "--org", "clinic-a", "--reason", REASON,
        "--db", db_target,
    )  # fmt: skip
    assert code == 0 and out["clients"] == [client["client_id"]]
    assert sorted(out["keys"]) == sorted([mine[0]["id"], mine[2]["id"], rotated["id"]])
    for key in (mine[0], mine[2], rotated):
        assert store.api_key(key["key"]) is None
    assert store.api_key(theirs["key"]) is not None
    assert store.client(other["client_id"]) is not None and store.access_token(token) is not None
    revoked = events(store, "key.revoked")
    assert revoked[0]["id"] == mine[1]["id"] and "reason" not in revoked[0]  # not the break-glass
    assert mine[3]["id"] not in {e["id"] for e in revoked}  # expired: no event
    for event in (*revoked[1:], *events(store, "client.revoked")):
        assert event["reason"] == REASON and event["revoked_by"].startswith("command line (")
    code, _, err = run(
        capsys, "access", "revoke", "all", "--org", "clinic-a", "--reason", REASON,
        "--db", db_target,
    )  # fmt: skip
    assert code == 1 and "no key or client of clinic-a in force" in err


def test_the_listings_read_an_older_database_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """From before Phase 4 (keys without scopes, expiry or rotation) and before Backend Services
    (no clients): keys listed as they are, none of them usable; no client."""
    db = tmp_path / "old.db"
    with AuditStore(db) as store:
        key = store.create_api_key(name="k", role="service", org="clinic-a", created_by="Ada")
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DROP TABLE oauth_tokens")
        connection.execute("DROP TABLE oauth_clients")
        for column in ("scopes", "expires_at", "revoked_by", "replaced_by", "preset"):
            connection.execute(f"ALTER TABLE api_keys DROP COLUMN {column}")
        connection.execute("PRAGMA journal_mode=DELETE")
    before = db.read_bytes()
    code, listed, _ = run(capsys, "access", "keys", "--db", str(db))
    assert code == 0 and [(k["id"], k["status"], k["scope"]) for k in listed] == [
        (key["id"], "expired", None)  # no expiry is refused, as the server refuses it
    ]
    code, listed, _ = run(capsys, "access", "clients", "--db", str(db))
    assert code == 0 and listed == []
    assert db.read_bytes() == before


def put_every_kind(store: AuditStore) -> int:
    """A row of each kind the session table holds, and an expired person's session."""
    until = int(time.time()) + 600
    for kind in ("user", "member", "oidc-pending", "session"):
        store.smart_put(f"{kind}-1", kind, {"sub": "s"}, until)
    store.smart_put("pending-1", "pending", {"iss": "e"}, until, secret="binding")
    store.smart_put("user-old", "user", {"sub": "s"}, int(time.time()) - 1)
    assert store.smart_claim("jti", "client\njti-1", until)
    return until


def test_ending_sessions_ends_a_group_s_kinds_and_never_a_claim(
    db_target: str, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    store = AuditStore(db_target)
    Decision(inbox_pack, store=store)({"message": {"body": "Refill please."}})  # a server's
    until = put_every_kind(store)
    code, out, err = run(
        capsys, "access", "end-sessions", "--kind", "people", "--reason", REASON,
        "--db", db_target,
    )  # fmt: skip
    assert code == 0 and "ended 3 in" in err  # live ones: the expired row is not counted
    assert out["kinds"] == ["member", "oidc-pending", "user"] and out["ended"] == 3
    for kind in ("user", "member", "oidc-pending"):
        assert store.smart_get(f"{kind}-1", kind) is None
    assert store.smart_get("session-1", "session") is not None  # launches: not asked
    assert not store.smart_claim("jti", "client\njti-1", until)  # still claimed
    code, out, _ = run(
        capsys, "access", "end-sessions", "--kind", "all", "--reason", REASON, "--db", db_target
    )
    assert code == 0 and out["ended"] == 2
    assert store.smart_get("session-1", "session") is None
    assert store.smart_get("pending-1", "pending", secret="binding") is None
    assert not store.smart_claim("jti", "client\njti-1", until)  # a claim outlives everything
    first, second = events(store, "sessions.ended")
    assert (first["kinds"], second["kinds"]) == (
        ["member", "oidc-pending", "user"],
        ["member", "oidc-pending", "pending", "session", "user"],
    )
    assert first["reason"] == REASON and first["ended_by"].startswith("command line (")
    assert "org" not in first  # the deployment's, no organization's
    assert store.verify()["ok"]


def test_the_break_glass_refuses_a_database_that_is_not_the_server_s_and_a_missing_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    missing = str(tmp_path / "missing.db")
    for argv in (["revoke", "key", "k"], ["end-sessions", "--kind", "all"]):
        code, _, err = run(capsys, "access", *argv, "--reason", REASON, "--db", missing)
        assert code == 1 and "no database at" in err and not Path(missing).exists()
        code, _, err = run(capsys, "access", *argv, "--reason", REASON)
        assert code == 1 and "give --db" in err
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as connection, connection:
        connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    code, _, err = run(
        capsys, "access", "end-sessions", "--kind", "all", "--reason", REASON, "--db", str(other)
    )
    assert code == 1 and "not a Mendari database" in err
    fresh = tmp_path / "fresh.db"
    AuditStore(fresh).close()  # a server's before its first decision: people may be signed in
    code, out, err = run(
        capsys, "access", "end-sessions", "--kind", "all", "--reason", REASON, "--db", str(fresh)
    )
    assert code == 0 and out["ended"] == 0 and f"ended 0 in {fresh}" in err  # names where
    for reason in ("   ", "x" * 201):
        code, _, err = run(
            capsys, "access", "revoke", "key", "k", "--reason", reason, "--db", str(fresh)
        )
        assert code == 1 and "give a reason of at most 200 characters" in err
    assert break_glass_reason(" x" * 100) == ("x " * 100).strip()  # 199 characters: one line
    assert break_glass_reason("x" * 200) == "x" * 200
    code, _, err = run(capsys, "access", "revoke", "key", "k", "--reason", "y" * 200,
                       "--db", str(fresh))  # fmt: skip
    assert code == 1 and "no key k in force" in err  # the reason was accepted
    assert main(["access", "revoke", "key", "k", "--db", str(fresh)]) == 1  # a reason is required
    assert "required: --reason" in capsys.readouterr().err


def test_a_revoked_key_and_ended_sessions_are_refused_by_the_running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The server, PHI mode `encrypt` as sign-in needs, reads each credential and session on
    every request: the break-glass needs no restart, and opens its database as it is."""
    idp = FakeIdp()
    with signing_in_with(idp, tmp_path, monkeypatch) as site:
        secret = key_for(site, idp)
        bearer = {"authorization": f"Bearer {secret}"}
        assert site.get("/api/packs", headers=bearer).is_success
        sign_in(site, idp, roles=["admin"])
        assert site.get("/api/auth/me").json()["signed_in"] is True
        db = str(tmp_path / "a.db")
        [key] = AuditStore(db).api_keys()
        assert main(["access", "revoke", "key", key["id"], "--reason", REASON, "--db", db]) == 0
        assert site.get("/api/packs", headers=bearer).status_code == 401
        assert main(["access", "end-sessions", "--kind", "people", "--reason", REASON,
                     "--db", db]) == 0  # fmt: skip
        assert site.get("/api/auth/me").json()["signed_in"] is False
        assert AuditStore(db).verify()["ok"]
    capsys.readouterr()


def test_every_session_kind_the_server_writes_is_in_a_group_but_claims() -> None:
    """A new kind of session must be put in `SESSION_GROUPS` (or be a claim, never ended: a
    Backend Services assertion's `jti`, a CDS client's token's `cds-jti`), so the break-glass
    cannot miss one."""
    written: set[str] = {auth_module.MEMBER}
    for path in (ROOT / "src" / "mendari").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            position = {"smart_put": 1, "smart_take": 1, "smart_read": 1, "smart_claim": 0}
            if name in position and len(node.args) > position[name]:
                kind = node.args[position[name]]
                if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                    written.add(kind.value)
    grouped = {kind for kinds in SESSION_GROUPS.values() for kind in kinds}
    claims = {"jti", "cds-jti"}
    assert written - claims == grouped
    assert claims <= written and not claims & grouped


def test_ending_sessions_renders_as_an_audit_event_without_its_reason() -> None:
    payload = {
        "kinds": ["member", "user"],
        "ended": 4,
        "ended_by": "command line (ops)",
        "ended_at": "2026-10-08T00:00:00.000+00:00",
        "reason": REASON,
    }
    chained_as = {"subject": "sessions", "event": "sessions.ended", "payload": payload}
    event = audit_fhir.administrative_event(
        {**chained_as, "created_at": payload["ended_at"], "hash": "a" * 64}
    )
    assert fhir_structure.check(event) == [] and event["action"] == "D"
    assert event["agent"][0]["who"]["display"] == "command line (ops)"
    details = {d["type"]: d["valueString"] for d in event["entity"][0]["detail"]}
    assert (details["kinds"], details["ended"]) == ("member,user", "4")
    assert REASON not in json.dumps(event)
