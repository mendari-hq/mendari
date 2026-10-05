"""The registry service (hub/registry.py), on SQLite and Postgres."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.core.pack import Pack, load_pack, pack_digest
from mendari.core.registry import resolve_pack
from mendari.hub import archive
from mendari.hub import registry as registry_module
from mendari.hub.registry import RegistryStore, create_registry_app

ADMIN = "admin-token-for-tests"
REFERRAL = resolve_pack("mendari/referral-router")
GUARD = resolve_pack("mendari/ai-guard")


@dataclass
class Registry:
    client: TestClient
    token: str  # the mendari namespace's publisher token
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
            json={"name": "mendari", "display_name": "Mendari"},
            headers=admin,
        ).status_code
        == 201
    )
    token = client.post("/v1/namespaces/mendari/tokens", headers=admin).json()["token"]
    private, public = archive.generate_key()
    added = client.post(
        "/v1/namespaces/mendari/keys",
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
        "mendari/referral-router",
        REFERRAL.version,
        REFERRAL.digest,
    )
    assert body["risk"] == {"level": 1, "category": "clinical information"}
    assert "CDS Hooks order-sign" in body["standards"] and "FHIR R4" in body["standards"]
    assert body["benchmark"]["accuracy"] is not None and body["card_markdown"]
    again = registry.publish()
    assert again.status_code == 409 and "immutable" in again.text
    pack = registry.client.get("/v1/packs/mendari/referral-router").json()
    assert pack["versions"] == [REFERRAL.version] and pack["latest"]["digest"] == REFERRAL.digest


def test_the_archive_downloads_byte_for_byte(registry: Registry) -> None:
    data = archive.build(REFERRAL, registry.key)
    registry.publish()
    response = registry.client.get(f"/v1/packs/mendari/referral-router/{REFERRAL.version}/archive")
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
        registry.client.post("/v1/namespaces/mendari/tokens", headers=publisher).status_code == 401
    )


def test_verification_sets_the_publisher_type(registry: Registry) -> None:
    registry.publish()
    admin = {"authorization": f"Bearer {ADMIN}"}
    patched = registry.client.patch(
        "/v1/namespaces/mendari", json={"publisher_type": "verified"}, headers=admin
    )
    assert patched.json()["publisher_type"] == "verified"
    pack = registry.client.get("/v1/packs/mendari/referral-router").json()
    assert pack["latest"]["publisher_type"] == "verified"
    namespace = registry.client.get("/v1/namespaces/mendari").json()
    assert namespace["packs"] == ["mendari/referral-router"]
    assert namespace["keys"][0]["key_id"] == archive.key_id(registry.key.public_key())
    found = registry.client.get("/v1/search", params={"publisher_type": "verified"}).json()
    assert [p["id"] for p in found] == ["mendari/referral-router"]


def test_search_by_words_risk_and_standard(registry: Registry) -> None:
    registry.publish()
    registry.publish(GUARD)

    def ids(**params: Any) -> list[str]:
        return [p["id"] for p in registry.client.get("/v1/search", params=params).json()]

    assert ids() == ["mendari/ai-guard", "mendari/referral-router"]
    assert ids(q="referral specialty") == ["mendari/referral-router"]
    assert ids(risk=2) == ["mendari/ai-guard"]
    assert ids(standard="cds hooks order-sign") == ["mendari/referral-router"]
    assert ids(q="nothing-like-this") == []
    assert "card_markdown" not in registry.client.get("/v1/search").json()[0]


def test_unknown_things_are_404(registry: Registry) -> None:
    assert registry.client.get("/v1/packs/mendari/none").status_code == 404
    assert registry.client.get("/v1/packs/mendari/referral-router/0.0.1").status_code == 404
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
    path = f"/v1/packs/mendari/referral-router/{REFERRAL.version}/reports"
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
    compared = registry.client.get("/v1/packs/mendari/referral-router/reports").json()
    assert [s["site"] for s in compared["sites"]] == ["Site A"]
    assert compared["benchmark"][REFERRAL.version]["accuracy"] is not None
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        stored = db.one("SELECT report FROM hub_reports")
    assert stored is not None and "misclassified" not in stored["report"]  # only the shareable part


# -- a publisher's content is refused with a reason, never a server error ------------------------


def test_a_pack_that_fails_to_load_in_any_way_is_refused_not_a_server_error(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: only a Mendari error from loading the published pack was caught; any other (raised by
    the publisher's content) was a 500. It is refused, named by its type alone."""

    def load(*args: Any, **options: Any) -> Any:
        raise RecursionError("text from the pack")

    monkeypatch.setattr(archive, "load_pack", load)
    refused = registry.publish()
    assert refused.status_code == 422, refused.text
    assert "the pack does not load (RecursionError)" in refused.text
    assert "text from the pack" not in refused.text


