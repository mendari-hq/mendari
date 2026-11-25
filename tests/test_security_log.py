# SPDX-License-Identifier: Apache-2.0
"""The security log (server/security_log.py): JSON lines on stdout from an allow-list of fields,
a request id on every response, one access line per API request and the security events; never
a credential, a query value, a body, case text or an exception's message."""

from __future__ import annotations

import base64
import functools
import io
import json
import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari import alerts as alerts_module
from mendari.alerts import AlertSettings
from mendari.core.decision import Decision
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import EvalsetStore
from mendari.core.registry import resolve_pack
from mendari.server import app as app_module
from mendari.server import security_log, smart_routes
from mendari.server.app import create_app, quota_alert
from mendari.server.auth import person_id
from mendari.server.security_log import FIELDS, InternalError
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import (
    HASH_KEY_B64,
    ROOT,
    encrypt_mode,
    encrypted_store,
    evalset_on,
    hosted,
    member,
    message,
    security_lines,
    signed_in,
)
from tests.test_auth import FakeIdp, sign_in, signing_in_with
from tests.test_evalset_labelling import ROUTINE
from tests.test_evalset_labelling import cases as evalset_cases
from tests.test_evalset_store import CLINIC, MARKS, a_set
from tests.test_smart import APP, EHR, REFERRAL, FakeEhr, full_launch, launch

