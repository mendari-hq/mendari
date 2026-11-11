# SPDX-License-Identifier: Apache-2.0
"""The registry's counter-signature (LP-19i, `hub/acceptance.py`): the registry signs a record
for each version when it becomes listed, and the client checks it against a shipped or pinned
key before it fetches the archive (`hub/client.py` `_accepted`). A version with no record, or a
record about something else, is refused once the registry has a key. On SQLite and Postgres.
Every key here is made at runtime; none is kept."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from mendari.cli import main
from mendari.core import checkpoint
from mendari.core.origin import pulled_record
from mendari.core.registry import resolve_pack
from mendari.errors import HubError
from mendari.hub import acceptance, yank
from mendari.hub import client as client_module
from mendari.hub import registry as registry_module
from mendari.hub.client import RegistryClient, config_dir, packs_cache, pull
from mendari.hub.registry import REVIEWER, RegistryStore
from tests.conftest import REGISTRY_ADMIN, Registry
from tests.test_hub_client import (  # noqa: F401 - `hub` is the live registry fixture
    SHIPPED,
    HostileRegistry,
    Hub,
    copy_pack,
    hub,
)
from tests.test_registry_claims import Claimed

ADMIN = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
REFERRAL = resolve_pack("mendari/referral-router")
PATH = f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}"
SUBJECT = f"{REFERRAL.id}@{REFERRAL.version}"
URL = "https://registry.example"
SEED = os.urandom(32)  # the registry's key in these tests: made here, never kept
OTHER_SEED = os.urandom(32)


def _key(seed: bytes = SEED) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _keyed(monkeypatch: pytest.MonkeyPatch, seed: bytes | None = SEED) -> None:
    """Give the registry its signing key (None: take it away)."""
    if seed is None:
        monkeypatch.delenv(yank.KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(yank.KEY_ENV, base64.b64encode(seed).decode())


Change = Callable[[dict[str, Any]], Any]


def _client(
    registry: Registry,
    change: Change | None = None,
    seen: list[str] | None = None,
    url: str = URL,
) -> RegistryClient:
    """The real client, its requests answered by the registry under test. `change` rewrites a
    version as the registry answers it (a registry that lies); `seen` collects the paths asked."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        if seen is not None:
            seen.append(path)
        answer = registry.client.request(request.method, path, content=request.content)
        content = answer.content
        if change is not None and answer.status_code == 200 and "/v1/packs/" in path:
            found = answer.json()
            if isinstance(found, dict) and "checked" in found:
                content = json.dumps(change(found)).encode()
            elif isinstance(found, dict) and isinstance(found.get("latest"), dict):
                content = json.dumps({**found, "latest": change(found["latest"])}).encode()
        return httpx.Response(answer.status_code, content=content)

    return RegistryClient(url, transport=httpx.MockTransport(handle))


def _events(db_target: str) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_events ORDER BY seq")


def _verify(db_target: str) -> dict[str, Any]:
    with RegistryStore(db_target) as store:
        return store.verify()


