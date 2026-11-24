# SPDX-License-Identifier: Apache-2.0
"""Namespace claims (LP-19c, `hub/registry.py` `claim`, `decide_claim`, `Viewer`): an
organization claims a name, reserved names are the operator's, and a first publish is served
to nobody but its own token and a reviewer until the claim is approved. To the public a pending
or refused namespace answers exactly as one that does not exist. On SQLite and Postgres."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari.cli import main
from mendari.core.pack import Pack
from mendari.core.registry import resolve_pack
from mendari.errors import HubError
from mendari.hub import archive, yank
from mendari.hub import registry as registry_module
from mendari.hub.client import RegistryClient, pull
from mendari.hub.registry import PUBLIC, REVIEWER, RegistryStore, Viewer, token_id
from tests.conftest import Registry, shared_benchmark
from tests.test_hub_client import SHIPPED, copy_pack

CLAIMANT = "user:3f9a"  # a person as a chain names one: an id, never an address
REVIEWED_BY = "user:77aa"
REFERRAL = resolve_pack("mendari/referral-router")
# Every read of a namespace and a pack in it, by path.
READS = (
    "/v1/namespaces/{ns}",
    "/v1/packs/{ns}/inbox",
    "/v1/packs/{ns}/inbox/reports",
    "/v1/packs/{ns}/inbox/{version}",
    "/v1/packs/{ns}/inbox/{version}/archive",
)


def _claim(db_target: str, name: str = "acme", **more: Any) -> dict[str, Any]:
    given = {"org": "acme", "by": CLAIMANT, "display_name": "Acme Health", **more}
    with RegistryStore(db_target) as store:
        return store.claim(name, **given)


def _decide(db_target: str, name: str = "acme", *, approve: bool, note: str = "") -> dict[str, Any]:
    with RegistryStore(db_target) as store:
        return store.decide_claim(name, approve=approve, by=REVIEWED_BY, note=note)


def _claims(db_target: str, **which: Any) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store:
        return store.claims(**which)


def _events(db_target: str) -> list[dict[str, Any]]:
    with RegistryStore(db_target) as store, store._db.session(exclusive=False) as db:
        return db.all("SELECT * FROM hub_events ORDER BY seq")


def _verify(db_target: str) -> dict[str, Any]:
    with RegistryStore(db_target) as store:
        return store.verify()


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


class Claimed:
    """A claimed namespace as its publisher holds it: a token, a key, and a pack to publish."""

    def __init__(self, registry: Registry, db_target: str, tmp_path: Path, name: str = "acme"):
        self.registry, self.name = registry, name
        self.claim = _claim(db_target, name)
        with RegistryStore(db_target) as store:
            minted = store.mint_token(
                name, org="acme", owner=CLAIMANT, name="ci", days=30, by=CLAIMANT
            )
        self.token, self.token_id = str(minted["token"]), str(minted["id"])
        private, public = archive.generate_key()
        self.key = archive.load_private_key(private)
        added = registry.client.post(
            f"/v1/namespaces/{name}/keys",
            json={"public_pem": public.decode()},
            headers=_bearer(self.token),
        )
        assert added.status_code == 201, added.text
        self.tmp_path = tmp_path

    def pack(self, version: str = SHIPPED, name: str = "inbox") -> Pack:
        return copy_pack(self.tmp_path, f"{self.name}/{name}", version)

    def publish(self, version: str = SHIPPED, name: str = "inbox") -> dict[str, Any]:
        answer = self.registry.publish(self.pack(version, name), key=self.key, token=self.token)
        assert answer.status_code == 201, answer.text
        published: dict[str, Any] = answer.json()
        return published

    def recall(self) -> dict[str, Any]:
        """The publisher recalls its own version, signed with the namespace's key."""
        path = f"/v1/packs/{self.name}/inbox/{SHIPPED}"
        meta = self.registry.client.get(path, headers=_bearer(self.token)).json()
        record = yank.sign(
            pack=f"{self.name}/inbox", version=SHIPPED, digest=meta["digest"],
            archive_sha256=meta["archive_sha256"], reason="a bad rule", severity="high",
            signer="publisher", key=self.key, key_id=archive.key_id(self.key.public_key()),
        )  # fmt: skip
        answer = self.registry.client.post(
            f"{path}/yank", json={"record": record}, headers=_bearer(self.token)
        )
        assert answer.status_code == 201, answer.text
        return record


