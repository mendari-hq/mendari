# SPDX-License-Identifier: Apache-2.0
"""The audit trail as FHIR AuditEvent and Provenance (standards/audit_fhir.py)."""

from __future__ import annotations

import ast
import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Agent, AuditStore, Decision, DecisionResult
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.phi import PhiPolicy
from mendari.server.app import create_app
from mendari.standards import audit_fhir
from mendari.standards.fhir import AGENT_ID_SYSTEM, DECISION_ID_SYSTEM
from tests.conftest import ROOT, message

EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO (`trail`)
SECRET = "SECRET-NOTE-TEXT"

# The automated decisions here run the triage fixture's screen on its canned agent.
SCREEN_AGENT = {"screen": "agent"}


def trail(store: AuditStore, agent: Agent) -> tuple[DecisionResult, DecisionResult, DecisionResult]:
    """Three decisions: one confirmed in review, one overridden, one automated with a downstream
    outcome and a thumbs-down that carries a note."""
    decide = Decision(agent, providers=SCREEN_AGENT, store=store)
    confirmed, overridden, automated = (
        decide(message(EMERGENCY)),
        decide(message(EMERGENCY)),
        decide(message(ROUTINE)),
    )
    assert [r.outcome.value for r in (confirmed, overridden, automated)] == [
        "HUMAN_REVIEW",
        "HUMAN_REVIEW",
        "AUTO",
    ]
    store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1", note=SECRET)
    store.resolve_review(overridden.id, final_label="billing", reviewer="rn-2")
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


def test_every_chained_event_becomes_an_audit_event(store: AuditStore, triage_agent: Agent) -> None:
    trail(store, triage_agent)
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


def test_the_export_keeps_the_hash_chain(store: AuditStore, triage_agent: Agent) -> None:
    trail(store, triage_agent)
    events = by_type(list(audit_fhir.resources(store.iter_events())), "AuditEvent")
    for previous, current in itertools.pairwise(events):
        assert detail(current, "prev_hash") == detail(previous, "hash")
    rows = store.events()
    assert [detail(e, "hash") for e in events] == [row["hash"] for row in rows]
    assert store.verify()["head"] == detail(events[-1], "hash")


def test_provenance_says_who_created_verified_or_changed_the_result(
    store: AuditStore, triage_agent: Agent
) -> None:
    confirmed, overridden, automated = trail(store, triage_agent)
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
        created["agent"][0]["who"]["identifier"]["value"]
        == f"{triage_agent.id}@{triage_agent.version}"
    )
    assert (
        created["target"][0]["identifier"]["system"] == DECISION_ID_SYSTEM
    )  # the Task's identifier


def test_the_export_carries_codes_never_case_text(store: AuditStore, triage_agent: Agent) -> None:
    trail(store, triage_agent)
    text = json.dumps(list(audit_fhir.resources(store.iter_events())))
    assert SECRET not in text
    assert "chest pain" not in text.lower()
    events = by_type(json.loads(text), "AuditEvent")
    feedback = next(e for e in events if e["subtype"][0]["code"] == "feedback.recorded")
    assert detail(feedback, "rating") == "down"
    assert detail(feedback, "expected") == "destination=front_desk"
    review = next(e for e in events if e["subtype"][0]["code"] == "review.resolved")
    assert review["agent"][0] == {"who": {"display": "rn-1"}, "requestor": True}


def test_a_decision_names_who_asked_for_it(store: AuditStore, triage_agent: Agent) -> None:
    """With sign-in on, `decision.created` names who asked (`requested_by`): the AuditEvent has
    them as a second agent, the requestor (the engine is not), and the CREATE Provenance as a
    second agent of role "requester", after the engine that authored it. A decision made with
    nobody named (open mode, the command line, or before this) has the engine alone."""
    decide = Decision(triage_agent, store=store)
    unnamed = decide(message(ROUTINE))
    with tenancy.acting_for("clinic-a", "kim@clinic-a.example.org"):
        named = decide(message(ROUTINE))
    exported = list(audit_fhir.resources(store.iter_events()))
    event = {
        e["entity"][0]["what"]["identifier"]["value"]: e for e in by_type(exported, "AuditEvent")
    }
    made = {p["target"][0]["identifier"]["value"]: p for p in by_type(exported, "Provenance")}
    engine = audit_fhir.engine({"agent_id": triage_agent.id, "agent_version": triage_agent.version})
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


