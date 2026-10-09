# SPDX-License-Identifier: Apache-2.0
"""Where a pulled pack came from, and the cap on it (LP-18d, ADR-0012): a pack from a registry
never automates until its organization approves its digest, at any risk level, and nothing in
the pack lifts it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.origin import MARKER, ORIGIN_FORMAT, UNAPPROVED, pulled_record
from mendari.core.policy import GovernanceOverride
from mendari.core.spec import Outcome
from mendari.hub.client import packs_cache, pull
from mendari.server.app import create_app
from tests.test_hub_client import Hub, copy_pack, hub  # noqa: F401 - the live registry fixture
from tests.test_registry_engine import BILL, copied, outside_sorter

GOVERNANCE = GovernanceOverride(approved_by="committee", reference="minutes 12")


def _risk(level: int) -> Any:
    def edit(manifest: dict[str, Any]) -> None:
        manifest["risk"]["level"] = level

    return edit


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_a_pulled_pack_never_automates_before_its_digest_is_approved(
    tmp_path: Path, level: int
) -> None:
    """At every risk level (2 and 3 with their governance approval too): HUMAN_REVIEW with the
    same recommendation, saying why; approved, the pack decides under its own policy."""
    pack = outside_sorter(tmp_path, manifest=_risk(level))
    governance = GOVERNANCE if level >= 2 else None
    held = Decision(pack, governance=governance).decide(BILL)
    approved = Decision(pack, governance=governance, origin_approval=lambda _: True).decide(BILL)
    assert approved.outcome is Outcome.AUTO
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert held.recommendation == approved.recommendation
    assert any(UNAPPROVED in reason for reason in held.reasons)
    # An approval of another digest is none.
    other = Decision(pack, origin_approval=lambda p: p.digest == "sha256:" + "0" * 64)
    assert other.decide(BILL).outcome is Outcome.HUMAN_REVIEW


def test_nothing_in_the_pack_lifts_the_cap(tmp_path: Path) -> None:
    """Not a publisher type of `verified`, nor being a byte-for-byte copy of a reference pack
    (the cap follows where the pack came from, not its bytes)."""

    def verified(manifest: dict[str, Any]) -> None:
        manifest["publisher"]["type"] = "verified"

    claimed = outside_sorter(tmp_path / "claimed", manifest=verified)
    assert Decision(claimed).decide(BILL).outcome is Outcome.HUMAN_REVIEW
    reference = load_pack(copied(tmp_path, "patient-inbox-router"), origin="hub")
    assert not reference.bounded  # the maintained engine, and still capped
    assert Decision(reference)._origin_holds() == [UNAPPROVED]
    owned = load_pack(copied(tmp_path / "own", "patient-inbox-router"), origin="local")
    assert owned.digest == reference.digest and Decision(owned)._origin_holds() == []


def test_a_recall_recorded_at_pull_holds_even_an_approved_pack(tmp_path: Path) -> None:
    pack = outside_sorter(tmp_path)
    record = {"format": ORIGIN_FORMAT, "yank": {"severity": "high", "reason": "misroutes"}}
    (pack.root / MARKER).write_text(json.dumps(record))
    result = Decision(pack, origin_approval=lambda _: True).decide(BILL)
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert any("recalled by its registry (high)" in reason for reason in result.reasons)


def test_checking_a_pack_measures_its_own_policy(tmp_path: Path) -> None:
    """`mendari test` and `mendari eval` check what the pack itself decides: the cap is a
    serving rule (`origin_cap=False`)."""
    pack = outside_sorter(tmp_path)
    assert Decision(pack, origin_cap=False).decide(BILL).outcome is Outcome.AUTO


def test_the_marker_records_where_a_pull_came_from(
    hub: Hub,  # noqa: F811
    tmp_path: Path,
) -> None:
    token, key = hub.namespace("site-a")
    pack = copy_pack(tmp_path, "site-a/inbox")
    published = hub.client(token).push(pack, key)
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
        pack.digest,
    )
    assert record["yank"] is None
    # A marker from before LP-18d (the archive's SHA-256 alone) still reads.
    (pulled.root / MARKER).write_text(published["archive_sha256"] + "\n")
    assert pulled_record(pulled.root) == {"archive_sha256": published["archive_sha256"]}
    assert pull("site-a/inbox", hub.client()).pulled["format"] == ORIGIN_FORMAT  # rewritten


def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, Any]:
    """A server whose search path holds a pulled pack (the visit sorter under an id outside
    the community namespace), which automates `BILL` under its own policy."""
    pack = outside_sorter(tmp_path / "source")
    target = packs_cache() / "someone" / "visit-sorter" / pack.version
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pack.root, target)
    (target / MARKER).write_text(
        json.dumps({"format": ORIGIN_FORMAT, "registry": "https://hub.example.org"})
    )
    monkeypatch.setenv("MENDARI_PACKS", "someone/visit-sorter")
    return TestClient(create_app(db_path=tmp_path / "engine.db")), pack.digest, BILL


def test_an_organization_approves_a_pulled_digest_and_can_revoke_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, digest, automated = _served(tmp_path, monkeypatch)
    with client:
        decide = "/api/packs/someone/visit-sorter/decide"
        assert client.post(decide, json=automated).json()["outcome"] == "HUMAN_REVIEW"
        state = client.get(
            "/api/registry-approvals", params={"pack_id": "someone/visit-sorter"}
        ).json()
        assert state["origin"] == "hub" and state["active"] is None
        assert state["pulled"]["registry"] == "https://hub.example.org"
        body = {"pack_id": "someone/visit-sorter", "pack_digest": digest, "reference": "minutes 4"}
        wrong = client.post("/api/registry-approvals", json={**body, "pack_digest": "sha256:0" * 8})
        assert wrong.status_code == 422
        local = {**body, "pack_id": "mendari/patient-inbox-router"}
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
    assert events == ["registry_pack.approved", "registry_pack.revoked"]
    assert store.verify()["ok"] is True


def test_the_command_line_decides_a_pulled_pack_capped_unless_its_trail_approved_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client, digest, automated = _served(tmp_path, monkeypatch)
    with client:
        pass
    text = json.dumps(automated)
    (tmp_path / "input.json").write_text(text)
    args = ["decide", "someone/visit-sorter", "--input", str(tmp_path / "input.json")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "HUMAN_REVIEW"
    AuditStore(tmp_path / "engine.db").approve_registry_pack(
        pack_id="someone/visit-sorter",
        pack_digest=digest,
        registry="x",
        approver="a",
        reference="r",
    )
    assert main([*args, "--db", str(tmp_path / "engine.db")]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "AUTO"
