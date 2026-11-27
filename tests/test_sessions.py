# SPDX-License-Identifier: Apache-2.0
"""Browser sessions (server/sessions.py): idle and absolute expiry held in the row's
`expires_at`, roles worked out on every request, `__Host-` cookie names (the old plain names
never read on a host that uses them), and sign-ins and EHR launches tied to the browser that
started them. The store's side runs on SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import sessions
from mendari.server.app import create_app
from mendari.server.auth import safe_next
from tests.conftest import DASHBOARD, encrypt_mode, signed_in
from tests.test_auth import IDP, FakeIdp, sign_in, signing_in_with

HTTPS = "https://mendari.test"
MINUTE = 60
HOUR = 3600


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Move every clock that reads `time.time()` (the store's, the session rules') forward."""
    start = time.time()
    offset = [0]
    monkeypatch.setattr(time, "time", lambda: start + offset[0])

    def advance(seconds: int) -> None:
        offset[0] += seconds

    return advance


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def server(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Sign-in on (no identity provider is reached), on a host that uses `__Host-` names, called
    as the dashboard's browser calls it (Mendari's own request header)."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    with TestClient(create_app(db_path=db_target), base_url=HTTPS, headers=DASHBOARD) as client:
        yield client


def expiry(db: str, sid: str, kind: str = "user") -> int:
    with AuditStore(db) as store:
        row = store.smart_read(sid, kind)
    assert row is not None
    return row[1]


def sid_of(headers: dict[str, str]) -> str:
    return headers["cookie"].partition("=")[2]


# -- idle and absolute expiry -------------------------------------------------------------------


def test_a_session_ends_after_the_idle_time(
    server: TestClient, db_target: str, clock: Callable[[int], None]
) -> None:
    with AuditStore(db_target) as store:
        person = signed_in(store, role="viewer")
    assert server.get("/api/audit", headers=person).status_code == 200
    clock(29 * MINUTE)
    assert server.get("/api/audit", headers=person).status_code == 200  # pushed forward
    clock(30 * MINUTE + 1)
    assert server.get("/api/audit", headers=person).status_code == 401


def test_activity_extends_it_up_to_the_absolute_limit(
    server: TestClient, db_target: str, clock: Callable[[int], None]
) -> None:
    with AuditStore(db_target) as store:
        person = signed_in(store, role="viewer")
    for _ in range(8 * 6 - 1):  # a request every ten minutes, for just under eight hours
        assert server.get("/api/audit", headers=person).status_code == 200
        clock(10 * MINUTE)
    assert expiry(db_target, sid_of(person)) <= time.time() + 10 * MINUTE  # never past 8 h
    clock(10 * MINUTE)
    assert server.get("/api/audit", headers=person).status_code == 401  # busy, and still ended