def test_events_page_in_chain_order(store: AuditStore, triage_agent: Agent) -> None:
    trail(store, triage_agent)
    first = store.events(limit=3)
    rest = store.events(after_seq=first[-1]["seq"])
    assert [e["seq"] for e in first + rest] == [e["seq"] for e in store.events()]
    assert len(list(store.iter_events())) == 7
    assert store.events(agent_id="someone/else") == []
    assert store.events(since="2999-01-01") == []


def test_one_decision_as_a_bundle(store: AuditStore, triage_agent: Agent) -> None:
    _, overridden, _ = trail(store, triage_agent)
    item = store.get(overridden.id)
    assert item is not None
    bundle = audit_fhir.decision_bundle(item)
    kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert kinds == ["AuditEvent", "Provenance", "AuditEvent", "Provenance"]
    assert bundle == audit_fhir.decision_bundle(item)  # stable fullUrls
    assert len({e["fullUrl"] for e in bundle["entry"]}) == 4


def test_the_api_serves_a_bundle_and_ndjson(tmp_path: Path, triage_agent: Agent) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decided = client.post(f"/api/agents/{triage_agent.id}/decide", json=message(EMERGENCY)).json()
    client.post(f"/api/review/{decided['id']}", json={"final_label": "nurse", "reviewer": "rn-1"})
    one = client.get(f"/api/audit/{decided['id']}/fhir")
    assert one.headers["content-type"].startswith("application/fhir+json")
    assert one.json()["resourceType"] == "Bundle"
    assert client.get("/api/audit/nope/fhir").status_code == 404
    export = client.get("/api/audit/export/fhir", params={"agent_id": triage_agent.id})
    assert export.headers["content-type"].startswith("application/fhir+ndjson")
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert [r["resourceType"] for r in lines] == [
        "AuditEvent",
        "Provenance",
        "AuditEvent",
        "Provenance",
    ]
    assert client.get("/api/audit/export/fhir", params={"until": "2000-01-01"}).text == ""


def test_the_exports_bounds_are_read_as_times_not_text(tmp_path: Path, triage_agent: Agent) -> None:
    """`since` with another offset was compared as text with the stored UTC times, so an hour
    after the decision, written at -05:00, still exported it; `zzz` exported nothing, silently."""
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    client.post(f"/api/agents/{triage_agent.id}/decide", json=message(EMERGENCY))
    export = client.get("/api/audit/export/fhir").text.splitlines()
    recorded = datetime.fromisoformat(json.loads(export[0])["recorded"])
    later = (recorded + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-5)))
    after = client.get("/api/audit/export/fhir", params={"since": later.isoformat()})
    assert after.status_code == 200 and after.text == ""
    before = (recorded - timedelta(hours=1)).astimezone(timezone(timedelta(hours=-5)))
    assert client.get("/api/audit/export/fhir", params={"since": before.isoformat()}).text
    assert client.get("/api/audit/export/fhir", params={"since": "2000-01-01"}).text
    for bad in ("zzz", "2026-13-01", "2026-10-01T00:00:00"):  # the last: a local time
        refused = client.get("/api/audit/export/fhir", params={"until": bad})
        assert refused.status_code == 400 and "until is a UTC day" in refused.text