INBOX = "mendari/patient-inbox-router"
TRIAL = "mendari/clinical-trial-prescreener"  # risk level 2: approvable
DECIDE = f"/api/agents/{INBOX}/decide"
CASE_TEXT = "Synthetic: Rosalind Q. Example has crushing chest pain since noon"
QUERY_VALUE = "QueryValueNeverLogged"


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def app(tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with signing_in_with(idp, tmp_path, monkeypatch) as client:
        yield client


def logged(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The security log's lines since the last read, every stdout line checked to be one."""
    out = capsys.readouterr().out
    for line in out.splitlines():
        parsed = json.loads(line)  # nothing else is printed: every line is a whole JSON object
        assert set(parsed) <= set(FIELDS), line
    return security_lines(out)


def key(db: Path, role: str = "service", scope: str | None = None) -> tuple[str, dict[str, str]]:
    """A key's id, and the headers that send it."""
    with AuditStore(db) as store:
        made = store.create_api_key(
            name="ehr", role=role, org="default", created_by="t", scopes=scope
        )
    return made["id"], {"authorization": f"Bearer {made['key']}"}


def test_every_line_is_json_with_allowed_keys_and_never_a_secret(
    app: TestClient, idp: FakeIdp, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_id, machine = key(tmp_path / "a.db")
    sign_in(app, idp, roles=["admin"], name="Dr Kim", email="kim@example.org")
    session = app.cookies["__Host-mendari_session"]
    capsys.readouterr()
    decided = app.post(DECIDE, json=message(CASE_TEXT), headers=machine).json()
    app.get("/api/audit", params={"pack_id": QUERY_VALUE})
    app.get(f"/api/audit/{decided['id']}")
    app.get("/fhir/Task", params={"identifier": QUERY_VALUE})
    app.post("/fhir/Task/_search", data={"status": QUERY_VALUE})
    app.get("/api/health")
    app.get("/api/health/ready")  # probes, like health: no access line
    app.head("/api/health/ready")
    app.get("/static/style.css")
    app.get("/")
    app.get("/api/no/such/route", params={"x": QUERY_VALUE})
    lines = logged(capsys)
    text = json.dumps(lines)
    secret = machine["authorization"].removeprefix("Bearer ")
    for never in (CASE_TEXT, "Rosalind", QUERY_VALUE, secret, session, "Dr Kim", "kim@example"):
        assert never not in text, never
    access = [(e["method"], e.get("route"), e["status"]) for e in lines if e["event"] == "request"]
    assert access == [  # one per API request: none for health, readiness, static files, the page
        ("POST", "/api/agents/{namespace}/{name}/decide", 200),
        ("GET", "/api/audit", 200),
        ("GET", "/api/audit/{decision_id}", 200),
        ("GET", "/fhir/Task", 200),
        ("POST", "/fhir/Task/_search", 200),
        ("GET", None, 404),  # no route: its path is not logged
    ]
    first = lines[0]
    assert first["actor"] == f"key:{key_id}" and first["org"] == "default"
    assert first["outcome"] == "ok" and isinstance(first["latency_ms"], int)
    assert lines[1]["actor"].startswith("user:")  # the person's opaque id, never a name


def test_every_response_carries_a_request_id_and_errors_repeat_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    made = client.get("/api/agents")
    uuid.UUID(made.headers["x-request-id"])  # a new uuid4
    assert client.get("/static/style.css").headers["x-request-id"]
    own = client.get("/api/agents", headers={"x-request-id": "ehr-1234:abc.def_9"})
    assert own.headers["x-request-id"] == "ehr-1234:abc.def_9"
    for forged in ("short", "has a space in it", 'x{"event":"forged"}', "a" * 129):
        answer = client.get("/api/agents", headers={"x-request-id": forged})
        uuid.UUID(answer.headers["x-request-id"])  # not taken: replaced
    vercel = {
        "x-vercel-id": "iad1::iad1::abcde-1727900000000-0123456789ab",
        "x-request-id": "x" * 9,
    }
    assert client.get("/api/agents", headers=vercel).headers["x-request-id"] == "x" * 9
    hosted(monkeypatch)  # only Vercel's own header is trusted, and only there
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    assert (
        client.get("/api/agents", headers=vercel).headers["x-request-id"] == vercel["x-vercel-id"]
    )
    capsys.readouterr()
    missing = client.get("/api/audit/nope")
    assert missing.status_code == 404
    assert missing.json()["request_id"] == missing.headers["x-request-id"]
    fhir = client.get("/fhir/Task/nope")
    assert fhir.json()["resourceType"] == "OperationOutcome" and fhir.headers["x-request-id"]
    lines = logged(capsys)
    assert [e["request_id"] for e in lines] == [
        missing.headers["x-request-id"],
        fhir.headers["x-request-id"],
    ]


@pytest.mark.parametrize(
    ("headers", "params", "event", "reason", "status"),
    [
        ({"authorization": "Bearer junk"}, {}, "auth.credential.refused", "unknown_prefix", 401),
        ({"authorization": "Bearer ohk_unknown"}, {}, "auth.credential.refused", "key", 401),
        ({"authorization": "Bearer mat_0123456789"}, {}, "auth.credential.refused", "token", 401),
        ({"authorization": "Bearer ohk_a=b"}, {}, "auth.credential.refused", "malformed", 401),
        ({"authorization": "Basic dXNlcjpwYXNz"}, {}, "auth.credential.refused", "scheme", 401),
        ({"cookie": "__Host-mendari_session=ended"}, {}, "auth.credential.refused", "session", 401),
        ({}, {"access_token": "ohk_x"}, "auth.credential.refused", "in_url_or_form", 400),
        ("viewer", {}, "auth.permission.refused", "decide", 403),
        ("service", {}, "auth.permission.refused", "people_only", 403),
        ("scoped", {}, "auth.permission.refused", "out_of_scope", 403),
    ],
)
def test_each_refusal_is_one_line_with_its_reason(
    app: TestClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    headers: dict[str, str] | str,
    params: dict[str, str],
    event: str,
    reason: str,
    status: int,
) -> None:
    db = tmp_path / "a.db"
    actor = None
    path, method = DECIDE, "POST"
    if headers == "viewer":
        actor, headers = key(db, "viewer")
    elif headers == "service":
        actor, headers = key(db, "service")
        path = "/api/auth/keys"
    elif headers == "scoped":
        actor, headers = key(db, "viewer", scope="system/Task.rs")
        path, method = "/api/audit", "GET"
    assert isinstance(headers, dict)
    capsys.readouterr()
    answer = app.request(method, path, headers=headers, params=params, json={})
    assert answer.status_code == status
    lines = logged(capsys)
    assert [(e["event"], e.get("reason")) for e in lines] == [(event, reason), ("request", None)]
    refused, access = lines
    assert refused["outcome"] == "refused" and refused["route"] == access["route"]
    assert refused["request_id"] == access["request_id"] == answer.headers["x-request-id"]
    assert refused.get("actor") == (None if actor is None else f"key:{actor}")
    assert access["status"] == status and access["outcome"] in ("refused", "invalid")


def test_two_authorization_headers_are_one_line(
    app: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    sent = [("authorization", "Bearer ohk_a"), ("authorization", "Bearer ohk_b")]
    assert app.get("/api/audit", headers=sent).status_code == 400
    lines = logged(capsys)
    assert (lines[0]["event"], lines[0]["reason"]) == ("auth.credential.refused", "headers")


def test_sign_in_and_out_are_logged_by_opaque_id_with_the_reason_refused(
    app: TestClient, idp: FakeIdp, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert sign_in(app, idp, roles=["marketing"], email="nobody@example.org").status_code == 403
    assert app.get("/auth/callback", params={"code": "c", "state": "s"}).status_code == 400
    assert app.get("/auth/callback", params={"error": "access_denied"}).status_code == 400
    sign_in(app, idp, roles=["admin"], name='Kim\n{"event":"forged"}')
    assert app.post("/auth/logout").status_code == 204
    events = [
        (e["event"], e.get("reason"), e.get("actor"))
        for e in logged(capsys)
        if e["event"] != "request"
    ]
    person = events[0][2]
    assert person is not None and person.startswith("user:")
    assert events == [
        ("auth.signin.refused", "no_role", person),
        ("auth.signin.refused", "state_unknown", None),
        ("auth.signin.refused", "idp_error", None),
        ("auth.signin.ok", None, person),  # the same subject: the same id
        ("auth.logout", None, None),
    ]


def test_a_newline_in_a_value_stays_inside_its_line(capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    security_log.event("auth.signin.refused", reason='x\n{"event":"forged"}\r\u2028y')
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "forged" in json.loads(out)["reason"]


def test_an_unhandled_error_logs_its_type_and_frames_never_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_: Any, **__: Any) -> None:
        raise RuntimeError(CASE_TEXT)

    monkeypatch.setattr(Decision, "decide", broken)
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    app = create_app(db_path=tmp_path / "a.db")
    quiet = TestClient(app, raise_server_exceptions=False)
    capsys.readouterr()
    answer = quiet.post(DECIDE, json=message("hi"))
    assert answer.status_code == 500 and CASE_TEXT not in answer.text
    assert answer.json()["request_id"] == answer.headers["x-request-id"]
    out = capsys.readouterr()
    assert CASE_TEXT not in out.out + out.err
    error, access = security_lines(out.out)
    assert error["event"] == "error.unhandled" and error["error"] == "RuntimeError"
    assert any(frame.endswith(" broken") for frame in error["frames"])
    assert all("site-packages" not in frame for frame in error["frames"])
    assert access["status"] == 500 and access["outcome"] == "error"
    # What reaches the server (and its own traceback print) names the type only.
    with pytest.raises(InternalError) as raised:
        TestClient(app).post(DECIDE, json=message("hi"))
    assert str(raised.value) == "RuntimeError" and raised.value.__suppress_context__
    # In `off` mode (synthetic data) the server sees the exception as before; the log does not.
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    with pytest.raises(RuntimeError, match="Rosalind"):
        TestClient(create_app(db_path=tmp_path / "b.db")).post(DECIDE, json=message("hi"))
    assert CASE_TEXT not in capsys.readouterr().out


def test_changes_exports_evaluations_and_input_reads_are_logged_with_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    with AuditStore(db) as store:
        admin = signed_in(store, role="admin")
        governance = signed_in(store, role="governance")
    urgent = message("Crushing chest pain, " + CASE_TEXT)  # waits for review
    decided = client.post(DECIDE, json=urgent, headers=admin).json()
    client.post(f"/api/audit/{decided['id']}/feedback", json={"rating": "down"}, headers=admin)
    capsys.readouterr()
    made = client.post("/api/auth/keys", json={"name": "ehr", "role": "service"}, headers=admin)
    assert client.delete(f"/api/auth/keys/{made.json()['id']}", headers=admin).status_code == 204
    body = {"pack_id": TRIAL, "reference": "minutes 12", "days": 30}
    approval = client.post("/api/governance", json=body, headers=governance).json()
    assert client.delete(f"/api/governance/{approval['id']}", headers=governance).status_code == 204
    run = {"dataset": "test", "limit": 3}
    assert client.post(f"/api/agents/{INBOX}/evals", json=run, headers=admin).status_code == 200
    client.get("/api/audit/export/fhir", params={"pack_id": QUERY_VALUE}, headers=admin)
    administrative = client.get(
        "/api/audit/administrative/export/fhir",
        params={"since": "2026-01-01T00:00:00Z"},
        headers=admin,
    )
    assert administrative.status_code == 200, administrative.text
    client.get(f"/api/agents/{INBOX}/feedback/export", headers=admin)
    for url in (f"/api/audit/{decided['id']}", "/api/review", "/api/feedback"):
        assert CASE_TEXT in client.get(url, headers=admin).text
    lines = logged(capsys)
    assert QUERY_VALUE not in json.dumps(lines) and CASE_TEXT not in json.dumps(lines)
    events = [(e["event"], e.get("ids"), e.get("count")) for e in lines if e["event"] != "request"]
    one = [decided["id"]]
    assert events == [
        ("admin.key.created", [made.json()["id"]], None),
        ("admin.key.revoked", [made.json()["id"]], None),
        ("governance.approved", [approval["id"]], None),
        ("governance.revoked", [approval["id"]], None),
        ("eval.run", [INBOX], 3),
        ("export.audit", None, None),
        ("export.audit.administrative", None, None),
        ("export.feedback", [INBOX], 1),
        ("phi.inputs.read", one, 1),
        ("phi.inputs.read", one, 1),
        ("phi.inputs.read", one, 1),
        ("phi.inputs.read", one, 1),
    ]
    actors = {e["actor"] for e in lines if e["event"] != "request"}
    assert all(actor.startswith("user:") for actor in actors)


def test_a_note_read_without_its_input_is_logged_as_the_chain_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Notes are sealed apart from the chain (stage H) and outlive a purged input: a reviewer who
    opens one gets an `input.read` in the chain, and the log says so too."""
    phi_key = os.urandom(32)
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(phi_key).decode())
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    with AuditStore(db) as store:
        reviewer = signed_in(store, role="reviewer")
    decided = client.post(DECIDE, json=message("Crushing chest pain"), headers=reviewer).json()
    review = {"final_label": "nurse", "reviewer": "x", "note": CASE_TEXT}
    assert client.post(f"/api/review/{decided['id']}", json=review, headers=reviewer).is_success
    policy = app_module.phi_policy()
    with AuditStore(db, phi=policy) as store:
        assert store.purge_inputs(0) == 1  # the note stays
    capsys.readouterr()
    shown = client.get(f"/api/audit/{decided['id']}", headers=reviewer).json()
    assert "input_restricted" not in shown and CASE_TEXT in json.dumps(shown["events"])
    reads = [e for e in shown["events"] if e["event"] == "input.read"]
    assert reads[-1]["payload"]["reader"] == "reviewer@default.example.org"
    [line] = [e for e in logged(capsys) if e["event"] == "phi.inputs.read"]
    assert (line["ids"], line["count"]) == ([decided["id"]], 1)


def test_labelling_is_logged_by_set_id_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A labeller shown their next hidden case, their label, and a label refused (LP-11j): the
    events name the hidden set by its id and nothing else of it (no case, assignment, label,
    input or attribute), and the access lines the routes' templates. The operator's guide lists
    each event among the security events (docs/deploying-with-phi.md, §7)."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    keys = EvalsetKeys(*evalset_on(monkeypatch))
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    with encrypted_store(db) as store:
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, resolve_pack(INBOX))
        hidden.add_cases(set_id, evalset_cases(1))
        hidden.sample(set_id, cases=1)
        heads = [
            member(store, org=CLINIC, roles=["labeller"], sub=f"labeller-{n}") for n in range(2)
        ]
        users = [person_id("https://idp.example.org", f"labeller-{n}") for n in range(2)]
        hidden.assign(set_id, users)
    capsys.readouterr()
    shown = client.get("/api/labelling/next", headers=heads[0]).json()
    url = f"/api/labelling/{shown['assignment']}"
    assert client.post(url, json={"labels": ROUTINE}, headers=heads[0]).is_success
    assert client.post(url, json={"labels": ROUTINE}, headers=heads[1]).status_code == 404
    lines = logged(capsys)
    events = [
        (e["event"], e.get("ids"), e["outcome"], e.get("reason"), e["actor"])
        for e in lines
        if e["event"] != "request"
    ]
    assert events == [
        ("evalset.case.read", [set_id], "ok", None, users[0]),
        ("evalset.labelled", [set_id], "ok", None, users[0]),
        ("evalset.labelled", None, "refused", "no_assignment", users[1]),
    ]
    routes = {e["route"] for e in lines if e["event"] == "request"}
    assert routes == {"/api/labelling/next", "/api/labelling/{assignment_id}"}
    guide = (ROOT / "docs" / "deploying-with-phi.md").read_text()
    assert [event for event, *_ in events if f"`{event}`" not in guide] == []
    written = json.dumps(lines)
    for private in (shown["assignment"], MARKS["input"], MARKS["author"], "routine", "nurse"):
        assert private not in written, private


def test_curating_is_logged_by_set_id_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An organization's administrator drawing a hidden set's cases, assigning them, and an
    adjudication refused for another organization's set (LP-11k): the events name the set by
    its id and how many, a refusal (`no_set`) no id at all, and the access lines the routes'
    templates; never a case, an assignment, a stratum, an input or a label word. The
    operator's guide lists each event among the security events (docs/deploying-with-phi.md,
    §7)."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    keys = EvalsetKeys(*evalset_on(monkeypatch))
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
    with encrypted_store(db) as store:
        hidden = EvalsetStore(store, keys)
        set_id = a_set(hidden, resolve_pack(INBOX))
        theirs = a_set(hidden, resolve_pack(INBOX), "clinic-b")
        hidden.add_cases(set_id, evalset_cases(2))
        for n in range(2):
            member(store, org=CLINIC, roles=["labeller"], sub=f"labeller-{n}")
        users = [person_id("https://idp.example.org", f"labeller-{n}") for n in range(2)]
        admin = member(store, org=CLINIC, roles=["admin"], sub="curator")
    capsys.readouterr()
    base = f"/api/evalsets/{set_id}"
    assert client.post(f"{base}/sample", json={"cases": 2}, headers=admin).is_success
    told = client.post(f"{base}/assignments", json={"labellers": users}, headers=admin)
    assert told.is_success
    other = f"/api/evalsets/{theirs}/adjudication"
    assert client.post(other, json={"labellers": users}, headers=admin).status_code == 404
    lines = logged(capsys)
    events = [
        (e["event"], e.get("ids"), e.get("count"), e["outcome"], e.get("reason"))
        for e in lines
        if e["event"] != "request"
    ]
    assert events == [
        ("evalset.sampled", [set_id], 2, "ok", None),
        ("evalset.assigned", [set_id], 4, "ok", None),
        ("evalset.adjudication.assigned", None, None, "refused", "no_set"),
    ]
    routes = {e["route"] for e in lines if e["event"] == "request"}
    assert routes == {
        "/api/evalsets/{set_id}/sample",
        "/api/evalsets/{set_id}/assignments",
        "/api/evalsets/{set_id}/adjudication",
    }
    guide = (ROOT / "docs" / "deploying-with-phi.md").read_text()
    assert [event for event, *_ in events if f"`{event}`" not in guide] == []
    written = json.dumps(lines)
    for private in (theirs, MARKS["input"], MARKS["stratum"], MARKS["name"], "routine"):
        assert private not in written, private


def test_a_rate_limit_is_logged_once_per_bucket_and_a_quota_once_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,decide=2/min")
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=1")
    monkeypatch.setattr(alerts_module.httpx, "post", lambda *_, **__: None)
    db = tmp_path / "a.db"
    client = TestClient(create_app(db_path=db))
    key_id, machine = key(db)
    capsys.readouterr()
    statuses = [
        client.post(DECIDE, json=message("hi"), headers=machine).status_code for _ in range(5)
    ]
    assert statuses == [200, 200, 429, 429, 429]
    lines = logged(capsys)
    events = [
        (e["event"], e.get("reason"), e.get("actor")) for e in lines if e["event"] != "request"
    ]
    assert events == [
        ("quota.reached", "decisions", None),
        ("ratelimit.hit", "decide", f"key:{key_id}"),  # once, not for every refusal
    ]
    quota = next(e for e in lines if e["event"] == "quota.reached")
    assert quota["org"] == "default" and quota["count"] == 1 and quota["outcome"] == "limited"
    limited = [e for e in lines if e["event"] == "request" and e["status"] == 429]
    assert len(limited) == 3 and {e["outcome"] for e in limited} == {"limited"}


def test_the_logger_writes_to_stdout_on_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    """Its own handler, not passed on to the root logger (whose handlers would print it again),
    and set up again by every app: a host's logging configuration cannot mute it."""
    security = logging.getLogger("mendari.security")
    security.disabled = True
    security_log.configure()
    security_log.configure()
    assert security.propagate is False and security.disabled is False
    assert sum(isinstance(h, security_log._Stdout) for h in security.handlers) == 1
    capsys.readouterr()
    security_log.event("auth.logout")
    assert json.loads(capsys.readouterr().out)["event"] == "auth.logout"


# -- the EHR launch ---------------------------------------------------------------------------


@pytest.fixture
def smart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeEhr]]:
    """An EHR launch, as in tests/test_smart.py."""
    ehr = FakeEhr()
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    transport = httpx.MockTransport(ehr)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=APP) as client:
        yield client, ehr


def test_each_ehr_launch_is_logged_with_its_reason_and_never_its_values(
    smart: tuple[TestClient, FakeEhr], capsys: pytest.CaptureFixture[str]
) -> None:
    """A launch is a sign-in: its refusals are logged like `/auth/callback`'s (a forged state is
    a launch-CSRF or replay probe), and the reference its error page shows is in the log."""
    client, ehr = smart
    capsys.readouterr()
    forged = client.get("/smart/callback", params={"code": QUERY_VALUE, "state": "forged-state"})
    assert forged.status_code == 400 and "Reference" in forged.text
    assert client.get("/smart/callback", params={"error": "access_denied"}).status_code == 400
    assert client.get("/smart/launch").status_code == 422  # no iss or launch
    elsewhere = {"iss": "https://evil.example/fhir", "launch": QUERY_VALUE, "pack": REFERRAL}
    assert client.get("/smart/launch", params=elsewhere).status_code != 302
    started = launch(client)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    other = TestClient(client.app, base_url=APP)  # someone else's launch
    assert other.get("/smart/callback", params={"code": "c", "state": state}).status_code == 400
    started = launch(client)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    unknown = client.get("/smart/callback", params={"code": "not-issued", "state": state})
    assert unknown.status_code == 400  # the EHR refuses the code
    assert full_launch(client, ehr).status_code == 302
    lines = logged(capsys)
    text = json.dumps(lines)
    for never in (QUERY_VALUE, "forged-state", "evil.example", "not-issued", "ref-p1", "access-"):
        assert never not in text, never
    events = [(e["event"], e.get("reason")) for e in lines if e["event"] != "request"]
    assert events == [
        ("smart.launch.refused", "state_unknown"),
        ("smart.launch.refused", "ehr_error"),
        ("smart.launch.refused", "issuer"),  # not on the allow-list
        ("smart.launch.refused", "browser"),
        ("smart.launch.refused", "token"),
        ("smart.launch.ok", None),
    ]
    access = [(e["route"], e["status"]) for e in lines if e["event"] == "request"]
    assert access[:3] == [
        ("/smart/callback", 400),
        ("/smart/callback", 400),
        ("/smart/launch", 422),
    ]
    assert access[-2:] == [("/smart/launch", 302), ("/smart/callback", 302)]
    refused = next(e for e in lines if e["event"] == "smart.launch.refused")
    assert refused["request_id"] == forged.headers["x-request-id"] in forged.text


# -- library loggers ----------------------------------------------------------------------------


@pytest.fixture
def info_root() -> Iterator[io.StringIO]:
    """The root logger as Vercel's runtime leaves it: INFO, with a handler (here, a buffer)."""
    root = logging.getLogger()
    saved = root.level, root.handlers[:]
    printed = io.StringIO()
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(printed)], force=True)
    try:
        yield printed
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(saved[0])
        for handler in saved[1]:
            root.addHandler(handler)