def test_a_touch_writes_at_most_once_per_interval(
    server: TestClient,
    db_target: str,
    clock: Callable[[int], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[int] = []
    touch = AuditStore.smart_touch

    def spy(self: AuditStore, key: str, kind: str, until: int) -> bool:
        touched.append(until)
        return touch(self, key, kind, until)

    monkeypatch.setattr(AuditStore, "smart_touch", spy)
    with AuditStore(db_target) as store:
        person = signed_in(store, role="viewer")
    for _ in range(5):  # every minute for four minutes: nothing to write
        assert server.get("/api/audit", headers=person).status_code == 200
        clock(MINUTE)
    assert touched == []
    assert server.get("/api/audit", headers=person).status_code == 200  # five minutes in
    assert len(touched) == 1 and expiry(db_target, sid_of(person)) == touched[0]
    assert server.get("/api/audit", headers=person).status_code == 200
    assert len(touched) == 1


def test_concurrent_touches_never_shorten_a_session(store: AuditStore, db_target: str) -> None:
    """Instances touch through their own connections, in any order: the latest expiry stays, and
    an ended row is never brought back."""
    now = int(time.time())
    store.smart_put("sid", "user", {}, now + 100)
    assert store.smart_touch("sid", "user", now + 900) is True
    assert store.smart_touch("sid", "user", now + 500) is False  # a slower instance's, earlier
    others = [AuditStore(db_target) for _ in range(4)]
    for offset, other in zip((600, 1200, 700, 1100), others, strict=True):
        other.smart_touch("sid", "user", now + offset)
        other.close()
    assert store.smart_read("sid", "user") == ({}, now + 1200)
    store.smart_put("ended", "user", {}, now - 1)
    assert store.smart_touch("ended", "user", now + 900) is False
    assert store.smart_read("ended", "user") is None
    assert store.smart_touch("sid", "session", now + 9000) is False  # its own kind only


def test_the_old_cookie_name_is_ignored(server: TestClient, db_target: str) -> None:
    """On a host that uses `__Host-` names, the plain `mendari_session` and `mendari_smart` (read
    for one release, for rows from before Phase 4b) are never read: a row they name, old or new,
    is not reached, and is left as it was."""
    now = int(time.time())
    old = {"iss": IDP, "sub": "u1", "name": "Nurse Kim", "roles": ["reviewer"], "org": "default"}
    with AuditStore(db_target) as store:
        store.smart_put("old-sid", "user", old, now + 7 * HOUR)
        person = signed_in(store, role="viewer")
    for sid in ("old-sid", sid_of(person)):
        plain = {"cookie": f"mendari_session={sid}"}
        assert server.get("/api/audit", headers=plain).status_code == 401
        assert server.get("/api/auth/me", headers=plain).json()["signed_in"] is False
    for base in (sessions.SESSION, sessions.SMART):
        for host, read in (("mendari.test", None), ("127.0.0.1", "sid")):
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/",
                "server": (host, 443),
                "headers": [(b"host", host.encode()), (b"cookie", f"{base}=sid".encode())],
            }
            assert sessions.read(Request(scope), base) == read, (base, host)
    assert expiry(db_target, "old-sid") == now + 7 * HOUR  # untouched: never resumed
    assert server.get("/api/audit", headers=person).status_code == 200


def test_a_row_without_an_absolute_limit_has_ended(server: TestClient, db_target: str) -> None:
    """Every session row keeps its absolute limit (`sessions.begin`): one without it, written
    past the store, is a session that has ended. It is left as it is, to expire."""
    now = int(time.time())
    row: dict[str, Any] = {"iss": IDP, "sub": "u1", "name": "Nurse Kim", "org": "default"}
    row["claims"] = {"roles": ["reviewer"]}
    with AuditStore(db_target) as store:
        store.smart_put("no-limit", "user", row, now + 7 * HOUR)
    cookie = {"cookie": "__Host-mendari_session=no-limit"}
    assert server.get("/api/auth/me", headers=cookie).json()["signed_in"] is False
    assert server.get("/api/audit", headers=cookie).status_code == 401
    assert expiry(db_target, "no-limit") == now + 7 * HOUR  # not moved, never resumed


@pytest.mark.parametrize("field", ["iss", "claims", "actor"])
def test_a_session_without_what_sign_in_keeps_is_not_read_another_way(
    db_target: str, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """A person's session keeps their issuer, the claims their roles come from and the name the
    chain gives them, as `/auth/callback` writes them. A row without one, written past the
    store, is not read as of the issuer configured now, as a person with no role or under a
    name made up here: the server answers its error, and it signs nobody in."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    claims = {"roles": ["reviewer"], "email": "kim@example.org", "email_verified": True}
    row: dict[str, Any] = {"iss": IDP, "sub": "u1", "name": "Nurse Kim", "actor": "kim@example.org"}
    row |= {"claims": claims, "org": "default"}
    del row[field]
    session, expires_at = sessions.begin(row, int(time.time()), sessions.Lifetime())
    with AuditStore(db_target) as store:
        store.smart_put("partial", "user", session, expires_at)
    cookie = {"cookie": "__Host-mendari_session=partial", **DASHBOARD}
    app = create_app(db_path=db_target)
    with TestClient(app, base_url=HTTPS, raise_server_exceptions=False) as client:
        answer = client.get("/api/audit", headers=cookie)
    assert (answer.status_code, answer.json()["error"]) == (500, "InternalError")


def test_the_limits_are_settings_bounded_at_startup(
    tmp_path: Path,
    idp: FakeIdp,
    monkeypatch: pytest.MonkeyPatch,
    clock: Callable[[int], None],
) -> None:
    for name, value in [
        ("MENDARI_SESSION_IDLE_MINUTES", "4"),
        ("MENDARI_SESSION_IDLE_MINUTES", "121"),
        ("MENDARI_SESSION_IDLE_MINUTES", "thirty"),
        ("MENDARI_SESSION_MAX_HOURS", "0"),
        ("MENDARI_SESSION_MAX_HOURS", "13"),
    ]:
        with monkeypatch.context() as each:
            each.setenv(name, value)
            with pytest.raises(MendariError, match=name):
                create_app(db_path=tmp_path / "bad.db")
    monkeypatch.setenv("MENDARI_SESSION_IDLE_MINUTES", "15")
    monkeypatch.setenv("MENDARI_SESSION_MAX_HOURS", "1")
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as busy:
        idle = TestClient(busy.app, base_url=HTTPS, headers=DASHBOARD)
        sign_in(busy, idp, roles=["clinicians"])
        sign_in(idle, idp, roles=["clinicians"])
        assert busy.get("/api/health").json()["sessions"] == {"idle_minutes": 15, "max_hours": 1}
        clock(10 * MINUTE)
        assert busy.get("/api/audit").status_code == 200
        clock(6 * MINUTE)
        assert idle.get("/api/audit").status_code == 401  # 16 minutes without a request
        for _ in range(5):  # busy every ten minutes or so
            assert busy.get("/api/audit").status_code == 200
            clock(10 * MINUTE)
        assert busy.get("/api/audit").status_code == 401  # an hour at most


def test_the_rule_ends_a_session_early_never_late() -> None:
    life = sessions.Lifetime()
    expires, absolute = life.start(1000)
    assert (expires, absolute) == (1000 + 30 * MINUTE, 1000 + 8 * HOUR)
    assert life.extended(1000 + 4 * MINUTE, expires, absolute) is None  # under 5 minutes' move
    assert life.extended(1000 + 5 * MINUTE, expires, absolute) == 1000 + 35 * MINUTE
    assert life.extended(1000 + 8 * HOUR - MINUTE, expires, absolute) == absolute
    assert life.extended(1000, expires + 1, absolute) is None  # never back
    assert life.start(1000, ends_by=1000 + HOUR) == (1000 + 30 * MINUTE, 1000 + HOUR)


# -- roles on every request ---------------------------------------------------------------------


def test_a_role_removed_from_the_map_ends_access_at_once(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles come from the claims the session kept and the settings as they are now: a redeploy
    with a narrower role map takes effect on the next request, not at the session's end."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as before:
        assert sign_in(before, idp, roles=["clinicians"]).status_code == 302
        cookies = dict(before.cookies)
        assert before.get("/api/auth/me").json()["principal"]["roles"] == ["reviewer"]
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=viewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as after:
        after.cookies.update(cookies)
        assert after.get("/api/auth/me").json()["principal"]["roles"] == ["viewer"]
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "it=admin")  # clinicians: no role at all
    with signing_in_with(idp, tmp_path, monkeypatch) as later:
        later.cookies.update(cookies)
        assert later.get("/api/audit").status_code == 401
        assert later.get("/api/auth/me").json() == {"mode": "sign-in", "signed_in": False}


def test_a_role_from_an_email_is_checked_again_against_the_map(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ROLE_CLAIM", "email")
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "owner@example.org=admin")
    with signing_in_with(idp, tmp_path, monkeypatch) as before:
        sign_in(before, idp, email="owner@example.org", email_verified=True)
        cookies = dict(before.cookies)
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "owner@example.org=viewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as after:
        after.cookies.update(cookies)
        assert after.get("/api/auth/me").json()["principal"]["roles"] == ["viewer"]


# -- cookie names -------------------------------------------------------------------------------


def test_a_plain_name_cookie_reaches_no_row(server: TestClient, db_target: str) -> None:
    """A sibling subdomain can set a plain-named cookie for this host, never a `__Host-` one: a
    row is reached only through the `__Host-` name."""
    with AuditStore(db_target) as store:
        person = signed_in(store, role="viewer")
    sid = sid_of(person)
    assert person["cookie"].startswith("__Host-mendari_session=")
    assert server.get("/api/audit", headers=person).status_code == 200
    plain = {"cookie": f"mendari_session={sid}"}
    assert server.get("/api/audit", headers=plain).status_code == 401
    with AuditStore(db_target) as store:
        assert store.smart_read(sid, "user") is not None  # refused, not ended


def test_local_and_dotless_hosts_keep_the_plain_names(
    db_target: str, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ZAP, pilot-check and the sandbox run on 127.0.0.1 or a compose service's name, over plain
    http, where browsers may refuse the `__Host-` prefix."""
    assert [
        sessions.cookie_name("mendari_session", h) for h in ("mendari.test", "app.mendari.co")
    ] == ["__Host-mendari_session"] * 2
    for host in ("localhost", "127.0.0.1", "::1", "engine", "app.localhost", None):
        assert sessions.cookie_name("mendari_session", host) == "mendari_session", host
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        local = TestClient(app.app, base_url="http://127.0.0.1:8000")
        started = local.get("/auth/login", follow_redirects=False)
        assert started.headers["set-cookie"].startswith("mendari_signin=")
        with AuditStore(tmp_path / "a.db") as store:
            person = signed_in(store, role="viewer", host="127.0.0.1")
        assert person["cookie"].startswith("mendari_session=")
        assert local.get("/api/audit", headers=person).status_code == 200


def test_logging_out_clears_the_cookie_under_its_name(server: TestClient, db_target: str) -> None:
    with AuditStore(db_target) as store:
        person = signed_in(store, role="viewer")
    done = server.post("/auth/logout", headers=person)
    cleared = [c for c in done.headers.get_list("set-cookie") if "max-age=0" in c.lower()]
    assert [c.split("=")[0] for c in cleared] == ["__Host-mendari_session"]
    assert server.get("/api/audit", headers=person).status_code == 401


# -- sign-ins tied to the browser ----------------------------------------------------------------


def started_sign_in(app: TestClient, idp: FakeIdp, **claims: Any) -> tuple[str, str]:
    started = app.get("/auth/login", follow_redirects=False)
    code = idp.authorize(started.headers["location"], **claims)
    return code, parse_qs(urlsplit(started.headers["location"]).query)["state"][0]


def test_a_callback_from_another_browser_is_refused(
    tmp_path: Path,
    idp: FakeIdp,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Login CSRF: someone signs in at the identity provider as themselves, stops before the
    callback, and sends its URL to a victim, who would then work in the sender's session."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as attacker:
        code, state = started_sign_in(attacker, idp, roles=["clinicians"])
        victim = TestClient(attacker.app, base_url=HTTPS)
        capsys.readouterr()
        landed = victim.get("/auth/callback", params={"code": code, "state": state})
        assert landed.status_code == 400 and "another browser or tab" in landed.text
        assert '"reason":"browser"' in capsys.readouterr().out
        assert not [c for c in victim.cookies if "mendari_session" in c]
        # The state was used up: the sender cannot finish it either.
        again = attacker.get("/auth/callback", params={"code": code, "state": state})
        assert again.status_code == 400 and "unknown, used or expired" in again.text


def test_the_binding_cookie_is_short_lived_and_cleared_after_use(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        started = app.get("/auth/login", follow_redirects=False)
        binding = started.headers["set-cookie"]
        assert binding.startswith("__Host-mendari_signin=")
        assert "max-age=600" in binding.lower() and "httponly" in binding.lower()
        code = idp.authorize(started.headers["location"], roles=["clinicians"])
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        done = app.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert done.status_code == 302
        cleared = [c for c in done.headers.get_list("set-cookie") if "mendari_signin" in c]
        assert cleared and all("max-age=0" in c.lower() for c in cleared)


def pending_rows(db: Path, kind: str) -> int:
    with closing(sqlite3.connect(db)) as connection:
        query = "SELECT COUNT(*) FROM smart_sessions WHERE kind = ?"
        return int(connection.execute(query, (kind,)).fetchone()[0])


def test_a_sign_in_started_on_another_name_moves_to_the_public_url_first(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding cookie is host-only and the callback comes back to MENDARI_PUBLIC_URL, so a
    sign-in started on another name (a `*.vercel.app` alias, a second DNS name) begins again
    there, before anything is bound or written; then it completes."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        browser = TestClient(app.app, base_url="https://mendari-abc.vercel.app")
        moved = browser.get(
            "/auth/login", params={"next": "https://evil.example/"}, follow_redirects=False
        )
        assert moved.status_code == 302 and "set-cookie" not in moved.headers
        assert moved.headers["location"] == f"{HTTPS}/auth/login?next=%2F&moved=1"  # safe_next
        assert pending_rows(tmp_path / "a.db", "oidc-pending") == 0
        started = browser.get(moved.headers["location"], follow_redirects=False)
        assert started.headers["location"].startswith(f"{IDP}/authorize?")
        code = idp.authorize(started.headers["location"], roles=["clinicians"])
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        done = browser.get(
            f"{HTTPS}/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert done.status_code == 302 and done.headers["location"] == "/"
        assert browser.get(f"{HTTPS}/api/auth/me").json()["signed_in"] is True
        # The public URL's origin however it is written stays; another port, scheme or a
        # trailing dot (another host to a browser) moves.
        for base, moves in [
            ("https://MENDARI.test:443", False),
            ("https://mendari.test.", True),
            ("https://mendari.test:8443", True),
            ("http://mendari.test", True),
        ]:
            here = TestClient(app.app, base_url=base).get("/auth/login", follow_redirects=False)
            assert here.headers["location"].startswith(f"{HTTPS}/auth/login?") is moves, base


def test_a_proxy_that_rewrites_the_host_gets_one_move_never_a_loop(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a proxy that rewrites `Host` (to `engine:8000`, say), every request looks like
    another name: the move happens once, and the sign-in then starts where it arrived, as
    before (the browser keeps the cookie for the name it used)."""
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        behind = TestClient(app.app, base_url="http://engine:8000")
        moved = behind.get("/auth/login", follow_redirects=False)
        assert moved.headers["location"] == f"{HTTPS}/auth/login?next=%2F&moved=1"
        query = urlsplit(moved.headers["location"]).query
        started = behind.get(f"/auth/login?{query}", follow_redirects=False)
        assert started.headers["location"].startswith(f"{IDP}/authorize?")
        assert started.headers["set-cookie"].startswith("mendari_signin=")
        assert pending_rows(tmp_path / "a.db", "oidc-pending") == 1


def test_a_pending_sign_in_without_a_digest_is_refused(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending row that keeps no browser digest (as the release before Phase 4b made them) is
    refused, from any browser, and is used up: it never signs anyone in."""
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        code, state = started_sign_in(app, idp, roles=["clinicians"])
        with AuditStore(tmp_path / "a.db") as store:
            pending = store.smart_take(state, "oidc-pending")
            assert pending is not None and "browser" in pending
            del pending["browser"]
            store.smart_put(state, "oidc-pending", pending, int(time.time()) + 600)
        # Even the browser that started it (its binding cookie still held) is refused.
        assert any(name.endswith("mendari_signin") for name in app.cookies)
        done = app.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert done.status_code == 400 and "another browser or tab" in done.text
        assert not [c for c in app.cookies if "mendari_session" in c]
        again = app.get("/auth/callback", params={"code": code, "state": state})
        assert again.status_code == 400 and "unknown, used or expired" in again.text


@pytest.mark.parametrize(
    ("asked", "lands"),
    [
        ("/\\evil.example/steal", "/"),  # a browser reads `/\` as `//`
        ("/\\/evil.example", "/"),
        ("/\tevil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("//evil.example", "/"),
        ("https://evil.example", "/"),
        ("/?agent=mendari-test/triage#review", "/?agent=mendari-test/triage#review"),
    ],
)
def test_the_return_address_with_a_backslash_or_a_tab_stays_on_this_site(
    tmp_path: Path, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch, asked: str, lands: str
) -> None:
    assert safe_next(asked) == lands
    monkeypatch.setenv("MENDARI_OIDC_ROLE_MAP", "clinicians=reviewer")
    with signing_in_with(idp, tmp_path, monkeypatch) as app:
        started = app.get("/auth/login", params={"next": asked}, follow_redirects=False)
        code = idp.authorize(started.headers["location"], roles=["clinicians"])
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        done = app.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        location = done.headers["location"]
        assert location == lands
        assert urlsplit(location).netloc == "" and not location.startswith("//")