def _write(db_target: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        db.all(sql, params)


def _record(registry: Registry, path: str = PATH) -> Any:
    return registry.client.get(path).json()["checked"]["accepted"]


def _cached() -> list[Path]:
    return sorted(packs_cache().rglob("pack.yaml"))


def _keys_file(path: Path, *keys: Ed25519PrivateKey) -> Path:
    listed = [yank.registry_public(key) for key in keys]
    path.write_text(json.dumps({"format": acceptance.KEYS_FORMAT, "keys": listed}))
    return path


def test_an_accepted_version_carries_a_record_that_verifies(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    published = registry.publish()
    assert published.status_code == 201, published.text
    version = registry.client.get(PATH).json()
    record = version["checked"]["accepted"]
    assert published.json()["checked"]["accepted"] == record
    own = registry.client.get("/v1/registry-key").json()
    assert own == yank.registry_public(_key())
    assert acceptance.problem(record, _key().public_key(), own["key_id"]) is None
    assert set(record) == {*acceptance.FIELDS, "signature"}
    assert {name: record[name] for name in acceptance.FIELDS} == {
        "format": "mendari.registry-acceptance/1",
        "pack": REFERRAL.id,
        "version": REFERRAL.version,
        "digest": REFERRAL.digest,
        "archive_sha256": version["archive_sha256"],
        "publisher_key_id": version["checked"]["signature"]["key_id"],
        "key_id": own["key_id"],
        "at": record["at"],
    }
    # Every read of the version serves the same record.
    assert (
        registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"]["checked"]["accepted"]
        == record
    )
    (found,) = registry.client.get("/v1/search").json()
    assert found["checked"]["accepted"] == record
    # Chained after the publish, with the row's hash and no signature bytes of its own.
    last = _events(db_target)[-2:]
    assert [e["event"] for e in last] == ["pack.published", "pack.accepted"]
    payload = json.loads(last[1]["payload"])
    assert last[1]["subject"] == SUBJECT and payload["row"]["kind"] == "acceptances"
    assert (payload["digest"], payload["key_id"]) == (REFERRAL.digest, own["key_id"])
    assert _verify(db_target)["ok"]
    seen: list[str] = []
    with _client(registry, seen=seen) as client:
        pulled = pull(REFERRAL.id, client)
    assert pulled.digest == REFERRAL.digest
    assert pulled_record(pulled.root)["accepted"] is True
    # The record is checked before the archive is asked for.
    assert seen.index("/v1/registry-key") < seen.index(f"{PATH}/archive")
    # A record edited in the database no longer verifies, in the chain or at a pull.
    edited = json.dumps({**record, "digest": "sha256:" + "0" * 64}, sort_keys=True)
    _write(db_target, "UPDATE hub_acceptances SET record = ?", (edited,))
    assert _verify(db_target)["problem"] == "1 of the acceptances changed since their chained event"
    with _client(registry) as client, pytest.raises(HubError, match="signature does not hold"):
        pull(REFERRAL.id, client)


def test_a_version_planted_in_the_database_has_no_record_and_is_not_pulled(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    # Written past the registry, by someone who can write its database: a version it never
    # accepted, here a copy of the real one's row under a newer number.
    _write(
        db_target,
        "INSERT INTO hub_versions (namespace, name, version, created_at, archive_sha256, metadata) "
        "SELECT namespace, name, '9.9.9', created_at, archive_sha256, metadata FROM hub_versions",
    )
    planted = registry.client.get(f"/v1/packs/{REFERRAL.id}/9.9.9").json()
    assert planted["checked"]["accepted"] is None
    assert registry.client.get(f"/v1/packs/{REFERRAL.id}").json()["latest"]["version"] == "9.9.9"
    seen: list[str] = []
    for ref in (REFERRAL.id, f"{REFERRAL.id}@9.9.9"):  # as the latest, and by its number
        with (
            _client(registry, seen=seen) as client,
            pytest.raises(HubError, match="is not counter-signed by the registry") as refused,
        ):
            pull(ref, client)
        assert "mendari registry countersign" in str(refused.value)
    assert not any(path.endswith("/archive") for path in seen)  # never fetched
    assert _cached() == []  # nothing cached
    with _client(registry) as client:  # the version the registry accepted still pulls
        assert pull(f"{REFERRAL.id}@{REFERRAL.version}", client).digest == REFERRAL.digest
    # The registry's own record, moved onto the planted version, names another version.
    moved = _record(registry)
    with (
        _client(
            registry, lambda v: {**v, "checked": {**v["checked"], "accepted": moved}}
        ) as client,
        pytest.raises(HubError, match="it names another version"),
    ):
        pull(f"{REFERRAL.id}@9.9.9", client)
    assert _verify(db_target)["problem"] == "1 of the versions have no chained event"


@pytest.mark.parametrize(
    ("field", "other", "problem"),
    [
        ("pack", "mendari/another", "it names another pack"),
        ("version", "9.9.9", "it names another version"),
        ("digest", "sha256:" + "1" * 64, "it names another digest"),
        ("archive_sha256", "2" * 64, "it names another archive sha256"),
        ("publisher_key_id", "ed25519:" + "3" * 16, "it names another publisher key id"),
        ("signature", "", "its signature does not hold"),
        ("signer", "", "it was signed with another key"),
    ],
)
def test_a_record_for_another_version_digest_archive_or_key_is_refused(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, field: str, other: str, problem: str
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    real = _record(registry)
    named = {name: real[name] for name in acceptance.NAMED}
    if field == "signature":  # the real fields, signed by someone else under the registry's id
        forged = acceptance.sign(**named, key=_key(OTHER_SEED))
        record = {**real, "signature": forged["signature"]}
    elif field == "signer":  # a whole record by another key, honestly named
        record = acceptance.sign(**named, key=_key(OTHER_SEED))
    else:  # signed by the registry's own key, about something else
        record = acceptance.sign(**{**named, field: other}, key=_key())
        assert acceptance.problem(record, _key().public_key(), real["key_id"]) is None
    seen: list[str] = []
    lie: Change = lambda v: {**v, "checked": {**v["checked"], "accepted": record}}  # noqa: E731
    with _client(registry, lie, seen) as client, pytest.raises(HubError) as refused:
        pull(REFERRAL.id, client)
    assert problem in str(refused.value) and SUBJECT in str(refused.value)
    assert not any(path.endswith("/archive") for path in seen) and _cached() == []


def test_a_recall_or_a_checkpoint_signature_is_not_an_acceptance(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    real = _record(registry)
    public, key_id = _key().public_key(), real["key_id"]
    # The registry's own recall of the same version, signed with the same key.
    body = {"reason": "a safety notice from the registry", "severity": "critical"}
    recall = registry.client.post(f"{PATH}/yank", json=body, headers=ADMIN).json()["yank"]
    assert yank.problem(recall, public, key_id) is None
    statement = checkpoint.sign("a" * 64, date(2026, 11, 10), _key())  # and a checkpoint by it
    assert checkpoint.problem(statement, public) is None
    for other in (recall, statement):
        assert acceptance.problem(other, public, key_id) is not None  # not one as it is
        replayed = {**real, "signature": other["signature"]}  # nor is its signature one
        assert acceptance.problem(replayed, public, key_id) == "its signature does not hold"
        dressed = {**{name: other.get(name, real[name]) for name in acceptance.FIELDS}}
        dressed["signature"] = other["signature"]  # its fields under an acceptance's names
        assert acceptance.problem(dressed, public, key_id) is not None
    assert acceptance.FORMAT not in (yank.FORMAT, checkpoint.FORMAT)
    assert yank.problem(real, public, key_id) is not None  # and an acceptance recalls nothing
    assert checkpoint.problem(real, public) is not None
    # Served in the record's place, a recall is refused by a pull of the exact version.
    lie: Change = lambda v: {**v, "checked": {**v["checked"], "accepted": recall}}  # noqa: E731
    with _client(registry, lie) as client, pytest.raises(HubError, match="is not one"):
        pull(f"{REFERRAL.id}@{REFERRAL.version}", client)
    # The recalled version is still counter-signed: its exact version pulls, to read why.
    with _client(registry) as client:
        assert pull(f"{REFERRAL.id}@{REFERRAL.version}", client).digest == REFERRAL.digest


def test_a_pending_version_is_signed_at_approval_and_a_refused_one_never(
    registry: Registry, db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    acme = Claimed(registry, db_target, tmp_path)
    own = {"authorization": f"Bearer {acme.token}"}
    path = f"/v1/packs/acme/inbox/{SHIPPED}"
    assert acme.publish()["checked"]["accepted"] is None  # pending: not listed, not signed
    assert acme.publish("9.9.9")["checked"]["accepted"] is None
    assert registry.client.get(path, headers=own).json()["checked"]["accepted"] is None
    assert "pack.accepted" not in [e["event"] for e in _events(db_target)]
    with RegistryStore(db_target) as store:  # the backfill signs listed versions only
        assert store.countersign(by="operator") == {"signed": [], "skipped": []}
        store.decide_claim("acme", approve=True, by="user:77aa")
    events = _events(db_target)[-3:]
    assert [(e["event"], e["subject"]) for e in events] == [
        ("namespace.approved", "namespace:acme"),
        ("pack.accepted", f"acme/inbox@{SHIPPED}"),
        ("pack.accepted", "acme/inbox@9.9.9"),
    ]
    assert json.loads(events[1]["payload"])["by"] == "user:77aa"
    key_id = yank.registry_public(_key())["key_id"]
    for version in (SHIPPED, "9.9.9"):
        record = _record(registry, f"/v1/packs/acme/inbox/{version}")
        assert acceptance.problem(record, _key().public_key(), key_id) is None
        assert (record["pack"], record["version"]) == ("acme/inbox", version)
    with _client(registry) as client:
        assert pulled_record(pull(f"acme/inbox@{SHIPPED}", client).root)["accepted"] is True
    # Listed from then: a later version is signed as it is published.
    assert acme.publish("9.9.10")["checked"]["accepted"]["version"] == "9.9.10"
    # A refused namespace's version is never signed: not at the refusal, not by the backfill.
    zeta = Claimed(registry, db_target, tmp_path, "zeta")
    assert zeta.publish()["checked"]["accepted"] is None
    with RegistryStore(db_target) as store:
        store.decide_claim("zeta", approve=False, by="user:77aa")
        assert store.countersign(by="operator") == {"signed": [], "skipped": []}
        assert store.version("zeta", "inbox", SHIPPED, REVIEWER)["checked"]["accepted"] is None
    assert [e["subject"] for e in _events(db_target) if e["event"] == "pack.accepted"] == [
        f"acme/inbox@{SHIPPED}",
        "acme/inbox@9.9.9",
        "acme/inbox@9.9.10",
    ]
    assert _verify(db_target)["ok"]


def test_a_registry_that_drops_or_changes_its_key_is_refused(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    with _client(registry) as client:
        first = pull(REFERRAL.id, client)  # pins the registry's key, on first use
    pins = json.loads((config_dir() / "trusted_keys.json").read_text())
    assert pins[URL]["@registry"] == yank.registry_public(_key())["key_id"]
    _keyed(monkeypatch, None)  # the registry drops its key: the record is still served
    assert _record(registry) is not None
    with _client(registry) as client, pytest.raises(HubError, match="publishes none now"):
        pull(REFERRAL.id, client)  # the cached copy is not used either
    _keyed(monkeypatch, OTHER_SEED)  # or presents another
    with _client(registry) as client, pytest.raises(HubError, match="not the one this machine"):
        pull(REFERRAL.id, client)

    # A key that is not the key its id names is no key.
    def misnamed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/registry-key":
            wrong = {**yank.registry_public(_key()), "key_id": "ed25519:" + "0" * 16}
            return httpx.Response(200, json=wrong)
        answer = registry.client.request(request.method, request.url.raw_path.decode())
        return httpx.Response(answer.status_code, content=answer.content)

    with (
        RegistryClient(URL, transport=httpx.MockTransport(misnamed)) as client,
        pytest.raises(HubError, match="signing key is not a key"),
    ):
        pull(REFERRAL.id, client)
    assert first.root.exists()  # a refusal removes nothing that was verified before
    # A machine that never saw the registry pins the new key, and the records, signed with the
    # old one, do not verify with it.
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(tmp_path / "another-machine"))
    monkeypatch.setenv("MENDARI_CACHE_DIR", str(tmp_path / "another-cache"))
    with _client(registry) as client, pytest.raises(HubError, match="signed with another key"):
        pull(REFERRAL.id, client)
    assert _cached() == []


def test_a_shipped_key_is_trusted_without_first_use(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    shipped = _keys_file(tmp_path / "keys.json", _key(OTHER_SEED), _key())
    assert list(acceptance.shipped_keys(shipped)) == [
        yank.registry_public(key)["key_id"] for key in (_key(OTHER_SEED), _key())
    ]
    # This machine pinned another key for the registry: as a changed key, it is refused...
    pins = config_dir() / "trusted_keys.json"
    pins.parent.mkdir(parents=True)
    pins.write_text(
        json.dumps({URL: {"@registry": yank.registry_public(_key(OTHER_SEED))["key_id"]}})
    )
    with _client(registry) as client, pytest.raises(HubError, match="not the one this machine"):
        pull(REFERRAL.id, client)
    # ...but the key it presents is a shipped one, which needs no first use.
    with _client(registry) as client:
        pulled = pull(REFERRAL.id, client, registry_keys=shipped)
    assert pulled_record(pulled.root)["accepted"] is True
    # A shipped key only names who to trust: a record still has to verify with it.
    forged = acceptance.sign(
        **{name: _record(registry)[name] for name in acceptance.NAMED}, key=_key(OTHER_SEED)
    )
    lie: Change = lambda v: {**v, "checked": {**v["checked"], "accepted": forged}}  # noqa: E731
    with _client(registry, lie) as client, pytest.raises(HubError, match="another key"):
        pull(REFERRAL.id, client, registry_keys=shipped)
    # A file listing only another key trusts nothing more, and a broken one is refused whole.
    other = _keys_file(tmp_path / "other.json", _key(OTHER_SEED))
    with _client(registry) as client, pytest.raises(HubError, match="not the one this machine"):
        pull(REFERRAL.id, client, registry_keys=other)
    listed = yank.registry_public(_key())
    for broken in (
        "not json",
        json.dumps({"format": "mendari.grading-keys/1", "keys": []}),
        json.dumps({"format": acceptance.KEYS_FORMAT, "keys": [], "hosts": ["registry.example"]}),
        json.dumps({"format": acceptance.KEYS_FORMAT, "keys": [{**listed, "host": "x"}]}),
        json.dumps({"format": acceptance.KEYS_FORMAT, "keys": [{**listed, "public_key": "AAAA"}]}),
        json.dumps({"format": acceptance.KEYS_FORMAT, "keys": [listed, listed]}),
        json.dumps(
            {
                "format": acceptance.KEYS_FORMAT,
                "keys": [{**listed, "key_id": "ed25519:" + "0" * 16}],
            }
        ),
    ):
        (tmp_path / "broken.json").write_text(broken)
        with pytest.raises(HubError, match=r"broken\.json"):
            acceptance.shipped_keys(tmp_path / "broken.json")
        with _client(registry) as client, pytest.raises(HubError, match=r"broken\.json"):
            pull(REFERRAL.id, client, registry_keys=tmp_path / "broken.json")
    with pytest.raises(HubError, match="cannot be read"):
        acceptance.shipped_keys(tmp_path / "missing.json")


def test_without_a_key_pull_works_and_says_not_counter_signed(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _keyed(monkeypatch, None)
    token, key = hub.namespace("site-a")
    hub.client(token).push(copy_pack(tmp_path, "site-a/inbox"), key)
    meta = hub.client().version("site-a", "inbox")
    assert meta["checked"]["accepted"] is None
    assert main(["pull", "site-a/inbox", "--registry", hub.url]) == 0
    out = capsys.readouterr().out
    assert "verified and cached" in out
    assert "not counter-signed: this registry publishes no signing key" in out
    marker = pulled_record(packs_cache() / "site-a" / "inbox" / SHIPPED)
    assert "accepted" in marker and marker["accepted"] is None
    assert "@registry" not in json.dumps(
        json.loads((config_dir() / "trusted_keys.json").read_text())
    )
    assert main(["pull", "site-a/inbox", "--registry", hub.url]) == 0  # the cached copy too
    assert "not counter-signed" in capsys.readouterr().out
    # The registry gets its key: what it published before has no record, and is refused until
    # its operator counter-signs.
    _keyed(monkeypatch)
    assert main(["pull", "site-a/inbox", "--registry", hub.url]) != 0
    assert (
        "is not counter-signed by the registry, which has a signing key" in capsys.readouterr().err
    )
    assert main(["registry", "countersign", "--db", str(tmp_path / "registry.db")]) == 0
    assert json.loads(capsys.readouterr().out)["signed"] == [f"site-a/inbox@{SHIPPED}"]
    assert main(["pull", "site-a/inbox", "--registry", hub.url]) == 0
    assert (
        "counter-signed by the registry: its record names this version" in capsys.readouterr().out
    )
    assert pulled_record(packs_cache() / "site-a" / "inbox" / SHIPPED)["accepted"] is True


def test_countersign_signs_what_was_published_before_and_twice_changes_nothing(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201  # published while the registry had no key
    assert registry.publish(version="9.9.9").status_code == 422  # (the archive names its version)
    inbox = copy_pack(tmp_path, "mendari/inbox")
    assert registry.publish(inbox).status_code == 201
    gone = copy_pack(tmp_path, "mendari/gone")
    assert registry.publish(gone).status_code == 201
    body = {"reason": "the archive carried a file it must not"}
    deleted = registry.client.post(
        f"/v1/packs/mendari/gone/{SHIPPED}/delete", json=body, headers=ADMIN
    )
    assert deleted.status_code == 200, deleted.text
    assert _record(registry) is None
    before = _events(db_target)
    command = ["registry", "countersign", "--db", db_target]
    assert main(command) != 0  # no key: nothing to sign with, and nothing written
    assert yank.KEY_ENV in capsys.readouterr().err
    monkeypatch.setenv(yank.KEY_ENV, "not-a-key")
    assert main(command) != 0
    assert yank.KEY_ENV in capsys.readouterr().err
    assert _events(db_target) == before
    _keyed(monkeypatch)
    assert main(command) == 0
    done = json.loads(capsys.readouterr().out)
    assert done == {
        "signed": [f"mendari/inbox@{SHIPPED}", SUBJECT],
        "skipped": [],
        "checkpoint": None,
    }
    signed = _events(db_target)
    assert [(e["event"], e["subject"]) for e in signed[len(before) :]] == [
        ("pack.accepted", f"mendari/inbox@{SHIPPED}"),
        ("pack.accepted", SUBJECT),
    ]
    assert json.loads(signed[-1]["payload"])["by"]  # who ran it, on the chain
    record = _record(registry)
    key_id = yank.registry_public(_key())["key_id"]
    assert acceptance.problem(record, _key().public_key(), key_id) is None
    deleted_version = registry.client.get(f"/v1/packs/mendari/gone/{SHIPPED}", headers=ADMIN)
    assert deleted_version.json()["checked"]["accepted"] is None  # a deleted version, never
    assert main(command) == 0  # again: nothing to sign, nothing written
    assert json.loads(capsys.readouterr().out) == {
        "signed": [],
        "skipped": [],
        "checkpoint": None,
    }
    assert _events(db_target) == signed and _record(registry) == record
    assert _verify(db_target)["ok"]
    assert main(["registry", "verify", "--db", db_target]) == 0
    with _client(registry) as client:
        assert pulled_record(pull(REFERRAL.id, client).root)["accepted"] is True


def _plant(db_target: str, namespace: str, version: str = "9.9.9") -> None:
    """Write a version past the registry, as someone who can write its database would: a copy
    of the namespace's real row under another number, and with it an archive no key of the
    namespace signed (the hostile registry's own build of the referral pack)."""
    _write(
        db_target,
        "INSERT INTO hub_versions (namespace, name, version, created_at, archive_sha256, metadata) "
        "SELECT namespace, name, ?, created_at, archive_sha256, metadata FROM hub_versions "
        "WHERE namespace = ? LIMIT 1",
        (version, namespace),
    )


def _restart_chain(db_target: str) -> None:
    """Rewrite the unkeyed chain whole, as a writer of the database can: every row it holds
    now is hashed anew in one first event, so `verify` passes with the planted rows in it."""
    _drop(db_target, "hub_events")
    _drop(db_target, "hub_meta")
    assert _verify(db_target)["ok"]


def test_countersign_refuses_a_broken_chain_and_never_signs_a_planted_version(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The backfill signs after the fact, so it is the one place a row the registry never
    accepted could gain its signature. It refuses a chain that does not verify; and where the
    chain was rewritten to hide the row, it still signs no version whose stored archive is not
    signed by a key of its namespace, for this pack, version and digest."""
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    _plant(db_target, "mendari")
    _keyed(monkeypatch)
    command = ["registry", "countersign", "--db", db_target]
    before = _events(db_target)
    assert main(command) != 0
    err = capsys.readouterr().err
    assert "nothing was counter-signed" in err and "1 of the versions have no chained event" in err
    assert "mendari registry verify" in err
    assert _events(db_target) == before and _record(registry) is None
    # The chain rewritten whole, the planted row in it: `verify` passes, the archive check does
    # not (the row's archive is the real version's, whose signed manifest names that version).
    _restart_chain(db_target)
    assert main(command) != 0
    printed = capsys.readouterr()
    assert json.loads(printed.out) == {
        "signed": [SUBJECT],
        "skipped": [f"{REFERRAL.id}@9.9.9: its archive is another pack or version"],
        "checkpoint": None,
    }
    assert "1 not counter-signed" in printed.err and "ran without a checkpoint" in printed.err
    assert (
        registry.client.get(f"/v1/packs/{REFERRAL.id}/9.9.9").json()["checked"]["accepted"] is None
    )
    with _client(registry) as client, pytest.raises(HubError, match="is not counter-signed"):
        pull(f"{REFERRAL.id}@9.9.9", client)
    # Each other way a stored version is not one the registry accepted, by what the check says.
    other = HostileRegistry(tmp_path)  # a real archive of the pack, signed by nobody's key here
    sha = hashlib.sha256(other.data).hexdigest()
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        row = db.one("SELECT * FROM hub_versions WHERE version = ?", (REFERRAL.version,))
        assert row is not None and registry_module._not_genuine(db, row) is None
        db.all("INSERT INTO hub_archives (archive_sha256, data) VALUES (?, ?)", (sha, other.data))
        unsigned = {**row, "archive_sha256": sha}
        assert "signed by a key that is not trusted" in str(
            registry_module._not_genuine(db, unsigned)
        )
        assert registry_module._not_genuine(db, {**row, "archive_sha256": "0" * 64}) == (
            "its archive is missing"
        )
        elsewhere = {**row, "metadata": json.dumps({"digest": "sha256:" + "1" * 64, "key_id": "k"})}
        assert "another build" in str(registry_module._not_genuine(db, elsewhere))
        assert "without a digest" in str(
            registry_module._not_genuine(db, {**row, "metadata": "{}"})
        )
        db.all("UPDATE hub_archives SET data = ? WHERE archive_sha256 = ?", (b"x", sha))
        assert registry_module._not_genuine(db, unsigned) == (
            "its archive is not the bytes its name hashes"
        )


def test_approval_never_signs_a_version_planted_while_the_claim_was_pending(
    registry: Registry, db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    acme = Claimed(registry, db_target, tmp_path)
    acme.publish()
    _plant(db_target, "acme")
    with RegistryStore(db_target) as store:
        store.decide_claim("acme", approve=True, by="user:77aa")
    assert [e["subject"] for e in _events(db_target) if e["event"] == "pack.accepted"] == [
        f"acme/inbox@{SHIPPED}"
    ]
    assert _record(registry, f"/v1/packs/acme/inbox/{SHIPPED}") is not None
    assert _record(registry, "/v1/packs/acme/inbox/9.9.9") is None
    with _client(registry) as client:
        with pytest.raises(HubError, match="is not counter-signed"):
            pull("acme/inbox", client)  # the planted one is the newest: refused, not skipped
        assert pulled_record(pull(f"acme/inbox@{SHIPPED}", client).root)["accepted"] is True


def test_a_signing_key_that_is_not_one_is_a_clear_error_and_never_shown(
    registry: Registry,
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "not-a-key-" + base64.b64encode(os.urandom(9)).decode()
    monkeypatch.setenv(yank.KEY_ENV, secret)
    refused = registry.publish()
    assert refused.status_code == 503, refused.text
    said = refused.json()
    assert said["error"] == "HubError" and "the registry cannot counter-sign" in said["detail"]
    assert yank.KEY_ENV in said["detail"] and "32 random bytes in base64" in said["detail"]
    assert secret not in refused.text
    assert registry.client.get(PATH).status_code == 404  # nothing was published
    _keyed(monkeypatch)
    acme = Claimed(registry, db_target, tmp_path)
    acme.publish()
    monkeypatch.setenv(yank.KEY_ENV, secret)
    with RegistryStore(db_target) as store:
        with pytest.raises(HubError, match="cannot counter-sign") as approval:
            store.decide_claim("acme", approve=True, by="user:77aa")
        assert approval.value.status == 503 and secret not in str(approval.value)
        assert store.claims(namespace="acme")[0]["status"] == "pending"  # nothing decided
        assert store.decide_claim("acme", approve=False, by="user:77aa")["status"] == "refused"
    assert main(["registry", "countersign", "--db", db_target]) != 0
    err = capsys.readouterr().err
    assert yank.KEY_ENV in err and secret not in err


def test_a_pin_file_that_cannot_be_read_is_refused_and_is_written_whole(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    pins = config_dir() / "trusted_keys.json"
    with _client(registry) as client:
        pull(REFERRAL.id, client)
    assert pins.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in pins.parent.iterdir()) == ["trusted_keys.json"]  # no scrap
    whole = pins.read_text()
    _keyed(monkeypatch, None)  # the registry shows no key: with the pins lost, an unsigned pull
    shutil.rmtree(packs_cache())
    for broken in (whole[: len(whole) // 2], "", "[]", '{"https://registry.example": []}', "\xff"):
        pins.write_text(broken)
        with _client(registry) as client, pytest.raises(HubError, match="cannot be read") as lost:
            pull(REFERRAL.id, client)
        assert str(pins) in str(lost.value) and "remove it" in str(lost.value)
        assert pins.read_text() == broken and _cached() == []  # never overwritten, nothing pulled
    pins.write_text(whole)
    with _client(registry) as client, pytest.raises(HubError, match="publishes none now"):
        pull(REFERRAL.id, client)

    # A write that fails part way leaves the old file, whole.
    def cut_short(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(client_module.json, "dump", cut_short)
    with pytest.raises(OSError, match="disk full"):
        client_module._write_private(pins, {"lost": {}})
    assert pins.read_text() == whole
    assert sorted(path.name for path in pins.parent.iterdir()) == ["trusted_keys.json"]


@pytest.mark.parametrize(
    "typed",
    [
        "https://REGISTRY.example",
        "https://registry.example:443",
        "HTTPS://Registry.Example:443/",
        "https://registry.example/",
    ],
)
def test_one_registry_has_one_pin_however_its_address_is_typed(
    registry: Registry, monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    with _client(registry) as client:
        pull(REFERRAL.id, client)  # pinned as https://registry.example
    assert list(json.loads((config_dir() / "trusted_keys.json").read_text())) == [URL]
    shutil.rmtree(packs_cache())
    _keyed(monkeypatch, OTHER_SEED)  # another key, shown at the same registry typed another way
    with _client(registry, url=typed) as client, pytest.raises(HubError, match="not the one this"):
        pull(REFERRAL.id, client)
    _keyed(monkeypatch, None)
    with _client(registry, url=typed) as client, pytest.raises(HubError, match="publishes none"):
        pull(REFERRAL.id, client)
    assert client_module._pin_url(typed) == URL
    # Another host, port, scheme or path is another registry.
    for other in (
        "https://registry.example:8443",
        "https://registry.example/v2",
        "https://b.example",
    ):
        assert client_module._pin_url(other) == other
    assert client_module._pin_url("http://LOCALHOST:80/") == "http://localhost"
    assert client_module._pin_url("http://[::1]:8100") == "http://[::1]:8100"


def test_pins_filed_under_an_address_as_it_was_typed_are_still_read(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin file from before pins were filed by normalised address: its entries count."""
    _keyed(monkeypatch)
    assert registry.publish().status_code == 201
    old = yank.registry_public(_key(OTHER_SEED))["key_id"]
    pins = config_dir() / "trusted_keys.json"
    pins.parent.mkdir(parents=True)
    pins.write_text(
        json.dumps({"https://Registry.Example:443": {"@registry": old, "acme": ["k1"]}})
    )
    with _client(registry) as client, pytest.raises(HubError, match="not the one this machine"):
        pull(REFERRAL.id, client)
    kept = json.loads(pins.read_text())
    assert kept["https://Registry.Example:443"]["@registry"] == old  # nothing overwritten
    assert client_module._pins_of(kept, URL) == {"@registry": old, "acme": ["k1"]}


def test_a_machine_can_require_the_counter_signature(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`MENDARI_REGISTRY_COUNTERSIGNATURE`: `required` refuses a registry that shows no key
    (the unsigned pull a first contact with an impostor would get); `shipped` also refuses a key
    that is merely shown, so nothing is trusted on first use."""
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    env = client_module.COUNTERSIGNATURE_ENV
    seen: list[str] = []
    for mode in ("required", "shipped", " Required "):
        monkeypatch.setenv(env, mode)
        with (
            _client(registry, seen=seen) as client,
            pytest.raises(HubError, match="publishes no signing key") as refused,
        ):
            pull(REFERRAL.id, client)
        assert env in str(refused.value)
    assert not any(path.endswith("/archive") for path in seen) and _cached() == []
    monkeypatch.setenv(env, "sometimes")  # not a mode: refused, never read as optional
    with _client(registry) as client, pytest.raises(HubError, match="optional, required, shipped"):
        pull(REFERRAL.id, client)
    monkeypatch.setenv(env, "optional")
    with _client(registry) as client:
        assert pulled_record(pull(REFERRAL.id, client).root)["accepted"] is None
    # The registry gets its key and counter-signs. `required` takes it, pinned on first use.
    shutil.rmtree(packs_cache())
    _keyed(monkeypatch)
    with RegistryStore(db_target) as store:
        assert store.countersign(by="operator")["signed"] == [SUBJECT]
    monkeypatch.setenv(env, "shipped")  # but it is not a shipped key: refused
    with _client(registry) as client, pytest.raises(HubError, match="not one shipped"):
        pull(REFERRAL.id, client)
    assert _cached() == []
    shipped = _keys_file(tmp_path / "keys.json", _key())
    with _client(registry) as client:
        pulled = pull(REFERRAL.id, client, registry_keys=shipped)
    assert pulled_record(pulled.root)["accepted"] is True
    monkeypatch.setenv(env, "required")
    with _client(registry) as client:
        assert pulled_record(pull(REFERRAL.id, client).root)["accepted"] is True
    assert client_module.COUNTERSIGNATURE_MODES == ("optional", "required", "shipped")


def test_a_database_from_before_counter_signatures_reads_and_verifies(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database from before LP-19i has no `hub_acceptances`: read as it is it verifies and
    serves, with no record; opened by a registry it gains the table and no event."""
    _keyed(monkeypatch, None)
    assert registry.publish().status_code == 201
    version = registry.client.get(PATH).json()
    _drop(db_target, "hub_acceptances")
    with RegistryStore(db_target, read_only=True) as store:
        before = store.verify()
        assert before["ok"], before
        found = store.version("mendari", "referral-router", REFERRAL.version)
        assert found["checked"]["accepted"] is None and found["digest"] == version["digest"]
        assert [p["id"] for p in store.latest()] == [REFERRAL.id]
        assert len(store.versions("mendari", "referral-router")) == 1
        with store._db.session(exclusive=False) as db:
            events = db.all("SELECT * FROM hub_events ORDER BY seq")
            assert not db.columns("hub_acceptances")  # still as it was: nothing written
    assert _events(db_target) == events  # opened read-write: the table comes, no event does
    assert _verify(db_target) == before
    assert registry.client.get(PATH).json() == version
    with _client(registry) as client:
        assert pulled_record(pull(REFERRAL.id, client).root)["accepted"] is None


def _drop(db_target: str, table: str) -> None:
    """Drop a table past the store: opening a `RegistryStore` to write creates it again."""
    if db_target.startswith("postgres"):
        import psycopg

        with psycopg.connect(db_target, autocommit=True) as connection:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        return
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_target)) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()


def test_the_shipped_file_holds_public_keys_only() -> None:
    """The file in the wheel: the format and a list of keys, each an id and 32 public bytes.
    No hostname, no private key. It ships empty until the registry's owner makes its key."""
    assert acceptance.KEYS_FILE.parent == Path(acceptance.__file__).parent
    assert acceptance.KEYS_FILE.name == "registry_keys.json"
    text = acceptance.KEYS_FILE.read_text(encoding="utf-8")
    listing = json.loads(text)
    assert set(listing) == {"format", "keys"} and listing["format"] == "mendari.registry-keys/1"
    for entry in listing["keys"]:
        assert set(entry) == {"key_id", "public_key"}
        assert len(base64.b64decode(entry["public_key"], validate=True)) == 32
    assert len(acceptance.shipped_keys()) == len(listing["keys"])
    for word in ("PRIVATE", "seed", "secret", "http", "://", "host", "url"):
        assert word.lower() not in text.lower()


def test_a_private_key_in_the_keys_file_is_not_read_as_a_public_one(tmp_path: Path) -> None:
    listed = yank.registry_public(_key())
    seed = base64.b64encode(SEED).decode()
    pem = _key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    for entry in ({**listed, "private_key": seed}, {**listed, "public_key": pem}):
        path = tmp_path / "keys.json"
        path.write_text(json.dumps({"format": acceptance.KEYS_FORMAT, "keys": [entry]}))
        with pytest.raises(HubError, match=r"keys\.json"):
            acceptance.shipped_keys(path)