def test_no_library_logs_a_url_when_the_host_logs_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, info_root: io.StringIO
) -> None:
    """httpx logs each request's URL at INFO: the alert webhook's secret path, a FHIR read's
    Patient id. Every app quiets it (and psycopg's pool), whatever the host did before."""
    for name in security_log.QUIETED:
        logging.getLogger(name).setLevel(logging.NOTSET)
    create_app(db_path=tmp_path / "a.db")
    answered = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "pt-secret"}))

    def post(url: str, **options: Any) -> httpx.Response:
        with httpx.Client(transport=answered) as client:
            return client.post(url, **options)

    monkeypatch.setattr(alerts_module.httpx, "post", post)
    webhook = "https://hooks.example.org/services/T000/B000/WebhookSecretToken"
    with AuditStore(tmp_path / "b.db") as store:
        quota_alert(AlertSettings(webhook), store)("default", "decisions", 5)
    with FhirClient(EHR, allowed={EHR}, transport=answered) as fhir:
        fhir.read("Patient/pt-secret")
    logging.getLogger("httpx").warning("a warning still shows")
    printed = info_root.getvalue()
    assert "a warning still shows" in printed
    for never in ("WebhookSecretToken", "hooks.example.org", "pt-secret", EHR):
        assert never not in printed, never
