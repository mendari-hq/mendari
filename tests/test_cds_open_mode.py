# SPDX-License-Identifier: Apache-2.0
"""Open mode answers CDS Hooks on this machine only (LP-23c, ADR-0014): with sign-in off, a
request to `/cds-services` or a child goes on only from a loopback address, sent to a `Host`
that names this machine, with no forwarding header; or under `MENDARI_CDS_SANDBOX=1`, which
stops startup on Vercel and with sign-in on. Anything else is one `403` with fixed words, logged
as `open_mode_remote`, before any credential is read: a verified token does not lift it. The
rest of open mode is as before.

The suite runs its servers as if the flag were set (`tests/conftest.py`, `_cds_sandbox`); this
module takes that away, so the rule and the flag are tested as they are. Requests from this
machine go over a real loopback socket to a server on 127.0.0.1, and Starlette's TestClient
plays other machines' (it connects as any address it is given). Keys are made at run time."""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server.app import create_app, mounted_routes
from mendari.server.auth import CDS_PATH, OPEN_MODE_REMOTE
from mendari.server.cds_clients import SANDBOX_ENV, CdsOpenMode, on_this_machine
from tests.conftest import encrypt_mode, hosted, security_lines, without_request_id
from tests.jwt_signers import cds_token
from tests.test_cds_clients import DISCOVERY, EHR, HOOK, RSA_EHR, register
from tests.test_limits import order_sign

ELSEWHERE = ("203.0.113.7", 50000)  # another machine (TEST-NET-3)
LOOPBACK = ("127.0.0.1", 50000)
HERE = "http://127.0.0.1:8000"  # this machine, by address: the base URL a local caller uses
REFUSED = {"error": "AuthError", "detail": OPEN_MODE_REMOTE}


@pytest.fixture(autouse=True)
def _cds_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overrides the suite's (`tests/conftest.py`): no flag, and the real rule."""
    monkeypatch.delenv(SANDBOX_ENV, raising=False)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def on_loopback(app: FastAPI, port: int) -> Iterator[str]:
    """`app` served by uvicorn on 127.0.0.1:`port` (its defaults: proxy headers read from
    127.0.0.1 only, as `mendari serve` does) until the block ends; yields its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the local server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def from_(app: FastAPI, peer: tuple[str, int], base: str = HERE) -> TestClient:
    """A caller at `peer`, sending to `base` (its `Host`)."""
    return TestClient(app, base_url=base, client=peer, raise_server_exceptions=False)


def decisions(db: Path) -> int:
    with AuditStore(db) as store:
        return len(store.query(limit=50))


def refusals(out: str) -> list[tuple[str, str]]:
    return [
        (e["event"], e.get("reason", "")) for e in security_lines(out) if e["event"] != "request"
    ]


def assert_refused(answer: Any) -> None:  # httpx's, or TestClient's
    assert answer.status_code == 403, answer.text
    assert without_request_id(answer.json(), answer) == REFUSED


def test_open_mode_answers_cds_hooks_on_this_machine_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With sign-in off and no flag, a caller on this machine lists the services and calls a
    hook, over a real loopback socket, by address or by `localhost`. The same calls from another
    address are each one `403` with fixed words, logged `open_mode_remote`, and decide nothing;
    so is TestClient's own peer, which is no address at all."""
    db = tmp_path / "a.db"
    app = create_app(db_path=db)
    capsys.readouterr()
    remote = from_(app, ELSEWHERE)
    for answer in (remote.get(DISCOVERY), remote.post(HOOK, json=order_sign(1))):
        assert_refused(answer)
    assert_refused(TestClient(app).get(DISCOVERY))  # `testclient`, Host `testserver`
    assert (
        refusals(capsys.readouterr().out) == [("auth.permission.refused", "open_mode_remote")] * 3
    )
    assert decisions(db) == 0
    port = free_port()
    with on_loopback(app, port) as base, httpx.Client(base_url=base, timeout=30) as http:
        listed = http.get(DISCOVERY)
        assert listed.status_code == 200, listed.text
        assert "mendari.referral-router" in {s["id"] for s in listed.json()["services"]}
        called = http.post(HOOK, json=order_sign(1))
        assert called.status_code == 200 and len(called.json()["cards"]) == 1
        named = http.get(DISCOVERY, headers={"host": f"localhost:{port}"})
        assert named.status_code == 200
    assert refusals(capsys.readouterr().out) == []
    assert decisions(db) == 1