@pytest.mark.parametrize(
    ("relative", "content", "reason"),
    [
        ("evals/benchmark.json", b"[1]", None),
        ("evals/benchmark.json", b'{"questions": [1], "dataset": "x"}', None),
        ("evals/benchmark.json", b"[" * 100_000 + b"]" * 100_000, None),
        ("DECISION_CARD.md", b"\xff\xfe junk", "DECISION_CARD.md is not UTF-8 text"),
    ],
    ids=["benchmark-a-list", "benchmark-odd-shapes", "benchmark-too-deep", "card-not-utf8"],
)
def test_malformed_attestations_never_make_a_publish_a_server_error(
    registry: Registry, tmp_path: Path, relative: str, content: bytes, reason: str | None
) -> None:
    """T12: `describe` read evals/benchmark.json and DECISION_CARD.md outside the guarded block,
    so a list, a nested document or a card that is not UTF-8 made the publish a 500. A malformed
    benchmark is shown as none; a card that is not text is refused with its reason."""
    root = tmp_path / "pack"
    shutil.copytree(REFERRAL.root, root)
    (root / relative).write_bytes(content)
    answer = registry.publish(load_pack(root))
    if reason is None:
        assert answer.status_code == 201, answer.text
        shown = answer.json()["benchmark"]
        assert shown is None or shown == {"dataset": None, "n": None, "accuracy": None}
    else:
        assert answer.status_code == 422, answer.text
        assert reason in answer.text


def test_an_over_long_version_is_refused_and_search_keeps_working(
    registry: Registry, tmp_path: Path
) -> None:
    """T10: a pack whose manifest named a version of 4,400 digits was stored, after which
    `GET /v1/search` compared it with `int()` and answered 500 for every caller."""
    assert registry.publish().status_code == 201
    huge = "1" + "0" * 4400 + ".0.0"
    root = tmp_path / "pack"
    shutil.copytree(REFERRAL.root, root)
    manifest = (
        (root / "pack.yaml")
        .read_text()
        .replace(f"version: {REFERRAL.version}", f"version: '{huge}'", 1)
    )
    (root / "pack.yaml").write_text(manifest)
    signed = Pack(
        manifest=REFERRAL.manifest.model_copy(update={"version": huge}),
        root=root,
        digest=pack_digest(root),
        input_schema=REFERRAL.input_schema,
    )
    refused = registry.publish(signed)
    assert refused.status_code == 422, refused.text
    search = registry.client.get("/v1/search")
    assert search.status_code == 200
    assert [item["version"] for item in search.json()] == [REFERRAL.version]


def _nested(depth: int) -> Any:
    value: Any = 1
    for _ in range(depth):
        value = [value]
    return value


def test_a_deeply_nested_attestation_is_shown_as_none_and_search_keeps_working(
    registry: Registry, tmp_path: Path
) -> None:
    """`describe` stored `dataset.name`, `n`, `accuracy` and the leaderboard as any JSON value.
    One nested 300 levels deep was committed, then the response serializer gave up (about 255
    levels): the publish was a 500, and so was `GET /v1/search` for everyone, for good."""
    assert registry.publish(GUARD).status_code == 201
    root = tmp_path / "pack"
    shutil.copytree(REFERRAL.root, root)
    benchmark = json.loads((root / "evals" / "benchmark.json").read_text())
    benchmark["dataset"] = {"name": _nested(300)}
    primary = benchmark["questions"][REFERRAL.manifest.policy.primary]
    primary["n"], primary["accuracy"] = True, float("nan")
    (root / "evals" / "benchmark.json").write_text(json.dumps(benchmark))
    (root / "evals" / "leaderboard.json").write_text(json.dumps({"rows": _nested(300)}))
    answer = registry.publish(load_pack(root))
    assert answer.status_code == 201, answer.text
    assert answer.json()["benchmark"] == {"dataset": None, "n": None, "accuracy": None}
    assert answer.json()["leaderboard"] is None
    assert registry.client.get("/v1/search").status_code == 200
    for path in ("", f"/{REFERRAL.version}"):
        assert registry.client.get(f"/v1/packs/{REFERRAL.id}{path}").status_code == 200


def test_metadata_that_cannot_be_served_is_refused_before_it_is_stored(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def describe(pack: Any, verified: Any) -> dict[str, Any]:
        return {"pack_name": "x", "deep": _nested(300)}

    monkeypatch.setattr(registry_module, "describe", describe)
    refused = registry.publish()
    assert refused.status_code == 422, refused.text
    assert "the pack's metadata cannot be served (PydanticSerializationError)" in refused.text
    monkeypatch.undo()
    assert registry.client.get("/v1/search").json() == []
    assert registry.publish().status_code == 201


def test_a_shipped_benchmark_and_leaderboard_are_shown_as_they_are(registry: Registry) -> None:
    shown = registry.publish().json()
    benchmark = json.loads(REFERRAL.attestation("evals/benchmark.json").read_text())
    primary = benchmark["questions"][REFERRAL.manifest.policy.primary]
    assert shown["benchmark"] == {
        "dataset": benchmark["dataset"]["name"],
        "n": primary["n"],
        "accuracy": primary["accuracy"],
    }
    leaderboard = REFERRAL.attestation("evals/leaderboard.json")
    assert shown["leaderboard"] == json.loads(leaderboard.read_text())
