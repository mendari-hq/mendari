# SPDX-License-Identifier: Apache-2.0
"""Promotion, and serving the promoted version (LP-20h): its own chained record, bound to a
digest, signed off by someone who wrote no version of it, on the organization's own reports of
that digest; served to that organization alone, never instead of an agent on disk, and named on
every decision it makes."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari.core.agent_store import AgentStore, StoreError
from tests.support.registry import ROUTER_RULES, TRIAGE
from tests.test_store_jobs import _author, _copy, _dataset, _job, _run
from tests.test_store_uploads import ORG_A, ORG_B, Store, store_world

SERVED = TRIAGE.id  # an agent this deployment serves from disk: the triage fixture
MESSAGE = {"message": {"body": "Could I get a refill of my lisinopril please?"}}


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    yield from store_world(db_target, tmp_path, monkeypatch)


def _report(world: Store, digest: str, org: str = ORG_A) -> str:
    dataset = _dataset(world, digest, org=org).json()
    return str(
        _run(
            world,
            _job(world, org=org, version=digest, dataset={"id": dataset["id"]})["id"],
            org=org,
        )["report_id"]
    )


def _promote(
    world: Store, digest: str, reports: list[str], who: str = "governance", org: str = ORG_A
) -> Any:
    return world.client.post(
        "/api/store/promotions",
        json={"version": digest, "reports": reports, "reference": "CAB-2026-11"},
        headers=world.as_(org, who),
    )


def _decide(world: Store, agent_id: str, org: str = ORG_A) -> Any:
    return world.client.post(
        f"/api/agents/{agent_id}/decide", json=MESSAGE, headers=world.as_(org, "reviewer")
    )


def _promoted(world: Store) -> tuple[str, str, str]:
    digest = _copy(world)
    report = _report(world, digest)
    made = _promote(world, digest, [report])
    assert made.status_code == 201, made.text
    return digest, report, str(made.json()["id"])


def test_the_author_cannot_approve_their_own_promotion(world: Store) -> None:
    """A person who holds `governance` and `agent_author` copies an agent: they may not promote it,
    nor a version a second author edited from it."""
    from tests.conftest import member

    dual = member(world.db, org=ORG_A, roles=["governance", "agent_author"], sub="dual")
    made = world.client.post(
        "/api/store/copies", json={"agent": SERVED, "name": "inbox-router"}, headers=dual
    )
    assert made.status_code == 201, made.text
    digest = made.json()["digest"]
    report = _report(world, digest)
    refused = world.client.post(
        "/api/store/promotions",
        json={"version": digest, "reports": [report], "reference": "x"},
        headers=dual,
    )
    assert refused.status_code == 403 and "author" in refused.text
    agents: AgentStore = world.client.app.state.agent_store  # type: ignore[attr-defined]
    rules = agents.files(ORG_A, digest)[ROUTER_RULES].decode()
    edited = world.client.post(
        f"/api/store/versions/{digest}/edits",
        json={"files": [{"path": ROUTER_RULES, "content": rules + "\n# b\n"}]},
        headers=_author(world),
    ).json()
    assert len(agents.authors(ORG_A, edited["digest"])) == 2
    again = world.client.post(
        "/api/store/promotions",
        json={
            "version": edited["digest"],
            "reports": [_report(world, edited["digest"])],
            "reference": "x",
        },
        headers=dual,
    )
    assert again.status_code == 403
    assert _promote(world, edited["digest"], [_report(world, edited["digest"])]).status_code == 201


@pytest.mark.parametrize("which", ["none", "another digest", "another organization", "unfinished"])
def test_a_promotion_names_reports_of_this_digest_and_this_organization(
    world: Store, which: str
) -> None:
    digest = _copy(world)
    if which == "none":
        reports: list[str] = []
    elif which == "another digest":
        other = _copy(world, name="other-copy")
        reports = [_report(world, other)]
    elif which == "another organization":
        theirs = _copy(world, org=ORG_B)
        reports = [_report(world, theirs, org=ORG_B)]
    else:
        dataset = _dataset(world, digest).json()
        job = _job(world, version=digest, dataset={"id": dataset["id"]})
        reports = [job["id"]]  # a job's id, never a report: it has not finished
    refused = _promote(world, digest, reports)
    assert refused.status_code == 422, refused.text
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 404


def test_every_risk_level_needs_a_promotion(world: Store) -> None:
    digest = _copy(world)
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 404
    listed = world.client.get("/api/agents", headers=world.as_(ORG_A, "viewer")).json()
    assert f"{ORG_A}/inbox-router" not in {p["id"] for p in listed}
    _promote(world, digest, [_report(world, digest)])
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 200


def test_the_automation_approval_is_not_a_promotion_and_a_promotion_is_not_one(
    world: Store,
) -> None:
    digest = _copy(world)
    approval = world.client.post(
        "/api/governance",
        json={"agent_id": f"{ORG_A}/inbox-router", "reference": "x", "days": 30},
        headers=world.as_(ORG_A, "governance"),
    )
    assert approval.status_code in (404, 422)  # an unserved draft cannot even be approved
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 404
    _promote(world, digest, [_report(world, digest)])
    with world.db._db.session(exclusive=False) as db:
        assert db.one("SELECT COUNT(*) AS n FROM governance_approvals")["n"] == 0
        kinds = {r["event"] for r in db.all("SELECT event FROM audit_events")}
    assert "store_version.promoted" in kinds and "governance.approved" not in kinds


def test_a_promoted_version_is_served_to_its_organization_and_to_no_other(world: Store) -> None:
    digest, _, promotion = _promoted(world)
    agent_id = f"{ORG_A}/inbox-router"
    decided = _decide(world, agent_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["agent"]["digest"] == digest
    assert decided.json()["promotion"] == promotion
    mine = world.client.get("/api/agents", headers=world.as_(ORG_A, "viewer")).json()
    assert agent_id in {p["id"] for p in mine}
    theirs = world.client.get("/api/agents", headers=world.as_(ORG_B, "viewer")).json()
    assert agent_id not in {p["id"] for p in theirs}
    assert _decide(world, agent_id, org=ORG_B).status_code == 404
    key = world.client.post(
        "/api/auth/keys", json={"role": "service", "name": "k"}, headers=world.as_(ORG_B, "admin")
    ).json()["key"]
    machine = {"authorization": f"Bearer {key}"}
    assert (
        world.client.post(
            f"/api/agents/{agent_id}/decide", json=MESSAGE, headers=machine
        ).status_code
        == 404
    )
    discovered = world.client.get("/cds-services", headers=machine).json()
    assert all(agent_id not in str(s) for s in discovered.get("services", []))


def test_promotion_is_bound_to_the_digest(world: Store) -> None:
    digest, _, _ = _promoted(world)
    rules = world.client.app.state.agent_store.files(ORG_A, digest)[ROUTER_RULES]  # type: ignore[attr-defined]
    edited = world.client.post(
        f"/api/store/versions/{digest}/edits",
        json={"files": [{"path": ROUTER_RULES, "content": rules.decode() + "\n#x\n"}]},
        headers=_author(world),
    ).json()
    assert edited["digest"] != digest
    assert _decide(world, f"{ORG_A}/inbox-router").json()["agent"]["digest"] == digest


def test_promoting_retires_the_version_before_and_both_are_chained(world: Store) -> None:
    first, _, promotion = _promoted(world)
    rules = world.client.app.state.agent_store.files(ORG_A, first)[ROUTER_RULES]  # type: ignore[attr-defined]
    second = world.client.post(
        f"/api/store/versions/{first}/edits",
        json={"files": [{"path": ROUTER_RULES, "content": rules.decode() + "\n#2\n"}]},
        headers=_author(world),
    ).json()["digest"]
    made = _promote(world, second, [_report(world, second)])
    assert made.status_code == 201
    assert _decide(world, f"{ORG_A}/inbox-router").json()["agent"]["digest"] == second
    agents: AgentStore = world.client.app.state.agent_store  # type: ignore[attr-defined]
    assert agents.version(ORG_A, first)["state"] == "retired"
    assert agents.version(ORG_A, second)["state"] == "promoted"
    assert agents.promotion(ORG_A, promotion)["retired_at"] is not None
    with world.db._db.session(exclusive=False) as db:
        kinds = [r["event"] for r in db.all("SELECT event FROM audit_events WHERE event LIKE ?",
                                            ("store_version.%",))]  # fmt: skip
    assert kinds.count("store_version.promoted") == 2 and kinds.count("store_version.retired") == 1
    assert world.db.verify()["ok"]


def test_a_retired_promotion_stops_serving_at_once_in_this_process(world: Store) -> None:
    _, _, promotion = _promoted(world)
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 200
    retired = world.client.post(
        f"/api/store/promotions/{promotion}/retire", headers=world.as_(ORG_A, "governance")
    )
    assert retired.status_code == 200
    assert _decide(world, f"{ORG_A}/inbox-router").status_code == 404


def test_a_store_agent_never_replaces_an_agent_the_deployment_serves_from_disk(
    world: Store,
) -> None:
    digest = _copy(world)
    report = _report(world, digest)
    agents: AgentStore = world.client.app.state.agent_store  # type: ignore[attr-defined]
    with pytest.raises(StoreError) as error:
        agents.promote(
            ORG_A, digest, approver="someone-else", reports=[report], reference="x",
            served_ids=[f"{ORG_A}/inbox-router"],
        )  # fmt: skip
    assert error.value.status == 409 and "from disk" in str(error.value)


def test_a_decision_names_the_promotion_that_let_its_version_serve(world: Store) -> None:
    _, _, promotion = _promoted(world)
    decided = _decide(world, f"{ORG_A}/inbox-router").json()
    read = world.client.get(f"/api/audit/{decided['id']}", headers=world.as_(ORG_A, "reviewer"))
    assert read.status_code == 200 and read.json()["record"]["promotion"] == promotion


def test_a_held_agent_and_the_kill_switch_still_stop_a_store_agent(world: Store) -> None:
    digest, _, _ = _promoted(world)
    held = world.client.post(
        "/api/holds",
        json={"agent_id": f"{ORG_A}/inbox-router", "reason": "stop it"},
        headers=world.as_(ORG_A, "governance"),
    )
    assert held.status_code == 201, held.text
    decided = _decide(world, f"{ORG_A}/inbox-router").json()
    assert decided["outcome"] != "AUTO"
    assert any("automation is on hold" in reason for reason in decided["reasons"]) or decided[
        "outcome"
    ] in ("HUMAN_REVIEW", "ABSTAIN")
    assert digest


def test_health_answers_within_its_bound_while_a_store_agent_is_built(world: Store) -> None:
    _promoted(world)
    started = time.monotonic()
    assert world.client.get("/api/health").status_code == 200
    assert time.monotonic() - started < 0.5


def test_a_key_a_client_and_an_author_without_the_role_never_promote(world: Store) -> None:
    digest = _copy(world)
    report = _report(world, digest)
    for role in ("agent_author", "reviewer", "viewer", "service"):
        refused = _promote(world, digest, [report], who=role)
        assert refused.status_code == 403, role
    key = world.client.post(
        "/api/auth/keys", json={"role": "service", "name": "k"}, headers=world.as_(ORG_A, "admin")
    ).json()["key"]
    machine = {"authorization": f"Bearer {key}"}
    assert (
        world.client.post(
            "/api/store/promotions",
            json={"version": digest, "reports": [report], "reference": "x"},
            headers=machine,
        ).status_code
        == 403
    )


def test_audit_verify_passes_after_a_promotion(world: Store) -> None:
    _, _, promotion = _promoted(world)
    world.client.post(
        f"/api/store/promotions/{promotion}/retire", headers=world.as_(ORG_A, "governance")
    )
    assert world.db.verify()["ok"]


def test_what_the_security_review_found_is_closed(world: Store) -> None:
    """LP-20h's adversarial review: a promotion to recommend only never automates; the author of
    a version is refused its automation approval; a promoted version that no longer loads leaves
    its organization's other packs served."""
    from mendari.core.registry import resolve_agent
    from tests.conftest import member

    dual = member(world.db, org=ORG_A, roles=["governance", "agent_author"], sub="dual")
    made = world.client.post(
        "/api/store/copies", json={"agent": SERVED, "name": "inbox-router"}, headers=dual
    ).json()
    promoted = _promote(world, made["digest"], [_report(world, made["digest"])])
    assert promoted.status_code == 201, promoted.text
    agents: AgentStore = world.client.app.state.agent_store  # type: ignore[attr-defined]
    assert agents.promoted(ORG_A)[f"{ORG_A}/inbox-router"][2] is False
    decided = _decide(world, f"{ORG_A}/inbox-router").json()
    assert decided["outcome"] != "AUTO"
    assert any("promoted to recommend only" in reason for reason in decided["reasons"])

    refused = world.client.post(
        "/api/governance",
        json={"agent_id": f"{ORG_A}/inbox-router", "reference": "CAB-2026-11", "days": 30},
        headers=dual,
    )
    assert refused.status_code == 403 and "author" in refused.text

    with world.db._db.session(exclusive=True) as db:  # its files gone: it no longer loads
        db.all("DELETE FROM store_files")
    agents._cache.clear()
    time.sleep(5.1)  # past the per-organization cache
    listed = world.client.get("/api/agents", headers=world.as_(ORG_A, "viewer"))
    assert listed.status_code == 200
    ids = {p["id"] for p in listed.json()}
    assert resolve_agent(SERVED).id in ids and f"{ORG_A}/inbox-router" not in ids