def _request(client: tuple[str, int] | None, headers: list[tuple[str, str]]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": DISCOVERY,
        "raw_path": DISCOVERY.encode(),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": client,
    }
    return Request(scope)


def test_a_spoofed_host_or_forwarded_header_is_not_this_machine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """From a loopback address, a `Host` that is not this machine's name (a page that points its
    own name at 127.0.0.1 sends that name), or any forwarding header (a proxy on this machine
    connects from loopback too) is not this machine; nor is another address that names
    `localhost`. A real proxy's `X-Forwarded-For` from 127.0.0.1, which uvicorn trusts and reads
    as the caller's address, is refused too. Loopback by IPv6, by an IPv4-mapped address, and a
    `.localhost` name are this machine."""
    db = tmp_path / "a.db"
    app = create_app(db_path=db)
    capsys.readouterr()
    for base in ("http://mendari.example.org", "http://localhost.example.org", "http://10.0.0.5"):
        assert_refused(from_(app, LOOPBACK, base).get(DISCOVERY))
    local = from_(app, LOOPBACK)
    forwarded = [
        {"forwarded": "for=203.0.113.7"},
        {"x-forwarded-for": "203.0.113.7"},
        {"x-forwarded-for": "127.0.0.1"},
        {"x-forwarded-host": "mendari.example.org"},
        {"x-real-ip": "203.0.113.7"},
    ]
    for headers in forwarded:
        assert_refused(local.post(HOOK, json=order_sign(1), headers=headers))
    assert_refused(from_(app, ELSEWHERE, "http://localhost:8000").get(DISCOVERY))
    assert (
        refusals(capsys.readouterr().out) == [("auth.permission.refused", "open_mode_remote")] * 9
    )
    for peer, base in (
        (LOOPBACK, HERE),
        (LOOPBACK, "http://app.localhost:8000"),
        (("::1", 50000), "http://[::1]:8000"),
        (("::ffff:127.0.0.1", 50000), "http://localhost:8000"),
        (("127.0.0.2", 50000), "http://127.0.0.2"),
    ):
        assert from_(app, peer, base).get(DISCOVERY).status_code == 200, (peer, base)
    assert decisions(db) == 0
    port = free_port()
    with on_loopback(app, port) as base, httpx.Client(base_url=base, timeout=30) as http:
        assert http.get(DISCOVERY).status_code == 200
        assert_refused(http.get(DISCOVERY, headers={"x-forwarded-for": "198.51.100.4"}))
    # One `Host`, and only a name: two, none, or one with a user's part are not this machine.
    host = ("host", "localhost")
    for client, sent in (
        (LOOPBACK, [host, host]),
        (LOOPBACK, []),
        (LOOPBACK, [("host", "evil.example@localhost")]),
        (LOOPBACK, [("host", "localhost/x")]),
        (None, [host]),
        (("unknown", 0), [host]),
    ):
        assert not on_this_machine(_request(client, sent)), (client, sent)
    assert on_this_machine(_request(LOOPBACK, [("host", "LOCALHOST:8000")]))
    assert not CdsOpenMode().answers(_request(ELSEWHERE, [host]))
    assert CdsOpenMode(sandbox=True).answers(_request(ELSEWHERE, [host]))


def sign_in_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off


