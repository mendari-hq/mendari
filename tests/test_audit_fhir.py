"""The audit trail as FHIR AuditEvent and Provenance (standards/audit_fhir.py)."""

from __future__ import annotations

import ast
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, DecisionResult, Pack
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.phi import PhiPolicy
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from mendari.standards.fhir import DECISION_ID_SYSTEM, PACK_ID_SYSTEM
from tests.conftest import ROOT, SCREENED, message

EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO (`trail`)
SECRET = "SECRET-NOTE-TEXT"

# The automated decisions here run the urgency screen with a fake Jev (`screened`).
pytestmark = pytest.mark.usefixtures("screened")


def trail(store: AuditStore, pack: Pack) -> tuple[DecisionResult, DecisionResult, DecisionResult]:
    """Three decisions: one confirmed in review, one overridden, one automated with a downstream
    outcome and a thumbs-down that carries a note."""
    decide = Decision(pack, providers=SCREENED, store=store)
    confirmed, overridden, automated = (
        decide(message(EMERGENCY)),
        decide(message(EMERGENCY)),
        decide(message(ROUTINE)),
    )
    store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    store.resolve_review(overridden.id, final_label="physician", reviewer="rn-2")
    store.record_outcome(
        automated.id, final_label="front_desk", source="staff-reroute", recorded_by="rn-9"
    )
    store.record_feedback(
        automated.id,
        rating="down",
        note=SECRET,
        expected={"question": "destination", "label": "front_desk"},
    )
    return confirmed, overridden, automated


