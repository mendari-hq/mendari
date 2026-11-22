# SPDX-License-Identifier: Apache-2.0
"""The registry mounted under the engine, and the people's routes (LP-19d, ADR-0015).

`MENDARI_REGISTRY=on` serves the registry at `/registry/v1/...` on the engine's app and
database, and (with sign-in on) lets an organization's administrators claim namespaces, register
publisher keys and mint, list and revoke publisher tokens at `/api/registry/...`. Unset, nothing
is mounted and no registry table is made. The mounted registry reads no session cookie and no
engine credential: publishing is by publisher token only. On SQLite and Postgres."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from mendari import AuditStore
from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.hub import archive
from mendari.hub.registry import HSTS, REGISTRY_HEADERS, REVIEWER, RegistryStore, _rate_class
from mendari.server import registry_routes
from mendari.server.app import create_app
from mendari.server.auth import person_id
from tests.conftest import encrypt_mode, encrypted_store, member, security_lines
from tests.test_backend_services import RSA_KEY, ask, assertion
from tests.test_hub_client import copy_pack

APP = "https://mendari.test"
IDP = "https://idp.example.org"
SAME_ORIGIN = {"sec-fetch-site": "same-origin", "origin": APP}
CROSS_SITE = {"sec-fetch-site": "cross-site", "origin": "https://evil.example"}
PEOPLE_ROUTES = (
    ("GET", "/api/registry/namespaces"),
    ("POST", "/api/registry/namespaces"),
    ("POST", "/api/registry/namespaces/acme/keys"),
    ("GET", "/api/registry/tokens"),
    ("POST", "/api/registry/tokens"),
    ("DELETE", "/api/registry/tokens/0123456789abcdef"),
)


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def sign_in_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")


@dataclass
class World:
    app: FastAPI
    http: TestClient
    db: str
    tmp_path: Path

    @property
    def registry(self) -> RegistryStore:
        store: RegistryStore = self.app.state.registry_store
        return store

    def person(
        self, org: str | None = "acme", role: str = "admin", sub: str = ""
    ) -> dict[str, str]:
        with encrypted_store(self.db) as store:
            return member(store, org=org, roles=[role], sub=sub or f"{org}-{role}")

    def claim(self, name: str = "acme", org: str = "acme") -> dict[str, Any]:
        made = self.http.post(
            "/api/registry/namespaces",
            json={"name": name, "display_name": name.title()},
            headers=self.person(org),
        )
        assert made.status_code == 201, made.text
        claimed: dict[str, Any] = made.json()
        return claimed

    def key(self, namespace: str = "acme", org: str = "acme") -> archive.Ed25519PrivateKey:
        private, public = archive.generate_key()
        added = self.http.post(
            f"/api/registry/namespaces/{namespace}/keys",
            json={"public_pem": public.decode()},
            headers=self.person(org),
        )
        assert added.status_code == 201, added.text
        return archive.load_private_key(private)

    def mint(self, namespace: str = "acme", org: str = "acme", **more: Any) -> dict[str, Any]:
        made = self.http.post(
            "/api/registry/tokens",
            json={"namespace": namespace, "name": "ci", **more},
            headers=self.person(org),
        )
        assert made.status_code == 201, made.text
        minted: dict[str, Any] = made.json()
        return minted

    def pack(self, pack_id: str = "acme/inbox") -> Pack:
        return copy_pack(self.tmp_path, pack_id)

    def publish(self, pack: Pack, key: Any, headers: dict[str, str]) -> Any:
        namespace, name = pack.id.split("/")
        return self.http.put(
            f"/registry/v1/packs/{namespace}/{name}/{pack.version}",
            content=archive.build(pack, key),
            headers=headers,
        )

    def published(self) -> tuple[str, Pack]:
        """`acme` claimed, with a key, a token and its first version published: the token."""
        self.claim()
        key = self.key()
        token = str(self.mint()["token"])
        pack = self.pack()
        answer = self.publish(pack, key, bearer(token))
        assert answer.status_code == 201, answer.text
        return token, pack

    def events(self) -> list[dict[str, Any]]:
        with self.registry._db.session(exclusive=False) as db:
            rows = db.all("SELECT event, payload FROM hub_events ORDER BY seq")
        return [{"event": row["event"], **json.loads(row["payload"])} for row in rows]


@contextmanager
def served(db: str, tmp_path: Path) -> Iterator[World]:
    app = create_app(db_path=db)
    try:
        with TestClient(app, base_url=APP) as http:
            yield World(app, http, db, tmp_path)
    finally:
        store: RegistryStore | None = getattr(app.state, "registry_store", None)
        if store is not None:
            store.close()


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    with served(db_target, tmp_path) as served_world:
        yield served_world


def test_without_the_switch_nothing_is_mounted_and_no_hub_table_is_made(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch)
    for unset in ("", "off"):
        monkeypatch.setenv("MENDARI_REGISTRY", unset)
        with served(db_target, tmp_path) as off:
            admin = off.person("default")
            assert not hasattr(off.app.state, "registry_store")
            # Signed out, each path reads as any route no rule lists: the engine as it was.
            nowhere = off.http.get("/nowhere/v1/health")
            mount = off.http.get("/registry/v1/health")
            assert (mount.status_code, mount.json()["detail"]) == (401, nowhere.json()["detail"])
            assert off.http.put("/registry/v1/packs/a/b/1.0.0", content=b"x").status_code == 401
            assert off.http.get("/registry/v1/health", headers=admin).status_code == 404
            for method, path in PEOPLE_ROUTES:
                assert off.http.request(method, path, json={}).status_code == 401, path
                assert off.http.request(method, path, json={}, headers=admin).status_code == 404, (
                    path
                )
            # Nor does another site's page reach it as a public route (the origin check).
            crossed = off.http.get("/registry/v1/health", headers={**admin, **CROSS_SITE})
            assert crossed.status_code == 403
            viewer = off.person("default", "viewer")
            assert off.http.post("/api/registry/tokens", json={}, headers=viewer).status_code == 403
            assert "registry" not in off.http.get("/api/health", headers=admin).json()
            schema = off.http.get("/openapi.json", headers=admin).json()
            assert not [
                path for path in schema["paths"] if path.startswith(("/registry", "/api/registry/"))
            ]
            assert "publisherToken" not in schema["components"]["securitySchemes"]
            with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
                for table in ("hub_namespaces", "hub_events", "hub_publisher_tokens", "hub_claims"):
                    assert db.columns(table) == set(), table
    monkeypatch.setenv("MENDARI_REGISTRY", "yes")
    with pytest.raises(MendariError, match="MENDARI_REGISTRY is on or off"):
        create_app(db_path=db_target)


def test_a_member_claims_mints_publishes_and_only_to_the_claimed_namespace(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = world.person()
    claimed = world.claim()
    assert (claimed["namespace"], claimed["status"], claimed["listed"]) == (
        "acme",
        "pending",
        False,
    )
    who = person_id(IDP, "acme-admin")
    assert claimed["claimed_by"] == who and "org" not in claimed
    key = world.key()
    minted = world.mint(days=30)
    token = minted["token"]
    assert token.startswith("ohr_") and minted["namespace"] == "acme"
    assert (minted["org"], minted["owner"]) == ("acme", who)
    listed = world.http.get("/api/registry/tokens", headers=admin).json()
    assert [t["id"] for t in listed] == [minted["id"]] and token not in json.dumps(listed)
    assert listed[0]["last_used_day"] is None

    published = world.publish(world.pack(), key, bearer(token))
    assert published.status_code == 201, published.text
    # Only to the claimed namespace: another one, claimed by nobody or by someone else.
    world.claim("other", org="other")
    for elsewhere in ("other/inbox", "nobody/inbox"):
        refused = world.publish(world.pack(elsewhere), key, bearer(token))
        assert refused.status_code == 403, refused.text
    (space,) = world.http.get("/api/registry/namespaces", headers=admin).json()
    assert space["status"] == "pending" and [k["key_id"] for k in space["keys"]] == [key_id(key)]
    assert world.http.get("/api/registry/tokens", headers=admin).json()[0]["last_used_day"]

    revoked = world.http.delete(f"/api/registry/tokens/{minted['id']}", headers=admin)
    assert revoked.status_code == 204
    again = copy_pack(world.tmp_path, "acme/inbox", "9.9.9")
    assert world.publish(again, key, bearer(token)).status_code == 403

    # The registry's chain names the person by id and the organization; never an address.
    events = world.events()
    mine = [e for e in events if e.get("org") == "acme"]
    assert [e["event"] for e in mine] == [
        "namespace.created",
        "namespace.claimed",
        "token.issued",
        "pack.published",
        "token.revoked",
    ]
    # Each by the person, but the publish: by the token, named by its id.
    assert [e["by"] for e in mine] == [who, who, who, f"token:{minted['id']}", who]
    assert [e["by"] for e in events if e["event"] == "key.added"] == [who]
    assert "example.org" not in json.dumps(events) and token not in json.dumps(events)
    # The security log: ids only, and the route's template under the mount.
    out = capsys.readouterr().out
    assert token not in out
    lines = security_lines(out)
    logged = {line["event"]: line for line in lines if line.get("org") == "acme"}
    for event in (
        "admin.registry_namespace.claimed",
        "admin.registry_key.added",
        "admin.registry_token.created",
        "admin.registry_token.revoked",
    ):
        assert (logged[event]["actor"], logged[event]["org"]) == (who, "acme"), event
    assert logged["admin.registry_token.created"]["ids"] == [minted["id"]]
    routes = {line.get("route") for line in lines if line["event"] == "request"}
    assert "/registry/v1/packs/{namespace}/{name}/{version}" in routes
    assert not [route for route in routes if route and "acme" in route]


def key_id(key: archive.Ed25519PrivateKey) -> str:
    return archive.key_id(key.public_key())


def test_a_session_cookie_is_not_a_publish_credential(world: World) -> None:
    world.claim()
    key = world.key()
    pack = world.pack()
    for headers in (world.person(), {**world.person(), **SAME_ORIGIN}):
        refused = world.publish(pack, key, headers)
        assert refused.status_code == 403, refused.text
        added = world.http.post(
            "/registry/v1/namespaces/acme/keys",
            json={"public_pem": archive.generate_key()[1].decode()},
            headers=headers,
        )
        assert added.status_code == 403
        # Nor does it read what only the namespace's own token reads: a pending namespace.
        assert world.http.get("/registry/v1/namespaces/acme", headers=headers).status_code == 404
    assert "pack.published" not in [e["event"] for e in world.events()]


def test_the_mounted_registry_has_no_administrator_token(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared administrator token is gone (LP-19g): under the engine, the old variable set
    makes no route and no credential."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_ADMIN_TOKEN", "an-administrator-token")
    with served(db_target, tmp_path) as world:
        admin = bearer("an-administrator-token")
        made = world.http.post("/registry/v1/namespaces", json={"name": "taken"}, headers=admin)
        assert made.status_code == 404  # no such route
        world.claim()
        assert world.publish(world.pack(), world.key(), admin).status_code == 403
        assert world.http.get("/registry/v1/namespaces/acme", headers=admin).status_code == 404


def test_an_engine_key_is_refused_by_the_registry_and_a_publisher_token_by_the_engine(
    world: World,
) -> None:
    token, _ = world.published()
    made = world.http.post(
        "/api/auth/keys", json={"name": "machine", "role": "service"}, headers=world.person()
    )
    assert made.status_code == 201, made.text
    engine_key = bearer(made.json()["key"])
    assert world.http.get("/api/packs", headers=engine_key).status_code == 200
    again = copy_pack(world.tmp_path, "acme/inbox", "9.9.9")
    assert world.publish(again, world.key(), engine_key).status_code == 403
    # It reads as the public does: the pending namespace does not exist for it.
    assert world.http.get("/registry/v1/namespaces/acme", headers=engine_key).status_code == 404
    assert world.http.get("/registry/v1/namespaces/acme", headers=bearer(token)).status_code == 200
    for path in ("/api/packs", "/api/audit", "/api/registry/tokens", "/api/auth/keys"):
        refused = world.http.get(path, headers=bearer(token))
        assert refused.status_code == 401, path
    minted = world.http.post(
        "/api/registry/tokens", json={"namespace": "acme", "name": "x"}, headers=bearer(token)
    )
    assert minted.status_code == 401


def test_a_key_or_a_client_cannot_mint_a_publisher_token(world: World) -> None:
    """People only: a credential a machine holds never makes another that outlives it."""
    world.claim()
    admin = world.person()
    credentials = []
    for role in ("viewer", "service"):
        made = world.http.post("/api/auth/keys", json={"name": role, "role": role}, headers=admin)
        credentials.append(bearer(made.json()["key"]))
    registered = world.http.post(
        "/api/auth/clients",
        json={"name": "backend", "role": "service", "jwks": {"keys": [RSA_KEY.jwk]}},
        headers=admin,
    )
    assert registered.status_code == 201, registered.text
    granted = ask(world.http, assertion(RSA_KEY, registered.json()["client_id"]))
    assert granted.status_code == 200, granted.text
    credentials.append(bearer(granted.json()["access_token"]))
    body = {"namespace": "acme", "name": "ci", "public_pem": "x" * 50}
    for headers in credentials:
        for method, path in PEOPLE_ROUTES:
            refused = world.http.request(method, path, json=body, headers=headers)
            assert refused.status_code == 403, (method, path, refused.text)
    assert world.registry.tokens() == []


def test_a_viewer_cannot_claim_or_mint(world: World) -> None:
    world.claim()
    body = {"namespace": "acme", "name": "ci", "public_pem": "x" * 50}
    for role in ("viewer", "reviewer", "governance", "service"):
        for method, path in PEOPLE_ROUTES:
            refused = world.http.request(method, path, json=body, headers=world.person(role=role))
            assert refused.status_code == 403, (role, method, path)
    for method, path in PEOPLE_ROUTES:
        assert world.http.request(method, path, json=body).status_code == 401, path
    assert world.registry.tokens() == [] and len(world.registry.claims()) == 1


def test_one_organization_never_sees_or_revokes_anothers_tokens_or_claims(world: World) -> None:
    token, _ = world.published()
    (minted,) = world.registry.tokens(org="acme")
    other = world.person("other")
    assert world.http.get("/api/registry/namespaces", headers=other).json() == []
    assert world.http.get("/api/registry/tokens", headers=other).json() == []
    pem = archive.generate_key()[1].decode()
    answers = []
    for name in ("acme", "nobody"):  # another organization's, and one that does not exist
        key = world.http.post(
            f"/api/registry/namespaces/{name}/keys", json={"public_pem": pem}, headers=other
        )
        mint = world.http.post(
            "/api/registry/tokens", json={"namespace": name, "name": "x"}, headers=other
        )
        assert (key.status_code, mint.status_code) == (404, 404)
        for answer in (key, mint):
            said = {k: v for k, v in answer.json().items() if k != "request_id"}
            answers.append(json.dumps(said).replace(name, "NAME"))
    assert answers[:2] == answers[2:]
    # A body that names another organization changes nothing: the guard's organization acts.
    sneaky = world.http.post(
        "/api/registry/tokens",
        json={"namespace": "acme", "name": "x", "org": "acme", "owner": minted["owner"]},
        headers=other,
    )
    assert sneaky.status_code == 404
    revoked = world.http.delete(f"/api/registry/tokens/{minted['id']}", headers=other)
    unknown = world.http.delete("/api/registry/tokens/0123456789abcdef", headers=other)
    assert (revoked.status_code, unknown.status_code) == (404, 404)
    assert revoked.json()["detail"] == unknown.json()["detail"]
    assert (
        world.http.post(
            "/api/registry/namespaces", json={"name": "acme"}, headers=other
        ).status_code
        == 409
    )  # taken: never by whom
    # Nothing of acme's changed, and its token still works.
    assert world.registry.tokens(org="acme")[0]["revoked_at"] is None
    assert len(world.registry.tokens()) == 1 and world.registry.claims(org="other") == []
    assert world.http.get("/registry/v1/namespaces/acme", headers=bearer(token)).status_code == 200


def test_a_person_of_no_organization_is_refused(world: World) -> None:
    world.claim()
    nobody = world.person(org=None, sub="nobody")
    body = {"namespace": "acme", "name": "ci", "public_pem": "x" * 50}
    for method, path in PEOPLE_ROUTES:
        refused = world.http.request(method, path, json=body, headers=nobody)
        assert refused.status_code == 403, (method, path)
        assert refused.json()["error"] == "NoOrganization"
    assert world.registry.tokens() == [] and len(world.registry.claims()) == 1


def carries_the_registrys_headers(answer: Any) -> None:
    for name, value in {**REGISTRY_HEADERS, "Strict-Transport-Security": HSTS}.items():
        assert answer.headers[name] == value, (answer.request.url.path, name)
    assert "set-cookie" not in answer.headers


def test_public_reads_need_no_sign_in_and_carry_the_registrys_headers(world: World) -> None:
    token, pack = world.published()
    version = f"/registry/v1/packs/acme/inbox/{pack.version}"
    assert world.http.get(version).status_code == 404  # pending: invisible to the public
    world.registry.decide_claim("acme", approve=True, by="operator")
    # As the Hub's server reads (fetch metadata from another site), a browser with a session
    # on another site's page, and with nothing at all.
    for headers in ({}, CROSS_SITE, {**world.person("other"), **CROSS_SITE}):
        for path in (
            "/registry/v1/health",
            "/registry/v1/search?q=inbox",
            "/registry/v1/namespaces/acme",
            "/registry/v1/packs/acme/inbox",
            version,
            "/registry/v1/yanks",
            "/registry/v1/chain/head",
        ):
            answer = world.http.get(path, headers=headers)
            assert answer.status_code == 200, (path, answer.text)
            assert answer.headers["content-type"] == "application/json"
            carries_the_registrys_headers(answer)
    assert world.http.get("/registry/v1/health").json()["packs"] == 1
    data = world.http.get(f"{version}/archive", headers=CROSS_SITE)
    assert data.status_code == 200 and data.headers["content-type"] == "application/octet-stream"
    assert data.headers["content-disposition"] == "attachment"
    carries_the_registrys_headers(data)
    # Every other kind of answer: unknown, invalid, refused, not allowed, and what the engine
    # itself answers under the mount (another site's write, refused before the registry).
    kinds = {
        404: world.http.get("/registry/v1/packs/acme/none"),
        422: world.http.get("/registry/v1/search?risk=high"),
        403: world.http.put(version, content=b"x", headers=bearer("ohr_made-up")),
        401: world.http.post(f"{version}/reports", json={}, headers=bearer("ohr_made-up")),
        405: world.http.delete("/registry/v1/health", headers=bearer(token)),
        409: world.http.put(
            version, content=archive.build(pack, world.key()), headers=bearer(token)
        ),
    }
    for status, answer in kinds.items():
        assert answer.status_code == status, answer.text
        carries_the_registrys_headers(answer)
    engines = world.http.put(version, content=b"x", headers={**world.person(), **CROSS_SITE})
    assert engines.status_code == 403 and "cross-site" in engines.json()["detail"]
    carries_the_registrys_headers(engines)
    for missing in ("/registry/docs", "/registry/redoc", "/registry/openapi.json"):
        assert world.http.get(missing).status_code == 404  # no page of the registry's own
    # Signed out, the engine's health keeps its three fields; a signed-in reader learns the
    # registry is on, and the API reference describes its routes.
    assert set(world.http.get("/api/health").json()) == {"status", "version", "sign_in"}
    reader = world.person(role="viewer")
    assert world.http.get("/api/health", headers=reader).json()["registry"] is True
    schema = world.http.get("/openapi.json", headers=reader).json()
    publish = schema["paths"]["/registry/v1/packs/{namespace}/{name}/{version}"]["put"]
    assert publish["security"] == [{"publisherToken": []}]
    assert schema["paths"]["/registry/v1/search"]["get"]["security"] == []
    assert schema["paths"]["/api/registry/tokens"]["post"]["security"] == [{"session": []}]
    keys = schema["paths"]["/registry/v1/namespaces/{namespace}/keys"]
    assert "#/components/schemas/Registry" in json.dumps(keys)
    assert "RegistryKeyRequest" in schema["components"]["schemas"]
    # A namespace's keys are described, each with `revoked_at` (null while it is live).
    answer = schema["paths"]["/registry/v1/namespaces/{namespace}"]["get"]["responses"]["200"]
    assert answer["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RegistryNamespaceAnswer"
    }
    listed = schema["components"]["schemas"]["RegistryPublisherKey"]
    assert (
        "revoked_at" in listed["required"]
        and "revoked" in listed["properties"]["revoked_at"]["description"]
    )


def test_an_archive_over_the_engines_body_limit_is_a_413_that_names_it(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_MAX_BODY_BYTES", str(64 * 1024))
    with served(db_target, tmp_path) as world:
        world.claim()
        token = bearer(world.mint()["token"])
        path = "/registry/v1/packs/acme/inbox/1.0.0"
        large = b"\0" * (64 * 1024 + 1)

        def chunks() -> Iterator[bytes]:  # no declared length: counted as it arrives
            yield large[: 40 * 1024]
            yield large[40 * 1024 :]

        for answer in (
            world.http.put(path, content=large, headers=token),
            world.http.put(path, content=chunks(), headers=token),
        ):
            assert answer.status_code == 413, answer.text
            assert answer.json()["detail"] == "a request body is 65536 bytes at most"
            carries_the_registrys_headers(answer)
        assert "pack.published" not in [e["event"] for e in world.events()]


def test_the_registry_chain_and_the_audit_chain_both_verify_in_one_database(world: World) -> None:
    world.published()
    world.registry.decide_claim("acme", approve=True, by="operator")
    made = world.http.post(
        "/api/auth/keys", json={"name": "machine", "role": "viewer"}, headers=world.person()
    )
    assert made.status_code == 201
    checked = world.registry.verify()
    assert checked["ok"] is True and checked["events"] >= 6, checked
    with encrypted_store(world.db) as store:
        audit = store.verify()
        with store._db.session(exclusive=False) as db:
            kinds = {str(row["event"]) for row in db.all("SELECT event FROM audit_events")}
    assert audit["ok"] is True, audit
    # One chain per store: the registry's events are in its own, never written twice.
    assert "key.created" in kinds and not kinds & {"token.issued", "namespace.claimed"}
    assert "key.created" not in {event["event"] for event in world.events()}


def test_another_sites_page_cannot_claim_or_mint(world: World) -> None:
    """The origin check (`server/origin.py`): a signed-in administrator's browser on another
    site's page reaches none of the people's routes, to write or to read."""
    world.claim()
    minted = world.mint()
    admin = {key: value for key, value in world.person().items() if key == "cookie"}
    body = {"name": "evil", "namespace": "acme", "public_pem": "x" * 50}
    foreign = (
        CROSS_SITE,
        {"sec-fetch-site": "same-site", "origin": "https://evil.mendari.test"},
        {"origin": "https://evil.example"},  # a browser without fetch metadata
        {"origin": "null"},
    )
    routes = (*PEOPLE_ROUTES[:5], ("DELETE", f"/api/registry/tokens/{minted['id']}"))
    for headers in foreign:
        for method, path in routes:
            refused = world.http.request(method, path, json=body, headers={**admin, **headers})
            assert refused.status_code == 403, (headers, method, path)
            assert refused.json()["detail"] == "cross-site request refused"
    # A link another site's page sends the browser to (no fetch metadata, no `Origin`).
    assert world.http.get("/api/registry/tokens", headers=admin).status_code == 403
    assert [t["revoked_at"] for t in world.registry.tokens()] == [None]
    assert [c["namespace"] for c in world.registry.claims()] == ["acme"]
    # The same requests from Mendari's own page pass.
    own = {**admin, **SAME_ORIGIN}
    assert world.http.get("/api/registry/tokens", headers=own).status_code == 200
    assert world.http.delete(routes[5][1], headers=own).status_code == 204


def test_what_a_person_sends_is_checked_before_anything_is_written(world: World) -> None:
    admin = world.person()
    world.claim()
    refused: dict[int, list[tuple[str, str, dict[str, Any] | None]]] = {
        422: [
            ("POST", "/api/registry/namespaces", {"name": "Not A Name"}),
            ("POST", "/api/registry/namespaces", {"name": "mendari"}),  # reserved
            ("POST", "/api/registry/namespaces", {"name": "mendari-labs"}),
            ("POST", "/api/registry/namespaces", {"name": "mendari\n"}),  # not the reserved name's
            ("POST", "/api/registry/namespaces", {"name": "ok\n"}),
            ("POST", "/api/registry/tokens", {"namespace": "acme\n", "name": "x"}),
            ("POST", "/api/registry/namespaces", {"name": "ok", "display_name": "a‮b"}),
            ("POST", "/api/registry/tokens", {"namespace": "acme", "name": "x", "days": 366}),
            ("POST", "/api/registry/tokens", {"namespace": "acme", "name": "x", "days": 0}),
            ("POST", "/api/registry/tokens", {"namespace": "acme", "name": "a\nb"}),
            ("POST", "/api/registry/namespaces/acme/keys", {"public_pem": "x" * 50}),
        ],
        409: [("POST", "/api/registry/namespaces", {"name": "acme"})],
        404: [("DELETE", "/api/registry/tokens/not-an-id", None)],
    }
    before = world.events()
    for status, requests in refused.items():
        for method, path, body in requests:
            answer = world.http.request(method, path, json=body, headers=admin)
            assert answer.status_code == status, (path, body, answer.text)
            assert answer.headers["cache-control"] == "no-store"
    assert world.events() == before
    # A refused claim's namespace takes no new key and no new token from its people. (A token
    # minted while it was pending still writes there, as LP-19c left it: nothing of a refused
    # namespace is ever listed.)
    world.registry.decide_claim("acme", approve=False, by="operator")
    for path, sent in (
        ("/api/registry/tokens", {"namespace": "acme", "name": "x"}),
        ("/api/registry/namespaces/acme/keys", {"public_pem": archive.generate_key()[1].decode()}),
    ):
        assert world.http.post(path, json=sent, headers=admin).status_code == 409, path
    assert world.registry.tokens() == []


def test_an_organization_makes_a_bounded_number_of_tokens_and_keys(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each is a row and a chained event for good, and a token is a budget of writes."""
    monkeypatch.setattr(registry_routes, "MAX_LIVE_TOKENS", 2)
    monkeypatch.setattr(registry_routes, "MAX_TOKENS_A_DAY", 3)
    monkeypatch.setattr(registry_routes, "MAX_KEYS", 2)
    admin = world.person()
    world.claim()
    body = {"namespace": "acme", "name": "ci"}
    first, _ = world.mint(), world.mint()
    full = world.http.post("/api/registry/tokens", json=body, headers=admin)
    assert full.status_code == 409 and "2 working tokens" in full.json()["detail"]
    # Revoking one makes room, until the day's count: revoking and minting again is bounded.
    assert (
        world.http.delete(f"/api/registry/tokens/{first['id']}", headers=admin).status_code == 204
    )
    world.mint()
    assert (
        world.http.delete(f"/api/registry/tokens/{first['id']}", headers=admin).status_code == 204
    )
    (live, _) = [t for t in world.registry.tokens() if t["revoked_at"] is None]
    assert world.http.delete(f"/api/registry/tokens/{live['id']}", headers=admin).status_code == 204
    over = world.http.post("/api/registry/tokens", json=body, headers=admin)
    assert over.status_code == 429 and "3 tokens a day" in over.json()["detail"]
    assert len(world.registry.tokens()) == 3
    world.key(), world.key()
    third = world.http.post(
        "/api/registry/namespaces/acme/keys",
        json={"public_pem": archive.generate_key()[1].decode()},
        headers=admin,
    )
    assert third.status_code == 409 and "2 publisher keys" in third.json()["detail"]


