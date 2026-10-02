"""API keys in the store (core/audit.py, ADR-0004): machine roles, scopes, expiry, legacy keys
and the chain, on SQLite and Postgres."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from mendari.core.audit import KEY_MAX_DAYS, AuditStore


def chained(store: AuditStore, key_id: str) -> list[dict[str, object]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all(
            "SELECT event, payload FROM audit_events WHERE decision_id = ? ORDER BY seq",
            (f"key:{key_id}",),
        )
    return [{"event": row["event"], **json.loads(row["payload"])} for row in rows]


def days_from_now(stamp: str) -> float:
    return (datetime.fromisoformat(stamp) - datetime.now(UTC)) / timedelta(days=1)


def set_column(store: AuditStore, key_id: str, column: str, value: str | None) -> None:
    with store._db.session(exclusive=True) as db:
        db.all(f"UPDATE api_keys SET {column} = ? WHERE id = ?", (value, key_id))


def test_a_key_is_for_a_machine_role_and_lives_one_to_365_days(store: AuditStore) -> None:
    for role in ("reviewer", "governance", "admin", "root"):
        with pytest.raises(ValueError, match="role"):
            store.create_api_key(name="x", role=role, org="default", created_by="p")
    for days in (0, -1, KEY_MAX_DAYS + 1):
        with pytest.raises(ValueError, match="days"):
            store.create_api_key(name="x", role="service", org="default", created_by="p", days=days)
    assert store.api_keys() == []
    made = store.create_api_key(name="ehr", role="service", org="default", created_by="p")
    assert 89.9 < days_from_now(made["expires_at"]) <= 90
    assert made["scope"] is None and made["key"].startswith("ohk_")
    year = store.create_api_key(
        name="y", role="viewer", org="default", created_by="p", days=365, scopes="system/*.rs"
    )
    assert 364.9 < days_from_now(year["expires_at"]) <= 365
    assert store.api_key(year["key"]) == {
        "id": year["id"],
        "name": "y",
        "role": "viewer",
        "org": "default",
        "scope": "system/*.rs",
        "expires_at": year["expires_at"],
    }


def test_creating_and_revoking_are_chained_without_the_key(store: AuditStore) -> None:
    made = store.create_api_key(
        name="ehr", role="service", org="clinic-a", created_by="Ada", scopes="system/Task.r"
    )
    assert store.revoke_api_key(made["id"], "clinic-a", revoked_by="Grace") is True
    events = chained(store, made["id"])
    assert [e["event"] for e in events] == ["key.created", "key.revoked"]
    created, revoked = events
    assert {k: created[k] for k in ("id", "name", "role", "org", "scope", "created_by")} == {
        "id": made["id"],
        "name": "ehr",
        "role": "service",
        "org": "clinic-a",
        "scope": "system/Task.r",
        "created_by": "Ada",
    }
    assert created["expires_at"] == made["expires_at"]
    assert (revoked["id"], revoked["revoked_by"]) == (made["id"], "Grace")
    digest = hashlib.sha256(made["key"].encode()).hexdigest()
    with store._db.session(exclusive=False) as db:
        chain = json.dumps(db.all("SELECT * FROM audit_events"))
    assert made["key"] not in chain and made["key"][4:] not in chain and digest not in chain
    assert store.verify()["ok"] is True


def test_a_revoked_or_expired_key_is_refused(store: AuditStore) -> None:
    revoked = store.create_api_key(name="r", role="viewer", org="default", created_by="p")
    expired = store.create_api_key(name="e", role="viewer", org="default", created_by="p")
    current = store.create_api_key(name="c", role="viewer", org="default", created_by="p")
    store.revoke_api_key(revoked["id"], revoked_by="p")
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    set_column(store, expired["id"], "expires_at", past)
    assert store.api_key(revoked["key"]) is None
    assert store.api_key(expired["key"]) is None
    assert store.api_key(current["key"]) is not None
    assert store.api_key("ohk_unknown") is None
    # A row without an expiry (written by an older instance during a rollout) is refused until
    # the next start gives it one.
    set_column(store, current["id"], "expires_at", None)
    assert store.api_key(current["key"]) is None


def test_revoking_is_once_and_within_the_organization(store: AuditStore) -> None:
    made = store.create_api_key(name="k", role="viewer", org="clinic-a", created_by="p")
    assert store.revoke_api_key(made["id"], "clinic-b", revoked_by="Mallory") is False
    assert store.api_key(made["key"]) is not None
    assert store.revoke_api_key(made["id"], "clinic-a", revoked_by="Ada") is True
    assert store.revoke_api_key(made["id"], "clinic-a", revoked_by="Ada") is False
    assert store.revoke_api_key("nope", revoked_by="Ada") is False
    assert [e["event"] for e in chained(store, made["id"])] == ["key.created", "key.revoked"]


def test_a_listing_shows_scope_and_expiry_never_the_key(store: AuditStore) -> None:
    made = store.create_api_key(
        name="k", role="service", org="clinic-a", created_by="Ada", scopes="system/Task.rs"
    )
    store.create_api_key(name="other", role="viewer", org="clinic-b", created_by="Bo")
    store.revoke_api_key(made["id"], revoked_by="Grace")
    [listed] = store.api_keys("clinic-a")
    assert set(listed) == {
        "id",
        "name",
        "role",
        "org",
        "scope",
        "created_by",
        "created_at",
        "expires_at",
        "revoked_at",
        "revoked_by",
    }
    assert (listed["scope"], listed["revoked_by"]) == ("system/Task.rs", "Grace")
    assert listed["expires_at"] == made["expires_at"]
    assert made["key"] not in json.dumps(store.api_keys())
    assert len(store.api_keys()) == 2


def test_keys_from_before_expiry_get_30_days_once(db_target: str) -> None:
    """A database from before Phase 4: api_keys without the new columns, and a key without an
    expiry. The first start adds the columns and gives the key 30 days; later starts leave it."""
    with AuditStore(db_target) as store, store._db.session(exclusive=True) as db:
        for column in ("scopes", "expires_at", "revoked_by"):
            db.all(f"ALTER TABLE api_keys DROP COLUMN {column}")
        db.all(
            "INSERT INTO api_keys (id, key_sha256, name, role, org, created_by, created_at) "
            "VALUES ('old1', ?, 'old admin', 'admin', 'default', 'p', '2026-01-01')",
            (hashlib.sha256(b"ohk_old").hexdigest(),),
        )
    with AuditStore(db_target) as store:
        first = store.api_key("ohk_old")
        assert first is not None and first["role"] == "admin" and first["scope"] is None
        assert 29.9 < days_from_now(first["expires_at"]) <= 30
    with AuditStore(db_target) as store:
        again = store.api_key("ohk_old")
        assert again is not None and again["expires_at"] == first["expires_at"]  # idempotent
        new = store.create_api_key(name="n", role="viewer", org="default", created_by="p")
        assert 89.9 < days_from_now(new["expires_at"]) <= 90
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
        set_column(store, "old1", "expires_at", past)
        assert store.api_key("ohk_old") is None