@pytest.fixture
def claimed(registry: Registry, db_target: str, tmp_path: Path) -> Claimed:
    return Claimed(registry, db_target, tmp_path)


def _client(registry: Registry, token: str | None = None) -> RegistryClient:
    """The real client (`hub/client.py`), its requests answered by the registry under test."""

    def handle(request: httpx.Request) -> httpx.Response:
        sent = {k: v for k, v in request.headers.items() if k in ("authorization", "content-type")}
        answer = registry.client.request(
            request.method, request.url.raw_path.decode(), content=request.content, headers=sent
        )
        kept = {k: v for k, v in answer.headers.items() if k.startswith("x-archive")}
        return httpx.Response(answer.status_code, headers=kept, content=answer.content)

    return RegistryClient("https://registry.example", token, transport=httpx.MockTransport(handle))


def _reads(registry: Registry, namespace: str, token: str | None = None) -> list[tuple[int, str]]:
    """Every read of `namespace`, as (status, body with the name taken out)."""
    answers = []
    for path in READS:
        answer = registry.client.get(
            path.format(ns=namespace, version=SHIPPED), headers=_bearer(token) if token else {}
        )
        answers.append((answer.status_code, answer.text.replace(namespace, "NAME")))
    return answers


@pytest.mark.parametrize("name", ["mendari", "openhealth", "mendari-labs", "openhealth-packs"])
def test_a_reserved_namespace_is_never_claimed(
    registry: Registry, db_target: str, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    events = len(_events(db_target))
    with pytest.raises(HubError, match=f"the namespace '{name}' is reserved") as refused:
        _claim(db_target, name)
    assert refused.value.status == 422
    assert _claims(db_target) == [] and len(_events(db_target)) == events
    assert _claim(db_target, "mendarian")["status"] == "pending"  # a lookalike is a reviewer's
    # The operator's command is the only way to one (`mendari` is the fixture's already).
    claim = ["registry", "claim", "--db", db_target, "--namespace", name, "--org", "mendari"]
    code = main([*claim, "--reason", "the project's own namespace"])
    assert code == (1 if name == "mendari" else 0), capsys.readouterr().err
    if name != "mendari":
        made = json.loads(capsys.readouterr().out)
        assert (made["namespace"], made["org"], made["status"]) == (name, "mendari", "pending")
        assert made["claimed_by"].startswith("command line (")
        claimed = json.loads(_events(db_target)[-1]["payload"])
        assert claimed["reason"] == "the project's own namespace"
    assert _verify(db_target)["ok"]


def test_a_claimed_name_is_not_claimed_twice(registry: Registry, db_target: str) -> None:
    first = _claim(db_target)
    assert first == {
        "namespace": "acme",
        "org": "acme",
        "claimed_by": CLAIMANT,
        "created_at": first["created_at"],
        "status": "pending",
        "decided_by": None,
        "decided_at": None,
        "note": None,
    }
    events = len(_events(db_target))
    registry.operator.create_namespace("other", "community", "")  # one with no claim
    for name, org in (("acme", "acme"), ("acme", "rival"), ("other", "rival"), ("mendari", "x")):
        with pytest.raises(HubError) as taken:  # claimed, made by the operator, or reserved
            _claim(db_target, name, org=org)
        if name != "mendari":
            assert (str(taken.value), taken.value.status) == (f"namespace {name!r} exists", 409)
    with pytest.raises(HubError) as again:  # and the operator cannot make it over the claim
        registry.operator.create_namespace("acme", "community", "")
    assert again.value.status == 409
    for bad in ("Acme", "-acme", "a" * 64, "ac me", ""):
        with pytest.raises(HubError, match="lowercase letters, digits and dashes"):
            _claim(db_target, bad)
    for label in ({"org": " "}, {"by": "a\nb"}, {"display_name": "x" * 201}, {"org": "‮"}):
        with pytest.raises(HubError):
            _claim(db_target, "fresh", **label)
    assert _claims(db_target) == [first] and len(_events(db_target)) == events + 1
    assert _verify(db_target)["ok"]


def test_an_organization_holds_three_pending_claims_at_most(
    registry: Registry, db_target: str, tmp_path: Path
) -> None:
    held = Claimed(registry, db_target, tmp_path, "acme-one")
    for name in ("acme-two", "acme-three"):
        _claim(db_target, name)
    events = len(_events(db_target))
    with pytest.raises(HubError, match="holds 3 pending claims at most") as full:
        _claim(db_target, "acme-four")
    assert full.value.status == 409 and len(_events(db_target)) == events
    with RegistryStore(db_target) as store, pytest.raises(HubError, match="no namespace"):
        store.namespace("acme-four", REVIEWER)  # nothing was made of the fourth
    assert _claim(db_target, "rival-one", org="rival")["org"] == "rival"  # each its own three
    # A decided claim is no longer pending: approved (with its first publish) or refused.
    held.publish()
    assert _decide(db_target, "acme-one", approve=True)["status"] == "approved"
    assert _claim(db_target, "acme-four")["status"] == "pending"
    with pytest.raises(HubError, match="holds 3 pending claims at most"):
        _claim(db_target, "acme-five")
    assert _decide(db_target, "acme-two", approve=False, note="a lookalike")["status"] == "refused"
    assert _claim(db_target, "acme-five")["status"] == "pending"
    assert {c["namespace"] for c in _claims(db_target, org="acme", status="pending")} == {
        "acme-three",
        "acme-four",
        "acme-five",
    }
    assert _verify(db_target)["ok"]


def test_a_first_publish_is_invisible_to_search_pull_the_namespace_the_recall_list_and_health(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    assert registry.publish().status_code == 201  # a listed pack, beside it
    published = claimed.publish()
    assert published["listed"] is False and published["id"] == "acme/inbox"
    report = shared_benchmark()
    for who in ({}, _bearer(registry.token), _bearer("ohr_made-up")):  # nobody, another publisher
        assert [p["id"] for p in registry.client.get("/v1/search", headers=who).json()] == [
            REFERRAL.id
        ]
        assert registry.client.get("/v1/search?q=inbox", headers=who).json() == []
        assert registry.client.get("/v1/health", headers=who).json()["packs"] == 1
        for path in READS:
            hidden = registry.client.get(path.format(ns="acme", version=SHIPPED), headers=who)
            assert hidden.status_code == 404, path
        shared = registry.client.post(
            f"/v1/packs/acme/inbox/{SHIPPED}/reports",
            json={"site": "Site A", "report": report},
            headers=who,
        )
        assert shared.status_code == 401  # a report is a site's, signed (LP-22f)
    with pytest.raises(HubError, match="no agent acme/inbox"):
        pull("acme/inbox", _client(registry))
    with pytest.raises(HubError, match="no agent acme/inbox"):
        pull(f"acme/inbox@{SHIPPED}", _client(registry, registry.token))
    # Its recall is not on the public list either (a running server reads that list).
    record = claimed.recall()
    assert registry.client.get("/v1/yanks").json() == []
    assert registry.client.get("/v1/yanks", headers=_bearer(registry.token)).json() == []
    own = registry.client.get("/v1/yanks", headers=_bearer(claimed.token)).json()
    assert [(r["pack"], r["yank"]) for r in own] == [("acme/inbox", record)]
    # The store's own answer, whatever route asks (the rule is in the store, not per route).
    with RegistryStore(db_target) as store:
        assert [p["id"] for p in store.latest()] == [REFERRAL.id]
        assert store.versions("acme", "inbox") == []
        assert store.reports("acme", "inbox") == {"reports": [], "superseded": 0}
        assert store.recalled() == [] and len(store.recalled(REVIEWER)) == 1
        for read in (
            lambda: store.namespace("acme"),
            lambda: store.version("acme", "inbox", SHIPPED),
            lambda: store.version("acme", "inbox", SHIPPED, Viewer(namespace="mendari")),
            lambda: store.archive(published["archive_sha256"]),
            lambda: store.archive(published["archive_sha256"], Viewer(namespace="mendari")),
        ):
            with pytest.raises(HubError) as hidden_read:
                read()
            assert hidden_read.value.status == 404
        assert store.archive(published["archive_sha256"], Viewer(namespace="acme"))
    assert _verify(db_target)["ok"]


@pytest.mark.parametrize("state", ["pending", "refused"])
def test_a_pending_namespace_answers_as_one_that_does_not_exist(
    registry: Registry, db_target: str, claimed: Claimed, state: str
) -> None:
    claimed.publish()
    if state == "refused":
        _decide(db_target, approve=False, note="not who it says it is")
    nowhere = _reads(registry, "nobody")
    assert [status for status, _ in nowhere] == [404] * len(READS)
    # The same status and the same body, to nobody and to another namespace's publisher.
    assert _reads(registry, "acme") == nowhere
    assert _reads(registry, "acme", registry.token) == _reads(registry, "nobody", registry.token)
    assert _reads(registry, "acme", registry.token) == nowhere
    # A write another publisher may make to any listed version: the same answer too.
    report = shared_benchmark()
    shared = [
        registry.client.post(
            f"/v1/packs/{name}/inbox/{SHIPPED}/reports",
            json={"site": "Site A", "report": report},
            headers=_bearer(registry.token),
        )
        for name in ("acme", "nobody")
    ]
    assert [a.status_code for a in shared] == [401, 401]  # a site's, signed (LP-22f)
    assert shared[0].text.replace("acme", "NAME") == shared[1].text.replace("nobody", "NAME")
    # And its publisher routes refuse another token as they refuse it for no namespace at all.
    keys = [
        registry.client.post(
            f"/v1/namespaces/{name}/keys",
            json={"public_pem": archive.generate_key()[1].decode()},
            headers=_bearer(registry.token),
        )
        for name in ("acme", "nobody")
    ]
    assert [a.status_code for a in keys] == [403, 403]
    assert keys[0].text.replace("acme", "NAME") == keys[1].text.replace("nobody", "NAME")


def test_its_own_token_reads_its_pending_version(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    published = claimed.publish()
    own = _bearer(claimed.token)
    assert [status for status, _ in _reads(registry, "acme", claimed.token)] == [200] * len(READS)
    namespace = registry.client.get("/v1/namespaces/acme", headers=own).json()
    assert namespace["listed"] is False and namespace["packs"] == ["acme/inbox"]
    assert namespace["claim"]["status"] == "pending" and namespace["claim"]["org"] == "acme"
    version = registry.client.get(f"/v1/packs/acme/inbox/{SHIPPED}", headers=own).json()
    assert version["listed"] is False and version["digest"] == published["digest"]
    assert [p["id"] for p in registry.client.get("/v1/search", headers=own).json()] == [
        "acme/inbox"
    ]
    # `mendari push` confirms with the client it published with; a pull with it works too.
    with _client(registry, claimed.token) as client:
        assert client.version("acme", "inbox")["listed"] is False
        assert pull("acme/inbox", client).digest == published["digest"]
    # A reviewer reads it, through the store (the engine's reviewer routes, or the operator's
    # commands): no token reads as a reviewer over the registry's own routes (LP-19g).
    reviewed = registry.operator.version("acme", "inbox", SHIPPED, REVIEWER)
    assert reviewed["digest"] == published["digest"]
    assert registry.client.get("/v1/health", headers=own).json()["packs"] == 0  # the public count
    with RegistryStore(db_target) as store:
        store.revoke_token(claimed.token_id, org="acme", by=CLAIMANT)
    assert [status for status, _ in _reads(registry, "acme", claimed.token)] == [404] * len(READS)


def test_approved_it_is_listed_and_pulls(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    published = claimed.publish()
    approved = _decide(db_target, approve=True, note="checked the publisher and the card")
    assert (approved["status"], approved["decided_by"]) == ("approved", REVIEWED_BY)
    assert approved["decided_at"] is not None and approved["note"].startswith("checked")
    assert [status for status, _ in _reads(registry, "acme")] == [200] * len(READS)
    namespace = registry.client.get("/v1/namespaces/acme").json()
    assert namespace["listed"] is True and namespace["packs"] == ["acme/inbox"]
    assert namespace["claim"] is None  # the organization is its own and the reviewers' to see
    other = registry.client.get("/v1/namespaces/acme", headers=_bearer(registry.token)).json()
    assert other["claim"] is None  # another publisher is the public here
    own = registry.client.get("/v1/namespaces/acme", headers=_bearer(claimed.token)).json()
    assert own["claim"]["status"] == "approved" and own["claim"]["org"] == "acme"
    assert "acme" not in json.dumps({k: v for k, v in namespace.items() if k != "name"}).replace(
        "acme/inbox", ""
    ).replace("Acme Health", "")
    assert [p["id"] for p in registry.client.get("/v1/search").json()] == ["acme/inbox"]
    assert registry.client.get("/v1/health").json()["packs"] == 1
    with _client(registry) as client:  # the real pull, by anyone
        pulled = pull("acme/inbox", client)
    assert (pulled.id, pulled.version, pulled.digest) == (
        "acme/inbox",
        SHIPPED,
        published["digest"],
    )
    # From then a later version and a new pack name in the namespace list at once.
    assert claimed.publish("9.9.9")["listed"] is True
    assert claimed.publish(name="triage")["listed"] is True
    assert sorted(p["id"] for p in registry.client.get("/v1/search").json()) == [
        "acme/inbox",
        "acme/triage",
    ]
    assert registry.client.get("/v1/packs/acme/inbox").json()["latest"]["version"] == "9.9.9"
    listed = [
        json.loads(e["payload"])["listed"]
        for e in _events(db_target)
        if e["event"] == "pack.published"
    ]
    assert listed == [False, True, True]
    with pytest.raises(HubError, match="already approved") as decided:  # decided once
        _decide(db_target, approve=False)
    assert decided.value.status == 409
    assert _verify(db_target)["ok"]


def test_a_refused_namespace_never_lists_and_its_archive_is_kept(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    published = claimed.publish()
    data = registry.client.get(
        f"/v1/packs/acme/inbox/{SHIPPED}/archive", headers=_bearer(claimed.token)
    ).content
    refused = _decide(db_target, approve=False, note="not who it says it is")
    assert (refused["status"], refused["note"]) == ("refused", "not who it says it is")
    assert [status for status, _ in _reads(registry, "acme")] == [404] * len(READS)
    assert registry.client.get("/v1/search").json() == []
    assert registry.client.get("/v1/health").json()["packs"] == 0
    with pytest.raises(HubError, match="no agent acme/inbox"):
        pull("acme/inbox", _client(registry))
    # Kept: the archive, the version and the chain (a refusal removes nothing), for a reviewer
    # and for the publisher's own token, which reads why.
    with RegistryStore(db_target) as store:
        assert store.archive(published["archive_sha256"], REVIEWER) == data
        assert store.version("acme", "inbox", SHIPPED, REVIEWER)["listed"] is False
    own = registry.client.get("/v1/namespaces/acme", headers=_bearer(claimed.token)).json()
    assert own["claim"]["status"] == "refused" and own["claim"]["note"] == refused["note"]
    # It stays taken, and it is decided once: nothing lists it later.
    with pytest.raises(HubError, match="namespace 'acme' exists"):
        _claim(db_target, org="rival")
    with pytest.raises(HubError, match="already refused"):
        _decide(db_target, approve=True)
    claimed.publish("9.9.9")  # a later publish is as invisible
    assert [status for status, _ in _reads(registry, "acme")] == [404] * len(READS)
    assert registry.client.get("/v1/search").json() == []
    assert _verify(db_target)["ok"]


def test_a_claim_without_a_publish_cannot_be_approved(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    events = len(_events(db_target))
    with pytest.raises(HubError, match="no published version to review") as nothing:
        _decide(db_target, approve=True)
    assert nothing.value.status == 409
    for name, problem in (("nobody", "no claim on namespace"), ("mendari", "no claim on namesp")):
        with pytest.raises(HubError, match=problem) as unclaimed:  # none, or from before claims
            _decide(db_target, name, approve=True)
        assert unclaimed.value.status == 404
    for note in ("two\nlines", "x" * 301, "a ‮ reversed"):
        with pytest.raises(HubError, match="the note"):
            _decide(db_target, approve=False, note=note)
    assert _claims(db_target)[0]["status"] == "pending" and len(_events(db_target)) == events
    # A version that was deleted is nothing to review either.
    claimed.publish()
    registry.operator.delete("acme", "inbox", SHIPPED, reason="carried a secret", by="operator")
    with pytest.raises(HubError, match="no published version to review"):
        _decide(db_target, approve=True)
    claimed.publish("9.9.9")
    assert _decide(db_target, approve=True)["status"] == "approved"


def test_claim_approval_and_publish_are_chained_with_the_organization(
    registry: Registry, db_target: str, claimed: Claimed
) -> None:
    before = len(_events(db_target)) - 4  # the claim, its token and its key
    published = claimed.publish()
    _decide(db_target, approve=True, note="checked")
    events = _events(db_target)[before:]
    assert [e["event"] for e in events] == [
        "namespace.created",
        "namespace.claimed",
        "token.issued",
        "key.added",
        "pack.published",
        "namespace.approved",
    ]
    payloads = [json.loads(e["payload"]) for e in events]
    rows = [p.pop("row") for p in payloads]
    assert [r["kind"] for r in rows] == [
        "namespaces", "claims", "publisher_tokens", "keys", "versions", "claims",
    ]  # fmt: skip
    assert rows[1]["key"] == rows[5]["key"] == "acme" and rows[1]["sha256"] != rows[5]["sha256"]
    assert payloads[0] == {
        "namespace": "acme",
        "publisher_type": "community",
        "org": "acme",
        "by": CLAIMANT,
    }
    assert payloads[1] == {"namespace": "acme", "org": "acme", "by": CLAIMANT}
    assert payloads[4] == {
        "pack": "acme/inbox",
        "version": SHIPPED,
        "digest": published["digest"],
        "archive_sha256": published["archive_sha256"],
        "key_id": published["key_id"],
        "by": f"token:{token_id(claimed.token)}",
        "org": "acme",
        "listed": False,
    }
    assert payloads[5] == {"namespace": "acme", "org": "acme", "by": REVIEWED_BY, "note": "checked"}
    assert {e["subject"] for e in events if e["event"].startswith("namespace.")} == {
        "namespace:acme"
    }
    assert _verify(db_target)["ok"]
    # A claim approved past the API is not the row its newest event hashed.
    _claim(db_target, "acme-two")
    with RegistryStore(db_target) as store, store._db.session(exclusive=True) as db:
        db.all("UPDATE hub_claims SET status = 'approved' WHERE namespace = 'acme-two'")
    assert _verify(db_target)["problem"] == "1 of the claims changed since their chained event"


def test_the_operators_commands_claim_approve_refuse_and_list(
    registry: Registry, db_target: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A registry nobody signs in to: the operator's commands on its database, each chained
    with who ran it and why."""
    held = Claimed(registry, db_target, tmp_path)
    _claim(db_target, "lookalike", org="rival")
    decide = ["--db", db_target, "--namespace"]
    assert main(["registry", "approve", *decide, "acme", "--reason", "x"]) != 0
    assert "no published version to review" in capsys.readouterr().err
    held.publish()
    assert main(["registry", "approve", *decide, "acme", "--reason", "the  first publish"]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert (approved["status"], approved["note"]) == ("approved", "the first publish")
    assert approved["decided_by"].startswith("command line (")
    assert registry.client.get(f"/v1/packs/acme/inbox/{SHIPPED}").status_code == 200
    assert main(["registry", "refuse", *decide, "lookalike", "--reason", "a lookalike"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    last = _events(db_target)[-1]
    assert last["event"] == "namespace.refused"
    refused = json.loads(last["payload"])
    assert (refused["org"], refused["note"]) == ("rival", "a lookalike")
    assert refused["by"].startswith("command line (")

    assert main(["registry", "claims", "--db", db_target]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(c["namespace"], c["status"]) for c in listed] == [
        ("acme", "approved"),
        ("lookalike", "refused"),
    ]
    assert main(["registry", "claims", "--db", db_target, "--status", "refused"]) == 0
    assert [c["namespace"] for c in json.loads(capsys.readouterr().out)] == ["lookalike"]
    assert main(["registry", "claims", "--db", db_target, "--org", "nobody"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["registry", "refuse", *decide, "acme", "--reason", "x"]) != 0
    assert "already approved" in capsys.readouterr().err
    assert main(["registry", "approve", *decide, "nobody", "--reason", "x"]) != 0
    assert "no claim on namespace" in capsys.readouterr().err
    assert main(["registry", "verify", "--db", db_target]) == 0


def test_a_namespace_from_before_claims_stays_listed(
    registry: Registry, db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A namespace with no claim row (every one from before LP-19c) reads as it did. A database
    from before has no claims table: read as it is it verifies and serves; opened by a registry
    it gains the table, and its chain is as it was."""
    assert registry.publish().status_code == 201
    path = f"/v1/packs/{REFERRAL.id}/{REFERRAL.version}"
    version = registry.client.get(path).json()
    assert version["listed"] is True
    namespace = registry.client.get("/v1/namespaces/mendari").json()
    assert namespace["listed"] is True and namespace["claim"] is None
    assert json.loads(_events(db_target)[-1]["payload"])["org"] is None  # no claim, no link
    _drop_claims(db_target)
    with RegistryStore(db_target, read_only=True) as store:
        before = store.verify()
        assert before["ok"], before
        assert store.claims() == []
        assert [p["id"] for p in store.latest()] == [REFERRAL.id]
        found = store.version("mendari", "referral-router", REFERRAL.version)
        assert (found["digest"], found["listed"]) == (version["digest"], True)
        assert store.namespace("mendari", Viewer(namespace="mendari"))["claim"] is None
        assert store.archive(version["archive_sha256"])
        with store._db.session(exclusive=False) as db:
            events = db.all("SELECT * FROM hub_events ORDER BY seq")
            assert not db.columns("hub_claims")  # still as it was: nothing written
    assert main(["registry", "claims", "--db", db_target]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert _events(db_target) == events  # opened read-write: the table comes, no event does
    assert _verify(db_target) == before
    assert registry.client.get(path).json() == version
    assert [p["id"] for p in registry.client.get("/v1/search").json()] == [REFERRAL.id]
    with _client(registry) as client:
        assert pull(REFERRAL.id, client).digest == REFERRAL.digest
    assert registry_module.MAX_PENDING_CLAIMS == 3 and not PUBLIC.reviewer


def _drop_claims(db_target: str) -> None:
    """Drop the table past the store: opening a `RegistryStore` to write creates it again."""
    if db_target.startswith("postgres"):
        import psycopg

        with psycopg.connect(db_target, autocommit=True) as connection:
            connection.execute("DROP TABLE IF EXISTS hub_claims")
        return
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(db_target)) as connection:
        connection.execute("DROP TABLE IF EXISTS hub_claims")
        connection.commit()
