# SPDX-License-Identifier: Apache-2.0
"""Origin checks (server/origin.py): a request another site starts in a browser that holds a
Mendari cookie (or any browser, in open mode) reaches only public routes and pages, to read;
HSTS on every host but this machine."""

from __future__ import annotations

import base64
import functools
import json
import os
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mendari import Pack
from mendari.core.audit import AuditStore
from mendari.core.phi import PhiPolicy
from mendari.errors import AuthError, MendariError
from mendari.server import app as app_module
from mendari.server import smart_routes
from mendari.server.app import create_app, mounted_routes
from mendari.server.origin import REFUSED, OriginCheck, is_local_host, origin_of, trusted_origins
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import (
    HASH_KEY_B64,
    INPUT_HASH_KEY,
    encrypt_mode,
    message,
    security_lines,
    signed_in,
    without_request_id,
)
from tests.test_auth import FakeIdp, sign_in, signing_in_with
from tests.test_smart import EHR, FakeEhr, full_launch

HOST = "mendari.test"
SITE = f"https://{HOST}"
PUBLIC = "https://mendari.example.org"  # MENDARI_PUBLIC_URL: a name a proxy puts in front
ALIAS = "https://alias.example.org"  # MENDARI_TRUSTED_ORIGINS
COOKIE = {"cookie": "mendari_session=anything"}

# Every route another site's request may reach (with GET): the public routes and the pages. A
# link to anything else, the API's reads included, is refused: an EHR launch's patient and
# items to decide about too (they need the launch, `launch`, since ADR-0006).
NAVIGABLE = {
    "/",
    "/docs",
    "/docs/reference",
    "/redoc",
    "/invite",  # memberships mode: the page an invitation's link opens
    "/favicon.ico",
    "/auth/login",
    "/auth/callback",
    "/smart/launch",
    "/smart/callback",
    "/api/health",
    "/api/health/ready",  # status pages and probes: two fixed codes, from a cached read
    "/api/auth/me",
    "/api/smart/session",
    "/fhir/metadata",
    "/fhir/OperationDefinition/{resource_id}",
    "/fhir/.well-known/smart-configuration",
    # The scheduled purge: public to the guard, its own bearer secret to the route, which a
    # request another site starts cannot carry (it is answered 401, or 404 without the secret).
    "/api/internal/retention",
    "/api/internal/monitor",  # the scheduled monitor: the same secret
}

# (name, headers): how a request can arrive, besides its credential.
KINDS: list[tuple[str, dict[str, str]]] = [
    ("same-origin", {"sec-fetch-site": "same-origin", "origin": SITE}),
    ("cross-site", {"sec-fetch-site": "cross-site", "origin": "https://evil.example"}),
    ("same-site", {"sec-fetch-site": "same-site", "origin": "https://evil.mendari.test"}),
    ("address bar", {"sec-fetch-site": "none"}),
    ("unknown metadata", {"sec-fetch-site": "something-new"}),
    ("own host, no metadata", {"origin": SITE}),
    ("own host on another port", {"origin": f"{SITE}:8443"}),
    ("public URL, no metadata", {"origin": PUBLIC}),
    ("trusted alias, no metadata", {"origin": ALIAS}),
    ("foreign origin, no metadata", {"origin": "https://evil.example"}),
    ("origin null, no metadata", {"origin": "null"}),
    ("not a browser", {}),
]


# HEAD reads as GET does (readiness serves it, for uptime checks).
SAFE = ("GET", "HEAD")


def expected(kind: str, method: str, path: str) -> bool:
    """Whether a request of this kind with a Mendari cookie and no `Authorization` passes."""
    if kind in ("same-origin", "own host, no metadata", "public URL, no metadata"):
        return True
    if kind in ("trusted alias, no metadata", "not a browser"):
        return True
    if kind == "address bar":
        return method in SAFE
    return method in SAFE and path in NAVIGABLE  # another site's (or an unknown) request


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def request(method: str, path: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": concrete(path),
            "raw_path": concrete(path).encode(),
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in {"host": HOST, **headers}.items()],
        }
    )


def passes(check: OriginCheck, method: str, path: str, headers: dict[str, str]) -> bool:
    try:
        check.check(request(method, path, headers))
    except AuthError as error:
        assert (error.status, str(error)) == (403, REFUSED)
        return False
    return True


@pytest.fixture
def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every route, with sign-in, SMART Backend Services and memberships mode on (the most
    routes mounted)."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", SITE)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    return sorted(mounted_routes(create_app(db_path=tmp_path / "routes.db")))


