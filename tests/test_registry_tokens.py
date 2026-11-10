# SPDX-License-Identifier: Apache-2.0
"""Owned publisher tokens (LP-19b, `hub/registry.py` `mint_token`, `publisher`, `revoke_token`):
a token belongs to a person and an organization, ends, and can be revoked; each change is
chained; the last day it was used is kept outside the chain. On SQLite and Postgres."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from mendari.cli import main
from mendari.core import clock
from mendari.errors import HubError
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore, token_id
from tests.conftest import REGISTRY_ADMIN, Registry

ADMIN = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
OWNER = "user:3f9a"  # a person as a chain names one: an id, never an address


def _mint(db_target: str, namespace: str = "mendari", **more: Any) -> dict[str, Any]:
    given = {"org": "acme", "owner": OWNER, "name": "ci", "days": 30, "by": OWNER, **more}
    with RegistryStore(db_target) as store:
        return store.mint_token(namespace, **given)


def _tokens(db_target: str, **which: Any) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store:
        return store.tokens(**which)


def _events(db_target: str) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_events ORDER BY seq")


def _verify(db_target: str) -> dict[str, Any]:
    with RegistryStore(db_target) as store:
        return store.verify()


def _write(db_target: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        db.all(sql, params)


def _register_key(registry: Registry, token: str, namespace: str = "mendari") -> int:
    """A publisher's write that can be repeated: registering a key for the namespace."""
    answer = registry.client.post(
        f"/v1/namespaces/{namespace}/keys",
        json={"public_pem": archive.generate_key()[1].decode()},
        headers={"authorization": f"Bearer {token}"},
    )
    return int(answer.status_code)


def test_a_token_names_its_owner_organization_and_expiry_and_never_its_secret(
    registry: Registry, db_target: str
) -> None:
    minted = _mint(db_target)
    token = minted.pop("token")
    assert token.startswith("ohr_") and token != registry.token
    created = datetime.fromisoformat(minted["created_at"])
    assert minted == {
        "id": token_id(token),
        "namespace": "mendari",
        "org": "acme",
        "owner": OWNER,
        "name": "ci",
        "created_at": minted["created_at"],
        "expires_at": (created + timedelta(days=30)).isoformat(timespec="seconds"),
        "revoked_at": None,
        "revoked_by": None,
        "last_used_day": None,
    }
    assert _tokens(db_target) == [minted]  # the listing: no secret, no hash
    with RegistryStore(db_target) as store:
        assert store.publisher(token) == {
            "id": token_id(token),
            "namespace": "mendari",
            "org": "acme",
            "owner": OWNER,
        }
        with store._db.session(exclusive=False) as db:
            stored = db.all("SELECT * FROM hub_publisher_tokens")
    events = _events(db_target)
    issued = json.loads(events[-1]["payload"])
    assert events[-1]["event"] == "token.issued"
    assert issued.pop("row")["kind"] == "publisher_tokens"
    assert issued == {
        "namespace": "mendari",
        "token_id": token_id(token),
        "org": "acme",
        "owner": OWNER,
        "expires_at": minted["expires_at"],
        "by": OWNER,
    }
    assert token not in json.dumps([stored, events, _tokens(db_target)])
    assert registry.publish(token=token).status_code == 201  # and it publishes
    assert _verify(db_target)["ok"]


