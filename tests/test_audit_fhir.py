"""The audit trail as FHIR AuditEvent and Provenance (standards/audit_fhir.py)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openhealth import AuditStore, Decision, DecisionResult, Pack
from openhealth.cli import main
from openhealth.server.app import create_app
from openhealth.standards import audit_fhir
from openhealth.standards.fhir import DECISION_ID_SYSTEM
from tests.conftest import message

EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO
SECRET = "SECRET-NOTE-TEXT"


def trail(store: AuditStore, pack: Pack) -> tuple[DecisionResult, DecisionResult, DecisionResult]:
    """Three decisions: one confirmed in review, one overridden, one automated with a downstream
    outcome and a thumbs-down that carries a note."""
    decide = Decision(pack, store=store)
    confirmed, overridden, automated = (
        decide(message(EMERGENCY)),
        decide(message(EMERGENCY)),
        decide(message(ROUTINE)),
    )
    store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    store.resolve_review(overridden.id, final_label="physician", reviewer="rn-2")
    store.record_outcome(automated.id, final_label="front_desk", source="staff re-route")
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
