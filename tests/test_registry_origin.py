# SPDX-License-Identifier: Apache-2.0
"""Where a pulled agent came from, and the cap on it (LP-18d, ADR-0012): an agent from a registry
never automates until its organization approves its digest, at any risk level, and nothing in
the agent lifts it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision, load_agent
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.origin import MARKER, ORIGIN_FORMAT, UNAPPROVED
from mendari.core.policy import GovernanceOverride
from mendari.core.spec import Outcome
from mendari.hub.client import agents_cache, pull
from mendari.server.app import create_app
from tests.conftest import TRIAGE_DIR
from tests.support.registry import BILL, BLOCK_RULE, OUTSIDE_ID
from tests.test_hub_client import Hub, copy_agent, hub  # noqa: F401 - the live registry fixture
from tests.test_registry_engine import copied, outside_sorter

GOVERNANCE = GovernanceOverride(approved_by="committee", reference="minutes 12")


def _risk(level: int) -> Any:
    def edit(manifest: dict[str, Any]) -> None:
        manifest["risk"]["level"] = level

    return edit


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_a_pulled_agent_never_automates_before_its_digest_is_approved(
    tmp_path: Path, level: int
) -> None:
    """At every risk level (2 and 3 with their governance approval too): HUMAN_REVIEW with the
    same recommendation, saying why; approved, the agent decides under its own policy."""
    agent = outside_sorter(tmp_path, manifest=_risk(level))
    governance = GOVERNANCE if level >= 2 else None
    held = Decision(agent, governance=governance).decide(BILL)
    approved = Decision(agent, governance=governance, origin_approval=lambda _: True).decide(BILL)
    assert approved.outcome is Outcome.AUTO
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert held.recommendation == approved.recommendation
    assert any(UNAPPROVED in reason for reason in held.reasons)
    # An approval of another digest is none.
    other = Decision(agent, origin_approval=lambda p: p.digest == "sha256:" + "0" * 64)
    assert other.decide(BILL).outcome is Outcome.HUMAN_REVIEW


def test_nothing_in_the_agent_lifts_the_cap(tmp_path: Path) -> None:
    """Not a publisher type of `verified`, nor being a byte-for-byte copy of a reference agent
    (the cap follows where the agent came from, not its bytes)."""

    def verified(manifest: dict[str, Any]) -> None:
        manifest["publisher"]["type"] = "verified"

    claimed = outside_sorter(tmp_path / "claimed", manifest=verified)
    assert Decision(claimed).decide(BILL).outcome is Outcome.HUMAN_REVIEW
    reference = load_agent(copied(tmp_path, TRIAGE_DIR.name), origin="hub")
    assert not reference.bounded  # the maintained engine, and still capped
    assert Decision(reference)._origin_holds() == [UNAPPROVED]
    owned = load_agent(copied(tmp_path / "own", TRIAGE_DIR.name), origin="local")
    assert owned.digest == reference.digest and Decision(owned)._origin_holds() == []


def test_a_recall_recorded_at_pull_holds_even_an_approved_agent(tmp_path: Path) -> None:
    agent = outside_sorter(tmp_path)
    record = {"format": ORIGIN_FORMAT, "yank": {"severity": "high", "reason": "misroutes"}}
    (agent.root / MARKER).write_text(json.dumps(record))
    result = Decision(agent, origin_approval=lambda _: True).decide(BILL)
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert any("recalled by its registry (high)" in reason for reason in result.reasons)


def test_checking_an_agent_measures_its_own_policy(tmp_path: Path) -> None:
    """`mendari test` and `mendari eval` check what the agent itself decides: the cap is a
    serving rule (`origin_cap=False`)."""
    agent = outside_sorter(tmp_path)
    assert Decision(agent, origin_cap=False).decide(BILL).outcome is Outcome.AUTO


def test_the_marker_records_where_a_pull_came_from(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
) -> None:
    token, key = hub.namespace("site-a")
    agent = copy_agent(tmp_path, "site-a/inbox")
    published = hub.client(token).push(agent, key)
    pulled = pull("site-a/inbox", hub.client())
    record = pulled.pulled
    assert record["format"] == ORIGIN_FORMAT
    assert {k: record[k] for k in ("registry", "namespace", "publisher_type", "key_id")} == {
        "registry": hub.url,
        "namespace": "site-a",
        "publisher_type": "community",
        "key_id": published["key_id"],
    }
    assert (record["archive_sha256"], record["digest"]) == (
        published["archive_sha256"],
        agent.digest,
    )
    assert record["yank"] is None


def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, Any]:
    """A server whose search path holds a pulled pack (an outside publisher's agent), which
    automates `BILL` under its own policy."""
    agent = outside_sorter(tmp_path / "source")
    target = agents_cache() / OUTSIDE_ID / agent.version
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(agent.root, target)
    (target / MARKER).write_text(
        json.dumps({"format": ORIGIN_FORMAT, "registry": "https://hub.example.org"})
    )
    monkeypatch.setenv("MENDARI_AGENTS", OUTSIDE_ID)
    return TestClient(create_app(db_path=tmp_path / "engine.db")), agent.digest, BILL


def test_an_organization_approves_a_pulled_digest_and_can_revoke_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mendari.server import recalls

    monkeypatch.setattr(recalls, "fetch_yanks", lambda _: [])  # its registry recalls nothing
    monkeypatch.setattr(recalls, "fetch_signs", lambda _: False)  # and has no signing key
    client, digest, automated = _served(tmp_path, monkeypatch)
    with client:
        client.app.state.recall_watch.refresh("https://hub.example.org")  # type: ignore[attr-defined]
        decide = f"/api/agents/{OUTSIDE_ID}/decide"
        assert client.post(decide, json=automated).json()["outcome"] == "HUMAN_REVIEW"
        state = client.get("/api/registry-approvals", params={"agent_id": OUTSIDE_ID}).json()
        assert state["origin"] == "hub" and state["active"] is None
        assert state["pulled"]["registry"] == "https://hub.example.org"
        body = {"agent_id": OUTSIDE_ID, "agent_digest": digest, "reference": "minutes 4"}
        wrong = client.post(
            "/api/registry-approvals", json={**body, "agent_digest": "sha256:0" * 8}
        )
        assert wrong.status_code == 422
        local = {**body, "agent_id": load_agent(TRIAGE_DIR).id}  # served from its own files
        assert client.post("/api/registry-approvals", json=local).status_code in (404, 422)
        approved = client.post("/api/registry-approvals", json={**body, "approver": "dr-a"})
        assert approved.status_code == 201, approved.text
        assert client.post(decide, json=automated).json()["outcome"] == "AUTO"
        revoked = client.delete(f"/api/registry-approvals/{approved.json()['id']}")
        assert revoked.status_code == 204
        assert client.post(decide, json=automated).json()["outcome"] == "HUMAN_REVIEW"
    store = AuditStore(tmp_path / "engine.db")
    with store._db.session(exclusive=False) as db:
        events = [
            r["event"]
            for r in db.all(
                "SELECT event FROM audit_events WHERE decision_id = ? ORDER BY seq",
                (f"registry_approval:{approved.json()['id']}",),
            )
        ]
    assert events == ["registry_agent.approved", "registry_agent.revoked"]
    assert store.verify()["ok"] is True


def test_the_command_line_decides_a_pulled_agent_capped_unless_its_trail_approved_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, digest, automated = _served(tmp_path, monkeypatch)
    with client:
        pass
    text = json.dumps(automated)
    (tmp_path / "input.json").write_text(text)
    args = ["decide", OUTSIDE_ID, "--input", str(tmp_path / "input.json")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "HUMAN_REVIEW"
    AuditStore(tmp_path / "engine.db").approve_registry_agent(
        agent_id=OUTSIDE_ID,
        agent_digest=digest,
        registry="x",
        approver="a",
        reference="r",
    )
    assert main([*args, "--db", str(tmp_path / "engine.db")]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "AUTO"


def test_an_unapproved_pulled_agents_own_block_goes_to_a_person(tmp_path: Path) -> None:
    """The LP-18d review: its own rule's BLOCK is final and seen by nobody, so before approval
    (or while recalled) a person decides instead; approved, its rule blocks as written."""

    def blocks(manifest: dict[str, Any]) -> None:
        manifest["policy"]["rules"].append(BLOCK_RULE)

    agent = outside_sorter(tmp_path, manifest=blocks)
    held = Decision(agent).decide(BILL)
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert any("a person decides instead of its rule's BLOCK" in r for r in held.reasons)
    approved = Decision(agent, origin_approval=lambda _: True).decide(BILL)
    assert approved.outcome is Outcome.BLOCK


def test_risk_level_4_never_automates_even_approved(tmp_path: Path) -> None:
    agent = outside_sorter(tmp_path, manifest=_risk(4))
    decided = Decision(agent, governance=GOVERNANCE, origin_approval=lambda _: True).decide(BILL)
    assert decided.outcome is Outcome.HUMAN_REVIEW


def test_an_approval_is_one_organizations_and_one_a_digest(tmp_path: Path) -> None:
    from mendari.core import tenancy
    from mendari.errors import ReviewError
    from mendari.server.auth import permission_for

    store = AuditStore(tmp_path / "a.db")
    approval = {
        "agent_id": OUTSIDE_ID,
        "agent_digest": "sha256:" + "1" * 64,
        "registry": "https://hub.example.org",
        "approver": "a",
        "reference": "r",
    }
    with tenancy.acting_for("org-a"):
        made = store.approve_registry_agent(**approval)
        with pytest.raises(ReviewError, match="approved already"):
            store.approve_registry_agent(**approval)
    with tenancy.acting_for("org-b"):
        assert store.registry_approval(approval["agent_id"], approval["agent_digest"]) is None
        assert store.revoke_registry_approval(made["id"], revoked_by="b") is False
    with tenancy.acting_for("org-a"):
        assert store.registry_approval(approval["agent_id"], approval["agent_digest"]) is not None
    assert permission_for("POST", "/api/registry-approvals") == "approve"
    assert permission_for("DELETE", "/api/registry-approvals/x") == "approve"


def test_the_marker_never_records_a_registrys_credentials() -> None:
    from mendari.hub.client import _shown_url

    assert _shown_url("https://user:secret@hub.example.org:8443/base?token=t#x") == (
        "https://hub.example.org:8443/base"
    )
