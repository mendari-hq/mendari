# SPDX-License-Identifier: Apache-2.0
"""Finding an agent in the deployment's registry and adding it to an organization from the
console (`server/hub_routes.py`, `PackStore.admit_pulled`).

What the console is told of a registry is rebuilt field by field, so a registry's words reach a
page as text of a known kind and length. A version that is added is verified as `mendari pull`
verifies one, kept in its organization's own store (whichever instance answers next), and
governed as every agent from a registry is: adding is not approving. A tampered archive, a
wrong signature, a recalled or deleted version, a revoked key, a changed key and a registry that
does not answer each add nothing."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from mendari.core.pack import Pack, load_pack
from mendari.errors import HubError, MendariError
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from mendari.server import hub_routes
from mendari.server.app import create_app
from tests.conftest import DASHBOARD, DEMO_AGENT_DIR, TRIAGE_DIR, member, security_lines
from tests.support.registry import HTTPS
from tests.test_hub_client import Hub, free_port, hub  # noqa: F401 - the live registry fixture
from tests.test_store_uploads import ORG_A, ORG_B, Store, store_world

ACME = "acme/portal-agent"
OWN = load_pack(TRIAGE_DIR).id  # an agent this deployment serves from its own files
REFILL = {"message": {"body": "Could I get a refill of my lisinopril please?"}}
# What the local agent service answers each of the bundled agent's questions with.
ANSWERS = {"request": "refill", "urgent_symptoms": "no"}


def as_published(base: Path, pack_id: str = ACME, version: str | None = None) -> Pack:
    """The bundled agent under a publisher's own id (and version), as a publisher puts one in a
    registry: it asks a service of its own (an `agent` variant) and carries canned answers."""
    target = base / "published" / pack_id.replace("/", "-") / (version or "as-shipped")
    shutil.copytree(DEMO_AGENT_DIR, target)
    manifest = yaml.safe_load((target / "agent.yaml").read_text())
    manifest["id"] = pack_id
    if version is not None:
        manifest["version"] = version
    (target / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return load_pack(target)


def answered(body: Any) -> dict[str, Any]:
    """What an agent's service answers a request with: each question asked, confidently."""
    answers = {
        question["key"]: {"value": ANSWERS[question["key"]], "confidence": 0.97}
        for question in body["questions"]
    }
    return {"answers": answers, "model": "local-test-agent", "agent_version": "9.9.9"}


class Service:
    """An agent's service on this machine: it answers every question of the bundled agent
    (`ANSWERS`, confidently) and keeps what each request carried. `status` other than 200 makes
    it refuse, in words of its own that no answer of Mendari's may repeat (`SAID`); `answer`,
    when set, is what it answers instead."""

    SAID = "zq-remote-words-of-the-service"

    def __init__(self, port: int) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.url = f"{self.origin}/decide"
        self.seen: list[dict[str, Any]] = []  # each request's Authorization header and body
        self.status = 200
        self.answer: Any = None

    def authorizations(self) -> list[str | None]:
        return [call["authorization"] for call in self.seen]


@contextmanager
def serving() -> Iterator[Service]:
    """A `Service` listening on a free port of this machine, until the block ends."""
    port = free_port()
    made = Service(port)
    app = FastAPI()

    @app.post("/decide")
    async def decide(request: Request) -> JSONResponse:
        body = await request.json()
        made.seen.append({"authorization": request.headers.get("authorization"), "body": body})
        if made.status != 200:
            return JSONResponse({"detail": Service.SAID}, status_code=made.status)
        return JSONResponse(made.answer if made.answer is not None else answered(body))

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            with httpx.Client() as probe:
                probe.get(f"{made.origin}/openapi.json").raise_for_status()
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        yield made
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def service() -> Iterator[Service]:
    with serving() as made:
        yield made


