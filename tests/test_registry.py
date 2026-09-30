"""The registry service (hub/registry.py), on SQLite and Postgres."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openhealth.core.registry import resolve_pack
from openhealth.hub import archive
from openhealth.hub.registry import RegistryStore, create_registry_app

ADMIN = "admin-token-for-tests"
REFERRAL = resolve_pack("openhealth/referral-router")
GUARD = resolve_pack("openhealth/ai-guard")


@dataclass
class Registry:
    client: TestClient
    token: str  # the openhealth namespace's publisher token
    key: archive.Ed25519PrivateKey

    def publish(
        self,
        pack: Any = REFERRAL,
        *,
        key: Any = None,
        token: str | None = None,
        version: str | None = None,
    ) -> Any:
        data = archive.build(pack, key or self.key)
        namespace, name = pack.id.split("/")
        return self.client.put(
            f"/v1/packs/{namespace}/{name}/{version or pack.version}",
            content=data,
            headers={"authorization": f"Bearer {token or self.token}"},
        )


@pytest.fixture
def registry(db_target: str) -> Iterator[Registry]:
    client = TestClient(create_registry_app(db_target, admin_token=ADMIN))
    client.__enter__()  # runs the app's lifespan, so its store is closed at the end
    admin = {"authorization": f"Bearer {ADMIN}"}
    assert (
        client.post(
            "/v1/namespaces",
            json={"name": "openhealth", "display_name": "OpenHealth"},
            headers=admin,
        ).status_code
        == 201
    )
    token = client.post("/v1/namespaces/openhealth/tokens", headers=admin).json()["token"]
    private, public = archive.generate_key()
    added = client.post(
        "/v1/namespaces/openhealth/keys",
        json={"public_pem": public.decode()},
        headers={"authorization": f"Bearer {token}"},
    )
    assert added.status_code == 201
    yield Registry(client, token, archive.load_private_key(private))
    client.__exit__(None, None, None)


def test_a_signed_pack_is_published_once_and_read_back(registry: Registry) -> None:
    published = registry.publish()
    assert published.status_code == 201, published.text
    body = published.json()
    assert (body["id"], body["version"], body["digest"]) == (
        "openhealth/referral-router",
        "0.1.1",
        REFERRAL.digest,
    )
    assert body["risk"] == {"level": 1, "category": "clinical information"}
    assert "CDS Hooks order-sign" in body["standards"] and "FHIR R4" in body["standards"]
    assert body["benchmark"]["accuracy"] is not None and body["card_markdown"]
    again = registry.publish()
    assert again.status_code == 409 and "immutable" in again.text
    pack = registry.client.get("/v1/packs/openhealth/referral-router").json()
    assert pack["versions"] == ["0.1.1"] and pack["latest"]["digest"] == REFERRAL.digest


def test_the_archive_downloads_byte_for_byte(registry: Registry) -> None:
    data = archive.build(REFERRAL, registry.key)
    registry.publish()
    response = registry.client.get("/v1/packs/openhealth/referral-router/0.1.1/archive")
    assert response.content == data
    assert response.headers["x-archive-sha256"] == hashlib.sha256(data).hexdigest()


def test_publishing_needs_the_namespaces_token(registry: Registry) -> None:
    admin = {"authorization": f"Bearer {ADMIN}"}
    registry.client.post("/v1/namespaces", json={"name": "other"}, headers=admin)
    other_token = registry.client.post("/v1/namespaces/other/tokens", headers=admin).json()["token"]
    assert registry.publish(token=other_token).status_code == 403
    assert registry.publish(token="ohr_made_up").status_code == 403


def test_an_archive_signed_by_an_unregistered_key_is_refused(registry: Registry) -> None:
    stranger = archive.load_private_key(archive.generate_key()[0])
    refused = registry.publish(key=stranger)
    assert refused.status_code == 422 and "not trusted" in refused.text


def test_the_archive_must_be_the_pack_and_version_in_the_url(registry: Registry) -> None:
    refused = registry.publish(version="9.9.9")
    assert refused.status_code == 422 and "not this pack and version" in refused.text


def test_admin_routes_need_the_admin_token(registry: Registry) -> None:
    assert registry.client.post("/v1/namespaces", json={"name": "x"}).status_code == 401
    publisher = {"authorization": f"Bearer {registry.token}"}
    assert (
        registry.client.post("/v1/namespaces", json={"name": "x"}, headers=publisher).status_code
        == 401
    )
    assert (
        registry.client.post("/v1/namespaces/openhealth/tokens", headers=publisher).status_code
        == 401
    )


def test_verification_sets_the_publisher_type(registry: Registry) -> None:
    registry.publish()
    admin = {"authorization": f"Bearer {ADMIN}"}
    patched = registry.client.patch(
        "/v1/namespaces/openhealth", json={"publisher_type": "verified"}, headers=admin
    )
    assert patched.json()["publisher_type"] == "verified"
    pack = registry.client.get("/v1/packs/openhealth/referral-router").json()
    assert pack["latest"]["publisher_type"] == "verified"
    namespace = registry.client.get("/v1/namespaces/openhealth").json()
    assert namespace["packs"] == ["openhealth/referral-router"]
    assert namespace["keys"][0]["key_id"] == archive.key_id(registry.key.public_key())
    found = registry.client.get("/v1/search", params={"publisher_type": "verified"}).json()
    assert [p["id"] for p in found] == ["openhealth/referral-router"]


def test_search_by_words_risk_and_standard(registry: Registry) -> None:
    registry.publish()
    registry.publish(GUARD)

    def ids(**params: Any) -> list[str]:
        return [p["id"] for p in registry.client.get("/v1/search", params=params).json()]

    assert ids() == ["openhealth/ai-guard", "openhealth/referral-router"]
    assert ids(q="referral specialty") == ["openhealth/referral-router"]
    assert ids(risk=2) == ["openhealth/ai-guard"]
    assert ids(standard="cds hooks order-sign") == ["openhealth/referral-router"]
    assert ids(q="nothing-like-this") == []
    assert "card_markdown" not in registry.client.get("/v1/search").json()[0]


def test_unknown_things_are_404(registry: Registry) -> None:
    assert registry.client.get("/v1/packs/openhealth/none").status_code == 404
    assert registry.client.get("/v1/packs/openhealth/referral-router/0.0.1").status_code == 404
    assert registry.client.get("/v1/namespaces/nobody").status_code == 404


def test_tokens_are_stored_hashed(registry: Registry, db_target: str) -> None:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        stored = [row["token_sha256"] for row in db.all("SELECT token_sha256 FROM hub_tokens")]
    assert stored == [hashlib.sha256(registry.token.encode()).hexdigest()]
    assert registry.client.get("/v1/health").json()["status"] == "ok"


def test_sites_share_metrics_and_compare_with_the_benchmark(
    registry: Registry, db_target: str
) -> None:
    registry.publish()
    report = json.loads((REFERRAL.root / "evals" / "benchmark.json").read_text())
    publisher = {"authorization": f"Bearer {registry.token}"}
    path = "/v1/packs/openhealth/referral-router/0.1.1/reports"
    assert registry.client.post(path, json={"site": "site-a", "report": report}).status_code == 401
    shared = registry.client.post(
        path, json={"site": "Site A", "report": report}, headers=publisher
    )
    assert shared.status_code == 201, shared.text
    assert shared.json()["accuracy"] == report["questions"]["next_step"]["accuracy"]
    other = {**report, "pack": {**report["pack"], "version": "0.0.9"}}
    assert (
        registry.client.post(
            path, json={"site": "x", "report": other}, headers=publisher
        ).status_code
        == 422
    )
    compared = registry.client.get("/v1/packs/openhealth/referral-router/reports").json()
    assert [s["site"] for s in compared["sites"]] == ["Site A"]
    assert compared["benchmark"]["0.1.1"]["accuracy"] is not None
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        stored = db.one("SELECT report FROM hub_reports")
    assert stored is not None and "misclassified" not in stored["report"]  # only the shareable part
