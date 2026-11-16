# SPDX-License-Identifier: Apache-2.0
"""People register evaluating sites and their keys under the engine, and the registry's
reviewers verify them (LP-22i, ADR-0017): an organization's administrators (`keys`, people
only) at `/api/registry/sites`, the review organization's `registry_reviewer` members at
`/api/registry/review/sites`. Synthetic sites only; every key is made here."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari.hub.site_signing import init_site
from tests.test_registry_mount import World, bearer, served, sign_in_on
from tests.test_registry_review import REVIEW_ORG, review_org, reviewer

SITES = "/api/registry/sites"


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The registry under the engine, sign-in on, with a review organization."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", REVIEW_ORG)
    review_org(db_target)
    with served(db_target, tmp_path) as served_world:
        yield served_world


QUEUE = "/api/registry/review/sites"


def _register(world: World, org: str = "acme", name: str = "Acme Hospital (synthetic)") -> Any:
    made = world.http.post(SITES, json={"name": name}, headers=world.person(org))
    assert made.status_code == 201, made.text
    return made.json()


def _key_file(world: World, site_id: str) -> dict[str, Any]:
    return init_site(site_id, directory=Path(world.tmp_path) / f"site-{site_id}")


def test_a_member_registers_a_site_and_a_reviewer_verifies_it(world: World) -> None:
    site = _register(world)
    assert (site["status"], site["keys"]) == ("pending", [])
    assert "org" not in site
    key = _key_file(world, site["id"])
    added = world.http.post(
        f"{SITES}/{site['id']}/keys",
        json={"public_pem": key["public_pem"], "proof": key["proof"]},
        headers=world.person("acme"),
    )
    assert added.status_code == 201, added.text
    assert [k["key_id"] for k in added.json()["keys"]] == [key["key_id"]]
    rita = reviewer(world)
    (waiting,) = world.http.get(QUEUE, headers=rita).json()["pending"]
    assert (waiting["id"], waiting["org"], waiting["own"]) == (site["id"], "acme", False)
    assert waiting["keys"] == [key["key_id"]]
    decided = world.http.post(
        f"{QUEUE}/{site['id']}",
        json={"decision": "approve", "note": "called their CMIO"},
        headers=rita,
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "verified"
    assert world.http.get(QUEUE, headers=rita).json()["pending"] == []
    public = world.http.get(f"/registry/v1/sites/{site['id']}").json()
    assert public["status"] == "verified" and public["name"] == "Acme Hospital (synthetic)"
    again = world.http.post(f"{QUEUE}/{site['id']}", json={"decision": "refuse"}, headers=rita)
    assert again.status_code == 409
    # Revoked, with why and whether compromised.
    revoked = world.http.request(
        "DELETE",
        f"{SITES}/{site['id']}/keys/{key['key_id']}",
        json={"reason": "laptop lost", "compromised": True},
        headers=world.person("acme"),
    )
    assert revoked.status_code == 200, revoked.text
    (shown,) = revoked.json()["keys"]
    assert shown["revoked_at"] and shown["compromised"] is True
    assert world.registry.verify()["ok"]


def test_one_organization_never_sees_or_changes_anothers_sites(world: World) -> None:
    site = _register(world)
    key = _key_file(world, site["id"])
    other = world.person("globex")
    assert world.http.get(SITES, headers=other).json() == []
    attempts = [
        (
            "POST",
            f"{SITES}/{site['id']}/keys",
            {"public_pem": key["public_pem"], "proof": key["proof"]},
        ),
        ("DELETE", f"{SITES}/{site['id']}/keys/{key['key_id']}", {"reason": "x"}),
    ]
    for method, path, body in attempts:
        answer = world.http.request(method, path, json=body, headers=other)
        assert answer.status_code == 404, (path, answer.text)
    assert world.registry.site_record(site["id"])["keys"] == []


def test_a_key_a_client_and_a_viewer_cannot_register_a_site(world: World) -> None:
    boss = world.person("acme")
    made = world.http.post("/api/auth/keys", json={"name": "ci", "role": "service"}, headers=boss)
    key = bearer(made.json()["key"])
    for headers in (key, world.person("acme", "viewer"), world.person("acme", "reviewer"), None):
        answer = world.http.post(SITES, json={"name": "X (synthetic)"}, headers=headers)
        assert answer.status_code in (401, 403), answer.text
    assert world.registry.sites() == []


def test_a_reviewer_does_not_verify_their_own_organizations_site(world: World) -> None:
    own = _register(world, org=REVIEW_ORG, name="Review org's own site (synthetic)")
    rita = reviewer(world)
    (waiting,) = world.http.get(QUEUE, headers=rita).json()["pending"]
    assert waiting["own"] is True
    answer = world.http.post(f"{QUEUE}/{own['id']}", json={"decision": "approve"}, headers=rita)
    assert answer.status_code == 403
    assert world.registry.site_record(own["id"])["status"] == "pending"


def test_an_administrator_elsewhere_is_not_a_site_reviewer(world: World) -> None:
    site = _register(world)
    for headers in (
        world.person("acme"),
        world.person(REVIEW_ORG),
        world.person("acme", "registry_reviewer", sub="stray"),
    ):
        assert world.http.get(QUEUE, headers=headers).status_code == 403
        answer = world.http.post(
            f"{QUEUE}/{site['id']}", json={"decision": "approve"}, headers=headers
        )
        assert answer.status_code == 403
    assert world.registry.site_record(site["id"])["status"] == "pending"


def test_another_sites_page_cannot_register_or_verify(world: World) -> None:
    cross = {"sec-fetch-site": "cross-site", "origin": "https://evil.example"}
    boss = {"cookie": world.person("acme")["cookie"], **cross}
    assert world.http.post(SITES, json={"name": "X (synthetic)"}, headers=boss).status_code == 403
    site = _register(world)
    rita = {"cookie": reviewer(world)["cookie"], **cross}
    answer = world.http.post(f"{QUEUE}/{site['id']}", json={"decision": "approve"}, headers=rita)
    assert answer.status_code == 403
    assert world.registry.site_record(site["id"])["status"] == "pending"
