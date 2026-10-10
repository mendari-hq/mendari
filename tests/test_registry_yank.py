# SPDX-License-Identifier: Apache-2.0
"""Yank as a recall, and delete (LP-18c, `hub/yank.py`, `hub/registry.py`), on SQLite and
Postgres; and how a pull shows a recall (`hub/client.py` `recall_of`)."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.core.registry import resolve_pack
from mendari.errors import HubError
from mendari.hub import archive, yank
from mendari.hub.client import pull, recall_of
from mendari.hub.registry import RegistryStore
from tests.conftest import REGISTRY_ADMIN, Registry
from tests.test_hub_client import Hub, copy_pack, hub  # noqa: F401 - the live registry fixture

REFERRAL = resolve_pack("mendari/referral-router")
ADMIN = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
PATH = f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}"
REGISTRY_KEY = base64.b64encode(os.urandom(32)).decode()


def _record(registry: Registry, **changes: Any) -> dict[str, str]:
    """The referral pack's yank, signed by the namespace's key."""
    meta = registry.client.get(PATH).json()
    fields: dict[str, Any] = {
        "pack": REFERRAL.id,
        "version": REFERRAL.version,
        "digest": meta["digest"],
        "archive_sha256": meta["archive_sha256"],
        "reason": "routes chest pain as routine",
        "severity": "high",
        "signer": "publisher",
        "key": registry.key,
        "key_id": archive.key_id(registry.key.public_key()),
    }
    fields.update(changes)
    return yank.sign(**fields)


def _yank(registry: Registry, record: dict[str, Any], token: str | None = None) -> Any:
    headers = {"authorization": f"Bearer {token or registry.token}"}
    return registry.client.post(f"{PATH}/yank", json={"record": record}, headers=headers)


def test_a_yanked_version_leaves_latest_and_search_and_is_still_served_by_version(
    registry: Registry, db_target: str
) -> None:
    assert registry.publish().status_code == 201
    archive_before = registry.client.get(f"{PATH}/archive").content
    record = _record(registry)
    answer = _yank(registry, record)
    assert answer.status_code == 201, answer.text
    assert answer.json()["yank"] == record
    assert registry.client.get("/v1/search").json() == []
    pack = registry.client.get(f"/v1/packs/{REFERRAL.id}").json()
    assert pack["latest"] is None and pack["versions"] == []
    assert pack["recalled"] == [{"version": REFERRAL.version, "yank": record, "deleted": None}]
    assert registry.client.get("/v1/namespaces/mendari").json()["packs"] == []
    exact = registry.client.get(PATH).json()
    assert exact["yank"] == record and exact["card_markdown"]
    assert registry.client.get(f"{PATH}/archive").content == archive_before  # never deleted
    assert registry.client.get("/v1/yanks").json() == [
        {"pack": REFERRAL.id, "version": REFERRAL.version, "yank": record, "deleted": None}
    ]
    again = _yank(registry, _record(registry))
    assert again.status_code == 409 and "already recalled" in again.text
    with RegistryStore(db_target) as store:
        assert store.verify()["ok"]


def test_a_newer_version_stays_latest_when_an_older_one_is_yanked(
    registry: Registry, tmp_path: Path
) -> None:
    from tests.test_registry import INBOX, _at_version

    assert registry.publish(_at_version(tmp_path / "a", INBOX, "9.9.8")).status_code == 201
    assert registry.publish(_at_version(tmp_path / "b", INBOX, "9.9.9")).status_code == 201
    path = f"/v1/packs/{INBOX.id}/9.9.9"
    meta = registry.client.get(path).json()
    record = yank.sign(
        pack=INBOX.id, version="9.9.9", digest=meta["digest"],
        archive_sha256=meta["archive_sha256"], reason="a bad rule", severity="critical",
        signer="publisher", key=registry.key, key_id=archive.key_id(registry.key.public_key()),
    )  # fmt: skip
    headers = {"authorization": f"Bearer {registry.token}"}
    assert (
        registry.client.post(f"{path}/yank", json={"record": record}, headers=headers).status_code
        == 201
    )
    pack = registry.client.get(f"/v1/packs/{INBOX.id}").json()
    assert pack["latest"]["version"] == "9.9.8" and pack["versions"] == ["9.9.8"]
    assert [item["version"] for item in registry.client.get("/v1/search").json()] == ["9.9.8"]


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"digest": "sha256:" + "0" * 64}, "not about this version"),
        ({"version": "0.0.1"}, "not about this version"),
        ({"at": "2020-01-01T00:00:00Z"}, "time is not now"),
        ({"key": archive.load_private_key(archive.generate_key()[0])}, "does not verify"),
        ({"key_id": "ed25519:0000000000000000"}, "not this namespace's"),
        ({"severity": "urgent"}, "severity is one of"),
        ({"reason": "two\nlines"}, "one line of printable text"),
        ({"reason": "a ‮ reversed"}, "one line of printable text"),
    ],
    ids=[
        "other-digest", "other-version", "stale", "stranger-key", "unknown-key",
        "bad-severity", "two-lines", "bidi",
    ],
)  # fmt: skip
def test_a_forged_or_replayed_yank_is_refused(
    registry: Registry, changes: dict[str, Any], problem: str
) -> None:
    assert registry.publish().status_code == 201
    if "severity" in changes or "reason" in changes:  # sign would refuse: forge the fields
        record = {**_record(registry), **changes}
    else:
        record = _record(registry, **changes)
    refused = _yank(registry, record)
    assert refused.status_code == 422, refused.text
    assert problem in refused.text
    assert registry.client.get(PATH).json()["yank"] is None


def test_a_yank_whose_text_was_changed_after_signing_is_refused(registry: Registry) -> None:
    assert registry.publish().status_code == 201
    record = {**_record(registry), "severity": "low"}  # was high when signed
    refused = _yank(registry, record)
    assert refused.status_code == 422 and "signature does not hold" in refused.text


def test_only_the_namespace_or_the_registry_recalls(registry: Registry) -> None:
    assert registry.publish().status_code == 201
    registry.client.post("/v1/namespaces", json={"name": "other"}, headers=ADMIN)
    other = registry.client.post("/v1/namespaces/other/tokens", headers=ADMIN).json()["token"]
    assert _yank(registry, _record(registry), token=other).status_code == 403
    assert (
        registry.client.post(f"{PATH}/yank", json={"record": _record(registry)}).status_code == 403
    )
    posing = _yank(registry, {**_record(registry), "signer": "registry"})
    assert posing.status_code == 422 and "signed by the publisher" in posing.text


def test_the_registry_signs_its_own_recall(
    registry: Registry, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert registry.publish().status_code == 201
    body = {"reason": "a safety notice from the registry", "severity": "critical"}
    unkeyed = registry.client.post(f"{PATH}/yank", json=body, headers=ADMIN)
    assert unkeyed.status_code == 409 and "no signing key" in unkeyed.text
    assert registry.client.get("/v1/registry-key").status_code == 404
    monkeypatch.setenv(yank.KEY_ENV, REGISTRY_KEY)
    recalled = registry.client.post(f"{PATH}/yank", json=body, headers=ADMIN)
    assert recalled.status_code == 201, recalled.text
    record = recalled.json()["yank"]
    published = registry.client.get("/v1/registry-key").json()
    from mendari.core.checkpoint import load_public

    assert record["signer"] == "registry" and record["key_id"] == published["key_id"]
    assert yank.problem(record, load_public(published["public_key"]), published["key_id"]) is None
    with RegistryStore(db_target) as store:
        assert store.verify()["ok"]


def test_a_deleted_version_is_never_served_and_its_bytes_are_gone(
    registry: Registry, db_target: str
) -> None:
    assert registry.publish().status_code == 201
    publisher = {"authorization": f"Bearer {registry.token}"}
    body = {"reason": "the archive carried a file it must not"}
    assert registry.client.post(f"{PATH}/delete", json=body, headers=publisher).status_code == 401
    deleted = registry.client.post(f"{PATH}/delete", json=body, headers=ADMIN)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"]["reason"] == body["reason"]
    gone = registry.client.get(f"{PATH}/archive")
    assert gone.status_code == 410 and body["reason"] in gone.text
    assert registry.client.get("/v1/search").json() == []
    assert registry.publish().status_code == 409  # its version is never published again
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        assert db.all("SELECT archive_sha256 FROM hub_archives") == []
    with RegistryStore(db_target) as store:
        assert store.verify()["ok"]
    again = registry.client.post(f"{PATH}/delete", json=body, headers=ADMIN)
    assert again.status_code == 409


def test_a_pull_shows_a_recall_and_whether_its_signature_holds(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    hub.client(token).push(pack, key)
    publisher = hub.client(token)
    publisher.yank(
        "site-a", "inbox", pack.version, reason="misroutes refills", severity="medium", key=key
    )
    with pytest.raises(HubError, match="every version of site-a/inbox is recalled"):
        pull("site-a/inbox", hub.client())
    assert main(["pull", f"site-a/inbox@{pack.version}", "--registry", hub.url]) == 0
    err = capsys.readouterr().err
    assert "RECALLED (medium) by its publisher" in err and "misroutes refills" in err
    assert "the record's signature holds; do not automate with this version" in err
    meta = hub.client().version("site-a", "inbox", pack.version)
    assert recall_of(meta, hub.client(), pack) == {
        "severity": "medium",
        "reason": "misroutes refills",
        "signer": "publisher",
        "at": meta["yank"]["at"],
        "verified": True,
        "problem": None,
        "deleted": False,
    }
    forged = {**meta, "yank": {**meta["yank"], "reason": "nothing to see"}}
    shown = recall_of(forged, hub.client(), pack)
    assert shown is not None and not shown["verified"]
    assert shown["problem"] == "its signature does not hold"
    # A real record about another pack (a registry could serve one): a recall, not verified.
    other = copy_pack(tmp_path / "other", "site-a/other")
    shown = recall_of(meta, hub.client(), other)
    assert shown is not None and shown["problem"] == "it is about another pack, version or build"
    assert recall_of({**meta, "yank": None}, hub.client(), pack) is None


def test_a_deleted_version_is_removed_from_the_cache_and_never_pulled(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The LP-18c review: a version deleted after it was pulled was still served from the cache
    with no word. A pull now refuses it and removes the cached copy."""
    import httpx

    from mendari.hub import client

    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    hub.client(token).push(pack, key)
    cached = pull(f"site-a/inbox@{pack.version}", hub.client())
    assert cached.root.is_dir()
    with httpx.Client(
        base_url=hub.url, headers={"authorization": "Bearer admin-token-for-tests"}
    ) as http:
        http.post(
            f"/v1/packs/site-a/inbox/{pack.version}/delete", json={"reason": "carried a file"}
        ).raise_for_status()
    assert main(["pull", f"site-a/inbox@{pack.version}", "--registry", hub.url]) == 1
    assert "was deleted from the registry: carried a file" in capsys.readouterr().err
    assert not cached.root.exists()
    assert not (client.packs_cache() / "site-a" / "inbox" / pack.version).exists()


def test_a_recall_body_is_read_only_after_its_sender_is_checked(registry: Registry) -> None:
    """The LP-18c review: FastAPI read and parsed a recall's body (any size) before the route
    checked the token. Now the token comes first, and the body is capped."""
    assert registry.publish().status_code == 201
    huge = b'{"record": {"x": "' + b"a" * 5_000_000 + b'"}}'
    for route in ("yank", "delete", "review"):
        refused = registry.client.post(f"{PATH}/{route}", content=huge)
        assert refused.status_code in (401, 403), (route, refused.status_code)
        too_big = registry.client.post(f"{PATH}/{route}", content=huge, headers=ADMIN)
        assert too_big.status_code == 413, route
    not_json = registry.client.post(f"{PATH}/delete", content=b"{", headers=ADMIN)
    assert not_json.status_code == 422


def test_the_yank_command_needs_an_exact_version(capsys: pytest.CaptureFixture[str]) -> None:
    key = Path(os.devnull)
    code = main(["yank", "site-a/inbox", "--reason", "x", "--severity", "low", "--key", str(key)])
    assert code == 1 and "name the exact version" in capsys.readouterr().err