def test_requests_at_once_never_pass_an_organizations_caps(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caps on keys, working tokens, tokens a day and sites were counted in a read before
    the write: requests at once all read a count under the cap, and all wrote. Each write is
    slowed here so the reads land together; the count is made in the write's own exclusive
    session, so no cap is passed however many come at once."""
    monkeypatch.setattr(registry_routes, "MAX_KEYS", 2)
    monkeypatch.setattr(registry_routes, "MAX_LIVE_TOKENS", 2)
    monkeypatch.setattr(registry_routes, "MAX_SITES", 2)
    admin = world.person()
    world.claim()
    backend = world.registry._db
    plain = backend.session

    @contextmanager
    def slowed(*, exclusive: bool) -> Iterator[Any]:
        with plain(exclusive=exclusive) as db:
            if exclusive:
                time.sleep(0.05)
            yield db

    monkeypatch.setattr(backend, "session", slowed)

    def at_once(path: str, bodies: list[dict[str, Any]]) -> list[int]:
        with ThreadPoolExecutor(len(bodies)) as pool:
            sent = [pool.submit(world.http.post, path, json=b, headers=admin) for b in bodies]
            return sorted(future.result().status_code for future in sent)

    keys = [{"public_pem": archive.generate_key()[1].decode()} for _ in range(6)]
    assert at_once("/api/registry/namespaces/acme/keys", keys) == [201] * 2 + [409] * 4
    assert len(world.registry.namespace("acme", REVIEWER)["keys"]) == 2
    tokens = [{"namespace": "acme", "name": f"ci{n}"} for n in range(6)]
    assert at_once("/api/registry/tokens", tokens) == [201] * 2 + [409] * 4
    assert len(world.registry.tokens(org="acme")) == 2
    monkeypatch.setattr(registry_routes, "MAX_LIVE_TOKENS", 100)
    monkeypatch.setattr(registry_routes, "MAX_TOKENS_A_DAY", 4)
    assert at_once("/api/registry/tokens", tokens) == [201] * 2 + [429] * 4
    assert len(world.registry.tokens(org="acme")) == 4
    sites = [{"name": f"Site {n}"} for n in range(6)]
    assert at_once("/api/registry/sites", sites) == [201] * 2 + [409] * 4
    assert len(world.registry.sites(org="acme")) == 2


def test_a_namespaces_tokens_share_one_write_budget(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's write limit is per namespace: a fresh token is not a fresh budget."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_write=2/h:2")
    with served(db_target, tmp_path) as world:
        world.claim()
        world.claim("other", org="other")
        path = "/registry/v1/packs/{}/inbox/1.0.0"
        tokens = [bearer(world.mint()["token"]) for _ in range(3)]
        answers = [
            world.http.put(path.format("acme"), content=b"x", headers=token).status_code
            for token in tokens
        ]
        assert answers == [422, 422, 429]
        other = bearer(world.mint("other", org="other")["token"])  # another namespace's own
        assert world.http.put(path.format("other"), content=b"x", headers=other).status_code == 422


def test_a_site_report_is_counted_as_one_under_the_mount() -> None:
    """The registry's limits read the path as its own routes see it, whatever it is mounted
    under: a report keeps its own bucket."""

    def classed(root: str, path: str) -> str:
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": f"{root}{path}",
            "root_path": root,
            "headers": [],
            "query_string": b"",
            "client": ("192.0.2.1", 1),
            "server": ("mendari.test", 443),
            "scheme": "https",
        }
        return _rate_class(Request(scope), lambda _: None)[0]

    for root in ("", "/registry"):
        assert classed(root, "/v1/packs/acme/inbox/1.0.0/reports") == "registry_report"
        assert classed(root, "/v1/packs/acme/inbox/1.0.0/yank") == "registry_write"


def test_the_mounted_registry_counter_signs_and_a_malformed_key_is_a_clean_503(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LP-19i under the mount: with the registry's key, an approved namespace's publish is
    counter-signed and the key is served; a key that is set and is not one refuses the publish
    as the registry answers it, with the registry's headers, and stores nothing."""
    token, pack = world.published()
    version = f"/registry/v1/packs/acme/inbox/{pack.version}"
    assert world.http.get("/registry/v1/registry-key").status_code == 404  # no key: none served
    monkeypatch.setenv("MENDARI_REGISTRY_SIGNING_KEY", base64.b64encode(bytes(range(32))).decode())
    world.registry.decide_claim("acme", approve=True, by="operator")
    shown = world.http.get("/registry/v1/registry-key")
    assert shown.status_code == 200 and shown.json()["key_id"]
    carries_the_registrys_headers(shown)
    first = world.http.get(version)
    carries_the_registrys_headers(first)
    assert first.json()["checked"]["accepted"]["key_id"] == shown.json()["key_id"]
    key = world.key()
    later = world.publish(copy_pack(world.tmp_path, "acme/inbox", "9.9.8"), key, bearer(token))
    assert later.status_code == 201, later.text
    assert later.json()["checked"]["accepted"]["key_id"] == shown.json()["key_id"]
    monkeypatch.setenv("MENDARI_REGISTRY_SIGNING_KEY", "not-a-key")
    before = world.events()
    refused = world.publish(copy_pack(world.tmp_path, "acme/inbox", "9.9.9"), key, bearer(token))
    assert refused.status_code == 503, refused.text
    assert "not-a-key" not in refused.text
    carries_the_registrys_headers(refused)
    assert world.events() == before
