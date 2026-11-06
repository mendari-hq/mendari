# SPDX-License-Identifier: Apache-2.0
"""CORS for the CDS Hooks services (LP-23c, ADR-0014, `server/cors.py`): a page on an origin a
CDS client registered reads `/cds-services` and its children, and nothing else; its preflight is
answered before the guard, never with credentials; a page on any other origin gets no CORS
header and is refused as before; and a CDS client's token is refused from an origin that is
neither the client's nor this site's, another client's included. Over HTTP, with sign-in on and
in open mode (on this machine). Keys are made at run time."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from mendari.server.auth import CDS_ORIGIN, Auth
from mendari.server.cors import ALLOW_HEADERS, ALLOW_METHODS, MAX_AGE
from mendari.server.origin import REFUSED
from tests.conftest import encrypt_mode, security_lines, signed_in, without_request_id
from tests.jwt_signers import PUBLIC_URL, cds_token
from tests.test_cds_clients import (
    DISCOVERY,
    EHR,
    HOOK,
    OTHER_EHR,
    RSA_EHR,
    entry,
    register,
    token,
)
from tests.test_limits import order_sign

APP = "https://ehr-app.example.org"  # where the EHR's own page runs, registered for `epic`
OTHER_APP = "https://other-ehr.example.org"  # registered for `other`
EVIL = "https://evil.example"  # registered for no one
HERE = "http://127.0.0.1:8000"
LOOPBACK = ("127.0.0.1", 50000)
OTHER = entry("other", iss=f"{EHR}/other", jwks={"keys": [OTHER_EHR.jwk]}, origins=[OTHER_APP])


def bearer(sent: str) -> dict[str, str]:
    return {"authorization": f"Bearer {sent}"}


def page_on(origin: str) -> dict[str, str]:
    """What a browser adds to a page's `fetch` to another origin."""
    return {"origin": origin, "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}


def preflight(origin: str, method: str = "POST") -> dict[str, str]:
    return {
        **page_on(origin),
        "access-control-request-method": method,
        "access-control-request-headers": "authorization,content-type",
    }


def cors_headers(answer: Any) -> dict[str, str]:  # a TestClient's response
    return {k: v for k, v in answer.headers.items() if k.startswith("access-control-")}


def varies_by_origin(answer: Any) -> bool:
    return "origin" in {v.strip().lower() for v in answer.headers.get("vary", "").split(",")}


def claims(db: str | Path) -> int:
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        row = session.one("SELECT COUNT(*) AS n FROM smart_sessions WHERE kind = 'cds-jti'")
    assert row is not None
    return int(row["n"])


def sign_in_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off


@pytest.fixture
def signed(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """Sign-in on, `epic` registered with its page's origin, and `other` with its own."""
    sign_in_on(monkeypatch)
    register(monkeypatch, entry(origins=[APP]), OTHER)
    with TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL) as http:
        yield http, db_target


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    """Open mode, on this machine, `epic` registered (organization `default`) with its page's
    origin, its tokens addressed to `HERE` (after `signed`, sign-in off again)."""
    for name in ("MENDARI_OIDC_ISSUER", "MENDARI_OIDC_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    register(monkeypatch, entry(origins=[APP]), OTHER, public_url=HERE)
    db = tmp_path / "open.db"
    return TestClient(create_app(db_path=db), base_url=HERE, client=LOOPBACK), db


def here_token(path: str) -> str:
    return cds_token(RSA_EHR, EHR, f"{HERE}{path}")


def test_a_registered_origin_gets_cors_on_cds_services_only(
    signed: tuple[TestClient, str], local: tuple[TestClient, Path]
) -> None:
    """A page on `epic`'s origin, with `epic`'s token, lists the services and calls a hook, and
    each answer names that origin (`Access-Control-Allow-Origin`), varies by `Origin`, and never
    allows credentials; a refusal there names it too, so the page can read why. Off
    `/cds-services` the same page gets no CORS header: not on the REST API with a key, not on
    the public FHIR metadata, not on health, and its preflight there is refused as before. In
    open mode, on this machine, the page calls a hook with no token at all."""
    http, db = signed
    listed = http.get(DISCOVERY, headers={**page_on(APP), **bearer(token())})
    called = http.post(
        HOOK, json=order_sign(1), headers={**page_on(APP), **bearer(token(path=HOOK))}
    )
    expired = http.get(DISCOVERY, headers={**page_on(APP), **bearer(token(lifetime=-120))})
    assert (listed.status_code, called.status_code, expired.status_code) == (200, 200, 401)
    assert len(called.json()["cards"]) == 1
    for answer in (listed, called, expired):
        assert cors_headers(answer) == {"access-control-allow-origin": APP}
        assert varies_by_origin(answer)
    with AuditStore(db) as store:
        key = store.create_api_key(name="rest", role="service", org="default", created_by="t")
    elsewhere = [
        http.get("/api/packs", headers={**page_on(APP), **bearer(key["key"])}),
        http.get("/fhir/metadata", headers=page_on(APP)),
        http.get("/api/health", headers=page_on(APP)),
        http.options("/api/packs", headers=preflight(APP, "GET")),
    ]
    assert [answer.status_code for answer in elsewhere] == [200, 200, 200, 403]
    assert all(cors_headers(answer) == {} for answer in elsewhere)
    assert not any(varies_by_origin(answer) for answer in elsewhere)

    open_mode, _ = local
    hooked = open_mode.post(HOOK, json=order_sign(1), headers=page_on(APP))
    assert hooked.status_code == 200, hooked.text
    assert cors_headers(hooked) == {"access-control-allow-origin": APP}


@pytest.mark.parametrize("mode", ["sign-in", "open"])
def test_a_preflight_is_answered_before_the_guard_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    """`OPTIONS` with `Access-Control-Request-Method` from a registered origin, to discovery or a
    hook: `204` with no body, that origin, the two methods and two headers a CDS client sends,
    ten minutes, `Vary: Origin` and never `Access-Control-Allow-Credentials`, with the security
    headers every answer has. The guard never sees it: nothing is read, logged as refused,
    claimed or decided. An `OPTIONS` without the request method is not a preflight, and goes to
    the guard as before."""
    if mode == "sign-in":
        sign_in_on(monkeypatch)
    register(monkeypatch, entry(origins=[APP]))
    db = tmp_path / "a.db"
    http = TestClient(create_app(db_path=db), base_url=PUBLIC_URL)

    def unreachable(self: Auth, request: Any) -> None:
        raise AssertionError("the guard saw a preflight")

    capsys.readouterr()
    with monkeypatch.context() as patched:
        patched.setattr(Auth, "check", unreachable)
        for path in (DISCOVERY, HOOK):
            answer = http.options(path, headers=preflight(APP, "POST" if path == HOOK else "GET"))
            assert answer.status_code == 204, answer.text
            assert answer.content == b""
            assert cors_headers(answer) == {
                "access-control-allow-origin": APP,
                "access-control-allow-methods": ALLOW_METHODS,
                "access-control-allow-headers": ALLOW_HEADERS,
                "access-control-max-age": MAX_AGE,
            }
            assert varies_by_origin(answer)
            assert answer.headers["x-frame-options"] == "DENY"
            assert answer.headers["cross-origin-resource-policy"] == "same-origin"
    lines = security_lines(capsys.readouterr().out)
    assert [(line["event"], line["status"]) for line in lines] == [("request", 204)] * 2
    assert claims(db) == 0
    with AuditStore(db) as store:
        assert store.query(limit=5) == []
    plain = http.options(DISCOVERY, headers=page_on(APP))
    assert plain.status_code != 204
    assert cors_headers(plain) == {"access-control-allow-origin": APP}


def test_an_unregistered_origin_gets_no_cors_and_is_refused_as_before(
    signed: tuple[TestClient, str],
    local: tuple[TestClient, Path],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another site's preflight is the `403` it always was, with no CORS header. With sign-in on,
    `epic`'s valid token sent by a page on another site (or `Origin: null`) is a `403` before its
    `jti` is claimed, logged `cds_origin` with `cds:epic`; from this site's own origin it is
    answered, with no CORS header. A page on the registered origin that holds a Mendari cookie and
    no token is refused as another site's. In open mode another site's call is refused as before.
    With no origin registered, nothing on these paths carries CORS or `Vary`."""
    http, db = signed
    capsys.readouterr()
    for path in (DISCOVERY, HOOK):
        answer = http.options(path, headers=preflight(EVIL))
        assert answer.status_code == 403
        assert without_request_id(answer.json(), answer) == {
            "error": "AuthError",
            "detail": REFUSED,
        }
        assert cors_headers(answer) == {}
    for origin in (EVIL, "null"):
        sent = {**page_on(origin), **bearer(token(path=HOOK))}
        answer = http.post(HOOK, json=order_sign(1), headers=sent)
        assert answer.status_code == 403, answer.text
        assert without_request_id(answer.json(), answer) == {
            "error": "AuthError",
            "detail": CDS_ORIGIN,
        }
        assert cors_headers(answer) == {}
    lines = security_lines(capsys.readouterr().out)
    refused = [(e["event"], e["reason"], e.get("actor")) for e in lines if e["event"] != "request"]
    assert (
        refused
        == [("auth.origin.refused", "cross_site", None)] * 2
        + [("auth.origin.refused", "cds_origin", "cds:epic")] * 2
    )
    assert claims(db) == 0
    own = {"origin": PUBLIC_URL, "sec-fetch-site": "same-origin"}
    answer = http.post(HOOK, json=order_sign(1), headers={**own, **bearer(token(path=HOOK))})
    assert answer.status_code == 200 and cors_headers(answer) == {}
    with AuditStore(db) as store:
        cookie = signed_in(store, role="reviewer", host="mendari.test")["cookie"]
    with_cookie = http.post(HOOK, json=order_sign(1), headers={**page_on(APP), "cookie": cookie})
    assert with_cookie.status_code == 403
    assert without_request_id(with_cookie.json(), with_cookie)["detail"] == REFUSED

    open_mode, _ = local
    assert open_mode.post(HOOK, json=order_sign(1), headers=page_on(EVIL)).status_code == 403
    assert open_mode.options(HOOK, headers=preflight(EVIL)).status_code == 403

    register(monkeypatch, entry(), public_url=HERE)  # no origins
    plain = TestClient(create_app(db_path=tmp_path / "none.db"), base_url=HERE, client=LOOPBACK)
    for answer in (
        plain.get(DISCOVERY, headers=page_on(APP)),
        plain.get(DISCOVERY),
        plain.options(HOOK, headers=preflight(APP)),
    ):
        assert cors_headers(answer) == {} and not varies_by_origin(answer)


def test_an_origin_registered_for_another_client_is_refused(
    signed: tuple[TestClient, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """`other`'s token sent from `epic`'s page is a `403` (`cds_origin`, `cds:other`), claims
    nothing and decides nothing, though that origin is registered (so the page can read the
    refusal). Each client's own token from its own page is answered."""
    http, db = signed
    theirs = cds_token(OTHER_EHR, f"{EHR}/other", f"{PUBLIC_URL}{HOOK}")
    capsys.readouterr()
    refused = http.post(HOOK, json=order_sign(1), headers={**page_on(APP), **bearer(theirs)})
    assert refused.status_code == 403
    assert without_request_id(refused.json(), refused)["detail"] == CDS_ORIGIN
    assert cors_headers(refused) == {"access-control-allow-origin": APP}
    lines = security_lines(capsys.readouterr().out)
    assert [(e["reason"], e["actor"]) for e in lines if e["event"] != "request"] == [
        ("cds_origin", "cds:other")
    ]
    assert claims(db) == 0
    with AuditStore(db) as store:
        assert store.query(limit=5) == []
    again = cds_token(OTHER_EHR, f"{EHR}/other", f"{PUBLIC_URL}{HOOK}")
    for origin, sent in ((OTHER_APP, again), (APP, token(path=HOOK))):
        answer = http.post(HOOK, json=order_sign(1), headers={**page_on(origin), **bearer(sent)})
        assert answer.status_code == 200, answer.text
        assert cors_headers(answer) == {"access-control-allow-origin": origin}
    assert claims(db) == 2
