# SPDX-License-Identifier: Apache-2.0
"""A decision as a FHIR Task, derived from its record and chain (standards/task_fhir.py)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from mendari import AuditStore, Decision, DecisionResult, Pack
from mendari.core.phi import PhiPolicy
from mendari.standards import audit_fhir, davinci, task_fhir
from mendari.standards.fhir import PACK_ID_SYSTEM
from tests.conftest import ROOT, SCREENED, message

EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO (`decide`)
UNSURE = "hello?"  # ABSTAIN
SECRET = "SECRET-NOTE-TEXT"
BASE = "https://mendari.example/fhir"
PINNED = ROOT / "tests" / "fixtures" / "fhir" / "task-versions.json"

# Decisions from a store run the urgency screen with a fake Jev (`screened`), so routine ones
# are automated.
pytestmark = pytest.mark.usefixtures("screened")

# A fixed synthetic chain: a HUMAN_REVIEW decision that a reviewer overrode, then rated (no
# version), then corrected downstream. `seq` is global, so this decision's events are sparse.
DECISION_ID = "2b3f6c1e-8d4a-4f0b-9c71-5e2d8a6b4c90"
PACK = "mendari/patient-inbox-router"
REASON = "confidence 0.58 is below the auto threshold 0.90 for 'medical_assistant'"


def chain_item() -> dict[str, Any]:
    def event(seq: int, kind: str, at: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": seq,
            "event": kind,
            "payload": payload,
            "created_at": f"2026-10-01T{at}+00:00",
            "prev_hash": f"{seq - 1:064x}",
            "hash": f"{seq:064x}",
        }

    return {
        "id": DECISION_ID,
        "created_at": "2026-10-01T14:00:00.000+00:00",
        "pack_id": PACK,
        "pack_version": "0.3.0",
        "outcome": "HUMAN_REVIEW",
        "recommendation": "medical_assistant",
        "priority": "routine",
        "confidence": 0.58,
        "review_status": "resolved",
        "refused": 0,
        # Not the reviewer's label in the chain: the Task reads the chain, never this column.
        "final_label": "front_desk",
        "human_override": True,
        "reviewer": "rn-1",
        "record": {
            "id": DECISION_ID,
            "outcome": "HUMAN_REVIEW",
            "recommendation": "medical_assistant",
            "priority": "routine",
            "confidence": 0.58,
            "primary": "destination",
            "answers": {"destination": {"label": "medical_assistant", "confidence": 0.58}},
            "reasons": [REASON],
            "fallback": None,  # set only under ABSTAIN and BLOCK
            "input": None,
        },
        "events": [
            event(2, "decision.created", "14:00:00.004", {"record_sha256": "0" * 64}),
            event(
                10,
                "review.resolved",
                "14:20:00.000",
                {
                    "final_label": "billing",
                    "human_override": True,
                    "reviewer": "rn-1",
                    "note": SECRET,
                },
            ),
            event(11, "feedback.recorded", "14:25:00.000", {"rating": "up", "note": SECRET}),
            event(
                12,
                "outcome.recorded",
                "15:00:00.000",
                {"final_label": "front_desk", "source": "x", "recorded_by": "rn-9"},
            ),
        ],
    }


def outputs(task: dict[str, Any]) -> dict[str, Any]:
    """Each output by its type, as its value (a coding's code)."""
    found = {}
    for output in task.get("output", []):
        value = next(v for k, v in output.items() if k.startswith("value"))
        found[output["type"]["text"]] = value["code"] if isinstance(value, dict) else value
    return found


def test_the_rendered_json_for_a_fixed_chain_is_pinned() -> None:
    """ADR-0003: the derivation is code, so a release that changes it changes how an existing
    version renders. Any change to this file is a changelog item."""
    pinned = json.loads(PINNED.read_text(encoding="utf-8"))
    assert task_fhir.task_versions(chain_item(), BASE) == pinned


def test_versions_follow_the_events_that_change_the_result() -> None:
    first, reviewed, observed = task_fhir.task_versions(chain_item(), BASE)  # feedback: none
    assert [t["meta"]["versionId"] for t in (first, reviewed, observed)] == ["1", "2", "3"]
    assert [t["meta"]["lastUpdated"][11:19] for t in (first, reviewed, observed)] == [
        "14:00:00",
        "14:20:00",
        "15:00:00",
    ]
    assert [t["status"] for t in (first, reviewed, observed)] == [
        "requested",
        "completed",
        "completed",
    ]
    assert "end" not in first["executionPeriod"]
    assert reviewed["executionPeriod"]["end"] == observed["executionPeriod"]["end"]
    assert first["owner"]["identifier"] == {"system": PACK_ID_SYSTEM, "value": f"{PACK}@0.3.0"}
    assert reviewed["owner"] == {"display": "rn-1"}
    assert observed["relevantHistory"] == [
        {"reference": f"{BASE}/Provenance/{DECISION_ID}-p{seq}"} for seq in (2, 10, 12)
    ]
    assert task_fhir.current_task(chain_item(), BASE) == observed


def test_outputs_come_from_the_events_not_the_overwritten_column() -> None:
    first, reviewed, observed = task_fhir.task_versions(chain_item())
    assert outputs(first) == {"recommendation": "medical_assistant"}
    assert outputs(reviewed) == {**outputs(first), "final-label": "billing", "human-override": True}
    assert outputs(observed) == {**outputs(reviewed), "observed-label": "front_desk"}
    system = davinci.question_system(PACK, "destination")
    codings = [o["valueCoding"] for o in observed["output"] if "valueCoding" in o]
    assert {c["system"] for c in codings} == {system}
    assert all("display" not in c for c in codings)  # the display is the pack's criteria


def test_rendering_does_not_depend_on_which_packs_are_served() -> None:
    """A decision of a pack this deployment no longer serves keeps its canonical codings."""
    item = chain_item()
    item["pack_id"] = "someone/retired-pack"
    task = task_fhir.current_task(item)
    codings = [o["valueCoding"] for o in task["output"] if "valueCoding" in o]
    assert {c["system"] for c in codings} == {
        "https://mendari.co/packs/someone/retired-pack/CodeSystem/destination"
    }
    assert task["meta"]["tag"][0]["code"] == "someone/retired-pack@0.3.0"


def test_without_a_base_nothing_points_anywhere() -> None:
    for task in task_fhir.task_versions(chain_item()):
        assert "relevantHistory" not in task


def test_a_decision_without_its_creation_event_is_an_error() -> None:
    item = chain_item()
    item["events"] = item["events"][1:]
    with pytest.raises(ValueError, match=r"no decision\.created"):
        task_fhir.task_versions(item)


@pytest.mark.parametrize(
    ("review_status", "refused", "status"),
    [
        ("not_required", 0, "completed"),  # automated, or a policy rule blocked it
        ("pending", 0, "requested"),
        ("resolved", 0, "completed"),
        ("not_required", 1, "failed"),  # the input was refused before any provider ran
    ],
)
def test_status_is_a_function_of_two_columns(review_status: str, refused: int, status: str) -> None:
    assert task_fhir.task_status(review_status, refused) == status


# -- from a real store ---------------------------------------------------------------------


def decide(store: AuditStore, pack: Pack, body: str | None) -> DecisionResult:
    body_or_empty = message(body) if body is not None else {"message": {}}
    return Decision(pack, providers=SCREENED, store=store)(body_or_empty)


def current(store: AuditStore, decision_id: str) -> dict[str, Any]:
    item = store.item(decision_id)
    assert item is not None
    return task_fhir.current_task(item, BASE)


def test_each_kind_of_decision_from_the_store(store: AuditStore, inbox_pack: Pack) -> None:
    automated = decide(store, inbox_pack, ROUTINE)
    waiting = decide(store, inbox_pack, EMERGENCY)
    abstained = decide(store, inbox_pack, UNSURE)
    refused = decide(store, inbox_pack, None)
    assert [r.outcome.value for r in (automated, waiting, abstained, refused)] == [
        "AUTO",
        "HUMAN_REVIEW",
        "ABSTAIN",
        "BLOCK",
    ]
    store.resolve_review(abstained.id, final_label="front_desk", reviewer="fd-1")

    auto = current(store, automated.id)
    assert (auto["status"], auto["meta"]["versionId"]) == ("completed", "1")
    assert auto["owner"]["identifier"]["system"] == PACK_ID_SYSTEM
    assert "end" in auto["executionPeriod"]
    assert outputs(auto)["recommendation"] == automated.recommendation

    pending = current(store, waiting.id)
    assert (pending["status"], pending["priority"]) == ("requested", "stat")
    assert "end" not in pending["executionPeriod"]

    labelled = current(store, abstained.id)
    assert (labelled["status"], labelled["meta"]["versionId"]) == ("completed", "2")
    assert outputs(labelled) == {"fallback": inbox_pack.manifest.policy.fallback} | {
        "final-label": "front_desk"  # an abstention made no recommendation: no override
    }

    failed = current(store, refused.id)
    assert failed["status"] == "failed"
    assert failed["businessStatus"]["coding"][0]["code"] == "BLOCK"
    assert "note" not in failed  # older records' refusal reasons can quote the input


def test_the_latest_version_agrees_with_the_columns_search_reads(
    store: AuditStore, inbox_pack: Pack
) -> None:
    """Search filters status in SQL over the same columns (`task_status`)."""
    ids = [decide(store, inbox_pack, body).id for body in (ROUTINE, EMERGENCY, UNSURE, None)]
    store.resolve_review(ids[1], final_label="physician", reviewer="rn-1")
    store.record_outcome(ids[1], final_label="nurse", source="ehr", recorded_by="rn-9")
    for decision_id in ids:
        item = store.item(decision_id)
        assert item is not None
        columns = (item["review_status"], item["refused"])
        assert task_fhir.current_task(item)["status"] == task_fhir.task_status(*columns)


def test_versions_match_the_provenances_of_the_audit_trail(
    store: AuditStore, inbox_pack: Pack
) -> None:
    result = decide(store, inbox_pack, EMERGENCY)
    store.resolve_review(result.id, final_label="physician", reviewer="rn-1")
    store.record_feedback(result.id, rating="down", note=SECRET)
    store.record_outcome(result.id, final_label="nurse", source="ehr", recorded_by="rn-9")
    item = store.item(result.id)
    assert item is not None
    tasks = task_fhir.task_versions(item, BASE)
    provenances = [
        r
        for r in audit_fhir.decision_bundle(item)["entry"]
        if r["resource"]["resourceType"] == "Provenance"
    ]
    assert len(tasks) == len(provenances) == 3
    for task, entry in zip(tasks, provenances, strict=True):
        assert task["meta"]["lastUpdated"] == entry["resource"]["recorded"]
    assert tasks[-1]["relevantHistory"] == [
        {"reference": f"{BASE}/Provenance/{p['resource']['id']}"} for p in provenances
    ]


@pytest.mark.parametrize("mode", ["off", "hash", "encrypt"])
def test_no_case_text_or_notes_in_any_phi_mode(tmp_path: Path, inbox_pack: Pack, mode: str) -> None:
    key = os.urandom(32) if mode == "encrypt" else b""
    store = AuditStore(tmp_path / "phi.db", phi=PhiPolicy(mode=mode, key=key))  # type: ignore[arg-type]
    result = decide(store, inbox_pack, f"{EMERGENCY} {SECRET}")
    note = None if mode == "hash" else SECRET  # PHI mode hash refuses notes (tests/test_notes.py)
    store.resolve_review(result.id, final_label="nurse", reviewer="rn-1", note=note)
    store.record_feedback(result.id, rating="up", note=note)
    refused = Decision(inbox_pack, store=store)({"message": {"body": 7, "x": SECRET}})
    for decision_id in (result.id, refused.id):
        item = store.item(decision_id)
        assert item is not None
        text = json.dumps(task_fhir.task_versions(item, BASE))
        assert SECRET not in text
        assert "chest pain" not in text.lower()
        assert '"for"' not in text and '"focus"' not in text


def test_the_pinned_chain_is_a_copy_safe_input() -> None:
    """Rendering never changes the item it reads (search renders many from one fetch)."""
    item = chain_item()
    before = copy.deepcopy(item)
    task_fhir.task_versions(item, BASE)
    assert item == before
