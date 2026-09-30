"""The audit trail as FHIR R4: an AuditEvent per chained event, a Provenance per activity.

- **AuditEvent** for every event in the hash chain (`decision.created`, `review.resolved`,
  `outcome.recorded`, `feedback.recorded`):
  - who acted: the engine running the pack, or the reviewer, reporter or downstream source;
  - what it acted on: the decision, by the identifier its FHIR `Task` carries;
  - the event's place in the chain (`seq`, `prev_hash`, `hash`), so the export keeps the tamper
    evidence: consecutive events link up, and each hash can be checked against the store.
- **Provenance** for the activities that create or change the decision's result:
  - the decision itself (`CREATE`, authored by the engine, from the input identified by its
    hash);
  - a review (`UPDATE`; the reviewer verifies, or authors when overriding);
  - a downstream outcome (`UPDATE`).

Like evaluation reports, the export carries identifiers and codes, never case text: free-text
review and feedback notes stay in the store, and inputs are referred to by hash.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from openhealth.standards.fhir import DECISION_ID_SYSTEM, PACK_TAG_SYSTEM

EVENT_SYSTEM = "https://openhealth.dev/fhir/CodeSystem/audit-event"
INPUT_HASH_SYSTEM = "https://openhealth.dev/fhir/NamingSystem/input-hash"
TERMINOLOGY = "http://terminology.hl7.org/CodeSystem"
_REST = {
    "system": f"{TERMINOLOGY}/audit-event-type",
    "code": "rest",
    "display": "RESTful Operation",
}
_SERVER = {
    "system": f"{TERMINOLOGY}/security-source-type",
    "code": "4",
    "display": "Application Server",
}
_PARTICIPANT = f"{TERMINOLOGY}/provenance-participant-type"
_OBJECT = {"system": f"{TERMINOLOGY}/audit-entity-type", "code": "2", "display": "System Object"}
_EVENTS = {
    "decision.created": ("C", "Decision created"),
    "review.resolved": ("U", "Review resolved"),
    "outcome.recorded": ("U", "Downstream outcome recorded"),
    "feedback.recorded": ("C", "Feedback recorded"),
}
# What each event says about the decision, as codes (never notes or other free text).
_DETAILS = {
    "decision.created": ("outcome", "recommendation", "priority"),
    "review.resolved": ("final_label", "human_override"),
    "outcome.recorded": ("final_label",),
    "feedback.recorded": ("rating", "expected"),
}


def resources(events: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    """AuditEvents and Provenances for chained events (each joined with its decision's
    `pack_id`, `pack_version` and `record`, as `AuditStore.events` returns them)."""
    for event in events:
        yield audit_event(event)
        if event["event"] in ("decision.created", "review.resolved", "outcome.recorded"):
            yield provenance(event)


def decision_bundle(item: Mapping[str, Any]) -> dict[str, Any]:
    """One decision's audit trail (`AuditStore.get`) as a `collection` Bundle."""
    decision = {key: item[key] for key in ("pack_id", "pack_version", "record")}
    events = [{**event, **decision, "decision_id": item["id"]} for event in item["events"]]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": full_url(r), "resource": r} for r in resources(events)],
    }


def audit_event(event: Mapping[str, Any]) -> dict[str, Any]:
    action, display = _EVENTS.get(event["event"], ("E", event["event"]))
    payload = event["payload"]
    details = [
        {"type": "seq", "valueString": str(event["seq"])},
        {"type": "prev_hash", "valueString": event["prev_hash"]},
        {"type": "hash", "valueString": event["hash"]},
    ]
    facts = dict(_facts(event))
    details += [{"type": key, "valueString": value} for key, value in facts.items()]
    return {
        "resourceType": "AuditEvent",
        "id": f"{event['decision_id']}-{event['seq']}",
        "type": _REST,
        "subtype": [{"system": EVENT_SYSTEM, "code": event["event"], "display": display}],
        "action": action,
        "recorded": event["created_at"],
        "outcome": "0",
        "agent": [_actor(event, payload)],
        "source": {"observer": {"display": "OpenHealth Hub"}, "type": [_SERVER]},
        "entity": [{"what": _decision(event), "type": _OBJECT, "detail": details}],
    }


def provenance(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    kind = event["event"]
    if kind == "decision.created":
        activity, role = "CREATE", "author"
        agent = _engine(event)
    else:
        activity = "UPDATE"
        # Confirming the recommendation verifies it; choosing another label authors a new one.
        confirmed = kind == "review.resolved" and not payload.get("human_override")
        role = "verifier" if confirmed else "author"
        agent = {"display": str(payload.get("reviewer") or payload.get("source") or "unknown")}
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": f"{event['decision_id']}-p{event['seq']}",
        "target": [_decision(event)],
        "recorded": event["created_at"],
        "activity": {"coding": [{"system": f"{TERMINOLOGY}/v3-DataOperation", "code": activity}]},
        "agent": [{"type": {"coding": [{"system": _PARTICIPANT, "code": role}]}, "who": agent}],
    }
    input_hash = (event.get("record") or {}).get("input_hash")
    if kind == "decision.created" and input_hash:
        what = {"identifier": {"system": INPUT_HASH_SYSTEM, "value": input_hash}}
        resource["entity"] = [{"role": "source", "what": what}]
    return resource


def _decision(event: Mapping[str, Any]) -> dict[str, Any]:
    decision_id = event["decision_id"]
    return {
        "identifier": {"system": DECISION_ID_SYSTEM, "value": decision_id},
        "display": f"Decision {decision_id}",
    }


def _engine(event: Mapping[str, Any]) -> dict[str, Any]:
    pack = f"{event['pack_id']}@{event['pack_version']}"
    identifier = {"system": PACK_TAG_SYSTEM, "value": pack}
    return {"identifier": identifier, "display": f"OpenHealth engine · {pack}"}


def _actor(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    if event["event"] == "decision.created":
        return {"who": _engine(event), "requestor": False}
    name = payload.get("reviewer") or payload.get("reporter") or payload.get("source")
    return {"who": {"display": str(name or "anonymous")}, "requestor": True}


def _facts(event: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    created = event["event"] == "decision.created"
    source = (event.get("record") or {}) if created else event["payload"]
    for key in _DETAILS.get(event["event"], ()):
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping):  # a thumbs-down's right answer
            value = f"{value.get('question')}={value.get('label')}"
        yield key, str(value).lower() if isinstance(value, bool) else str(value)


def full_url(resource: Mapping[str, Any]) -> str:
    """A stable urn:uuid per entry (from the resource's type and id), so exporting the same trail
    twice gives the same Bundle."""
    key = f"{resource['resourceType']}/{resource['id']}".encode()
    return f"urn:uuid:{uuid.UUID(hashlib.sha256(key).hexdigest()[:32])}"