def test_every_route_and_kind_of_request_gets_what_it_should(
    routes: list[tuple[str, str]],
) -> None:
    """Each route x each kind of request: with a Mendari cookie, in both modes; with a bearer
    credential (not checked); with no cookie in open mode; and with no cookie with sign-in on,
    where reads are not checked but unsafe methods are (a browser leaves the SameSite=Lax cookie
    off another site's form POST, and the answer could still clear it)."""
    trusted = frozenset({PUBLIC, ALIAS})
    signed = OriginCheck(open_mode=False, trusted=trusted)
    open_mode = OriginCheck(open_mode=True, trusted=trusted)
    assert {p for m, p in routes if m == "GET"} >= NAVIGABLE
    wrong = []
    for method, path in routes:
        safe = method in ("GET", "HEAD")
        for kind, headers in KINDS:
            want = expected(kind, method, path)
            cases = [
                ("signed in", signed, {**COOKIE, **headers}, want),
                ("open, cookie", open_mode, {**COOKIE, **headers}, want),
                ("open, no cookie", open_mode, headers, want),
                ("bearer", signed, {**COOKIE, "authorization": "Bearer x", **headers}, True),
                ("no cookie", signed, {"cookie": "other=1", **headers}, want or safe),
            ]
            for who, check, sent, should in cases:
                if passes(check, method, path, sent) != should:
                    wrong.append((who, kind, method, path, should))
    assert wrong == []


def test_another_sites_requests_reach_exactly_the_public_routes_and_pages(
    routes: list[tuple[str, str]],
) -> None:
    check = OriginCheck(open_mode=False, trusted=frozenset())
    cross = {**COOKIE, "sec-fetch-site": "cross-site"}
    reached = {(m, p) for m, p in routes if passes(check, m, p, cross)}
    assert {p for m, p in reached if m == "GET"} == NAVIGABLE
    assert reached == {(m, p) for m, p in routes if m in SAFE and p in NAVIGABLE}
    # HEAD reads like GET; the old cookie name and `__Host-` both count as Mendari's.
    assert passes(check, "HEAD", "/", cross)
    assert not passes(check, "HEAD", "/api/review", cross)
    for name in ("mendari_smart", "__Host-mendari_session", "__Host-mendari_smart"):
        assert not passes(check, "GET", "/api/review", {**cross, "cookie": f"{name}=1"}), name


class SpyStore(AuditStore):
    """Records every public method called once `watching` is set."""

    watching = False
    calls: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_") and type(self).watching:
            type(self).calls.append(name)
        return super().__getattribute__(name)


@pytest.mark.parametrize("mode", ["sign-in", "open"])
def test_a_refused_request_never_reaches_the_store_a_body_or_a_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The check comes first in the guard: before the session lookup, any body and the route."""
    monkeypatch.setenv("MENDARI_PUBLIC_URL", SITE)
    if mode == "sign-in":
        monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
        monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
        encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
        monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setattr(app_module, "AuditStore", SpyStore)
    monkeypatch.setattr(SpyStore, "calls", [])
    app = create_app(db_path=tmp_path / "a.db")
    client = TestClient(app, base_url=SITE, raise_server_exceptions=False)
    cookie = signed_in(AuditStore(tmp_path / "a.db"), role="admin")

    async def unread(*args: object) -> AsyncIterator[bytes]:  # Starlette makes one, unread
        raise AssertionError("a body was read")
        yield b""

    async def unread_body(*args: object) -> bytes:
        raise AssertionError("a body was read")

    monkeypatch.setattr(Request, "stream", unread)
    monkeypatch.setattr(Request, "body", unread_body)
    monkeypatch.setattr(SpyStore, "watching", True)
    cross = {**cookie, "sec-fetch-site": "cross-site", "content-type": "application/json"}
    for method, path in mounted_routes(app):
        if method in SAFE and path in NAVIGABLE:
            continue
        refused = client.request(method, concrete(path), headers=cross, content=b"{}")
        assert refused.status_code == 403, (method, path)
        if app_module._FHIR_ROUTE.match(path):  # an OperationOutcome: the id in the header only
            assert refused.json()["issue"][0]["diagnostics"] == REFUSED, path
            assert refused.headers["x-request-id"], path
        else:
            body = without_request_id(refused.json(), refused)
            assert body == {"error": "AuthError", "detail": REFUSED}, path
        assert refused.headers["x-frame-options"] == "DENY"  # the security headers still apply
    assert SpyStore.calls == []


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"sec-fetch-site": "cross-site"}, "cross_site"),
        ({"sec-fetch-site": "same-site"}, "same_site"),
        ({"sec-fetch-site": "something-new"}, "unknown_site"),
        ({"origin": "https://evil.example"}, "foreign_origin"),
        ({"origin": "null"}, "null_origin"),
    ],
)
def test_a_refusal_is_logged_with_its_reason_and_answered_with_a_request_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    headers: dict[str, str],
    reason: str,
) -> None:
    """Open mode, so every browser request is checked: one `auth.origin.refused` line with a
    fixed reason (never the header's value), the access line, and the id the 403 carries."""
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url=SITE)
    capsys.readouterr()
    refused = client.post("/api/auth/keys", headers={**headers, "evil": "x"}, json={})
    assert refused.status_code == 403
    assert without_request_id(refused.json(), refused) == {"error": "AuthError", "detail": REFUSED}
    rid = refused.headers["x-request-id"]
    lines = security_lines(capsys.readouterr().out)
    event, access = lines
    assert event["event"] == "auth.origin.refused" and event["reason"] == reason
    assert event["outcome"] == "refused" and event["request_id"] == rid
    assert access["event"] == "request" and access["status"] == 403
    assert access["request_id"] == rid and access["outcome"] == "refused"
    assert "evil.example" not in json.dumps(lines) and "something-new" not in json.dumps(lines)
    # An address-bar request may only read.
    client.post("/api/auth/keys", headers={"sec-fetch-site": "none"}, json={})
    [event, _] = security_lines(capsys.readouterr().out)
    assert event["reason"] == "unsafe_navigation"


