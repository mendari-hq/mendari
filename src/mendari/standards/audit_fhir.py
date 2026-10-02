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
  - a review (`UPDATE`; the reviewer verifies the recommendation, or authors a label when
    overriding it or when there was none, an abstention);
  - a downstream outcome (`UPDATE`).

Like evaluation reports, the export carries identifiers and codes, never case text: free-text
review and feedback notes stay in the store, and inputs are referred to by hash. Narratives are
built from the same codes, ids and times.

Both are immutable entries with deterministic ids, so each is version `1`, last updated when it
was recorded. The subtype stays a Mendari code (`terminology.py` publishes the CodeSystem): a
FHIR `restful-interaction` subtype would claim REST interactions that never happened.

**Served under `/fhir` (`base`), and exported.** Mendari serves each decision as a Task
(`task_fhir.py`, ADR-0003), so the resources it serves under its FHIR base also refer to that Task
by an absolute URL: an AuditEvent to the Task, a Provenance to the Task version its activity
made (`{base}/Task/{id}/_history/{n}`). The `/api/audit` exports are `collection` Bundles with
`urn:uuid` full URLs, where a relative reference would resolve against nothing; they keep the
identifier only, as before.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from mendari.standards.fhir import DECISION_ID_SYSTEM, PACK_ID_SYSTEM, narrative

EVENT_SYSTEM = "https://mendari.co/fhir/CodeSystem/audit-event"
INPUT_HASH_SYSTEM = "https://mendari.co/fhir/NamingSystem/input-hash"
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
# Every event the store chains on a decision (a test checks the code records no other). Unknown
# ones are still exported, with the event name as display.
_EVENTS = {
    "decision.created": ("C", "Decision created"),
    "review.resolved": ("U", "Review resolved"),
    "outcome.recorded": ("U", "Downstream outcome recorded"),
    "feedback.recorded": ("C", "Feedback recorded"),
    "input.read": ("R", "Encrypted input read"),  # PHI mode: access logging
    "input.purged": ("D", "Encrypted input deleted by the retention policy"),
    "pas.submitted": ("E", "Prior authorization requested (Da Vinci PAS)"),
}
# The activities that create or change a decision's result: each gets a Provenance and makes a
# new version of the decision's Task (task_fhir.py). Other events change neither.
VERSION_EVENTS = ("decision.created", "review.resolved", "outcome.recorded")
# What each event says about the decision, as codes (never notes or other free text).
_DETAILS = {
    "decision.created": ("outcome", "recommendation", "priority"),
    "review.resolved": ("final_label", "human_override"),
    "outcome.recorded": ("final_label",),
    "feedback.recorded": ("rating", "expected"),
    "pas.submitted": ("outcome", "authorization"),
}


