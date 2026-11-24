# SPDX-License-Identifier: Apache-2.0
"""The registry's reviewers (LP-19e, ADR-0015).

`MENDARI_REGISTRY_REVIEW_ORG` names the one organization whose `registry_reviewer` members
review the registry mounted under the engine: the queue of pending claims and the acts that were
the administrator token's, at `/api/registry/review/...`. The role counts in that organization
only and is refused when given in any other; without the setting nobody reviews through the web.
A key, a client, open mode and claims mode never review. On SQLite and Postgres."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.hub import yank
from mendari.hub.registry import REVIEWER
from mendari.server.app import create_app
from mendari.server.auth import person_id
from tests.conftest import encrypted_store, security_lines, signed_in
from tests.test_backend_services import RSA_KEY, ask, assertion
from tests.test_hub_client import SHIPPED
from tests.test_registry_mount import APP, IDP, World, bearer, served, sign_in_on

REVIEW_ORG = "registry-review"
REVIEW = "/api/registry/review"
VERSION = f"{REVIEW}/packs/acme/inbox/{SHIPPED}"
# Every reviewer route, with a body that is valid for it.
REVIEW_ROUTES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", REVIEW, None),
    ("POST", f"{REVIEW}/namespaces/acme", {"decision": "approve", "note": "read"}),
    ("POST", f"{REVIEW}/namespaces/acme/publisher-type", {"publisher_type": "verified"}),
    ("GET", VERSION, None),
    ("POST", VERSION, {"risk_level": 1, "note": "read"}),
    ("POST", f"{VERSION}/recall", {"reason": "a fault", "severity": "high"}),
    ("POST", f"{VERSION}/delete", {"reason": "carried what it should not"}),
)


def review_org(db: str, name: str = REVIEW_ORG) -> None:
    """The review organization's record, made before the server starts as the operator makes
    it (`mendari access org create`)."""
    with encrypted_store(db) as store:
        store.create_organization(org=name, name="Registry review", created_by="test")


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", REVIEW_ORG)
    review_org(db_target)
    with served(db_target, tmp_path) as served_world:
        yield served_world


def reviewer(world: World, sub: str = "rita") -> dict[str, str]:
    return world.person(REVIEW_ORG, "registry_reviewer", sub=sub)


def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(yank.KEY_ENV, base64.b64encode(b"r" * 32).decode())


def refused_everywhere(world: World, headers: dict[str, str] | None, status: int) -> None:
    for method, path, body in REVIEW_ROUTES:
        answer = world.http.request(method, path, json=body, headers=headers)
        assert answer.status_code == status, (method, path, answer.status_code, answer.text)


def test_a_first_publish_stays_invisible_until_a_reviewer_approves_it(world: World) -> None:
    world.published()
    public = f"/registry/v1/agents/acme/inbox/{SHIPPED}"
    assert world.http.get(public).status_code == 404
    assert world.http.get("/registry/v1/search").json() == []
    rita = reviewer(world)
    (item,) = world.http.get(REVIEW, headers=rita).json()["pending"]
    assert (item["namespace"], item["org"], item["own"]) == ("acme", "acme", False)
    assert item["claimed_by"] == person_id(IDP, "acme-admin")
    assert [v["version"] for v in item["versions"]] == [SHIPPED]

    approved = world.http.post(
        f"{REVIEW}/namespaces/acme", json={"decision": "approve", "note": "read"}, headers=rita
    )
    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["decided_by"]) == (
        "approved",
        person_id(IDP, "rita"),
    )
    assert world.http.get(public).status_code == 200
    assert [p["id"] for p in world.http.get("/registry/v1/search").json()] == ["acme/inbox"]
    assert world.http.get(REVIEW, headers=rita).json() == {"pending": [], "more": 0}
    # Once: a decided claim is not decided again, either way.
    again = world.http.post(
        f"{REVIEW}/namespaces/acme", json={"decision": "refuse", "note": ""}, headers=rita
    )
    assert again.status_code == 409 and "already approved" in again.text


def test_an_administrator_of_another_organization_is_not_a_reviewer(world: World) -> None:
    world.published()
    # Every role of the claimant's own organization, and of the review organization but the
    # reviewer's: its administrator is not a reviewer until given the role.
    for org in ("acme", REVIEW_ORG):
        for role in ("admin", "governance", "reviewer", "viewer", "service", "labeller"):
            refused_everywhere(world, world.person(org, role, sub=f"{org}-{role}"), 403)
    # The role held in another organization (a membership written past the routes, as the
    # operator's command could) grants nothing there.
    stray = world.person("acme", "registry_reviewer", sub="stray")
    refused_everywhere(world, stray, 403)
    me = world.http.get("/api/auth/me", headers=stray).json()["principal"]
    assert me["permissions"] == [] and me["roles"] == []
    refused_everywhere(world, None, 401)
    assert world.registry.claims(status="pending")[0]["namespace"] == "acme"
    assert world.http.get(f"/registry/v1/agents/acme/inbox/{SHIPPED}").status_code == 404


def test_the_reviewer_role_cannot_be_granted_outside_the_review_organization(
    world: World,
) -> None:
    roles = ["registry_reviewer", "viewer"]
    elsewhere = world.person("acme")
    invited = world.http.post(
        "/api/auth/invitations", json={"email": "x@example.org", "roles": roles}, headers=elsewhere
    )
    assert invited.status_code == 422 and "review organization" in invited.text
    world.person("acme", "viewer", sub="vic")
    members = world.http.get("/api/auth/members", headers=elsewhere).json()
    vic = next(m for m in members if m["roles"] == ["viewer"])
    changed = world.http.put(
        f"/api/auth/members/{vic['id']}/roles", json={"roles": roles}, headers=elsewhere
    )
    assert changed.status_code == 422, changed.text
    # Nor by granting a request to join with it.
    with encrypted_store(world.db) as store:
        store.record_user(issuer=IDP, subject="asker", email=None, name="asker")
        asked = store.request_access(user_id=person_id(IDP, "asker"), org="acme")
    granted = world.http.post(
        f"/api/auth/access-requests/{asked['id']}",
        json={"outcome": "granted", "roles": roles},
        headers=elsewhere,
    )
    assert granted.status_code == 422, granted.text
    with encrypted_store(world.db) as store:
        assert not [m for m in store.memberships(org="acme") if "registry_reviewer" in m["roles"]]
    # In the review organization its administrator gives it, and it reviews at once.
    boss = world.person(REVIEW_ORG)
    world.person(REVIEW_ORG, "viewer", sub="new")
    members = world.http.get("/api/auth/members", headers=boss).json()
    new = next(m for m in members if m["roles"] == ["viewer"])
    given = world.http.put(
        f"/api/auth/members/{new['id']}/roles", json={"roles": roles}, headers=boss
    )
    assert given.status_code == 200, given.text
    assert world.http.get(REVIEW, headers=world.person(REVIEW_ORG, sub="new")).status_code == 200
    invited = world.http.post(
        "/api/auth/invitations", json={"email": "y@example.org", "roles": roles}, headers=boss
    )
    assert invited.status_code == 201, invited.text


def test_without_a_review_organization_nobody_reviews_and_nothing_lists(
    db_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    review_org(db_target)
    # Unset; then naming an organization that has no record (said at startup, never fatal).
    for named in (None, "nobody-here"):
        if named is not None:
            monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", named)
        with served(db_target, tmp_path) as world:
            held = world.person(REVIEW_ORG, "registry_reviewer", sub="held")
            for method, path, body in REVIEW_ROUTES:
                answer = world.http.request(method, path, json=body, headers=held)
                assert answer.status_code in (403, 404), (path, answer.status_code)
            me = world.http.get("/api/auth/me", headers=held).json()["principal"]
            assert "registry_review" not in me["permissions"]
            assert not [r for r in world.app.routes if getattr(r, "path", "").startswith(REVIEW)]
            # And nobody gives the role anywhere.
            invited = world.http.post(
                "/api/auth/invitations",
                json={"email": "x@example.org", "roles": ["registry_reviewer"]},
                headers=world.person(REVIEW_ORG),
            )
            assert invited.status_code == 422
            assert world.registry.claims() == []
    assert "no organization 'nobody-here' has a record" in caplog.text
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", "Not An Id")
    with pytest.raises(Exception, match="MENDARI_REGISTRY_REVIEW_ORG"):
        create_app(db_path=db_target)


def test_a_reviewer_does_not_decide_their_own_claim(world: World) -> None:
    """Someone who claims a namespace in one organization and reviews in the other."""
    both = world.person("acme", sub="both")  # an administrator of acme...
    made = world.http.post(
        "/api/registry/namespaces", json={"name": "acme", "display_name": "Acme"}, headers=both
    )
    assert made.status_code == 201, made.text
    key = world.key()
    world.publish(world.pack(), key, bearer(str(world.mint()["token"])))
    as_reviewer = world.person(REVIEW_ORG, "registry_reviewer", sub="both")  # ...who reviews
    (item,) = world.http.get(REVIEW, headers=as_reviewer).json()["pending"]
    assert item["own"] is True
    # Not its decision, and nothing the registry would then show as checked or do to it.
    before = len(world.events())
    for method, path, body in REVIEW_ROUTES:
        if method == "POST":
            own = world.http.post(path, json=body, headers=as_reviewer)
            assert own.status_code == 403 and "you claimed" in own.text, (path, own.text)
    refuse = {"decision": "refuse", "note": ""}
    own = world.http.post(f"{REVIEW}/namespaces/acme", json=refuse, headers=as_reviewer)
    assert own.status_code == 403
    assert world.registry.claims()[0]["status"] == "pending" and len(world.events()) == before
    other = world.http.post(
        f"{REVIEW}/namespaces/acme",
        json={"decision": "approve", "note": "read"},
        headers=reviewer(world),
    )
    assert other.status_code == 200, other.text


def test_a_key_a_client_open_mode_and_claims_mode_never_review(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.published()
    boss = world.person(REVIEW_ORG)
    credentials = []
    for role in ("viewer", "service"):
        made = world.http.post("/api/auth/keys", json={"name": role, "role": role}, headers=boss)
        credentials.append(bearer(made.json()["key"]))
    registered = world.http.post(
        "/api/auth/clients",
        json={"name": "backend", "role": "service", "jwks": {"keys": [RSA_KEY.jwk]}},
        headers=boss,
    )
    granted = ask(world.http, assertion(RSA_KEY, registered.json()["client_id"]))
    credentials.append(bearer(granted.json()["access_token"]))
    for headers in credentials:
        refused_everywhere(world, headers, 403)
    # A publisher token is no credential on the engine's own routes.
    refused_everywhere(world, bearer(str(world.mint()["token"])), 401)
    assert world.registry.claims()[0]["status"] == "pending"

    # Claims mode: roles come from the identity provider, and none of them is the reviewer's.
    monkeypatch.setenv("MENDARI_ACCOUNTS", "claims")
    claims_db = str(tmp_path / "claims.db")
    review_org(claims_db)
    with served(claims_db, tmp_path) as claims:
        with encrypted_store(claims_db) as store:
            # An administrator there, and someone whose identity provider sends the role's name.
            people = [
                signed_in(store, role=role, org=REVIEW_ORG)
                for role in ("admin", "registry_reviewer")
            ]
        for sent_by in (None, *people):
            for method, path, body in REVIEW_ROUTES:
                answer = claims.http.request(method, path, json=body, headers=sent_by)
                assert answer.status_code in (401, 403, 404), (path, answer.status_code)
        assert not [r for r in claims.app.routes if getattr(r, "path", "").startswith(REVIEW)]
    # Open mode (no sign-in): everything else is open, never this.
    for name in ("MENDARI_OIDC_ISSUER", "MENDARI_OIDC_CLIENT_ID", "MENDARI_ACCOUNTS"):
        monkeypatch.delenv(name)
    monkeypatch.delenv("MENDARI_BACKEND_SERVICES")
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    opened = create_app(db_path=str(tmp_path / "open.db"))
    try:
        with TestClient(opened, base_url=APP) as http:
            for method, path, body in REVIEW_ROUTES:
                answer = http.request(method, path, json=body)
                assert answer.status_code in (403, 404), (path, answer.status_code)
    finally:
        opened.state.registry_store.close()


def test_approval_refusal_review_recall_and_deletion_are_chained_by_the_reviewers_id(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    signing_key(monkeypatch)
    world.published()
    world.claim("lookalike", org="rival")
    rita = reviewer(world)
    who = person_id(IDP, "rita")
    sent = [
        (f"{REVIEW}/namespaces/acme", {"decision": "approve", "note": "read"}, 200),
        (f"{REVIEW}/namespaces/lookalike", {"decision": "refuse", "note": "a lookalike"}, 200),
        (f"{REVIEW}/namespaces/acme/publisher-type", {"publisher_type": "verified"}, 200),
        (VERSION, {"risk_level": 2, "note": "reads as level 2"}, 201),
        (f"{VERSION}/recall", {"reason": "a fault", "severity": "high"}, 201),
        (f"{VERSION}/delete", {"reason": "carried what it should not"}, 200),
    ]
    for path, body, status in sent:
        answer = world.http.post(path, json=body, headers=rita)
        assert answer.status_code == status, (path, answer.text)
    events = [
        e
        for e in world.events()
        if e["event"]
        in (
            "namespace.approved",
            "namespace.refused",
            "namespace.publisher_type_set",
            "pack.reviewed",
            "pack.yanked",
            "pack.deleted",
        )
    ]
    assert [e["event"] for e in events] == [
        "namespace.approved",
        "namespace.refused",
        "namespace.publisher_type_set",
        "pack.reviewed",
        "pack.yanked",
        "pack.deleted",
    ]
    assert [e.get("by", e.get("reviewer")) for e in events] == [who] * 6
    same = world.http.post(sent[2][0], json=sent[2][1], headers=rita)  # nothing changed:
    assert same.status_code == 200  # so nothing more is chained
    assert [e["event"] for e in world.events()].count("namespace.publisher_type_set") == 1
    assert [e.get("org") for e in events[:2]] == ["acme", "rival"]  # the claim's, not hers
    assert "example.org" not in json.dumps(world.events())
    assert world.registry.verify()["ok"] is True
    # What the acts did: served as verified and reviewed, then recalled, then gone.
    served_now = world.http.get(f"/registry/v1/agents/acme/inbox/{SHIPPED}").json()
    assert served_now["checked"]["publisher_type"] == "verified"
    assert served_now["checked"]["review"]["reviewer"] == who
    assert served_now["yank"]["signer"] == "registry" and served_now["deleted"]["reason"]
    assert world.http.get(f"/registry/v1/agents/acme/inbox/{SHIPPED}/archive").status_code != 200
    # Once each: a second recall or deletion, and what does not exist.
    for path, body, _ in sent[4:]:
        assert world.http.post(path, json=body, headers=rita).status_code == 409
    missing = f"{REVIEW}/packs/acme/nothing/1.0.0"
    assert world.http.post(missing, json=sent[3][1], headers=rita).status_code == 404
    odd = world.http.post(f"{REVIEW}/packs/acme/inbox/not a version", json=sent[3][1], headers=rita)
    assert odd.status_code == 404 and "not a version" not in odd.text
    # The security log: the reviewer's id, the review organization, ids only.
    logged = {line["event"]: line for line in security_lines(capsys.readouterr().out)}
    for event in (
        "admin.registry_claim.approved",
        "admin.registry_claim.refused",
        "admin.registry_namespace.publisher_type_set",
        "admin.registry_pack.reviewed",
        "admin.registry_pack.recalled",
        "admin.registry_pack.deleted",
    ):
        assert (logged[event]["actor"], logged[event]["org"]) == (who, REVIEW_ORG), event


def test_the_queue_shows_what_the_registry_checked_apart_from_what_is_claimed(
    world: World,
) -> None:
    world.published()
    world.claim("empty", org="other")  # claimed, nothing published: listed, with no version
    rita = reviewer(world)
    queue = world.http.get(REVIEW, headers=rita).json()
    assert [(i["namespace"], len(i["versions"])) for i in queue["pending"]] == [
        ("acme", 1),
        ("empty", 0),
    ]
    (version,) = queue["pending"][0]["versions"]
    assert version["checked"]["review"] == {"status": "unreviewed"}
    assert version["checked"]["digest"] == version["digest"]
    assert "risk" in version["claimed"] and "risk" not in version["checked"]
    # The queue carries no card (a publisher chooses its size); one version is read with the
    # card the registry rendered from the archive, never an uploaded one.
    assert "card_markdown" not in version
    one = world.http.get(VERSION, headers=rita).json()
    stored = world.registry.version("acme", "inbox", SHIPPED, REVIEWER)
    assert isinstance(one["card_markdown"], str) and one["card_markdown"]
    assert one["card_markdown"] == stored["card_markdown"]
    assert {k: v for k, v in one.items() if k != "card_markdown"} == version
    assert set(queue["pending"][0]) >= {"org", "claimed_by", "keys", "more_versions"}
    # A claim with nothing published cannot be approved yet, and says why.
    early = world.http.post(
        f"{REVIEW}/namespaces/empty", json={"decision": "approve", "note": ""}, headers=rita
    )
    assert early.status_code == 409 and "no published version" in early.text
    # What a reviewer sends is checked before anything is written.
    for path, body in (
        (f"{REVIEW}/namespaces/acme", {"decision": "maybe"}),
        (f"{REVIEW}/namespaces/acme", {"decision": "refuse", "note": "two\nlines"}),
        (f"{REVIEW}/namespaces/acme/publisher-type", {"publisher_type": "official"}),
        (VERSION, {"risk_level": 9}),
        (f"{VERSION}/recall", {"reason": "x", "severity": "dire"}),
        (f"{VERSION}/delete", {"reason": ""}),
        (f"{VERSION}/delete", {"reason": "   "}),
        (VERSION, {"risk_level": 1, "note": "  "}),
        (f"{VERSION}/recall", {"reason": " ", "severity": "low"}),
    ):
        assert world.http.post(path, json=body, headers=rita).status_code == 422, (path, body)
    assert [c["status"] for c in world.registry.claims()] == ["pending", "pending"]


def test_the_recall_needs_the_signing_key_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.published()
    rita = reviewer(world)
    body = {"reason": "a fault", "severity": "critical"}
    unkeyed = world.http.post(f"{VERSION}/recall", json=body, headers=rita)
    assert unkeyed.status_code == 409 and yank.KEY_ENV in unkeyed.text
    assert not [e for e in world.events() if e["event"] == "pack.yanked"]
    signing_key(monkeypatch)
    recalled = world.http.post(f"{VERSION}/recall", json=body, headers=rita)
    assert recalled.status_code == 201, recalled.text
    record = recalled.json()["yank"]
    assert (record["signer"], record["severity"], record["reason"]) == (
        "registry",
        "critical",
        "a fault",
    )


def test_another_sites_page_cannot_review(world: World) -> None:
    world.published()
    cookie = {"cookie": reviewer(world)["cookie"]}
    cross = {"sec-fetch-site": "cross-site", "origin": "https://evil.example"}
    for method, path, body in REVIEW_ROUTES:
        answer = world.http.request(method, path, json=body, headers={**cookie, **cross})
        assert answer.status_code == 403, (path, answer.status_code, answer.text)
    assert world.registry.claims()[0]["status"] == "pending"


def test_the_operators_commands_set_a_type_review_recall_and_delete(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Where nobody signs in, or there is no review organization: on the database."""
    if not world.db.endswith(".db"):
        pytest.skip("the operator's commands are tested on a SQLite file")
    world.published()
    capsys.readouterr()
    spec = f"acme/inbox@{SHIPPED}"
    db = ["--db", world.db]
    assert main(["registry", "recall", spec, *db, "--severity", "high", "--reason", "x"]) != 0
    assert yank.KEY_ENV in capsys.readouterr().err
    signing_key(monkeypatch)
    ran = [
        ["publisher-type", *db, "--namespace", "acme", "--type", "verified", "--reason", "met"],
        ["review", spec, *db, "--risk-level", "1", "--reason", "reads as level 1"],
        ["recall", spec, *db, "--severity", "high", "--reason", "a fault"],
        ["delete", spec, *db, "--reason", "carried what it should not"],
    ]
    for command in ran:
        assert main(["registry", *command]) == 0, capsys.readouterr().err
        json.loads(capsys.readouterr().out)
    events = [
        e for e in world.events() if str(e.get("by", e.get("reviewer"))).startswith("command")
    ]
    assert [e["event"] for e in events] == [
        "namespace.publisher_type_set",
        "pack.reviewed",
        "pack.yanked",
        "pack.deleted",
    ]
    assert (
        main(["registry", "review", "acme/inbox", *db, "--risk-level", "1", "--reason", "x"]) != 0
    )
    assert "namespace/name@version" in capsys.readouterr().err
    assert main(["registry", "delete", spec, *db, "--reason", "two\nlines"]) != 0
    assert main(["registry", "verify", *db]) == 0