@dataclass
class Adopting:
    """A memberships-mode server with organizations' agent stores on and a registry named, the
    registry itself, and the engine's database."""

    store: Store
    hub: Hub
    target: str
    tmp: Path
    publishers: dict[str, tuple[str, Any]] = field(default_factory=dict)  # namespace: token, key

    @property
    def client(self) -> TestClient:
        return self.store.client

    def as_(self, org: str, role: str) -> dict[str, str]:
        return self.store.as_(org, role)

    def publisher(self, namespace: str) -> tuple[str, Any]:
        if namespace not in self.publishers:
            self.publishers[namespace] = self.hub.namespace(namespace)
        return self.publishers[namespace]

    def publish(self, pack_id: str = ACME, version: str | None = None) -> Pack:
        """The bundled agent published under `pack_id` by its namespace's publisher."""
        token, key = self.publisher(pack_id.split("/")[0])
        pack = as_published(self.tmp, pack_id, version)
        self.hub.client(token).push(pack, key)
        return pack

    def pull(self, ref: str = ACME, org: str = ORG_A, who: str = "pack_author", **body: Any) -> Any:
        return self.client.post(
            "/api/store/pulls", json={"ref": ref, **body}, headers=self.as_(org, who)
        )

    def versions(self) -> list[dict[str, Any]]:
        """Every row of every organization's store."""
        with self.store.db._db.session(exclusive=False) as db:
            return [dict(row) for row in db.all("SELECT * FROM store_versions ORDER BY created_at")]

    def kinds(self) -> list[str]:
        with self.store.db._db.session(exclusive=False) as db:
            return [row["event"] for row in db.all("SELECT event FROM audit_events ORDER BY seq")]

    def nothing_was_added(self) -> None:
        """No organization's store holds a version, none is listed, none is served, and no
        event says one was made."""
        assert self.versions() == []
        assert "pack_version.created" not in self.kinds()
        for org in (ORG_A, ORG_B):
            listed = self.client.get("/api/store/agents", headers=self.as_(org, "pack_author"))
            assert listed.status_code == 200 and listed.json() == []
            served = self.client.get("/api/agents", headers=self.as_(org, "viewer")).json()
            assert ACME not in {agent["id"] for agent in served}
        assert self.store.db.verify()["ok"]


def adopting(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: Hub, url: str = ""
) -> Iterator[Adopting]:
    """A world whose server names `registry` (or `url`, an address of the test's own)."""
    monkeypatch.setenv("MENDARI_REGISTRY_URL", url or registry.url)
    for store in store_world(target, tmp_path, monkeypatch):
        yield Adopting(store, registry, target, tmp_path)