def resources(
    events: Iterable[Mapping[str, Any]],
    *,
    base: str | None = None,
    versions: Mapping[int, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """AuditEvents and Provenances for chained events (each joined with its decision's
    `pack_id`, `pack_version` and `record`, as `AuditStore.events` returns them). Served under
    the FHIR `base`, they refer to the Task by URL, a Provenance to the version its event made:
    `versions` maps each version-making event's `seq` to that number (`task_versions`)."""
    for event in events:
        yield audit_event(event, base=base)
        if event["event"] in VERSION_EVENTS:
            version = None if versions is None else versions.get(event["seq"])
            yield provenance(event, base=base, version=version)


def task_versions(events: Iterable[Mapping[str, Any]]) -> dict[int, int]:
    """The Task version (1, 2, ...) each version-making event of one decision made, by `seq`.
    `events` are all of the decision's events, in chain order (`AuditStore.decision_events`)."""
    made = [event["seq"] for event in events if event["event"] in VERSION_EVENTS]
    return {seq: number for number, seq in enumerate(made, start=1)}


def decision_bundle(item: Mapping[str, Any]) -> dict[str, Any]:
    """One decision's audit trail (`AuditStore.get`) as a `collection` Bundle."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": full_url(r), "resource": r} for r in resources(joined(item))],
    }


def joined(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A decision's events (`AuditStore.get` or `item`), each joined with its decision as
    `AuditStore.events` returns them, ready for `resources`."""
    decision = {key: item[key] for key in ("pack_id", "pack_version", "record")}
    return [{**event, **decision, "decision_id": item["id"]} for event in item["events"]]


def audit_event(event: Mapping[str, Any], *, base: str | None = None) -> dict[str, Any]:
    action, display = _EVENTS.get(event["event"], ("E", event["event"]))
    payload = event["payload"]
    details = [
        {"type": "seq", "valueString": str(event["seq"])},
        {"type": "prev_hash", "valueString": event["prev_hash"]},
        {"type": "hash", "valueString": event["hash"]},
    ]
    facts = dict(_facts(event))
    details += [{"type": key, "valueString": value} for key, value in facts.items()]
    summary = (
        f"{display} ({event['event']}, action {action}) on decision {event['decision_id']}, "
        f"audit chain entry {event['seq']}, recorded {event['created_at']}."
    )
    return {
        "resourceType": "AuditEvent",
        "id": f"{event['decision_id']}-{event['seq']}",
        "meta": _immutable(event),
        "text": narrative(summary),
        "type": _REST,
        "subtype": [{"system": EVENT_SYSTEM, "code": event["event"], "display": display}],
        "action": action,
        "recorded": event["created_at"],
        "outcome": "0",
        "agent": [_actor(event, payload)],
        "source": {"observer": {"display": "Mendari"}, "type": [_SERVER]},
        "entity": [
            {"what": _decision(event, _task(event, base)), "type": _OBJECT, "detail": details}
        ],
    }


def provenance(
    event: Mapping[str, Any], *, base: str | None = None, version: int | None = None
) -> dict[str, Any]:
    """Served under the FHIR `base`, the target is the Task `version` this event made."""
    if base is not None and version is None:
        raise ValueError("a served Provenance targets a Task version: pass the event's version")
    payload = event["payload"]
    kind = event["event"]
    if kind == "decision.created":
        activity, role = "CREATE", "author"
        agent = engine(event)
    else:
        activity = "UPDATE"
        # Confirming the recommendation verifies it; choosing another label authors a new one,
        # and so does labelling an abstention (no recommendation: `human_override` is None).
        confirmed = kind == "review.resolved" and payload.get("human_override") is False
        role = "verifier" if confirmed else "author"
        agent = {"display": str(payload.get("reviewer") or payload.get("source") or "unknown")}
    summary = (
        f"{activity} of decision {event['decision_id']} ({kind}); agent role {role}, "
        f"recorded {event['created_at']}."
    )
    resource: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": f"{event['decision_id']}-p{event['seq']}",
        "meta": _immutable(event),
        "text": narrative(summary),
        "target": [_decision(event, _task(event, base, version))],
        "recorded": event["created_at"],
        "activity": {"coding": [{"system": f"{TERMINOLOGY}/v3-DataOperation", "code": activity}]},
        "agent": [{"type": {"coding": [{"system": _PARTICIPANT, "code": role}]}, "who": agent}],
    }
    input_hash = (event.get("record") or {}).get("input_hash")
    if kind == "decision.created" and input_hash:
        what = {"identifier": {"system": INPUT_HASH_SYSTEM, "value": input_hash}}
        resource["entity"] = [{"role": "source", "what": what}]
    return resource


def _immutable(event: Mapping[str, Any]) -> dict[str, str]:
    return {"versionId": "1", "lastUpdated": event["created_at"]}


def _decision(event: Mapping[str, Any], url: str | None = None) -> dict[str, Any]:
    decision_id = event["decision_id"]
    reference = {} if url is None else {"reference": url}
    return {
        **reference,
        "identifier": {"system": DECISION_ID_SYSTEM, "value": decision_id},
        "display": f"Decision {decision_id}",
    }


def _task(event: Mapping[str, Any], base: str | None, version: int | None = None) -> str | None:
    """The decision's Task under the FHIR `base` (or one version of it), or None outside it."""
    if base is None:
        return None
    url = f"{base.rstrip('/')}/Task/{event['decision_id']}"
    return url if version is None else f"{url}/_history/{version}"


def engine(event: Mapping[str, Any]) -> dict[str, Any]:
    """The engine as an agent: the pack version (`pack_id`, `pack_version`) that decided."""
    pack = f"{event['pack_id']}@{event['pack_version']}"
    identifier = {"system": PACK_ID_SYSTEM, "value": pack}
    return {"identifier": identifier, "display": f"Mendari engine · {pack}"}


def _actor(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    if event["event"] == "decision.created":
        return {"who": engine(event), "requestor": False}
    if event["event"] == "input.purged":
        return {"who": engine(event), "requestor": False}
    name = (
        payload.get("reviewer")
        or payload.get("reporter")
        or payload.get("reader")
        or payload.get("requested_by")
        or payload.get("source")
    )
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