def by_type(resources: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [r for r in resources if r["resourceType"] == kind]


def detail(event: dict[str, Any], key: str) -> str | None:
    values = [d["valueString"] for d in event["entity"][0]["detail"] if d["type"] == key]
    return values[0] if values else None


def test_every_chained_event_becomes_an_audit_event(store: AuditStore, inbox_pack: Pack) -> None:
    trail(store, inbox_pack)
    exported = list(audit_fhir.resources(store.iter_events()))
    events = by_type(exported, "AuditEvent")
    subtypes = [e["subtype"][0]["code"] for e in events]
    assert subtypes == [
        "decision.created",
        "decision.created",
        "decision.created",
        "review.resolved",
        "review.resolved",
        "outcome.recorded",
        "feedback.recorded",
    ]
    assert [e["action"] for e in events] == ["C", "C", "C", "U", "U", "U", "C"]
    assert len({r["id"] for r in exported}) == len(exported)


def test_the_export_keeps_the_hash_chain(store: AuditStore, inbox_pack: Pack) -> None:
    trail(store, inbox_pack)
    events = by_type(list(audit_fhir.resources(store.iter_events())), "AuditEvent")
    for previous, current in itertools.pairwise(events):
        assert detail(current, "prev_hash") == detail(previous, "hash")
    rows = store.events()
    assert [detail(e, "hash") for e in events] == [row["hash"] for row in rows]
    assert store.verify()["head"] == detail(events[-1], "hash")


def test_provenance_says_who_created_verified_or_changed_the_result(
    store: AuditStore, inbox_pack: Pack
) -> None:
    confirmed, overridden, automated = trail(store, inbox_pack)
    provenance = by_type(list(audit_fhir.resources(store.iter_events())), "Provenance")
    code = lambda concept: concept["coding"][0]["code"]  # noqa: E731
    roles = {
        (p["target"][0]["identifier"]["value"], code(p["activity"]), code(p["agent"][0]["type"]))
        for p in provenance
    }
    authored = {
        (decision.id, "CREATE", "author") for decision in (confirmed, overridden, automated)
    }
    assert roles == authored | {
        (confirmed.id, "UPDATE", "verifier"),  # confirmed the recommendation
        (overridden.id, "UPDATE", "author"),  # chose another label
        (automated.id, "UPDATE", "author"),  # downstream outcome
    }
    created = next(p for p in provenance if p["activity"]["coding"][0]["code"] == "CREATE")
    assert created["entity"][0]["what"]["identifier"]["value"].startswith("sha256:")
    assert (
        created["agent"][0]["who"]["identifier"]["value"] == f"{inbox_pack.id}@{inbox_pack.version}"
    )
    assert (
        created["target"][0]["identifier"]["system"] == DECISION_ID_SYSTEM
    )  # the Task's identifier


def test_the_export_carries_codes_never_case_text(store: AuditStore, inbox_pack: Pack) -> None:
    trail(store, inbox_pack)
    text = json.dumps(list(audit_fhir.resources(store.iter_events())))
    assert SECRET not in text
    assert "chest pain" not in text.lower()
    events = by_type(json.loads(text), "AuditEvent")
    feedback = next(e for e in events if e["subtype"][0]["code"] == "feedback.recorded")
    assert detail(feedback, "rating") == "down"
    assert detail(feedback, "expected") == "destination=front_desk"
    review = next(e for e in events if e["subtype"][0]["code"] == "review.resolved")
    assert review["agent"][0] == {"who": {"display": "rn-1"}, "requestor": True}


def test_a_decision_names_who_asked_for_it(store: AuditStore, inbox_pack: Pack) -> None:
    """With sign-in on, `decision.created` names who asked (`requested_by`): the AuditEvent has
    them as a second agent, the requestor (the engine is not), and the CREATE Provenance as a
    second agent of role "requester", after the engine that authored it. A decision made with
    nobody named (open mode, the command line, or before this) has the engine alone."""
    decide = Decision(inbox_pack, store=store)
    unnamed = decide(message(ROUTINE))
    with tenancy.acting_for("clinic-a", "kim@clinic-a.example.org"):
        named = decide(message(ROUTINE))
    exported = list(audit_fhir.resources(store.iter_events()))
    event = {
        e["entity"][0]["what"]["identifier"]["value"]: e for e in by_type(exported, "AuditEvent")
    }
    made = {p["target"][0]["identifier"]["value"]: p for p in by_type(exported, "Provenance")}
    engine = audit_fhir.engine({"pack_id": inbox_pack.id, "pack_version": inbox_pack.version})
    assert event[named.id]["agent"] == [
        {"who": engine, "requestor": False},
        {"who": {"display": "kim@clinic-a.example.org"}, "requestor": True},
    ]
    assert made[named.id]["agent"][1] == {
        "who": {"display": "kim@clinic-a.example.org"},
        "role": [{"text": "requester"}],
    }
    assert made[named.id]["agent"][0]["who"] == engine
    assert event[unnamed.id]["agent"] == [{"who": engine, "requestor": False}]
    assert [a["who"] for a in made[unnamed.id]["agent"]] == [engine]


def test_events_page_in_chain_order(store: AuditStore, inbox_pack: Pack) -> None:
    trail(store, inbox_pack)
    first = store.events(limit=3)
    rest = store.events(after_seq=first[-1]["seq"])
    assert [e["seq"] for e in first + rest] == [e["seq"] for e in store.events()]
    assert len(list(store.iter_events())) == 7
    assert store.events(pack_id="someone/else") == []
    assert store.events(since="2999-01-01") == []


def test_one_decision_as_a_bundle(store: AuditStore, inbox_pack: Pack) -> None:
    _, overridden, _ = trail(store, inbox_pack)
    item = store.get(overridden.id)
    assert item is not None
    bundle = audit_fhir.decision_bundle(item)
    kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert kinds == ["AuditEvent", "Provenance", "AuditEvent", "Provenance"]
    assert bundle == audit_fhir.decision_bundle(item)  # stable fullUrls
    assert len({e["fullUrl"] for e in bundle["entry"]}) == 4


def test_the_api_serves_a_bundle_and_ndjson(tmp_path: Path, inbox_pack: Pack) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decided = client.post(f"/api/packs/{inbox_pack.id}/decide", json=message(EMERGENCY)).json()
    client.post(f"/api/review/{decided['id']}", json={"final_label": "nurse", "reviewer": "rn-1"})
    one = client.get(f"/api/audit/{decided['id']}/fhir")
    assert one.headers["content-type"].startswith("application/fhir+json")
    assert one.json()["resourceType"] == "Bundle"
    assert client.get("/api/audit/nope/fhir").status_code == 404
    export = client.get("/api/audit/export/fhir", params={"pack_id": inbox_pack.id})
    assert export.headers["content-type"].startswith("application/fhir+ndjson")
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert [r["resourceType"] for r in lines] == [
        "AuditEvent",
        "Provenance",
        "AuditEvent",
        "Provenance",
    ]
    assert client.get("/api/audit/export/fhir", params={"until": "2000-01-01"}).text == ""


def test_the_cli_writes_ndjson(
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    trail(AuditStore(db), inbox_pack)
    out = tmp_path / "audit.ndjson"
    assert main(["audit", "export", "--db", str(db), "--out", str(out)]) == 0
    assert "13 resources written" in capsys.readouterr().err
    assert len(out.read_text().splitlines()) == 13  # 7 AuditEvents, 6 Provenances


def test_reads_and_purges_of_encrypted_inputs_are_exported(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    """PHI mode's access log in FHIR: a read is action R by the reader, and a purge is action D
    by the engine."""
    store = AuditStore(tmp_path / "phi.db", phi=PhiPolicy(mode="encrypt", key=os.urandom(32)))
    result = Decision(inbox_pack, store=store)(message(SECRET))
    store.get(result.id, reader="rn-7")
    store.purge_inputs(0)
    events = by_type(list(audit_fhir.resources(store.iter_events())), "AuditEvent")
    read, purged = events[-2], events[-1]
    assert (read["subtype"][0]["code"], read["action"]) == ("input.read", "R")
    assert read["agent"][0]["who"]["display"] == "rn-7"
    assert (purged["subtype"][0]["code"], purged["action"]) == ("input.purged", "D")
    assert purged["agent"][0]["requestor"] is False
    assert SECRET not in json.dumps(events)


# Chained under `governance:<id>`, `deployment:<id>` or `key:<id>`, not a decision; the export
# never reads them.
NOT_ON_A_DECISION = {
    "governance.approved",
    "governance.revoked",
    "deployment.recorded",
    "key.created",
    "key.revoked",
    "client.registered",
    "client.revoked",
    "notice.acknowledged",
    "automation.held",
    "automation.lifted",
}


def test_every_event_the_code_records_has_an_audit_event_code() -> None:
    """The audit-event CodeSystem (standards/terminology.py) is generated from `_EVENTS`, so an
    event recorded on a decision but missing there would be exported with an undefined code."""
    recorded: set[str] = set()
    for path in (ROOT / "src" / "mendari").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            position = {"_append": 2, "_append_many": 1, "record_event": 1}.get(node.func.attr)
            if position is None or len(node.args) <= position:
                continue
            event = node.args[position]
            if isinstance(event, ast.Name) and event.id == "event":
                continue  # `record_event` passing its caller's literal on
            assert isinstance(event, ast.Constant), f"{path}: an event name that is not a literal"
            recorded.add(str(event.value))
    assert recorded - NOT_ON_A_DECISION == set(audit_fhir._EVENTS)


def test_audit_entries_are_immutable_versions_narrated_from_codes(
    store: AuditStore, inbox_pack: Pack
) -> None:
    trail(store, inbox_pack)
    exported = list(audit_fhir.resources(store.iter_events()))
    for resource in exported:
        assert resource["meta"] == {"versionId": "1", "lastUpdated": resource["recorded"]}
        assert resource["text"]["status"] == "generated"
        div = resource["text"]["div"]
        provenance = resource["resourceType"] == "Provenance"
        decision = resource["target"][0] if provenance else resource["entity"][0]["what"]
        assert decision["identifier"]["value"] in div
        for private in (SECRET, "rn-1", "rn-2", "rn-9", "staff-reroute", "chest pain"):
            assert private not in div
    for event in by_type(exported, "AuditEvent"):
        assert [c["system"] for c in event["subtype"]] == [audit_fhir.EVENT_SYSTEM]
    created = next(e for e in by_type(exported, "AuditEvent") if e["action"] == "C")
    engine = created["agent"][0]["who"]["identifier"]
    assert engine == {"system": PACK_ID_SYSTEM, "value": inbox_pack.ref}


BASE = "https://mendari.example/fhir"


def test_reviewing_an_abstention_authors_the_label(store: AuditStore, inbox_pack: Pack) -> None:
    """An abstention made no recommendation, so its reviewer verifies nothing: they author the
    label (`human_override` is None, not False)."""
    result = Decision(inbox_pack, store=store)(message("hello?"))
    assert result.outcome.value == "ABSTAIN"
    store.resolve_review(result.id, final_label="front_desk", reviewer="fd-1")
    item = store.get(result.id)
    assert item is not None
    provenance = by_type(
        [e["resource"] for e in audit_fhir.decision_bundle(item)["entry"]], "Provenance"
    )
    review = provenance[-1]
    assert review["activity"]["coding"][0]["code"] == "UPDATE"
    assert review["agent"][0]["type"]["coding"][0]["code"] == "author"
    assert review["agent"][0]["who"] == {"display": "fd-1"}


def test_served_resources_refer_to_the_task_by_url(store: AuditStore, inbox_pack: Pack) -> None:
    """Under /fhir an AuditEvent points at the Task, a Provenance at the Task version its event
    made; the identifier stays next to the reference."""
    confirmed, _, automated = trail(store, inbox_pack)
    for decision in (confirmed, automated):
        item = store.get(decision.id)
        assert item is not None
        events = audit_fhir.joined(item)
        versions = audit_fhir.task_versions(item["events"])
        served = list(audit_fhir.resources(events, base=BASE, versions=versions))
        task = f"{BASE}/Task/{decision.id}"
        for event in by_type(served, "AuditEvent"):
            what = event["entity"][0]["what"]
            assert what["reference"] == task
            assert what["identifier"] == {"system": DECISION_ID_SYSTEM, "value": decision.id}
        targets = [p["target"][0]["reference"] for p in by_type(served, "Provenance")]
        assert targets == [f"{task}/_history/{n}" for n in (1, 2)]
    # The automated decision: created (version 1), its outcome (2); the feedback made none.
    assert [e["event"] for e in events] == [
        "decision.created",
        "outcome.recorded",
        "feedback.recorded",
    ]


def test_the_exports_keep_identifiers_only(store: AuditStore, inbox_pack: Pack) -> None:
    """The /api/audit collection Bundles have urn:uuid full URLs: a reference there would
    resolve against nothing."""
    trail(store, inbox_pack)
    assert '"reference"' not in json.dumps(list(audit_fhir.resources(store.iter_events())))


def test_a_served_provenance_needs_its_task_version(store: AuditStore, inbox_pack: Pack) -> None:
    trail(store, inbox_pack)
    created = store.events()[0]
    with pytest.raises(ValueError, match="Task version"):
        audit_fhir.provenance(created, base=BASE)
    assert audit_fhir.task_versions([created]) == {created["seq"]: 1}