@pytest.fixture
def world(
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> Iterator[Adopting]:
    yield from adopting(db_target, tmp_path, monkeypatch, hub)


@pytest.fixture
def lite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> Iterator[Adopting]:
    """The same world on SQLite only, for what does not depend on the database."""
    yield from adopting(str(tmp_path / "audit.db"), tmp_path, monkeypatch, hub)


def evaluated(world: Adopting, digest: str, org: str = ORG_A) -> str:
    """An evaluation of a store version on the examples it carries, run as a store job by the
    organization's author: its report's id."""
    author = world.as_(org, "pack_author")
    made = world.client.post(
        "/api/store/jobs", json={"version": digest, "dataset": {"name": "examples"}}, headers=author
    )
    assert made.status_code == 201, made.text
    for _ in range(200):
        step = world.client.post(f"/api/store/jobs/{made.json()['id']}/advance", headers=author)
        assert step.status_code == 200, step.text
        if step.json()["state"] in ("done", "failed"):
            assert step.json()["state"] == "done", step.text
            return str(step.json()["report_id"])
    raise AssertionError("the job never finished")


def promote(world: Adopting, digest: str, org: str = ORG_A, who: str = "governance") -> Any:
    return world.client.post(
        "/api/store/promotions",
        json={
            "version": digest,
            "reports": [evaluated(world, digest, org)],
            "reference": "AI committee, item 2",
            "automate": True,
        },
        headers=world.as_(org, who),
    )


def counting(monkeypatch: pytest.MonkeyPatch, method: str) -> list[Any]:
    """The calls of one of the registry client's methods from now on (it still answers)."""
    asked: list[Any] = []
    real = getattr(RegistryClient, method)

    def counted(self: RegistryClient, *args: Any, **kwargs: Any) -> Any:
        asked.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(RegistryClient, method, counted)
    return asked


def decide(world: Adopting, org: str = ORG_A, pack_id: str = ACME) -> Any:
    return world.client.post(
        f"/api/agents/{pack_id}/decide", json=REFILL, headers=world.as_(org, "reviewer")
    )


# -- what the console is told ---------------------------------------------------------------------


def test_the_console_is_told_whether_a_registry_is_named_and_whether_agents_can_be_added(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> None:
    """No registry named: `GET /api/hub` says so and nothing else is mounted. A registry and no
    agent store (open mode here): it can be searched, and nothing can be added. Both: adding is
    offered."""
    with TestClient(create_app(db_path=tmp_path / "none.db")) as client:
        assert client.get("/api/hub").json() == {
            "registry": None,
            "add": {"available": False, "reason": "no_registry"},
        }
        assert client.get("/api/hub/search").status_code == 404
        assert client.post("/api/store/pulls", json={"ref": ACME}).status_code == 404
    monkeypatch.setenv("MENDARI_REGISTRY_URL", hub.url + "/")
    with TestClient(create_app(db_path=tmp_path / "open.db")) as client:
        assert client.get("/api/hub").json() == {
            "registry": hub.url,
            "add": {"available": False, "reason": "no_store"},
        }
        assert client.get("/api/hub/search").json()["agents"] == []
        assert client.post("/api/store/pulls", json={"ref": ACME}).status_code == 404
    for store in store_world(str(tmp_path / "store.db"), tmp_path, monkeypatch):
        shown = store.client.get("/api/hub", headers=store.as_(ORG_A, "viewer"))
        assert shown.json() == {"registry": hub.url, "add": {"available": True, "reason": None}}
        assert store.client.get("/api/hub").status_code == 401  # a reader's, never the public's


@pytest.mark.parametrize(
    "address",
    [
        "http://hub.example.org",
        "https://someone:secret-zq@hub.example.org",
        "ftp://hub.example.org",
    ],
)
def test_a_registry_address_that_is_not_https_stops_the_server_and_is_never_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_URL", address)
    with pytest.raises(MendariError, match="MENDARI_REGISTRY_URL") as refused:
        create_app(db_path=tmp_path / "bad.db")
    assert "hub.example.org" not in str(refused.value) and "secret-zq" not in str(refused.value)


def test_a_search_and_a_listing_say_what_the_registry_checked_apart_from_what_is_claimed(
    lite: Adopting,
) -> None:
    pack = lite.publish()
    viewer = lite.as_(ORG_A, "viewer")
    found = lite.client.get("/api/hub/search", params={"q": "portal"}, headers=viewer).json()
    assert found["registry"] == lite.hub.url and found["more"] is False
    [agent] = found["agents"]
    assert (agent["id"], agent["version"], agent["name"]) == (
        ACME,
        pack.version,
        pack.manifest.name,
    )
    assert agent["checked"]["signature"]["verified"] is True
    assert agent["checked"]["digest"] == pack.digest
    assert agent["checked"]["review"] == {
        "status": "unreviewed",
        "risk_level": None,
        "matches_declared": None,
    }
    assert agent["claimed"]["risk"] == {"level": 0, "category": "administrative"}
    assert agent["claimed"]["publisher"] == {"name": "Mendari", "type": "community"}
    assert agent["recall"] is None and agent["installed"] is None
    assert (
        lite.client.get("/api/hub/search", params={"q": "zq-nothing"}, headers=viewer).json()[
            "agents"
        ]
        == []
    )

    shown = lite.client.get(f"/api/hub/agents/{ACME}", headers=viewer).json()
    assert shown["versions"] == [pack.version] and shown["recalled"] == []
    assert shown["agent"]["checked"] == agent["checked"]
    assert shown["agent"]["card_markdown"].startswith("# Decision Card: Portal Message Agent")
    assert shown["agent"]["evaluations"] == {"published": 0, "listed": []}
    exact = lite.client.get(
        f"/api/hub/agents/{ACME}", params={"version": pack.version}, headers=viewer
    )
    assert exact.json()["agent"]["version"] == pack.version
    for missing in (
        "/api/hub/agents/acme/no-such-agent",
        f"/api/hub/agents/{ACME}?version=9.9.9",
        "/api/hub/agents/Acme/portal-agent",  # not an agent's name: never sent to the registry
        f"/api/hub/agents/{ACME}?version=..%2F..",
    ):
        refused = lite.client.get(missing, headers=viewer)
        assert refused.status_code == 404, missing
        assert refused.json()["detail"] == hub_routes.NOT_LISTED


HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


def test_what_a_registry_says_reaches_the_console_as_text_of_a_known_kind_and_length(
    lite: Adopting, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that answers markup, fields of the wrong kind, fields nobody asked for and
    text without end: the console's answer holds the fields this server names, each one a
    string cut to its length, a number in its range, a flag, or nothing."""
    listed = {
        "id": ACME,
        "pack_name": HOSTILE * 100,
        "version": "1.0.0",
        "published_at": {"$date": "now"},
        "summary": HOSTILE,
        "html": "<b>raw</b>",
        "standards": ["FHIR R4", {"x": 1}, HOSTILE * 10, *["s"] * 100],
        "checked": {
            "signature": {"algorithm": ["Ed25519"], "key_id": HOSTILE, "verified": "yes"},
            "digest": 7,
            "engine": "bounded",
            "accepted": "true",
            "review": {"status": HOSTILE, "risk_level": 99, "matches_declared": "no"},
            "script": HOSTILE,
        },
        "claimed": {
            "publisher": {"name": HOSTILE, "type": {"a": 1}, "url": "javascript:alert(3)"},
            "risk": {"level": "0", "category": HOSTILE * 100},
            "license": None,
        },
        "yank": {"severity": HOSTILE, "reason": HOSTILE * 100, "signer": 1, "at": HOSTILE},
    }
    monkeypatch.setattr(RegistryClient, "search", lambda self, **filters: [listed, "x", 3, None])
    viewer = lite.as_(ORG_A, "viewer")
    [agent] = lite.client.get("/api/hub/search", headers=viewer).json()["agents"]
    assert set(agent) == {
        "id", "name", "version", "published_at", "summary", "standards", "checked", "claimed",
        "recall", "installed",
    }  # fmt: skip
    assert agent["name"] == (HOSTILE * 100)[:200] and agent["published_at"] is None
    assert agent["standards"] == ["FHIR R4", (HOSTILE * 10)[:60], *["s"] * 18]
    assert agent["checked"]["signature"] == {
        "algorithm": None,
        "key_id": HOSTILE[:60],
        "verified": False,  # only the registry's own `true` counts
    }
    assert agent["checked"]["digest"] is None and agent["checked"]["countersigned"] is False
    assert agent["checked"]["review"] == {
        "status": "unreviewed",
        "risk_level": None,
        "matches_declared": None,
    }
    assert set(agent["checked"]) == {
        "signature", "digest", "archive_sha256", "engine", "status_label", "publisher_type",
        "countersigned", "review",
    }  # fmt: skip
    assert agent["claimed"] == {
        "publisher": {"name": HOSTILE, "type": None},
        "risk": {"level": None, "category": (HOSTILE * 100)[:100]},
        "license": None,
    }
    assert agent["recall"] == {
        "severity": "unknown",
        "reason": (HOSTILE * 100)[:500],
        "signer": None,
        "at": HOSTILE[:40],
        "deleted": False,
    }

    def record(self: RegistryClient, namespace: str, name: str) -> dict[str, Any]:
        return {
            "latest": {**listed, "card_markdown": HOSTILE * 100_000},
            "versions": ["1.0.0", 2, HOSTILE * 10],
            "recalled": [{"version": HOSTILE * 10, "yank": {"severity": "high"}}, "x"],
        }

    monkeypatch.setattr(RegistryClient, "agent", record)
    monkeypatch.setattr(
        RegistryClient,
        "evaluations",
        lambda self, *version: {"evaluations": [{"dataset": {"name": HOSTILE * 9, "n": -4}}, 5]},
    )
    shown = lite.client.get(f"/api/hub/agents/{ACME}", headers=viewer).json()
    assert shown["versions"] == ["1.0.0", (HOSTILE * 10)[:60]]
    assert shown["recalled"] == [
        {
            "version": (HOSTILE * 10)[:60],
            "severity": "high",
            "reason": None,
            "signer": None,
            "at": None,
            "deleted": False,
        }
    ]
    assert len(shown["agent"]["card_markdown"]) == hub_routes.MAX_CARD_CHARS
    assert shown["agent"]["evaluations"] == {
        "published": 2,
        "listed": [{"dataset": (HOSTILE * 9)[:100], "cases": None, "models": [], "rerun": None}],
    }
    assert "html" not in json.dumps(shown) and "javascript:" not in json.dumps(shown)


def test_a_registry_that_does_not_answer_is_one_refusal_and_none_of_its_words_are_repeated(
    lite: Adopting, monkeypatch: pytest.MonkeyPatch
) -> None:
    viewer = lite.as_(ORG_A, "viewer")
    said = "zq-the-registrys-own-words <script>"

    def failing(error: Exception) -> Any:
        def search(self: RegistryClient, **filters: Any) -> list[dict[str, Any]]:
            raise error

        return search

    for error, status, words in (
        (httpx.ConnectError(said), 502, hub_routes.NO_ANSWER),
        (httpx.ReadTimeout(said), 502, hub_routes.NO_ANSWER),
        (HubError(f"the registry answered HTTP 500: {said}", 500), 502, hub_routes.NOT_A_REGISTRY),
        (HubError(said, 404), 404, hub_routes.NOT_LISTED),
        (ValueError(said), 502, hub_routes.NOT_A_REGISTRY),
    ):
        monkeypatch.setattr(RegistryClient, "search", failing(error))
        refused = lite.client.get("/api/hub/search", headers=viewer)
        assert refused.status_code == status, error
        assert refused.json()["detail"] == words and "zq-" not in refused.text


# -- adding ----------------------------------------------------------------------------------------


def test_an_added_version_is_verified_and_kept_in_its_organizations_store_across_instances(
    world: Adopting,
) -> None:
    """The round trip, on SQLite and Postgres: published to a registry, added from the console,
    verified, kept as a draft of the organization that added it with the record of where it
    came from, chained; and there for a new instance on the same database, which was told
    nothing but the database."""
    pack = world.publish()
    key_id = archive.key_id(world.publisher("acme")[1].public_key())
    added = world.pull()
    assert added.status_code == 201, added.text
    made = added.json()
    assert (made["pack_id"], made["version"], made["digest"]) == (ACME, pack.version, pack.digest)
    assert (made["state"], made["source"], made["valid"]) == ("draft", "hub", True)
    assert made["pulled"]["registry"] == world.hub.url
    assert (made["pulled"]["namespace"], made["pulled"]["key_id"]) == ("acme", key_id)
    assert made["pulled"]["digest"] == pack.digest
    [row] = world.versions()
    assert (row["org"], row["source"], row["state"]) == (ORG_A, "hub", "draft")
    assert json.loads(row["pulled"])["archive_sha256"] == made["pulled"]["archive_sha256"]
    with world.store.db._db.session(exclusive=False) as db:
        [event] = db.all("SELECT payload FROM audit_events WHERE event = 'pack_version.created'")
        [pin] = db.all("SELECT * FROM store_pins")
    payload = json.loads(event["payload"])
    assert (payload["org"], payload["source"], payload["digest"]) == (ORG_A, "hub", pack.digest)
    assert payload["pulled"] == {
        "registry": world.hub.url,
        "namespace": "acme",
        "key_id": key_id,
        "archive_sha256": made["pulled"]["archive_sha256"],
        "accepted": None,
    }
    assert (pin["org"], pin["registry"], pin["name"]) == (ORG_A, world.hub.url, "acme")
    assert json.loads(pin["value"]) == [key_id]
    assert world.store.db.verify()["ok"]
    # A draft: it answers nobody, and the list of served agents does not name it.
    assert decide(world).status_code == 404
    author, viewer = world.as_(ORG_A, "pack_author"), world.as_(ORG_A, "viewer")
    assert ACME not in {a["id"] for a in world.client.get("/api/agents", headers=viewer).json()}

    with TestClient(create_app(db_path=world.target), base_url=HTTPS, headers=DASHBOARD) as another:
        [lineage] = another.get("/api/store/agents", headers=author).json()
        assert lineage["pack_id"] == ACME
        assert [v["digest"] for v in lineage["versions"]] == [pack.digest]
        read = another.get(f"/api/store/versions/{pack.digest}", headers=author)
        assert read.status_code == 200 and read.json()["pulled"]["key_id"] == key_id
        manifest = another.get(
            f"/api/store/versions/{pack.digest}/file", params={"path": "agent.yaml"}, headers=author
        )
        assert manifest.status_code == 200 and f"id: {ACME}" in manifest.text
        [found] = another.get("/api/hub/search", headers=viewer).json()["agents"]
        assert found["installed"] == {
            "version": pack.version,
            "digest": pack.digest,
            "state": "draft",
        }
        again = another.post("/api/store/pulls", json={"ref": ACME}, headers=author)
        assert again.status_code == 409 and "already" in again.json()["detail"]
    assert len(world.versions()) == 1


def test_one_organizations_added_agent_is_invisible_to_another_which_adds_its_own(
    lite: Adopting,
) -> None:
    pack = lite.publish()
    assert lite.pull().status_code == 201
    theirs = lite.as_(ORG_B, "pack_author")
    assert lite.client.get("/api/store/agents", headers=theirs).json() == []
    assert lite.client.get(f"/api/store/versions/{pack.digest}", headers=theirs).status_code == 404
    [found] = lite.client.get("/api/hub/search", headers=lite.as_(ORG_B, "viewer")).json()["agents"]
    assert found["installed"] is None
    shown = lite.client.get(f"/api/hub/agents/{ACME}", headers=lite.as_(ORG_B, "viewer")).json()
    assert shown["installed"] is None
    # A body or a query that names the other organization is not honoured.
    named = lite.client.post(
        "/api/store/pulls", params={"org": ORG_A}, json={"ref": ACME, "org": ORG_A}, headers=theirs
    )
    assert named.status_code == 201
    assert sorted(row["org"] for row in lite.versions()) == [ORG_A, ORG_B]
    with lite.store.db._db.session(exclusive=False) as db:
        assert sorted(row["org"] for row in db.all("SELECT org FROM store_pins")) == [ORG_A, ORG_B]
    assert (
        lite.client.delete(f"/api/store/versions/{pack.digest}", headers=theirs).status_code == 200
    )  # theirs is withdrawn; the other's stays
    assert {row["org"]: row["state"] for row in lite.versions()} == {
        ORG_A: "draft",
        ORG_B: "withdrawn",
    }


def test_only_the_stores_authors_add_and_only_people(lite: Adopting) -> None:
    lite.publish()
    for role in ("viewer", "reviewer", "governance", "admin"):
        refused = lite.pull(who=role)
        assert refused.status_code == 403, role
    key = lite.client.post(
        "/api/auth/keys", json={"role": "service", "name": "k"}, headers=lite.as_(ORG_A, "admin")
    ).json()["key"]
    machine = lite.client.post(
        "/api/store/pulls", json={"ref": ACME}, headers={"authorization": f"Bearer {key}"}
    )
    assert machine.status_code == 403
    assert lite.client.post("/api/store/pulls", json={"ref": ACME}).status_code == 401
    lite.nothing_was_added()
    for ref in ("acme", "acme/portal-agent@", "../x/y", "acme/portal-agent@1.0.0/..", "a/b@v 1"):
        assert lite.pull(ref).status_code in (404, 422), ref
    lite.nothing_was_added()


def test_adding_is_not_approving_a_pulled_agent_automates_only_once_every_requirement_is_met(
    world: Adopting, service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the registry to an automated decision, on SQLite and Postgres, each requirement in
    turn: a draft answers nothing; whoever added it cannot start serving it; served, it answers
    from its canned examples and a person decides; connected to its service, a person still
    decides until the organization approves that digest, which whoever added it cannot do; and
    a recall by its registry stops it again."""
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", service.origin)
    pack = world.publish()
    both = member(
        world.store.db, org=ORG_A, roles=["pack_author", "governance", "admin"], sub="both"
    )
    added = world.client.post("/api/store/pulls", json={"ref": ACME}, headers=both)
    assert added.status_code == 201, added.text
    assert decide(world).status_code == 404  # a draft
    report = evaluated(world, pack.digest)
    promotion = {
        "version": pack.digest,
        "reports": [report],
        "reference": "AI committee, item 2",
        "automate": True,
    }
    own = world.client.post("/api/store/promotions", json=promotion, headers=both)
    assert own.status_code == 403 and decide(world).status_code == 404
    started = world.client.post(
        "/api/store/promotions", json=promotion, headers=world.as_(ORG_A, "governance")
    )
    assert started.status_code == 201, started.text
    # The registry it came from is read for recalls from its first decision on; here now.
    watch = world.client.app.state.recall_watch  # type: ignore[attr-defined]
    watch.watch(world.hub.url)
    watch.refresh(world.hub.url)

    canned = decide(world).json()
    assert canned["outcome"] == "HUMAN_REVIEW" and canned["recommendation"] == "refill"
    assert any("canned answers" in reason for reason in canned["reasons"])
    assert any("not approved for automation" in reason for reason in canned["reasons"])
    assert service.seen == []

    connected = world.client.put(
        f"/api/connections/{ACME}", json={"endpoint": service.url, "token": "tok-zq"}, headers=both
    )
    assert connected.status_code == 200, connected.text
    unapproved = decide(world).json()
    assert unapproved["outcome"] == "HUMAN_REVIEW"
    assert not any("canned answers" in reason for reason in unapproved["reasons"])
    assert any("not approved for automation" in reason for reason in unapproved["reasons"])
    assert service.authorizations() == ["Bearer tok-zq"]

    approval = {"pack_id": ACME, "pack_digest": pack.digest, "reference": "minutes 9"}
    own_approval = world.client.post("/api/registry-approvals", json=approval, headers=both)
    assert own_approval.status_code == 403, own_approval.text
    assert decide(world).json()["outcome"] == "HUMAN_REVIEW"
    approved = world.client.post(
        "/api/registry-approvals", json=approval, headers=world.as_(ORG_A, "governance")
    )
    assert approved.status_code == 201, approved.text
    automated = decide(world).json()
    assert automated["outcome"] == "AUTO", automated["reasons"]
    assert automated["pack"]["digest"] == pack.digest
    assert automated["answers"]["request"]["provider"]["model"] == "local-test-agent"

    # Another organization is served nothing of it, whatever the first approved.
    assert decide(world, org=ORG_B).status_code == 404

    token, key = world.publisher("acme")
    world.hub.client(token).yank(
        "acme", "portal-agent", pack.version, reason="misroutes", severity="high", key=key
    )
    watch.refresh(world.hub.url)
    recalled = decide(world).json()
    assert recalled["outcome"] == "HUMAN_REVIEW"
    assert any("recalled (high) by its registry" in reason for reason in recalled["reasons"])
    assert world.store.db.verify()["ok"]


# -- what adds nothing ----------------------------------------------------------------------------


def _tampered(world: Adopting, monkeypatch: pytest.MonkeyPatch, pack: Pack) -> str:
    """One byte of the archive changed on its way."""
    served = RegistryClient.archive

    def changed(self: RegistryClient, *version: Any) -> bytes:
        data = bytearray(served(self, *version))
        data[len(data) // 2] ^= 0x01
        return bytes(data)

    monkeypatch.setattr(RegistryClient, "archive", changed)
    return "the downloaded archive is not the one the registry recorded"


def _signed_by_someone_else(world: Adopting, monkeypatch: pytest.MonkeyPatch, pack: Pack) -> str:
    """A registry that serves the same agent signed by a key that is not its namespace's, and
    records that archive as the version's own."""
    private, _ = archive.generate_key()
    forged = archive.build(pack, archive.load_private_key(private))
    record = RegistryClient.version

    def version(self: RegistryClient, *named: Any) -> dict[str, Any]:
        return {**record(self, *named), "archive_sha256": hashlib.sha256(forged).hexdigest()}

    monkeypatch.setattr(RegistryClient, "version", version)
    monkeypatch.setattr(RegistryClient, "archive", lambda self, *version: forged)
    return "not added: "


def _signed_with_a_revoked_key(world: Adopting, monkeypatch: pytest.MonkeyPatch, pack: Pack) -> str:
    """The publisher's key was revoked, and the registry says the version was signed after."""
    key_id = archive.key_id(world.publisher("acme")[1].public_key())
    with world.hub.operator() as store:
        store.revoke_key("acme", key_id, reason="lost", by="operator")
    record = RegistryClient.version

    def version(self: RegistryClient, *named: Any) -> dict[str, Any]:
        return {**record(self, *named), "published_at": "2099-01-01T00:00:00+00:00"}

    monkeypatch.setattr(RegistryClient, "version", version)
    return "revoked"


def _another_agents_archive(world: Adopting, monkeypatch: pytest.MonkeyPatch, pack: Pack) -> str:
    """The registry answers another agent's archive, signed by its own publisher, in its place."""
    other = world.publish("acme/another-agent")
    swapped = archive.build(other, world.publisher("acme")[1])
    record = RegistryClient.version

    def version(self: RegistryClient, *named: Any) -> dict[str, Any]:
        return {**record(self, *named), "archive_sha256": hashlib.sha256(swapped).hexdigest()}

    monkeypatch.setattr(RegistryClient, "version", version)
    monkeypatch.setattr(RegistryClient, "archive", lambda self, *version: swapped)
    return f"the archive is not {ACME}@{pack.version}, which the registry named"


@pytest.mark.parametrize(
    "how",
    [_tampered, _signed_by_someone_else, _signed_with_a_revoked_key, _another_agents_archive],
)
def test_an_archive_or_a_signature_that_does_not_verify_adds_nothing(
    lite: Adopting, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: Any
) -> None:
    pack = lite.publish()
    words = how(lite, monkeypatch, pack)
    capsys.readouterr()
    refused = lite.pull()
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"].startswith("not added: ") and words in refused.json()["detail"]
    lines = security_lines(capsys.readouterr().out)
    [said] = [line for line in lines if line["event"] == "store.pull.refused"]
    assert (said["outcome"], said["reason"], said["org"]) == ("refused", "verification", ORG_A)
    lite.nothing_was_added()


def test_a_recalled_or_deleted_version_is_not_added(lite: Adopting) -> None:
    pack = lite.publish()
    token, key = lite.publisher("acme")
    lite.hub.client(token).yank(
        "acme", "portal-agent", pack.version, reason="misroutes", severity="critical", key=key
    )
    exact = lite.pull(f"{ACME}@{pack.version}")
    assert exact.status_code == 409, exact.text
    assert "was recalled by its registry (severity: critical)" in exact.json()["detail"]
    newest = lite.pull()  # its newest version that is not recalled: there is none
    assert newest.status_code == 404 and newest.json()["detail"] == hub_routes.NOT_LISTED
    lite.nothing_was_added()
    shown = lite.client.get(f"/api/hub/agents/{ACME}", headers=lite.as_(ORG_A, "viewer")).json()
    assert shown["agent"] is None and shown["versions"] == []  # none that is not recalled
    [recalled] = shown["recalled"]
    assert (recalled["version"], recalled["severity"]) == (pack.version, "critical")
    assert (recalled["reason"], recalled["signer"]) == ("misroutes", "publisher")

    later = lite.publish(version="9.0.0")
    with lite.hub.operator() as store:
        store.delete("acme", "portal-agent", "9.0.0", reason="carried a file", by="operator")
    deleted = lite.pull(f"{ACME}@9.0.0")
    assert deleted.status_code == 404 and deleted.json()["detail"] == hub_routes.NOT_LISTED
    assert later.digest != pack.digest
    lite.nothing_was_added()


def test_a_registry_that_cannot_be_reached_adds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A registry address nothing listens at: one refusal, in fixed words."""
    closed = f"http://127.0.0.1:{free_port()}"
    for world in adopting(str(tmp_path / "audit.db"), tmp_path, monkeypatch, hub, url=closed):
        capsys.readouterr()
        refused = world.pull()
        assert refused.status_code == 502 and refused.json()["detail"] == hub_routes.NO_ANSWER
        [said] = [
            line
            for line in security_lines(capsys.readouterr().out)
            if line["event"] == "store.pull.refused"
        ]
        assert said["reason"] == "unreachable"
        viewer = world.as_(ORG_A, "viewer")
        for url in ("/api/hub/search", f"/api/hub/agents/{ACME}"):
            read = world.client.get(url, headers=viewer)
            assert read.status_code == 502 and read.json()["detail"] == hub_routes.NO_ANSWER
        world.nothing_was_added()


@pytest.mark.parametrize("answer", [["not", "a", "record"], "text", {"version": "1.0.0"}, None, 7])
def test_an_answer_that_is_not_a_registrys_adds_nothing(
    lite: Adopting, monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    lite.publish()
    monkeypatch.setattr(RegistryClient, "version", lambda self, *named: answer)
    refused = lite.pull()
    assert refused.status_code in (422, 502), refused.text
    assert "Nothing was changed" in refused.text or "not added" in refused.text
    lite.nothing_was_added()


def test_a_publishers_changed_key_is_refused_until_an_author_accepts_it_for_their_organization(
    world: Adopting,
) -> None:
    """Keys are pinned for each organization in the database (SQLite and Postgres): the first
    version an organization adds pins its publisher's keys; a version signed with a key it has
    not seen is refused, naming both, until one of its authors accepts it; and what one
    organization accepted changes nothing for another."""
    first = world.publish()
    token, key = world.publisher("acme")
    old = archive.key_id(key.public_key())
    assert world.pull().status_code == 201
    private, public = archive.generate_key()
    world.hub.client(token).add_key("acme", public)
    rotated = archive.load_private_key(private)
    new = archive.key_id(rotated.public_key())
    second = as_published(world.tmp, version="2.0.0")
    world.hub.client(token).push(second, rotated)

    refused = world.pull(f"{ACME}@2.0.0")
    assert refused.status_code == 409, refused.text
    assert refused.json()["key_changed"] == {"namespace": "acme", "key_id": new, "pinned": [old]}
    assert [row["digest"] for row in world.versions()] == [first.digest]
    # Another organization, which never added from this publisher, pins what it sees first.
    assert world.pull(f"{ACME}@2.0.0", org=ORG_B).status_code == 201
    still = world.pull(f"{ACME}@2.0.0")
    assert still.status_code == 409 and "key_changed" in still.json()

    accepted = world.pull(f"{ACME}@2.0.0", accept_new_key=True)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["pulled"]["key_id"] == new
    with world.store.db._db.session(exclusive=False) as db:
        pins = {
            row["org"]: json.loads(row["value"])
            for row in db.all("SELECT org, value FROM store_pins WHERE name = 'acme'")
        }
    assert pins == {ORG_A: sorted([old, new]), ORG_B: sorted([old, new])}
    assert sorted((row["org"], row["version"]) for row in world.versions()) == [
        (ORG_A, first.version),
        (ORG_A, "2.0.0"),
        (ORG_B, "2.0.0"),
    ]


def test_what_the_deployment_serves_from_its_own_files_is_never_replaced(
    lite: Adopting, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id this deployment serves from its files is refused before anything is asked of the
    registry."""
    asked = counting(monkeypatch, "version")
    refused = lite.pull(OWN)
    assert refused.status_code == 409 and "from its own files" in refused.json()["detail"]
    assert asked == []
    lite.nothing_was_added()


def test_a_quota_reached_stops_before_anything_is_downloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> None:
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "store_uploads=1")
    for world in adopting(str(tmp_path / "audit.db"), tmp_path, monkeypatch, hub):
        world.publish()
        world.publish(version="2.0.0")
        assert world.pull(f"{ACME}@2.0.0").status_code == 201
        asked = counting(monkeypatch, "archive")
        refused = world.pull()
        assert refused.status_code == 429, refused.text
        assert asked == [] and len(world.versions()) == 1


def test_reads_of_the_registry_are_counted_against_each_persons_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: Hub,  # noqa: F811
) -> None:
    """A search and a listing each make this server send a request to the registry: they count
    per person against the `label` class (`server/limits.py`); what the deployment is set up
    with does not, and another person has an allowance of their own."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on,label=2/min:2")
    for world in adopting(str(tmp_path / "audit.db"), tmp_path, monkeypatch, hub):
        world.publish()
        viewer = world.as_(ORG_A, "viewer")
        codes = [world.client.get("/api/hub/search", headers=viewer).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert world.client.get(f"/api/hub/agents/{ACME}", headers=viewer).status_code == 429
        assert world.client.get("/api/hub", headers=viewer).status_code == 200
        other = world.as_(ORG_A, "reviewer")
        assert world.client.get("/api/hub/search", headers=other).status_code == 200


def test_the_security_log_names_the_version_and_who_added_it(
    lite: Adopting, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pack = lite.publish()
    capsys.readouterr()
    assert lite.pull().status_code == 201
    lines = security_lines(capsys.readouterr().out)
    [added] = [line for line in lines if line["event"] == "store.pull.added"]
    assert (added["org"], added["ids"], added["route"]) == (
        ORG_A,
        [pack.digest],
        "/api/store/pulls",
    )
    assert added["actor"].startswith("user:")