def test_the_sandbox_flag_answers_from_anywhere_and_stops_startup_on_vercel_or_with_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`MENDARI_CDS_SANDBOX=1` in open mode: another machine lists the services and calls a hook,
    and the server says at startup that it answers any host. `0` is off. Any other value, and
    `1` on Vercel or with sign-in on, stops the server, naming the setting and, for a value, the
    ones it takes."""
    monkeypatch.setenv(SANDBOX_ENV, "1")
    with caplog.at_level("WARNING", logger="mendari.auth"):
        app = create_app(db_path=tmp_path / "sandbox.db")
    assert any(f"{SANDBOX_ENV}=1" in r.getMessage() for r in caplog.records)
    remote = from_(app, ELSEWHERE, "http://engine:8000")
    assert remote.get(DISCOVERY).status_code == 200
    called = remote.post(HOOK, json=order_sign(1))
    assert called.status_code == 200 and len(called.json()["cards"]) == 1

    monkeypatch.setenv(SANDBOX_ENV, "0")
    assert_refused(from_(create_app(db_path=tmp_path / "off.db"), ELSEWHERE).get(DISCOVERY))
    # The refusal names every value that starts, `0` among them.
    accepted = re.escape(f"{SANDBOX_ENV} must be 1 (on), or 0 or unset (off)")
    for value in ("yes", "true", "on", "2", "00"):
        monkeypatch.setenv(SANDBOX_ENV, value)
        with pytest.raises(MendariError, match=f"^{accepted}$"):
            create_app(db_path=tmp_path / "bad.db")

    monkeypatch.setenv(SANDBOX_ENV, "1")
    sign_in_on(monkeypatch)
    with pytest.raises(MendariError, match=f"{SANDBOX_ENV}=1 is for open mode only"):
        create_app(db_path=tmp_path / "signed.db")
    for name in ("MENDARI_OIDC_ISSUER", "MENDARI_OIDC_CLIENT_ID"):
        monkeypatch.delenv(name)
    hosted(monkeypatch)
    with pytest.raises(MendariError, match="never the hosted site"):
        create_app(db_path=tmp_path / "hosted.db")
    monkeypatch.delenv(SANDBOX_ENV)
    create_app(db_path=tmp_path / "hosted.db")  # the hosted site without it starts


def test_a_verified_token_does_not_lift_the_rule_in_open_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With an EHR registered (organization `default`), its valid token from another machine is
    the same `403`, read before the token: nothing is verified, claimed or decided. From this
    machine a fresh token is the `cds:epic` principal, as LP-23b has it."""
    register(monkeypatch, public_url=HERE)
    db = tmp_path / "a.db"
    app = create_app(db_path=db)
    assert from_(app, LOOPBACK).get("/api/health").json()["cds_clients"] == ["epic"]

    def token() -> dict[str, str]:
        return {"authorization": f"Bearer {cds_token(RSA_EHR, EHR, f'{HERE}{HOOK}')}"}

    capsys.readouterr()
    assert_refused(from_(app, ELSEWHERE).post(HOOK, json=order_sign(1), headers=token()))
    out = capsys.readouterr().out
    assert refusals(out) == [("auth.permission.refused", "open_mode_remote")]
    assert "cds:epic" not in out
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        claims = session.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE kind = 'cds-jti'")
    assert claims is not None and claims["n"] == 0 and decisions(db) == 0
    called = from_(app, LOOPBACK).post(HOOK, json=order_sign(1), headers=token())
    assert called.status_code == 200, called.text
    lines = security_lines(capsys.readouterr().out)
    assert [line["actor"] for line in lines if line["event"] == "request"] == ["cds:epic"]
    with AuditStore(db) as store:
        created = [e["payload"] for e in store.events() if e["event"] == "decision.created"]
    assert [payload.get("requested_by") for payload in created] == ["cds epic"]


def test_the_rest_of_open_mode_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """From another machine, over every route the app mounts: only `/cds-services` and its
    children get the refusal; every other route answers as open mode did (deciding, the FHIR
    base and the dashboard among them)."""
    app = create_app(db_path=tmp_path / "a.db")
    remote = from_(app, ELSEWHERE)
    capsys.readouterr()
    refused = set()
    for method, path in sorted(mounted_routes(app)):
        concrete = path.replace("{", "").replace("}", "")
        answer = remote.request(method, concrete, json={} if method != "GET" else None)
        if OPEN_MODE_REMOTE in answer.text:
            assert_refused(answer)
            refused.add((method, path))
    assert refused == {
        ("GET", "/cds-services"),
        ("POST", "/cds-services/{service_id}"),
    }
    assert all(CDS_PATH.match(path) for _, path in refused)
    reasons = [reason for _, reason in refusals(capsys.readouterr().out)]
    assert reasons.count("open_mode_remote") == 2
    assert remote.get("/api/packs").status_code == 200
    decided = remote.post(
        "/api/packs/mendari/patient-inbox-router/decide",
        json={"message": {"body": "Can I get a refill?", "channel": "portal"}},
    )
    assert decided.status_code == 200, decided.text
    assert remote.get("/fhir/metadata").status_code == 200
    assert remote.get("/").status_code == 200