def test_an_expired_token_is_refused(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _mint(db_target, days=1)["token"]
    assert _register_key(registry, token) == 201
    real = registry_module._now

    def later(hours: int) -> None:
        moved = datetime.fromisoformat(real()) + timedelta(hours=hours)
        monkeypatch.setattr(registry_module, "_now", lambda: moved.isoformat(timespec="seconds"))

    later(23)  # the clock moved, not a sleep
    assert _register_key(registry, token) == 201
    later(25)
    assert _register_key(registry, token) == 403
    assert registry.publish(token=token).status_code == 403
    with RegistryStore(db_target) as store:
        assert store.publisher(token) is None
    assert _register_key(registry, registry.token) == 201  # others are not touched


def test_a_revoked_token_is_refused_at_once_and_revoking_is_chained(
    registry: Registry, db_target: str
) -> None:
    minted = _mint(db_target)
    token = minted["token"]
    assert _register_key(registry, token) == 201
    with RegistryStore(db_target) as store:
        revoked = store.revoke_token(minted["id"], org="acme", by="user:77aa")
    assert revoked["revoked_at"] is not None and revoked["revoked_by"] == "user:77aa"
    assert _register_key(registry, token) == 403  # the running registry, at its next request
    assert registry.publish(token=token).status_code == 403
    events = _events(db_target)
    last = json.loads(events[-1]["payload"])
    assert events[-1]["event"] == "token.revoked"
    assert last.pop("row") == {
        "kind": "publisher_tokens",
        "key": minted["id"],
        "sha256": last_row_sha(db_target, minted["id"]),
    }
    assert last == {
        "namespace": "mendari",
        "token_id": minted["id"],
        "org": "acme",
        "by": "user:77aa",
    }
    assert _verify(db_target)["ok"]
    with RegistryStore(db_target) as store:  # again: nothing changes, nothing is chained
        assert store.revoke_token(minted["id"], org="acme", by="user:other") == revoked
    assert len(_events(db_target)) == len(events)
    # Un-revoked past the API: the row is no longer the one its newest event hashed.
    _write(db_target, "UPDATE hub_publisher_tokens SET revoked_at = NULL, revoked_by = NULL")
    problem = "1 of the publisher_tokens changed since their chained event"
    assert _verify(db_target)["problem"] == problem


def last_row_sha(db_target: str, id: str) -> str:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        row = db.one("SELECT * FROM hub_publisher_tokens WHERE id = ?", (id,))
    assert row is not None
    return registry_module._row_sha(row)


def test_a_token_publishes_only_to_its_namespace(registry: Registry, db_target: str) -> None:
    made = registry.client.post("/v1/namespaces", json={"name": "other"}, headers=ADMIN)
    assert made.status_code == 201, made.text
    minted = _mint(db_target, "other")
    assert _register_key(registry, minted["token"], "other") == 201
    assert _register_key(registry, minted["token"]) == 403
    assert registry.publish(token=minted["token"]).status_code == 403
    with pytest.raises(HubError, match="no namespace"):
        _mint(db_target, "nowhere")


def test_using_a_token_records_the_day_and_the_chain_still_verifies(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = registry.client.post("/v1/namespaces", json={"name": "other"}, headers=ADMIN)
    assert made.status_code == 201, made.text
    minted = _mint(db_target)
    today = clock.today()
    monkeypatch.setattr(clock, "today", lambda: today)  # one day, also across midnight
    assert _tokens(db_target)[0]["last_used_day"] is None
    assert _register_key(registry, minted["token"], "other") == 403  # refused: not a use
    assert _tokens(db_target)[0]["last_used_day"] is None
    assert _register_key(registry, minted["token"]) == 201
    assert _tokens(db_target)[0]["last_used_day"] == today.isoformat()
    events = len(_events(db_target))
    assert _register_key(registry, minted["token"]) == 201  # the same day: the same row
    assert _tokens(db_target)[0]["last_used_day"] == today.isoformat()
    monkeypatch.setattr(clock, "today", lambda: today + timedelta(days=3))
    assert _register_key(registry, minted["token"]) == 201
    assert _tokens(db_target)[0]["last_used_day"] == (today + timedelta(days=3)).isoformat()
    monkeypatch.setattr(clock, "today", lambda: today)  # a slower instance never moves it back
    assert _register_key(registry, minted["token"]) == 201
    assert _tokens(db_target)[0]["last_used_day"] == (today + timedelta(days=3)).isoformat()
    # Four keys were chained; the days were not, and the chain covers every row it should.
    assert len(_events(db_target)) == events + 3
    assert _verify(db_target)["ok"]


@pytest.mark.parametrize("days", [0, -1, 366, 10_000, True])
def test_a_token_lives_1_to_365_days(registry: Registry, db_target: str, days: int) -> None:
    with pytest.raises(HubError, match="a token lives 1 to 365 days"):
        _mint(db_target, days=days)
    assert _tokens(db_target) == []
    for allowed in (1, 365):
        minted = _mint(db_target, days=allowed)
        lives = datetime.fromisoformat(minted["expires_at"]) - datetime.fromisoformat(
            minted["created_at"]
        )
        assert lives == timedelta(days=allowed)


@pytest.mark.parametrize(
    "bad", [{"name": ""}, {"name": "x" * 81}, {"name": "a\nb"}, {"org": " "}, {"owner": "‮"}]
)
def test_a_tokens_labels_are_one_printable_line(
    registry: Registry, db_target: str, bad: dict[str, str]
) -> None:
    with pytest.raises(HubError):
        _mint(db_target, **bad)
    assert _tokens(db_target) == []


def test_another_organization_neither_lists_nor_revokes_it(
    registry: Registry, db_target: str
) -> None:
    minted = _mint(db_target)
    other = _mint(db_target, org="other-org", owner="user:b0b0")
    assert [t["id"] for t in _tokens(db_target, org="acme")] == [minted["id"]]
    assert [t["id"] for t in _tokens(db_target, org="other-org")] == [other["id"]]
    assert _tokens(db_target, org="nobody") == []
    events = len(_events(db_target))
    with RegistryStore(db_target) as store:
        with pytest.raises(HubError) as theirs:
            store.revoke_token(minted["id"], org="other-org", by="user:b0b0")
        with pytest.raises(HubError) as unknown:
            store.revoke_token("0" * 16, org="other-org", by="user:b0b0")
        with pytest.raises(HubError) as malformed:
            store.revoke_token("' OR 1=1 --", org="other-org", by="user:b0b0")
    # One answer: another organization's token reads as one that does not exist.
    assert {str(e.value) for e in (theirs, unknown, malformed)} == {"no such token"}
    assert {e.value.status for e in (theirs, unknown, malformed)} == {404}
    assert len(_events(db_target)) == events
    assert _register_key(registry, minted["token"]) == 201  # still good


def test_the_operators_token_records_operator_and_why(
    registry: Registry, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    minted_by = ["registry", "token", "--db", db_target, "--namespace", "mendari"]
    assert main([*minted_by, "--org", "acme", "--reason", "seed the  CI registry"]) == 0
    minted = json.loads(capsys.readouterr().out)
    assert (
        minted["owner"] == "operator" and minted["org"] == "acme" and minted["name"] == "operator"
    )
    lives = datetime.fromisoformat(minted["expires_at"]) - datetime.fromisoformat(
        minted["created_at"]
    )
    assert lives == timedelta(days=90)
    assert _register_key(registry, minted["token"]) == 201
    issued = json.loads(_events(db_target)[-2]["payload"])
    assert issued["owner"] == "operator" and issued["reason"] == "seed the CI registry"
    assert issued["by"].startswith("command line (")

    assert main(["registry", "tokens", "--db", db_target, "--org", "acme"]) == 0
    listed = capsys.readouterr().out
    assert [t["id"] for t in json.loads(listed)] == [minted["id"]]
    assert minted["token"] not in listed

    revoke = ["registry", "revoke-token", minted["id"], "--db", db_target]
    assert main([*revoke, "--reason", "the runner was retired"]) == 0
    assert json.loads(capsys.readouterr().out)["revoked_by"].startswith("command line (")
    revoked = json.loads(_events(db_target)[-1]["payload"])
    assert revoked["reason"] == "the runner was retired"
    assert _register_key(registry, minted["token"]) == 403
    assert main(["registry", "verify", "--db", db_target]) == 0

    assert main([*minted_by, "--org", "acme", "--reason", "x", "--days", "400"]) != 0
    assert "a token lives 1 to 365 days" in capsys.readouterr().err
    assert main(["registry", "revoke-token", "f" * 16, "--db", db_target, "--reason", "x"]) != 0
    assert "no such token" in capsys.readouterr().err


def test_a_database_from_before_owned_tokens_still_verifies(
    registry: Registry, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A database made before owned tokens has neither of their tables. Read as it is
    (`mendari registry verify`, read-only) it verifies and lists no owned token; opened by a
    registry it gains the tables, and its chain and every old row's hash are as they were."""
    assert registry.publish().status_code == 201
    _write_raw_drop(db_target)
    with RegistryStore(db_target, read_only=True) as store:
        before = store.verify()
        assert before["ok"], before
        assert store.tokens() == []
        with store._db.session(exclusive=False) as db:
            events = db.all("SELECT * FROM hub_events ORDER BY seq")
            assert not db.columns("hub_publisher_tokens")  # still as it was: nothing written
    assert main(["registry", "tokens", "--db", db_target]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert _events(db_target) == events  # opened read-write: the tables come, no event does
    assert _verify(db_target) == before
    assert _register_key(registry, registry.token) == 201  # a token from before still works
    minted = _mint(db_target)
    assert _register_key(registry, minted["token"]) == 201
    assert _verify(db_target)["ok"]


def _write_raw_drop(db_target: str) -> None:
    """Drop the tables past the store: opening a `RegistryStore` to write creates them again."""
    if db_target.startswith("postgres"):
        import psycopg

        with psycopg.connect(db_target, autocommit=True) as connection:
            connection.execute("DROP TABLE IF EXISTS hub_publisher_tokens")
            connection.execute("DROP TABLE IF EXISTS hub_token_use")
        return
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_target)) as connection:
        connection.execute("DROP TABLE IF EXISTS hub_publisher_tokens")
        connection.execute("DROP TABLE IF EXISTS hub_token_use")
        connection.commit()
