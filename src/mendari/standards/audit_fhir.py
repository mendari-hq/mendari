# SPDX-License-Identifier: Apache-2.0
"""The audit trail as FHIR R4: an AuditEvent per chained event, a Provenance per activity.

- **AuditEvent** for every event in the hash chain (`decision.created`, `review.resolved`,
  `outcome.recorded`, `feedback.recorded`, `card.feedback` and the others in `_EVENTS`):
  - who acted: the engine running the agent, or the reviewer, reporter, reader, requester, or
    whoever recorded a downstream outcome (the signed-in person or API key); a decision made
    with sign-in on also names who asked for it (`requested_by`), as the requestor;
  - what it acted on: the decision, by the identifier its FHIR `Task` carries;
  - the event's place in the chain (`seq`, `prev_hash`, `hash`), so the export keeps the tamper
    evidence: consecutive events link up, and each hash can be checked against the store.
- **Provenance** for the activities that create or change the decision's result:
  - the decision itself (`CREATE`, authored by the engine, from the input identified by its
    hash; with sign-in on, who asked for it is a second agent, of role "requester");
  - a review (`UPDATE`; the reviewer verifies the recommendation, or authors a label when
    overriding it or when there was none, an abstention);
  - a downstream outcome (`UPDATE`, authored by whoever recorded it).

**A downstream outcome's source** is the system that observed it, an identifier
(`core.audit.SYSTEM_ID`), not an agent: it is in the AuditEvent's `source.site`. Before Phase 4
the outcome's agent was its source, a string the caller chose; an event recorded then has no
recorder (`recorded_by`), so its agent is "not recorded", and its source is shown only when it
is an identifier.

Like evaluation reports, the export carries identifiers and codes, never case text: notes
(review, feedback) stay in the store, and inputs are referred to by hash. A note's erasure
(`note.erased`) is exported with its reason, a code. Narratives are built from the same codes,
ids and times.

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

from mendari.core.audit import RESULT_EVENTS, SYSTEM_ID
from mendari.standards.fhir import AGENT_ID_SYSTEM, DECISION_ID_SYSTEM, narrative

EVENT_SYSTEM = "https://mendari.co/fhir/CodeSystem/audit-event"
# The events on no decision (an organization's keys, clients, people, approvals, holds and
# reports; the deployment's own), as AuditEvent.subtype, and the chain id each is about.
ADMINISTRATIVE_SYSTEM = "https://mendari.co/fhir/CodeSystem/audit-administrative-event"
SUBJECT_SYSTEM = "https://mendari.co/fhir/NamingSystem/audit-subject"
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
# Who asked for a decision, on its Provenance: a role as text, since no agent type code fits.
REQUESTER = {"text": "requester"}
# An outcome recorded before its recorder was (Phase 4).
NOT_RECORDED = "not recorded"
# Every event the store chains on a decision (a test checks the code records no other). Unknown
# ones are still exported, with the event name as display.
_EVENTS = {
    "decision.created": ("C", "Decision created"),
    "review.resolved": ("U", "Review resolved"),
    "outcome.recorded": ("U", "Downstream outcome recorded"),
    "feedback.recorded": ("C", "Feedback recorded"),
    "input.read": ("R", "Encrypted input or notes read"),  # PHI mode: access logging
    "input.purged": ("D", "Encrypted input deleted by the retention policy"),
    "input.erased": ("D", "Encrypted input deleted on request"),
    "note.erased": ("D", "Note erased, on request or by the retention policy"),
    "card.feedback": ("C", "Card feedback recorded (CDS Hooks)"),
    # An action an agent proposed or a caller asked about, and its release or denial by the
    # decision's reviewer (spec §13.9): authorized and recorded here, run elsewhere.
    "action.proposed": ("C", "Action proposed"),
    "action.resolved": ("U", "Action released or denied by review"),
    # A text an agent wrote, held for the decision's reviewer, and that reviewer's verdict on
    # it (ADR-0027): kept and recorded here, sent or filed elsewhere.
    "output.held": ("C", "Written output held for review"),
    "output.resolved": ("U", "Written output released or withheld by review"),
}
# The activities that create or change a decision's result: each gets a Provenance and makes a
# new version of the decision's Task (task_fhir.py). Other events change neither. The store's
# own list, so a search on a Task's lastUpdated reads the same events.
VERSION_EVENTS = RESULT_EVENTS
# What each event says about the decision, as codes (never notes or other free text).
_DETAILS = {
    "decision.created": ("outcome", "recommendation", "priority"),
    "review.resolved": ("final_label", "human_override"),
    "outcome.recorded": ("final_label",),
    "feedback.recorded": ("rating", "expected"),
    "note.erased": ("reason",),
    "input.erased": ("reason",),
    # What a clinician did with a CDS Hooks card (LP-23f): codes and ids, never a comment.
    "card.feedback": ("service", "outcome", "accepted_suggestions", "override_reason"),
    # A proposed action: ids, the declared name, codes and the arguments' hash. The arguments,
    # and a name the manifest does not declare, are a note: never in an export.
    "action.proposed": (
        "action_id",
        "name",
        "effect",
        "disposition",
        "reason",
        "arguments_hash",
        "source",
    ),
    "action.resolved": ("action_id", "name", "effect", "disposition", "reason"),
    # A held text: the output's name as the manifest declares it, and the reviewer's verdict.
    # The text is a note: never in an export.
    "output.held": ("output",),
    "output.resolved": ("output", "verdict"),
}


def resources(
    events: Iterable[Mapping[str, Any]],
    *,
    base: str | None = None,
    versions: Mapping[int, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """AuditEvents and Provenances for chained events (each joined with its decision's
    `agent_id`, `agent_version` and `record`, as `AuditStore.events` returns them). Served under
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
    decision = {key: item[key] for key in ("agent_id", "agent_version", "record")}
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
        "agent": [_actor(event, payload), *_requested(event, requestor=True)],
        "source": _source(event),
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
        if kind == "outcome.recorded":
            agent = {"display": str(payload.get("recorded_by") or NOT_RECORDED)}
        else:
            agent = {"display": str(payload.get("reviewer") or "unknown")}
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
        "agent": [
            {"type": {"coding": [{"system": _PARTICIPANT, "code": role}]}, "who": agent},
            *_requested(event, role=[REQUESTER]),
        ],
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
    """The engine as an AuditEvent `agent`: the agent version that decided (`agent_id`,
    `agent_version`)."""
    agent = f"{event['agent_id']}@{event['agent_version']}"
    identifier = {"system": AGENT_ID_SYSTEM, "value": agent}
    return {"identifier": identifier, "display": f"Mendari engine · {agent}"}


