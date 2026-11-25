# SPDX-License-Identifier: Apache-2.0
"""Sites and site keys in the registry's store (LP-22d, ADR-0017): a site belongs to an
organization, is pending until a reviewer or the operator verifies it, signs with Ed25519 keys
that are never a publisher's or another site's, and every write is chained with its
organization. Synthetic sites only."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from mendari.cli import main
from mendari.errors import HubError
from mendari.hub import archive, sites
from mendari.hub.registry import RegistryStore
from mendari.hub.sites import MAX_SITE_KEYS
from tests.conftest import Registry


def _key(site_id: str) -> tuple[str, str]:
    """A new site key's public PEM and its proof of possession for `site_id`, as `mendari site
    init` makes them."""
    private_pem, public_pem = archive.generate_key()
    private = archive.load_private_key(private_pem)
    key_id = archive.key_id(private.public_key())
    proof = base64.b64encode(private.sign(sites.key_proof_bytes(site_id, key_id))).decode()
    return public_pem.decode(), proof


def _events(store: RegistryStore, prefix: str = "site.") -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT event, payload FROM hub_events ORDER BY seq")
    return [
        {"event": row["event"], **json.loads(row["payload"])}
        for row in rows
        if str(row["event"]).startswith(prefix)
    ]


def test_a_site_belongs_to_an_organization_and_is_pending_until_verified(
    registry: Registry,
) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North Health (synthetic)", by="user:1")
    assert (site["org"], site["status"], site["keys"]) == ("north-health", "pending", [])
    assert registry.client.get(f"/v1/sites/{site['id']}").status_code == 404
    verified = store.decide_site(
        site["id"], verify=True, by="user:reviewer", note="checked", by_org=None
    )
    assert (verified["status"], verified["decided_by"]) == ("verified", "user:reviewer")
    shown = registry.client.get(f"/v1/sites/{site['id']}")
    assert shown.status_code == 200
    assert shown.json() == {
        "id": site["id"],
        "name": "North Health (synthetic)",
        "status": "verified",
        "verified_at": verified["decided_at"],
        "keys": [],
    }
    with pytest.raises(HubError) as again:  # decided once
        store.decide_site(site["id"], verify=False, by="user:reviewer", note="again", by_org=None)
    assert again.value.status == 409
    refused = store.register_site(org="south-clinic", name="South (synthetic)", by="user:2")
    assert store.decide_site(refused["id"], verify=False, by="r", note="unknown", by_org=None)[
        "status"
    ] == ("refused")
    assert registry.client.get(f"/v1/sites/{refused['id']}").status_code == 404


def test_another_organization_neither_sees_nor_changes_a_site_or_its_keys(
    registry: Registry,
) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    store.add_site_key(site["id"], *_key(site["id"]), org="north-health", by="user:1")
    key_id = store.site_record(site["id"])["keys"][0]["key_id"]
    for act in (
        lambda: store.site_record(site["id"], org="south-clinic"),
        lambda: store.add_site_key(site["id"], *_key(site["id"]), org="south-clinic", by="user:9"),
        lambda: store.revoke_site_key(
            key_id, org="south-clinic", by="user:9", reason="x", compromised=False
        ),
        lambda: store.close_site(site["id"], org="south-clinic", by="user:9", reason="x"),
    ):
        with pytest.raises(HubError) as refused:
            act()
        assert refused.value.status == 404  # as a site that does not exist
        assert site["id"] not in str(refused.value)  # nor which site holds a key
    with pytest.raises(HubError) as unknown:
        store.revoke_site_key(
            "ed25519:" + "0" * 16, org="south-clinic", by="user:9", reason="x", compromised=False
        )
    with pytest.raises(HubError) as theirs:
        store.revoke_site_key(
            key_id, org="south-clinic", by="user:9", reason="x", compromised=False
        )
    assert str(unknown.value) == str(theirs.value)  # the same answer: nothing told apart
    assert store.sites(org="south-clinic") == []
    assert [s["id"] for s in store.sites(org="north-health")] == [site["id"]]
    assert store.site_record(site["id"])["keys"][0]["revoked_at"] is None


def _proof(private: archive.Ed25519PrivateKey, site_id: str) -> str:
    key_id = archive.key_id(private.public_key())
    return base64.b64encode(private.sign(sites.key_proof_bytes(site_id, key_id))).decode()


def test_a_site_key_is_ed25519_and_never_a_publishers_or_another_sites(
    registry: Registry,
) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    other = store.register_site(org="south-clinic", name="South (synthetic)", by="user:2")
    # The publisher's own key, with a proof that holds: still refused, one key one role.
    publisher = archive.public_pem(registry.key.public_key()).decode()
    with pytest.raises(HubError) as taken:
        store.add_site_key(
            site["id"], publisher, _proof(registry.key, site["id"]), org=None, by="o"
        )
    assert taken.value.status == 409 and "publisher" in str(taken.value)
    private = archive.load_private_key(archive.generate_key()[0])
    shared = archive.public_pem(private.public_key()).decode()
    store.add_site_key(site["id"], shared, _proof(private, site["id"]), org=None, by="o")
    with pytest.raises(HubError) as twice:  # its holder asks again for another site
        store.add_site_key(other["id"], shared, _proof(private, other["id"]), org=None, by="o")
    assert twice.value.status == 409
    # And a site key is never then added as a publisher's key.
    with pytest.raises(HubError) as reverse:
        store.add_key("mendari", shared)
    assert reverse.value.status == 409
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    p256 = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    for bad in (p256.decode(), "not a key", "x" * 5000):
        with pytest.raises(HubError):
            store.add_site_key(site["id"], bad, "AAAA", org=None, by="operator")


def test_a_key_is_added_only_with_its_holders_proof_and_stored_as_the_key_alone(
    registry: Registry,
) -> None:
    """The adversarial review of LP-22d: a public key alone could be registered by whoever saw
    it (to claim another site's reports, or block its registration); and the text a key came in
    was stored and served, a private key or a case's text with it."""
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    other = store.register_site(org="south-clinic", name="South (synthetic)", by="user:2")
    private = archive.load_private_key(archive.generate_key()[0])
    public = archive.public_pem(private.public_key()).decode()
    for proof in ("", "not base64!", _proof(private, other["id"]), _key(site["id"])[1]):
        with pytest.raises(HubError) as refused:
            store.add_site_key(site["id"], public, proof, org=None, by="operator")
        assert refused.value.status == 403
    private_pem = private.private_bytes(
        archive.serialization.Encoding.PEM,
        archive.serialization.PrivateFormat.PKCS8,
        archive.serialization.NoEncryption(),
    ).decode()
    sent = "Patient Jane Roe MRN 0001\n" + public + private_pem
    store.add_site_key(site["id"], sent, _proof(private, site["id"]), org=None, by="operator")
    (stored,) = store.site_record(site["id"])["keys"]
    assert stored["public_pem"] == public
    listed = json.dumps(store.sites())
    assert "Jane" not in listed and "PRIVATE" not in listed


def test_a_key_is_rotated_and_the_old_one_revoked_with_a_reason(registry: Registry) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    old = store.add_site_key(site["id"], *_key(site["id"]), org="north-health", by="user:1")[
        "key_id"
    ]
    new = store.add_site_key(site["id"], *_key(site["id"]), org="north-health", by="user:1")[
        "key_id"
    ]
    revoked = store.revoke_site_key(
        old, org="north-health", by="user:1", reason="rotated", compromised=False
    )
    keys = {key["key_id"]: key for key in revoked["keys"]}
    assert keys[old]["revoked_at"] is not None and keys[old]["compromised"] is False
    assert keys[new]["revoked_at"] is None
    with pytest.raises(HubError) as twice:
        store.revoke_site_key(old, org=None, by="operator", reason="again", compromised=True)
    assert twice.value.status == 409
    lost = store.revoke_site_key(
        new, org=None, by="operator", reason="laptop stolen", compromised=True
    )
    assert {key["key_id"]: key["compromised"] for key in lost["keys"]} == {old: False, new: True}
    (event,) = [
        e for e in _events(store) if e["event"] == "site.key_revoked" and e["key_id"] == new
    ]
    assert (event["reason"], event["compromised"], event["org"]) == (
        "laptop stolen",
        True,
        "north-health",
    )


def test_a_site_holds_five_live_keys_at_most(registry: Registry) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    ids = [
        store.add_site_key(site["id"], *_key(site["id"]), org=None, by="operator")["key_id"]
        for _ in range(MAX_SITE_KEYS)
    ]
    with pytest.raises(HubError) as full:
        store.add_site_key(site["id"], *_key(site["id"]), org=None, by="operator")
    assert full.value.status == 409
    store.revoke_site_key(ids[0], org=None, by="operator", reason="rotated", compromised=False)
    store.add_site_key(site["id"], *_key(site["id"]), org=None, by="operator")


def test_a_pending_or_closed_site_answers_as_one_that_does_not_exist(registry: Registry) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    store.add_site_key(site["id"], *_key(site["id"]), org=None, by="operator")
    assert registry.client.get(f"/v1/sites/{site['id']}").status_code == 404
    store.decide_site(site["id"], verify=True, by="user:reviewer", note="checked", by_org=None)
    shown = registry.client.get(f"/v1/sites/{site['id']}").json()
    assert [set(key) for key in shown["keys"]] == [
        {"key_id", "created_at", "revoked_at", "compromised"}  # never the org or who added it
    ]
    assert "org" not in shown
    closed = store.close_site(site["id"], org="north-health", by="user:1", reason="merged")
    assert closed["status"] == "closed"
    answer = registry.client.get(f"/v1/sites/{site['id']}")
    assert answer.status_code == 404 and "north" not in answer.text.lower()
    assert registry.client.get("/v1/sites/0123456789abcdef").status_code == 404
    with pytest.raises(HubError):  # a closed site takes no key
        store.add_site_key(site["id"], *_key(site["id"]), org=None, by="operator")


def test_registration_verification_keys_and_revocation_are_chained_with_the_organization(
    registry: Registry,
) -> None:
    store = registry.operator
    site = store.register_site(org="north-health", name="North (synthetic)", by="user:1")
    key_id = store.add_site_key(site["id"], *_key(site["id"]), org="north-health", by="user:1")[
        "key_id"
    ]
    store.decide_site(
        site["id"], verify=True, by="user:reviewer", note="checked by phone", by_org=None
    )
    store.revoke_site_key(key_id, org="north-health", by="user:1", reason="x", compromised=False)
    other = store.register_site(org="south-clinic", name="South (synthetic)", by="user:2")
    store.decide_site(other["id"], verify=False, by="user:reviewer", note="unknown", by_org=None)
    store.close_site(site["id"], org=None, by="operator", reason="test")
    events = _events(store)
    assert [e["event"] for e in events] == [
        "site.registered",
        "site.key_added",
        "site.verified",
        "site.key_revoked",
        "site.registered",
        "site.refused",
        "site.closed",
    ]
    assert all(e["org"] in ("north-health", "south-clinic") for e in events)
    assert all(e["row"]["kind"] in ("sites", "site_keys") for e in events)
    assert store.verify()["ok"]


def test_the_operators_site_records_operator_and_why(
    registry: Registry, db_target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    public = tmp_path / "site-key.json"

    def key_file(site_id: str) -> None:
        pem, proof = _key(site_id)
        public.write_text(json.dumps({"site": site_id, "public_pem": pem, "proof": proof}))

    def run(*args: str) -> Any:
        capsys.readouterr()
        assert main(["registry", *args, "--db", db_target]) == 0
        return json.loads(capsys.readouterr().out)

    site = run("site", "--org", "north-health", "--name", "North (synthetic)", "--reason", "pilot")
    assert site["status"] == "pending"
    key_file(site["id"])
    run("site-key", "--site", site["id"], "--key", str(public), "--reason", "first key")
    verified = run("site-verify", "--site", site["id"], "--reason", "called their CMIO")
    assert verified["status"] == "verified" and verified["decided_by"].startswith("command line")
    listed = run("sites")
    assert [s["id"] for s in listed] == [site["id"]]
    key_id = listed[0]["keys"][0]["key_id"]
    run("site-revoke-key", key_id, "--reason", "rotated")
    events = _events(registry.operator)
    registered = events[0]
    assert registered["reason"] == "pilot" and registered["by"].startswith("command line")
    assert [e["event"] for e in events] == [
        "site.registered",
        "site.key_added",
        "site.verified",
        "site.key_revoked",
    ]
    assert events[2]["note"] == "called their CMIO"
    assert events[3]["compromised"] is False