def test_the_cli_writes_ndjson(
    tmp_path: Path, triage_agent: Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    trail(AuditStore(db), triage_agent)
    out = tmp_path / "audit.ndjson"
    assert main(["audit", "export", "--db", str(db), "--out", str(out)]) == 0
    assert "13 resources written" in capsys.readouterr().err
    assert len(out.read_text().splitlines()) == 13  # 7 AuditEvents, 6 Provenances


def test_the_cli_reads_its_bounds_as_times_such_as_a_backups_name(
    tmp_path: Path, triage_agent: Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--since` / `--until` were compared as text with the stored UTC times: the time in a
    backup's name (`date -u +%Y%m%dT%H%M%SZ`, as `jobs.sh` names it, and what the runbook's
    step after a restore exports from) exported nothing, and a `Z` time left out the events of
    its own second, both silently. A time is read in ISO 8601, basic or extended, in UTC
    unless it gives its offset."""
    db = tmp_path / "a.db"
    with AuditStore(db) as store:
        trail(store, triage_agent)
        times = [datetime.fromisoformat(event["created_at"]) for event in store.iter_events()]

    def exported(*bounds: str) -> list[dict[str, Any]]:
        assert main(["audit", "export", "--db", str(db), *bounds]) == 0
        return [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    every = exported()
    first = min(times).replace(microsecond=0)  # the first event's second
    after = max(times).replace(microsecond=0) + timedelta(seconds=1)
    eastern = timezone(timedelta(hours=-5))
    for since in (
        first.strftime("%Y%m%dT%H%M%SZ"),  # a backup's name: mendari-<this>.dump
        first.strftime("%Y-%m-%dT%H:%M:%SZ"),
        first.strftime("%Y-%m-%dT%H:%M:%S"),  # no zone: UTC
        first.astimezone(eastern).isoformat(),
    ):
        assert exported("--since", since) == every, since
        assert exported("--until", since) == [], since
    for later in (after.strftime("%Y%m%dT%H%M%SZ"), after.astimezone(eastern).isoformat()):
        assert exported("--since", later) == [], later
        assert exported("--until", later) == every, later


def test_the_cli_refuses_a_bound_that_is_no_time(
    tmp_path: Path, triage_agent: Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bound it cannot read was compared as text all the same and exported nothing, with exit
    0: after a restore, an empty list of what to re-apply (revoked keys come back to life)."""
    db = tmp_path / "a.db"
    trail(AuditStore(db), triage_agent)
    capsys.readouterr()
    for flag in ("--since", "--until"):
        for bad in ("yesterday", "27-11-2026", "2026-13-01", "0001-01-01T00:00:00+01:00"):
            assert main(["audit", "export", "--db", str(db), flag, bad]) == 1, bad
            captured = capsys.readouterr()
            assert captured.out == "", bad
            assert f"argument {flag}: not a time: {bad!r}" in captured.err, bad


def test_reads_and_purges_of_encrypted_inputs_are_exported(
    tmp_path: Path, triage_agent: Agent
) -> None:
    """PHI mode's access log in FHIR: a read is action R by the reader, and a purge is action D
    by the engine."""
    store = AuditStore(tmp_path / "phi.db", phi=PhiPolicy(mode="encrypt", key=os.urandom(32)))
    result = Decision(triage_agent, store=store)(message(SECRET))
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
    # An agent's connection for an organization (`agent_connection:<id>`, ADR-0021).
    "agent_connection.set",
    "agent_connection.cleared",
    "agent_connection.origin_allowed",
    # A grading round of written output (`written:<id>`, ADR-0025).
    "written.round_created",
    "written.assigned",
    "written.item_read",
    "written.graded",
    "written.round_closed",
    "written.erased",
    "governance.approved",
    "governance.revoked",
    "registry_agent.approved",
    "registry_agent.revoked",
    "deployment.recorded",
    "key.created",
    "key.revoked",
    "client.registered",
    "client.revoked",
    "client.keys_replaced",  # a client's key set replaced, on the client (LP-12h)
    "notice.acknowledged",
    "automation.held",
    "automation.lifted",
    "retention.purged",  # a purge's summary (`RETENTION_CHAIN`); each deletion is on its decision
    "sessions.ended",  # the break-glass signing everyone out (`SESSIONS_CHAIN`, LP-21l)
    "key.rotated",  # an API key's rotation, on the key (LP-09)
    # People, organizations and memberships (LP-12d): on their own chain ids, never a decision.
    "user.recorded",
    "organization.created",
    "membership.created",
    "membership.roles_changed",
    "membership.removed",
    "membership.bound",
    "invitation.created",
    "invitation.accepted",
    "invitation.revoked",
    "access.requested",
    "access.handled",
    "phi.rotated",  # a PHI key rotation's batch (`KEYS_CHAIN`)
    "input_hash.epoch",  # a new input hash key (`KEYS_CHAIN`)
    "eval.recorded",  # a stored evaluation report (`eval:<report id>`, LP-10)
    # An agent store's versions (`store_version:<id>`, LP-20e).
    "store_version.created",
    "store_version.withdrawn",
    "store_key.added",  # its authors' keys (`store_key:<id>`, LP-20f)
    "store_key.revoked",
    "store_dataset.created",  # its datasets and jobs (LP-20g)
    "store_job.created",
    "store_version.promoted",  # promotion (LP-20h)
    "store_version.retired",
    "store_dataset.proposal_accepted",  # proposed tests (LP-20i)
    "store_dataset.proposal_dismissed",
}


def recorded_kinds() -> set[str]:
    """Every event kind the code chains, by its literal at each call, by position or as
    `event=`. Only `_append` (to `_append_many`) and `record_event` (to `_append`) pass their
    caller's on; any other call without a literal fails here."""
    recorded: set[str] = set()
    passing_on = {("_append", "_append_many"), ("record_event", "_append")}
    for path in (ROOT / "src" / "mendari").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        within = {  # each node's innermost function (`ast.walk` gives outer ones first)
            node: function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            for node in ast.walk(function)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            position = {"_append": 2, "_append_many": 1, "record_event": 1}.get(node.func.attr)
            if position is None:
                continue
            named = [keyword.value for keyword in node.keywords if keyword.arg == "event"]
            event = node.args[position] if len(node.args) > position else next(iter(named), None)
            where = f"{path}:{node.lineno}"
            if isinstance(event, ast.Name) and event.id == "event":
                assert (within.get(node), node.func.attr) in passing_on, f"{where}: passed on"
                continue
            assert isinstance(event, ast.Constant), f"{where}: an event name that is not a literal"
            recorded.add(str(event.value))
    return recorded


def test_every_event_the_code_records_has_an_audit_event_code() -> None:
    """The audit-event CodeSystem (standards/terminology.py) is generated from `_EVENTS`, so an
    event recorded on a decision but missing there would be exported with an undefined code."""
    assert recorded_kinds() - NOT_ON_A_DECISION == set(audit_fhir._EVENTS)


def test_audit_entries_are_immutable_versions_narrated_from_codes(
    store: AuditStore, triage_agent: Agent
) -> None:
    trail(store, triage_agent)
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
    assert engine == {"system": AGENT_ID_SYSTEM, "value": triage_agent.ref}


BASE = "https://mendari.example/fhir"


def test_reviewing_an_abstention_authors_the_label(store: AuditStore, triage_agent: Agent) -> None:
    """An abstention made no recommendation, so its reviewer verifies nothing: they author the
    label (`human_override` is None, not False)."""
    result = Decision(triage_agent, store=store)(message("hello?"))
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


def test_served_resources_refer_to_the_task_by_url(store: AuditStore, triage_agent: Agent) -> None:
    """Under /fhir an AuditEvent points at the Task, a Provenance at the Task version its event
    made; the identifier stays next to the reference."""
    confirmed, _, automated = trail(store, triage_agent)
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


def test_the_exports_keep_identifiers_only(store: AuditStore, triage_agent: Agent) -> None:
    """The /api/audit collection Bundles have urn:uuid full URLs: a reference there would
    resolve against nothing."""
    trail(store, triage_agent)
    assert '"reference"' not in json.dumps(list(audit_fhir.resources(store.iter_events())))


def test_a_served_provenance_needs_its_task_version(store: AuditStore, triage_agent: Agent) -> None:
    trail(store, triage_agent)
    created = store.events()[0]
    with pytest.raises(ValueError, match="Task version"):
        audit_fhir.provenance(created, base=BASE)
    assert audit_fhir.task_versions([created]) == {created["seq"]: 1}