def _requested(event: Mapping[str, Any], **shown: Any) -> list[dict[str, Any]]:
    """Who asked for a decision (`requested_by`), as one more agent with the `shown` fields: the
    AuditEvent marks it the requestor, the Provenance gives it the role "requester" (FHIR's
    agent types have no code for one). Chained on new `decision.created` events with sign-in on
    (ADR-0006); older events, and open mode, name nobody."""
    asked = event["payload"].get("requested_by") if event["event"] == "decision.created" else None
    return [] if not asked else [{"who": {"display": str(asked)}, **shown}]


def _actor(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    if event["event"] == "decision.created":
        return {"who": engine(event), "requestor": False}
    if event["event"] == "input.purged" or (
        event["event"] == "note.erased" and not payload.get("erased_by")
    ):
        return {"who": engine(event), "requestor": False}  # the retention policy
    if event["event"] == "action.proposed" and payload.get("source") == "answer":
        return {"who": engine(event), "requestor": False}  # proposed with the agent's answer
    if event["event"] == "output.held":
        return {"who": engine(event), "requestor": False}  # written with the agent's answer
    if event["event"] == "outcome.recorded":
        return {
            "who": {"display": str(payload.get("recorded_by") or NOT_RECORDED)},
            "requestor": True,
        }
    name = (
        payload.get("reviewer")
        or payload.get("reporter")
        or payload.get("reader")
        or payload.get("requested_by")
        or payload.get("erased_by")
    )
    return {"who": {"display": str(name or "anonymous")}, "requestor": True}


def _source(event: Mapping[str, Any]) -> dict[str, Any]:
    """Mendari observed every event; a downstream outcome also names the system that reported
    it, as `site`, when that is an identifier (always, since Phase 4)."""
    source: dict[str, Any] = {"observer": {"display": "Mendari"}, "type": [_SERVER]}
    reported = event["payload"].get("source") if event["event"] == "outcome.recorded" else None
    if isinstance(reported, str) and SYSTEM_ID.fullmatch(reported):
        source = {"site": reported, **source}
    return source


def _facts(event: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    created = event["event"] == "decision.created"
    source = (event.get("record") or {}) if created else event["payload"]
    for key in _DETAILS.get(event["event"], ()):
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, Mapping) and "code" in value:  # a card's override reason
            value = f"{value.get('system')}|{value.get('code')}"
        elif isinstance(value, Mapping):  # a thumbs-down's right answer
            value = f"{value.get('question')}={value.get('label')}"
        elif isinstance(value, list):  # a card's accepted suggestions
            if not value:
                continue
            value = ", ".join(str(item) for item in value)
        yield key, str(value).lower() if isinstance(value, bool) else str(value)


def full_url(resource: Mapping[str, Any]) -> str:
    """A stable urn:uuid per entry (from the resource's type and id), so exporting the same trail
    twice gives the same Bundle."""
    key = f"{resource['resourceType']}/{resource['id']}".encode()
    return f"urn:uuid:{uuid.UUID(hashlib.sha256(key).hexdigest()[:32])}"


# -- administrative events (LP-12g, ADR-0008) -----------------------------------------------------
# An event on no decision as an AuditEvent of its own: ids and codes, never a key's or client's
# name, a hold's reason or an approval's reference. Its id is its chain hash, which names no
# place in the chain; it has no Provenance and is never served under /fhir (an organization
# reads it at /api/audit/administrative, the operator from the command line).

_DCM = "http://dicom.nema.org/resources/ontology/DCM"
# Credentials, people and keys are security administration (DICOM's Security Alert, with its
# subtype where one fits); approvals, holds, reports and the deployment's records are not.
_SECURITY_ALERT = {"system": _DCM, "code": "110113", "display": "Security Alert"}
_CREDENTIALS = {"system": _DCM, "code": "110137", "display": "User Security Attributes Changed"}
_ROLES = {"system": _DCM, "code": "110136", "display": "Security Roles Changed"}
_CONFIGURATION = {"system": _DCM, "code": "110129", "display": "Security Configuration"}
# kind: (action, display, DICOM subtype or None when not security administration, else {}).
_ADMINISTRATIVE: dict[str, tuple[str, str, dict[str, str] | None]] = {
    "key.created": ("C", "API key created", _CREDENTIALS),
    "key.rotated": ("U", "API key rotated", _CREDENTIALS),
    "key.revoked": ("U", "API key revoked", _CREDENTIALS),
    "client.registered": ("C", "Backend Services client registered", _CREDENTIALS),
    "client.revoked": ("U", "Backend Services client revoked", _CREDENTIALS),
    "client.keys_replaced": ("U", "Backend Services client's key set replaced", _CREDENTIALS),
    "governance.approved": ("C", "Automation approved", None),
    "governance.revoked": ("U", "Automation approval revoked", None),
    "registry_agent.approved": ("C", "Agent from a registry approved", None),
    "registry_agent.revoked": ("U", "Agent from a registry approval revoked", None),
    "automation.held": ("C", "Automation held", None),
    "automation.lifted": ("U", "Automation hold lifted", None),
    "eval.recorded": ("C", "Evaluation report recorded", None),
    "organization.created": ("C", "Organization created", {}),
    "membership.created": ("C", "Membership created", _ROLES),
    "membership.roles_changed": ("U", "Membership roles changed", _ROLES),
    "membership.removed": ("D", "Membership removed", _ROLES),
    "membership.bound": ("U", "Membership bound to a person", _ROLES),
    "invitation.created": ("C", "Invitation created", {}),
    "invitation.accepted": ("U", "Invitation accepted", _ROLES),
    "invitation.revoked": ("U", "Invitation revoked", {}),
    "access.requested": ("C", "Access requested", {}),
    # Closing a request changes no role: a grant's role is its own `membership.created`.
    "access.handled": ("U", "Access request handled", {}),
    "notice.acknowledged": ("C", "Notice acknowledged", None),
    "user.recorded": ("C", "Person recorded", {}),
    "deployment.recorded": ("C", "Deployment recorded", None),
    "retention.purged": ("D", "Retention purge", None),
    "sessions.ended": ("D", "Sessions ended", {}),
    "phi.rotated": ("U", "PHI key rotation batch", _CONFIGURATION),
    "input_hash.epoch": ("C", "Input hash key epoch", _CONFIGURATION),
    # An agent store's versions (LP-20e): the digest and who acted, never a file.
    "store_version.created": ("C", "Agent store version added", None),
    "store_version.withdrawn": ("U", "Agent store version withdrawn", None),
    # Its authors' keys (LP-20f): the key's id and who acted.
    "store_key.added": ("C", "Agent store author key registered", None),
    "store_key.revoked": ("U", "Agent store author key revoked", None),
    # Its datasets and evaluation jobs (LP-20g): ids, a SHA-256 and counts, never a case.
    "store_dataset.created": ("C", "Agent store dataset added", None),
    "store_job.created": ("C", "Agent store evaluation job made", None),
    # Promotion (LP-20h): the digest, its authors, the approver and the reports relied on.
    "store_version.promoted": ("U", "Agent store version promoted to be served", None),
    "store_version.retired": ("U", "Agent store version's promotion retired", None),
    # Proposed tests from reviewers' corrections (LP-20i): the dataset, the case and the digest.
    "store_dataset.proposal_accepted": (
        "U",
        "Proposed test accepted for an agent store version",
        None,
    ),
    "store_dataset.proposal_dismissed": (
        "U",
        "Proposed test dismissed for an agent store version",
        None,
    ),
    # An agent's connection for an organization (ADR-0021): the agent and who acted; never the
    # token, nor where it leads (the origin stays in the chain's own payload).
    # A grading round of written output (ADR-0025): the round and its organization, and who
    # graded or closed. The item, the assignment, the commitment and the counts are no detail
    # here (`_ADMINISTRATIVE_DETAILS`).
    "written.round_created": ("C", "Grading round of written output created", None),
    "written.assigned": ("C", "Written output assigned to a grader", None),
    "written.item_read": ("R", "Written output read by its grader", None),
    "written.graded": ("C", "Written output graded", None),
    "written.round_closed": ("U", "Grading round closed and its texts erased", None),
    "written.erased": ("D", "Written outputs erased by retention", None),
    "agent_connection.set": ("U", "Agent connection set", _CONFIGURATION),
    "agent_connection.cleared": ("D", "Agent connection cleared", _CONFIGURATION),
    "agent_connection.origin_allowed": (
        "U",
        "Agent connection's address allowed by an operator",
        _CONFIGURATION,
    ),
}
# Who acted, by the first of these a payload has (ids: a principal's chain name or an id).
_ACTORS = (
    "created_by",
    "rotated_by",
    "revoked_by",
    "set_by",
    "lifted_by",
    "approver",
    "changed_by",
    "removed_by",
    "handled_by",
    "ended_by",
    "actor",
    "user",
)
# A kind whose actor is under a key that means something else elsewhere (`replaced_by` is the
# replacement key's id on `key.rotated`), or under a key of its own: its agent, never one of its
# details.
_ACTOR_KEY = {
    "client.keys_replaced": "replaced_by",
    # Who was shown or graded an item is its grader; `grader` on `written.assigned` is who was
    # assigned, not who acted. A round is closed `by` someone.
    "written.item_read": "grader",
    "written.graded": "grader",
    "written.round_closed": "by",
}
# What a payload says, as ids and codes (never `name`, `reason`, `reference` or `providers`).
_ADMINISTRATIVE_DETAILS = (
    "id",
    "client_id",
    "org",
    "role",
    "roles",
    "previous",
    "scope",
    "preset",
    "expires_at",
    "expires_at_before",
    "overlap_hours",
    "replaces",
    "replaced_by",
    "outcome",
    "agent_id",
    "agent_version",
    "agent_digest",
    "risk_level",
    "report",
    "result_sha256",
    "dataset_sha256",
    "ledger",
    "version",
    "user",
    "email_digest",
    "name_digest",
    "key_id",
    "to",
    "from",
    "environment",
    "release",
    "retention_days",
    "inputs",
    "details",
    "notes",
    "counts",
    "count",
    "source",
    "kinds",
    "ended",
)
# The kinds whose details include mappings of counts (a key rotation's `from`, by key id): each
# written `name=count`.
_COUNTED = ("phi.rotated",)


def administrative_resources(events: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    """An AuditEvent for each administrative event (`AuditStore.administrative_events` items)."""
    for event in events:
        yield administrative_event(event)


def administrative_event(event: Mapping[str, Any]) -> dict[str, Any]:
    kind, payload = event["event"], event["payload"]
    action, display, dicom = _ADMINISTRATIVE.get(kind, ("E", kind, None))
    actor_key = _ACTOR_KEY.get(kind)
    if actor_key is not None:
        actor = payload.get(actor_key) or None
    else:
        actor = next((payload[key] for key in _ACTORS if payload.get(key)), None)
    details = [{"type": "hash", "valueString": event["hash"]}]
    for key in _ADMINISTRATIVE_DETAILS:
        value = payload.get(key)
        if value is None or key == actor_key:
            continue
        if isinstance(value, Mapping):  # a key rotation's `from` or `counts` (`_COUNTED`)
            if kind not in _COUNTED:
                continue
            value = ",".join(f"{name}={count}" for name, count in value.items())
        elif isinstance(value, list | tuple):
            value = ",".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = str(value).lower()
        if str(value):  # an R4 string is never empty (the "" organization, an empty list)
            details.append({"type": key, "valueString": str(value)})
    subtype = [{"system": ADMINISTRATIVE_SYSTEM, "code": kind, "display": display}]
    summary = (
        f"{display} ({kind}, action {action}) on {event['subject']}, recorded "
        f"{event['created_at']}."
    )
    return {
        "resourceType": "AuditEvent",
        "id": event["hash"],
        "meta": _immutable(event),
        "text": narrative(summary),
        "type": _REST if dicom is None else _SECURITY_ALERT,
        "subtype": subtype + ([dicom] if dicom else []),
        "action": action,
        "recorded": event["created_at"],
        "outcome": "0",
        "agent": [
            {"who": {"display": str(actor)}, "requestor": True}
            if actor is not None
            else {"who": {"display": NOT_RECORDED}, "requestor": False}
        ],
        "source": {"observer": {"display": "Mendari"}, "type": [_SERVER]},
        "entity": [
            {
                "what": {
                    "identifier": {"system": SUBJECT_SYSTEM, "value": event["subject"]},
                    "display": event["subject"],
                },
                "type": _OBJECT,
                "detail": details,
            }
        ],
    }
