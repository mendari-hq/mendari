# SPDX-License-Identifier: Apache-2.0
"""The validation brief's gates say what was measured (core/validation_brief.py).

A gate with nothing to measure is `no_data` ("not enough data"), never `pass`: the safety gate
when no decision was automated (every shadow pilot) or nobody looked at an automated one, the
drift gate when the monitor had too few decisions to compare, the agreement gate without a
label, the volume gate without a decision. And the brief says what it is: computed here, and
signed by nothing.

On the bundled demo agent (`agents/portal-message-agent`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mendari import AuditStore, Decision, load_pack
from mendari.core import validation_brief
from mendari.core.pack import Pack
from mendari.evals.monitoring import MIN_FOR_OPERATIONS, monitor
from tests.conftest import DEMO_AGENT_DIR

# Part of the edit loop (`-m core`), beside the brief's other tests: fast, and on the decision
# path's own counts.
pytestmark = pytest.mark.core

REFILL = {"message": {"body": "Please send a refill of my metformin to the pharmacy."}}
URGENT = {"message": {"body": "I have chest pain and trouble breathing since last night."}}
RUNNING = {"reader": "agent"}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(DEMO_AGENT_DIR, origin="maintained")


def brief(store: AuditStore, pack: Pack, **kwargs: Any) -> dict[str, Any]:
    monitoring = monitor(store, pack, days=30, providers=RUNNING)
    return validation_brief.build(store, pack, days=30, monitoring=monitoring, **kwargs)


def states(found: dict[str, Any]) -> dict[str, str]:
    return {gate["key"]: gate["status"] for gate in found["gates"]}


def gate(found: dict[str, Any], key: str) -> dict[str, Any]:
    (one,) = [g for g in found["gates"] if g["key"] == key]
    return dict(one)


def test_with_no_decisions_no_gate_that_measures_passes(store: AuditStore, pack: Pack) -> None:
    found = brief(store, pack)
    assert found["format"] == "mendari.validation-brief/2"
    assert states(found) == {
        "volume": "no_data",
        "safety": "no_data",
        "agreement": "no_data",
        "governance": "pass",  # a risk level 0 agent of the deployment's own needs no approval
        "drift": "no_data",
    }
    assert found["ready_for_live"] is False
    assert gate(found, "safety")["detail"].startswith("0 of 0 decisions were automated")
    assert "Nothing was compared." in gate(found, "drift")["detail"]
    assert validation_brief.readiness_line(found["gates"]) == (
        "1 of 5 gates pass; 4 without enough data"
    )
    # Without the monitor's report the drift gate has nothing to read, and says that.
    unread = validation_brief.build(store, pack, days=30)
    assert gate(unread, "drift") == {
        "key": "drift",
        "label": "Calibration & operational drift",
        "status": "no_data",
        "detail": "The monitor was not read for this brief.",
    }


def test_a_shadow_pilot_never_passes_the_safety_gate(store: AuditStore, pack: Pack) -> None:
    """In a shadow pilot nothing is automated, so nothing shows how the agent does alone: the
    gate used to pass on "0 automated decisions rated wrong"."""
    hold = store.hold_automation(
        org=None,
        pack_id=pack.id,
        pack_digest=None,
        reason="shadow pilot: 7-day validation against clinicians",
        set_by="cmio-1",
    )
    decide = Decision(pack, store=store, hold_source=lambda _: f"hold {hold['id']}")
    for _ in range(4):
        assert decide.decide(REFILL).outcome.value == "HUMAN_REVIEW"
    found = brief(store, pack, holds=store.holds(None))
    assert found["shadow_pilot"]["active"] is True
    assert found["volume"]["automated"] == 0 and found["volume"]["would_automate"] == 4
    safety = gate(found, "safety")
    assert safety["status"] == "no_data"
    assert safety["detail"] == (
        "0 of 0 decisions were automated in the last 30 days, so nothing shows how the agent "
        "does when it acts alone. Automation was held for 4 decisions that would have been "
        "automated."
    )
    assert found["ready_for_live"] is False


def test_automated_decisions_nobody_looked_at_are_not_a_pass(store: AuditStore, pack: Pack) -> None:
    decide = Decision(pack, store=store)
    made = [decide.decide(REFILL) for _ in range(3)]
    assert {result.outcome.value for result in made} == {"AUTO"}
    unseen = gate(brief(store, pack), "safety")
    assert unseen["status"] == "no_data"
    assert unseen["detail"].startswith("3 decisions automated in the last 30 days, and nobody")
    # One of them rated right: the gate has something to pass on, and says how much.
    store.record_feedback(made[0].id, rating="up", reporter="rn-1")
    seen = gate(brief(store, pack), "safety")
    assert seen["status"] == "pass"
    assert seen["detail"] == (
        "0 rated wrong of 3 automated decisions (1 rating, 0 recorded outcomes), and no "
        "high-severity monitoring alerts."
    )
    # One rated wrong: it fails, as before.
    store.record_feedback(made[1].id, rating="down", reporter="rn-2")
    wrong = gate(brief(store, pack), "safety")
    assert wrong["status"] == "fail"
    assert wrong["detail"].startswith("1 rating of wrong on 3 automated decisions")


def test_the_drift_gate_passes_only_when_the_monitor_could_compare(
    store: AuditStore, pack: Pack
) -> None:
    decide = Decision(pack, store=store)
    for _ in range(MIN_FOR_OPERATIONS - 1):
        decide.decide(REFILL)
    few = gate(brief(store, pack), "drift")
    assert few["status"] == "no_data"
    assert few["detail"].startswith(f"{MIN_FOR_OPERATIONS - 1} decisions in the last 7 days")
    decide.decide(REFILL)
    enough = gate(brief(store, pack), "drift")
    assert enough["status"] == "pass"
    # It says what was compared, and what was not: the demo agent carries no benchmark.
    assert enough["detail"] == (
        f"No operational alert over {MIN_FOR_OPERATIONS} decisions in the last 7 days. "
        "Confidences were not compared with a benchmark: the agent carries no benchmark."
    )
    # An alert is a finding, with or without enough decisions.
    alerting = {"alerts": [{"kind": "drift", "severity": "medium", "message": "PSI 0.31"}]}
    found = validation_brief.build(store, pack, days=30, monitoring=alerting)
    assert gate(found, "drift")["status"] == "warn"
    alerting["alerts"][0]["severity"] = "high"
    found = validation_brief.build(store, pack, days=30, monitoring=alerting)
    assert gate(found, "drift")["status"] == "fail" and gate(found, "safety")["status"] == "fail"


def test_the_agreement_gate_needs_a_label(store: AuditStore, pack: Pack) -> None:
    held = Decision(pack, store=store).decide(URGENT)
    assert held.outcome.value == "HUMAN_REVIEW" and held.recommendation
    assert gate(brief(store, pack), "agreement")["status"] == "no_data"
    store.resolve_review(held.id, final_label=held.recommendation, reviewer="rn-1")
    agreed = gate(brief(store, pack), "agreement")
    assert agreed["status"] == "pass"
    assert agreed["detail"].startswith("1 of 1 [")  # what it is out of, with its interval


def test_the_brief_says_it_is_not_signed(store: AuditStore, pack: Pack) -> None:
    found = brief(store, pack)
    assert found["signed"] is False
    assert found["about"] == validation_brief.ABOUT and "It is not signed" in found["about"]
    text = validation_brief.render_markdown(found)
    assert validation_brief.ABOUT in text
    assert "- **Go-live readiness**: 1 of 5 gates pass; 4 without enough data" in text
    assert "ALL GATES PASSED" not in text and "[NOT ENOUGH DATA] Local pilot volume" in text
    assert "| Automated decisions | 0 of 0 |" in text  # a count with what it is out of
    generated = datetime.fromisoformat(found["generated_at"])
    assert datetime.now(UTC) - generated < timedelta(minutes=1)
