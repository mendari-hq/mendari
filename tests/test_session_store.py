# SPDX-License-Identifier: Apache-2.0
"""The session table (`smart_sessions`): pending sign-ins and launches, sessions, and one-time
claims. Every row is keyed by a digest of its kind and its value, so a value of one kind never
names a row of another, and every take or delete is scoped to its kind. Both backends (the
`db_target` fixture runs on SQLite and Postgres)."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core import audit
from mendari.core.audit import AuditStore
from mendari.server.app import create_app
from tests.conftest import encrypt_mode

IDP = "https://idp.example.org"
LATER = 600


@pytest.fixture
def signed_in(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A server with sign-in on (nothing here reaches the identity provider)."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    with TestClient(create_app(db_path=db_target), base_url="https://mendari.test") as client:
        yield client


def test_a_public_logout_deletes_only_its_own_kind_of_row(
    signed_in: TestClient, db_target: str
) -> None:
    """The critic's probe (Phase 4 research): `POST /auth/logout` and `/api/smart/logout` are
    public and used to delete whatever row the cookie's value named, of any kind. A crafted
    cookie could erase a pending sign-in (its `state`), an EHR launch's session, or a row whose
    value holds a newline (Starlette decodes `\\012` in a quoted cookie)."""
    store = AuditStore(db_target)
    later = int(time.time()) + LATER
    rows: dict[tuple[str, str], dict[str, Any]] = {
        ("state-abc", "oidc-pending"): {"next": "/"},
        ("smart-sid", "session"): {"patient": "p"},
        ("user-sid", "user"): {"name": "Nurse Kim", "roles": ["reviewer"], "org": "default"},
        ("client-1\njti-abc", "pending"): {"iss": "https://ehr.example.org/fhir"},
    }
    # A pending launch is sealed under its binding cookie, which the store must be given.
    binding = {"pending": "binding-cookie"}
    for (value, kind), data in rows.items():
        store.smart_put(value, kind, data, later, secret=binding.get(kind))
    crafted = ["state-abc", '"client-1\\012jti-abc"']
    logouts = {
        "/auth/logout": ("__Host-mendari_session", "smart-sid"),  # a person's: names a launch's row
        "/api/smart/logout": ("__Host-mendari_smart", "user-sid"),  # a launch's: names a person's
    }
    for path, (cookie, other) in logouts.items():
        for value in [*crafted, other]:
            signed_in.cookies.clear()
            done = signed_in.post(path, headers={"cookie": f"{cookie}={value}"})
            assert done.status_code == 204
    for (value, kind), data in rows.items():
        assert store.smart_get(value, kind, secret=binding.get(kind)) == data, (value, kind)
    # Each logout still ends its own kind of session.
    signed_in.cookies.clear()
    signed_in.post("/auth/logout", headers={"cookie": "__Host-mendari_session=user-sid"})
    signed_in.post("/api/smart/logout", headers={"cookie": "__Host-mendari_smart=smart-sid"})
    assert store.smart_get("user-sid", "user") is None
    assert store.smart_get("smart-sid", "session") is None
    assert store.smart_get("state-abc", "oidc-pending") == {"next": "/"}
    store.close()


def test_a_value_names_a_row_of_its_own_kind_only(store: AuditStore) -> None:
    later = int(time.time()) + LATER
    store.smart_put("same", "user", {"kind": "user"}, later)
    store.smart_put("same", "session", {"kind": "session"}, later)  # no collision
    assert store.smart_get("same", "user") == {"kind": "user"}
    assert store.smart_take("same", "pending", secret="binding") is None
    store.smart_delete("same", "oidc-pending")
    assert store.smart_take("same", "session") == {"kind": "session"}
    assert store.smart_get("same", "user") == {"kind": "user"}  # untouched by the take
    store.smart_delete("same", "user")
    assert store.smart_get("same", "user") is None


def test_rows_are_keyed_by_a_domain_separated_digest(store: AuditStore) -> None:
    store.smart_put("sid-1", "user", {}, int(time.time()) + LATER)
    with store._db.session(exclusive=False) as db:
        keys = [row["key_sha256"] for row in db.all("SELECT key_sha256 FROM smart_sessions")]
    assert keys == [hashlib.sha256(b"user\x00sid-1").hexdigest()]
    assert hashlib.sha256(b"sid-1").hexdigest() not in keys and "sid-1" not in keys


def test_rows_keyed_the_old_way_are_not_found_and_expire(store: AuditStore) -> None:
    """Rows from before the digest included the kind: sessions and pending sign-ins are
    short-lived, so after the deploy people sign in (and EHRs launch) again."""
    now = int(time.time())
    with store._db.session(exclusive=False) as db:
        db.all(
            "INSERT INTO smart_sessions (key_sha256, kind, expires_at, data) VALUES (?, ?, ?, ?)",
            (hashlib.sha256(b"old-sid").hexdigest(), "user", now + 1, "{}"),
        )
    assert store.smart_get("old-sid", "user") is None
    store.smart_put("new-sid", "user", {}, now + LATER)  # purges expired rows on the way
    with store._db.session(exclusive=False) as db:
        db.all("UPDATE smart_sessions SET expires_at = ? WHERE kind = ?", (now - 1, "user"))
    store.smart_put("newer-sid", "user", {}, now + LATER)
    with store._db.session(exclusive=False) as db:
        assert len(db.all("SELECT key_sha256 FROM smart_sessions")) == 1


@pytest.mark.parametrize("kind", ["", "User", "a\x00b", "x" * 41, "jti\n"])
def test_a_kind_is_a_short_lowercase_name(store: AuditStore, kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        store.smart_get("v", kind)


def test_a_claim_works_once_until_it_expires(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    assert store.smart_claim("jti", "client-1\njti-abc", now + 60) is True
    assert store.smart_claim("jti", "client-1\njti-abc", now + 60) is False  # a replay
    assert store.smart_claim("jti", "client-1\njti-abd", now + 60) is True
    assert store.smart_claim("other", "client-1\njti-abc", now + 60) is True  # its own kind
    store.smart_delete("client-1\njti-abc", "user")  # another kind: nothing to delete
    assert store.smart_claim("jti", "client-1\njti-abc", now + 60) is False
    assert store.smart_get("client-1\njti-abc", "jti") == {}
    with pytest.raises(ValueError, match="future"):
        store.smart_claim("jti", "late", now - 1)
    later = now + 61
    monkeypatch.setattr("mendari.core.audit.time.time", lambda: later)
    assert store.smart_claim("jti", "client-1\njti-abc", later + 60) is True  # reclaimed
    assert store.smart_claim("jti", "client-1\njti-abc", later + 60) is False


def test_a_claim_is_atomic_across_connections(store: AuditStore, db_target: str) -> None:
    """Serverless instances claim through their own connections: exactly one wins."""
    stores = [AuditStore(db_target) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    results: list[bool] = []
    expires = int(time.time()) + 60

    def claim(each: AuditStore) -> None:
        barrier.wait()
        results.append(each.smart_claim("jti", "client-1\nrace", expires))

    threads = [threading.Thread(target=claim, args=(each,)) for each in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for each in stores:
        each.close()
    assert sorted(results) == [False] * 5 + [True]


def test_a_take_works_once(store: AuditStore, db_target: str) -> None:
    store.smart_put("state-1", "pending", {"n": 1}, int(time.time()) + 60, secret="binding")
    other = AuditStore(db_target)
    assert other.smart_take("state-1", "pending", secret="binding") == {"n": 1}
    assert store.smart_take("state-1", "pending", secret="binding") is None
    other.close()


def test_session_rows_never_take_the_chain_lock(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads, puts, takes, claims and deletes are not hash-chain appends: an exclusive session
    would serialise every sign-in check behind the chain (the advisory lock on Postgres)."""
    opened: list[bool] = []
    session = store._db.session

    def spy(*, exclusive: bool) -> Any:
        opened.append(exclusive)
        return session(exclusive=exclusive)

    monkeypatch.setattr(store._db, "session", spy)
    later = int(time.time()) + 60
    store.smart_put("v", "user", {}, later)
    store.smart_get("v", "user")
    store.smart_take("v", "user")
    store.smart_claim("jti", "v", later)
    store.smart_delete("v", "jti")
    assert opened and not any(opened)


def test_session_rows_are_served_while_the_chain_is_locked(store: AuditStore) -> None:
    """Postgres: another instance holds the chain's advisory lock (a long append), and sign-in
    checks still answer."""
    if store.backend != "postgres":
        pytest.skip("the advisory lock is Postgres's")
    import psycopg

    from tests.conftest import POSTGRES_URL

    with psycopg.connect(POSTGRES_URL, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (audit._CHAIN_LOCK,))
        done = threading.Event()

        def use() -> None:
            later = int(time.time()) + 60
            store.smart_put("v", "user", {}, later)
            assert store.smart_get("v", "user") == {}
            assert store.smart_claim("jti", "v", later)
            assert store.smart_take("v", "user") == {}
            store.smart_delete("v", "jti")
            done.set()

        worker = threading.Thread(target=use, daemon=True)
        worker.start()
        worker.join(timeout=10)
        holder.execute("SELECT pg_advisory_unlock(%s)", (audit._CHAIN_LOCK,))
    assert done.is_set()
