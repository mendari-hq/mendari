"""Automation holds (`AuditStore.hold_automation`): one switch that sends to a person every
decision it covers that would have been automated, set and lifted without a redeploy, chained in
the audit trail. A hold only ever lowers automation, and changes nothing else about a decision."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Pack
from mendari.cli import main
from mendari.core.audit import AuditStore
from mendari.core.decision import Outcome
from mendari.core.policy import apply_policy
from mendari.server import app as app_module
from mendari.server.app import create_app
from tests.conftest import message

INBOX = "mendari/patient-inbox-router"
DECIDE = f"/api/packs/{INBOX}/decide"
REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"
CHEST = "Crushing chest pain for 30 minutes, going down my left arm."


def _id(hold: dict[str, Any] | None) -> str | None:
    return None if hold is None else str(hold["id"])


def test_a_hold_covers_what_it_names_and_nothing_else(db_target: str) -> None:
    with AuditStore(db_target) as store:
        pack = store.hold_automation(
            org="a", pack_id=INBOX, pack_digest=None, reason="checking", set_by="ops"
        )
        assert _id(store.hold_for(INBOX, "sha256:x", "a")) == pack["id"]
        assert store.hold_for(INBOX, "sha256:x", "b") is None  # another organization
        assert store.hold_for("mendari/referral-router", "sha256:x", "a") is None
        digest = store.hold_automation(
            org="b", pack_id=INBOX, pack_digest="sha256:" + "1" * 64, reason="v", set_by="ops"
        )
        assert _id(store.hold_for(INBOX, "sha256:" + "1" * 64, "b")) == digest["id"]
        assert store.hold_for(INBOX, "sha256:" + "2" * 64, "b") is None  # another version
        everywhere = store.hold_automation(
            org=None, pack_id=None, pack_digest=None, reason="all", set_by="cli"
        )
        assert _id(store.hold_for("x/y", "sha256:z", "c")) == everywhere["id"]
        assert [h["id"] for h in store.holds("a")] == [pack["id"], everywhere["id"]]

        assert not store.lift_hold(everywhere["id"], lifted_by="a admin", org="a")  # not its own
        assert store.lift_hold(everywhere["id"], lifted_by="cli", org=None)
        assert store.lift_hold(pack["id"], lifted_by="a admin", org="a")
        assert not store.lift_hold(pack["id"], lifted_by="a admin", org="a")  # already lifted
        assert store.hold_for(INBOX, "sha256:x", "a") is None
        assert store.verify()["ok"] is True
        with store._db.session(exclusive=False) as db:
            events = [r["event"] for r in db.all("SELECT event FROM audit_events ORDER BY seq")]
        assert events.count("automation.held") == 3 and events.count("automation.lifted") == 2


def test_a_hold_needs_a_reason_and_a_digest_its_pack(tmp_path: Path) -> None:
    with AuditStore(tmp_path / "h.db") as store:
        for reason in ("", " ", "x" * 201):
            with pytest.raises(Exception, match="reason"):
                store.hold_automation(
                    org="a", pack_id=None, pack_digest=None, reason=reason, set_by="ops"
                )
        with pytest.raises(Exception, match="names its pack"):
            store.hold_automation(
                org="a", pack_id=None, pack_digest="sha256:" + "1" * 64, reason="r", set_by="o"
            )


@pytest.mark.parametrize("confidence", [0.99, 0.7, 0.3])
def test_a_hold_only_ever_lowers_automation(inbox_pack: Pack, confidence: float) -> None:
    from tests.test_policy import routine

    answers = routine("medical_assistant", confidence)
    running = {"safety_screen": "rules_and_jev", "router": "naive_bayes"}
    free = apply_policy(inbox_pack.manifest, answers, {}, None, running)
    held = apply_policy(inbox_pack.manifest, answers, {}, None, running, hold="hold abc")
    expected = Outcome.HUMAN_REVIEW if free.outcome is Outcome.AUTO else free.outcome
    assert held.outcome is expected
    assert (held.recommendation, held.priority) == (free.recommendation, free.priority)
    if free.outcome is Outcome.AUTO:
        assert "automation is on hold: hold abc" in held.reasons


def test_holding_and_lifting_over_http_takes_effect_at_once(
    tmp_path: Path, screened: dict[str, str]
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "h.db", providers=screened))
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    hold = client.post("/api/holds", json={"pack_id": INBOX, "reason": "a missed emergency"})
    assert hold.status_code == 201, hold.text
    held = client.post(DECIDE, json=message(REFILL)).json()
    assert held["outcome"] == "HUMAN_REVIEW" and held["recommendation"] == "medical_assistant"
    assert f"automation is on hold: hold {hold.json()['id']}" in held["reasons"]
    emergency = client.post(DECIDE, json=message(CHEST)).json()  # rules still decide priority
    assert (emergency["outcome"], emergency["priority"]) == ("HUMAN_REVIEW", "stat")
    assert client.get("/api/holds").json()[0]["reason"] == "a missed emergency"
    assert client.get("/api/health").json()["holds"] == 1
    other = client.post("/api/packs/mendari/referral-router/examples")  # unrelated pack: unheld
    assert other.status_code in (200, 405)
    assert client.delete(f"/api/holds/{hold.json()['id']}").status_code == 204
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert client.delete(f"/api/holds/{hold.json()['id']}").status_code == 404


def test_a_hold_on_an_unknown_pack_is_refused(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "u.db"))
    answer = client.post("/api/holds", json={"pack_id": "mendari/nope", "reason": "typo"})
    assert answer.status_code == 404


def test_the_command_line_holds_every_organization_with_the_server_running(
    tmp_path: Path,
    screened: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app_module, "HOLD_SECONDS", 0.0)
    db = str(tmp_path / "cli.db")
    client = TestClient(create_app(db_path=db, providers=screened))
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert main(["automation", "hold", "--all-orgs", "--reason", "incident 7", "--db", db]) == 0
    with AuditStore(db) as store:
        [hold] = store.holds(everyone=True)
    assert hold["org"] is None and hold["pack_id"] is None and hold["reason"] == "incident 7"
    assert hold["id"] in capsys.readouterr().out
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "HUMAN_REVIEW"
    # A deployment's hold is lifted where it was set, never from one organization's API.
    assert client.delete(f"/api/holds/{hold['id']}").status_code == 404
    assert main(["automation", "list", "--db", db]) == 0
    assert hold["id"] in capsys.readouterr().out
    assert main(["automation", "lift", hold["id"], "--db", db]) == 0
    assert client.post(DECIDE, json=message(REFILL)).json()["outcome"] == "AUTO"
    assert main(["automation", "lift", hold["id"], "--db", db]) == 1


def test_fhir_and_cds_hooks_decisions_are_held_too(
    tmp_path: Path, screened: dict[str, str]
) -> None:
    from tests.test_fhir import COMMUNICATION, PATIENT

    client = TestClient(create_app(db_path=tmp_path / "f.db", providers=screened))
    client.post("/api/holds", json={"reason": "every pack"})
    bundle = {"communication": COMMUNICATION, "patient": PATIENT}
    fhir = client.post(f"/api/packs/{INBOX}/fhir/decide", json=bundle).json()
    assert fhir["decision"]["outcome"] != "AUTO"
    cards = client.post(
        "/cds-services/mendari.patient-inbox-router",
        json={
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": bundle,
        },
    )
    assert cards.status_code == 200, cards.text
    assert "AUTO" not in cards.json()["cards"][0]["detail"].splitlines()[2]