def test_a_link_to_the_review_queue_from_another_site_is_refused_and_nothing_is_chained(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under PHI `encrypt`, opening the queue chains an `input.read` per input in the reader's
    name. A link on another site must not make a reviewer's browser do that."""
    key = os.urandom(32)
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url=SITE)
    chain = AuditStore(
        tmp_path / "a.db", phi=PhiPolicy(mode="encrypt", key=key, hash_key=INPUT_HASH_KEY)
    )
    reviewer = signed_in(chain, role="reviewer")
    decided = client.post(
        f"/api/packs/{inbox_pack.id}/decide",
        json=message("Crushing chest pain since this morning"),
        headers=reviewer,
    )
    assert decided.json()["outcome"] == "HUMAN_REVIEW"
    before = chain.verify()["events"]

    link = {**reviewer, "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    refused = client.get("/api/review", headers=link)
    assert refused.status_code == 403 and refused.json()["detail"] == REFUSED
    assert chain.verify()["events"] == before

    # The dashboard's own request reads the queue, and the read is chained.
    own = client.get("/api/review", headers={**reviewer, "sec-fetch-site": "same-origin"})
    assert own.status_code == 200 and own.json()
    assert chain.verify()["events"] > before


def test_links_from_another_site_to_pages_and_public_routes_work(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed-in person following a link (or an identity provider's redirect) from another
    site: the dashboard, the capability statement, the sign-in callback and the API reference."""
    client = signing_in_with(idp, tmp_path, monkeypatch)
    assert sign_in(client, idp, roles=["admin"]).status_code == 302
    link = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
    assert client.get("/", headers=link).status_code == 200
    assert client.get("/docs/reference", headers=link).status_code == 200
    assert client.get("/fhir/metadata", headers=link).status_code == 200
    callback = client.get("/auth/callback", params={"state": "x"}, headers=link)
    assert callback.status_code == 400 and REFUSED not in callback.text  # the callback's own
    # ... and the session is untouched: a same-origin read still works.
    assert client.get("/api/audit", headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_signing_out_from_another_site_is_refused(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser leaves the SameSite=Lax session cookie off another site's form POST, so the
    request carries no cookie at all; answered, its Set-Cookie would still sign the person out."""
    client = signing_in_with(idp, tmp_path, monkeypatch)
    sign_in(client, idp, roles=["admin"])
    browser = TestClient(client.app, base_url=SITE)  # as the browser sends it: no cookie
    form = {"content-type": "application/x-www-form-urlencoded"}
    for sent in (
        {"sec-fetch-site": "cross-site", "origin": "https://evil.example"},
        {"sec-fetch-site": "same-site", "origin": "https://evil.mendari.test"},
        {"origin": "https://evil.example"},  # a browser without Fetch Metadata
        {"origin": "null"},
    ):
        refused = browser.post("/auth/logout", headers={**form, **sent})
        assert refused.status_code == 403 and "set-cookie" not in refused.headers, sent
    assert client.get("/api/auth/me").json()["signed_in"] is True
    # Not from a browser (no Origin, no Fetch Metadata): a script may still sign out.
    assert browser.post("/auth/logout").status_code == 204
    assert client.post("/auth/logout", headers={"sec-fetch-site": "same-origin"}).status_code == 204
    assert client.get("/api/auth/me").json()["signed_in"] is False


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def smart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeEhr]]:
    """An EHR launch in open mode, as in tests/test_smart.py."""
    ehr = FakeEhr()
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", SITE)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    transport = httpx.MockTransport(ehr)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=SITE) as client:
        yield client, ehr


def test_an_ehr_launch_works_and_its_session_ends_only_from_this_site(
    smart: tuple[TestClient, FakeEhr],
) -> None:
    client, ehr = smart
    done = full_launch(client, ehr)  # the EHR's redirects: no Fetch Metadata in the test client
    assert done.status_code == 302
    cross = {"sec-fetch-site": "cross-site"}
    assert client.get("/smart/launch", headers=cross, follow_redirects=False).status_code != 403
    assert client.post("/api/smart/logout", headers=cross).status_code == 403
    cookieless = TestClient(client.app, base_url=SITE)  # another site's form: no cookie sent
    assert cookieless.post("/api/smart/logout", headers=cross).status_code == 403
    assert client.post("/api/smart/decide", headers=cross, json={}).status_code == 403
    assert client.get("/api/smart/session").json()["active"] is True
    own = {"sec-fetch-site": "same-origin"}
    assert client.post("/api/smart/logout", headers=own).status_code == 204
    assert client.get("/api/smart/session").json() == {"active": False}


def test_the_ehr_launchs_cookies_say_samesite_lax_explicitly(
    smart: tuple[TestClient, FakeEhr],
) -> None:
    """Chrome lets a cross-site POST carry a cookie with no SameSite attribute for two minutes
    ("Lax by default"); one that says Lax gets no such exception. Setting and clearing."""
    launched, ehr = smart
    cookies = [full_launch(launched, ehr).headers["set-cookie"]]
    cookies.append(launched.post("/api/smart/logout").headers["set-cookie"])
    for cookie in cookies:
        assert "samesite=lax" in cookie.lower(), cookie


def test_cookies_of_a_sign_in_say_samesite_lax_explicitly(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signing_in_with(idp, tmp_path, monkeypatch)
    cookies = [sign_in(client, idp, roles=["admin"]).headers["set-cookie"]]
    cookies.append(client.post("/auth/logout").headers["set-cookie"])
    for cookie in cookies:
        assert "samesite=lax" in cookie.lower(), cookie


def test_hsts_on_every_host_but_this_machine(tmp_path: Path) -> None:
    app: FastAPI = create_app(db_path=tmp_path / "a.db")
    for base in (SITE, "http://mendari.internal:8000", "http://testserver"):
        response = TestClient(app, base_url=base).get("/api/health")
        assert response.headers["strict-transport-security"] == "max-age=63072000", base
    for base in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
        response = TestClient(app, base_url=base).get("/api/health")
        assert "strict-transport-security" not in response.headers, base
    assert is_local_host("app.localhost") and not is_local_host("localhost.example.org")


def test_trusted_origins_are_origins_and_a_bad_one_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://Mendari.Example.org:443/base")
    monkeypatch.setenv("MENDARI_TRUSTED_ORIGINS", " https://alias.example.org/, http://[::1]:8000")
    assert trusted_origins() == {
        "https://mendari.example.org",
        "https://alias.example.org",
        "http://[::1]:8000",
    }
    for bad in ("alias.example.org", "https://a.example.org/app", "ftp://a.example.org", "null"):
        monkeypatch.setenv("MENDARI_TRUSTED_ORIGINS", bad)
        with pytest.raises(MendariError, match="MENDARI_TRUSTED_ORIGINS"):
            create_app(db_path=tmp_path / "a.db")
    assert origin_of("HTTPS://A.example.org:443") == "https://a.example.org"
    assert origin_of("http://a.example.org:80") == "http://a.example.org"
    for junk in ("null", "", "https://", "https://u:p@a.example.org", "https://a:99999"):
        assert origin_of(junk) is None, junk


def test_scripts_with_a_cookie_and_no_browser_headers_keep_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """curl, ZAP and pilot-check send neither `Origin` nor Fetch Metadata."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    client = TestClient(create_app(db_path=tmp_path / "a.db"), base_url=SITE)
    cookie = signed_in(AuditStore(tmp_path / "a.db"), role="admin")
    made = client.post("/api/auth/keys", json={"name": "ci", "role": "viewer"}, headers=cookie)
    assert made.status_code == 201, made.text
    # A bearer credential decides alone, whatever the browser headers say.
    key = {"authorization": f"Bearer {made.json()['key']}", "sec-fetch-site": "cross-site"}
    assert client.get("/api/audit", headers=key).status_code == 200
