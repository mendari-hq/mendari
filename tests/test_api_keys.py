# SPDX-License-Identifier: Apache-2.0
"""API keys in the store (core/audit.py, ADR-0004): machine roles, scopes, expiry and the chain,
on SQLite and Postgres."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from mendari.core import audit as audit_module
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
        "replaced_by": None,  # set once the key is rotated (LP-09)
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
    # A row without an expiry (written past the store) is refused.
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
        "replaced_by",
        "preset",  # the integration path it was made for (LP-12i), if any
        "status",  # active, replaced, expired or revoked, worked out now (LP-12h)
    }
    assert (listed["scope"], listed["revoked_by"]) == ("system/Task.rs", "Grace")
    assert listed["status"] == "revoked"
    assert listed["preset"] is None
    assert listed["expires_at"] == made["expires_at"]
    assert made["key"] not in json.dumps(store.api_keys())
    assert len(store.api_keys()) == 2


def test_a_listing_says_each_keys_status(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worked out by the server as a key is refused: revoked, expired, replaced (rotated, in its
    overlap), else active (LP-12h)."""
    made = {
        name: store.create_api_key(name=name, role="viewer", org="clinic-a", created_by="Ada")
        for name in ("active", "rotated", "expired", "revoked")
    }
    new = store.rotate_api_key(made["rotated"]["id"], rotated_by="Ada", overlap_hours=2)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    set_column(store, made["expired"]["id"], "expires_at", past)
    store.revoke_api_key(made["revoked"]["id"], revoked_by="Ada")
    statuses = {k["id"]: k["status"] for k in store.api_keys("clinic-a")}
    assert statuses == {
        made["active"]["id"]: "active",
        made["rotated"]["id"]: "replaced",
        new["id"]: "active",
        made["expired"]["id"]: "expired",
        made["revoked"]["id"]: "revoked",
    }
    later = (datetime.now(UTC) + timedelta(hours=3)).isoformat(timespec="milliseconds")
    monkeypatch.setattr(audit_module, "_now", lambda: later)  # its overlap has passed
    assert {k["id"]: k["status"] for k in store.api_keys()}[made["rotated"]["id"]] == "expired"
    set_column(store, made["active"]["id"], "expires_at", None)  # refused: no expiry
    assert {k["id"]: k["status"] for k in store.api_keys()}[made["active"]["id"]] == "expired"


def test_a_keys_history_is_its_chain_in_order(store: AuditStore) -> None:
    old = store.create_api_key(
        name="ehr", role="service", org="clinic-a", created_by="Ada", preset="fhir",
        scopes="https://mendari.co/fhir/scope/decide",
    )  # fmt: skip
    new = store.rotate_api_key(old["id"], "clinic-a", rotated_by="Bo", overlap_hours=2)
    assert store.revoke_api_key(old["id"], "clinic-a", revoked_by="Grace") is True
    history = store.subject_history("key", old["id"], "clinic-a")
    assert history is not None
    assert (history["subject"], history["org"], history["truncated"]) == (
        f"key:{old['id']}",
        "clinic-a",
        False,
    )
    events = history["events"]
    assert [e["event"] for e in events] == ["key.created", "key.rotated", "key.revoked"]
    assert all(set(e) == {"event", "at", "payload"} for e in events)
    assert events[0]["payload"]["preset"] == "fhir"
    assert (events[1]["payload"]["replaced_by"], events[1]["payload"]["rotated_by"]) == (
        new["id"],
        "Bo",
    )
    assert events[1]["payload"]["overlap_hours"] == 2
    assert events[2]["payload"]["revoked_by"] == "Grace"
    successor = store.subject_history("key", new["id"], "clinic-a")
    assert successor is not None
    assert [e["event"] for e in successor["events"]] == ["key.created"]
    assert successor["events"][0]["payload"]["replaces"] == old["id"]
    shown = json.dumps([history, successor])
    for secret in (old["key"], new["key"]):
        assert secret not in shown and secret[4:] not in shown
        assert hashlib.sha256(secret.encode()).hexdigest() not in shown
    assert "ohk_" not in shown and '"seq"' not in shown and '"hash"' not in shown
    assert "prev_hash" not in shown
    assert store.verify()["ok"] is True


def test_history_is_read_through_its_rows_organization(store: AuditStore) -> None:
    made = store.create_api_key(name="k", role="viewer", org="clinic-a", created_by="Ada")
    assert store.subject_history("key", made["id"], "clinic-b") is None
    assert store.subject_history("key", made["id"], "") is None  # "" is an organization
    assert store.subject_history("key", made["id"], None) is not None  # open mode: every one
    assert store.subject_history("key", made["id"], "clinic-a") is not None
    assert store.subject_history("key", "nope", "clinic-a") is None
    assert store.subject_history("key", "nope", None) is None
    # An event whose payload names another organization than its row is never shown (fail
    # closed): a chain written wrongly, which LP-12g's verify reports.
    with store._db.session(exclusive=True) as db:
        store._append(db, f"key:{made['id']}", "key.revoked", {"id": made["id"], "org": "x"})
    shown = store.subject_history("key", made["id"], "clinic-a")
    assert shown is not None and [e["event"] for e in shown["events"]] == ["key.created"]


def test_history_keeps_the_newest_events(
    store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = store.create_api_key(name="k", role="viewer", org="clinic-a", created_by="Ada")
    store.rotate_api_key(made["id"], rotated_by="Ada", overlap_hours=2)
    store.revoke_api_key(made["id"], revoked_by="Ada")
    monkeypatch.setattr(audit_module, "HISTORY_LIMIT", 2)
    history = store.subject_history("key", made["id"], "clinic-a")
    assert history is not None and history["truncated"] is True
    assert [e["event"] for e in history["events"]] == ["key.rotated", "key.revoked"]
    monkeypatch.setattr(audit_module, "HISTORY_LIMIT", 3)
    whole = store.subject_history("key", made["id"], "clinic-a")
    assert whole is not None and whole["truncated"] is False and len(whole["events"]) == 3
