"""A Mendari decision as a FHIR R4 Task (ADR-0003), derived on read: nothing new is stored.

The Task Mendari serves is the decision itself, not the work item the decision routes (that one
`$decide` returns, and the EHR's server owns). Its status says only what Mendari knows:

- `requested`: the decision waits for a person in Mendari's review queue;
- `completed`: the answer is final, at once (AUTO, a policy BLOCK) or after the review;
- `failed`: the input was refused before any provider ran.

`businessStatus` is the outcome (AUTO, HUMAN_REVIEW, ABSTAIN, BLOCK).

**Versions.** The events that change the decision's result make its versions, in chain order:
`decision.created` is version 1, then each `review.resolved` and `outcome.recorded`
(`audit_fhir.VERSION_EVENTS`: exactly the events that get a Provenance). `meta.versionId` is
that ordinal and `meta.lastUpdated` the event's time. Feedback, input reads and purges, and PAS
submissions stay in the chain and make no version. The ordinal, not the global `seq`, so a
version number says nothing about how busy the rest of the deployment is.

**From the chain, not the row.** The outputs come from event payloads: the reviewer's label from
`review.resolved`, the observed label from the latest `outcome.recorded`. `record_outcome`
overwrites `decisions.final_label`, so that column would rewrite version 2.

**What a Task never carries:** input text, subject identifiers (no `for`, no `focus`), review or
feedback notes. `note` holds the decision's reasons, except for a refused input: records made
before the Phase 2 privacy fix can quote the input in those. Reviewer names appear in `owner`, as
they do in the audit export.

**The same in every configuration.** Label codings are in the pack's question CodeSystem
(`davinci.question_system`) whether or not this deployment still serves the pack, so a version
renders the same wherever and whenever it is read. They carry no display: a label's display is
its criteria (`fhir.display`), which only the pack holds.

**The derivation is code.** A release that changes what a version renders changes an existing
version under the same versionId. Tests pin the JSON for a fixed chain; any change to it is a
changelog item. For the same reason the server answers no conditional read (no 304).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from mendari.core.spec import Outcome
from mendari.standards import audit_fhir, davinci
from mendari.standards.fhir import DECISION_ID_SYSTEM, OUTCOME_SYSTEM, PACK_TAG_SYSTEM, narrative


def task_status(outcome: str, confidence: float | None, review_status: str) -> str:
    """The Task status from three `decisions` columns, and nothing else.

    Search filters on status in SQL with the same predicate over the same columns, so what a
    Task renders and what `Task?status=` finds can never disagree; keep the two in step:

    - `failed`: `outcome = 'BLOCK' AND confidence IS NULL`, a refused input. A refused input
      reaches no provider, so the primary question has no answer and no confidence; a policy
      BLOCK comes from a rule on an answered primary question (a test checks every shipped
      pack's BLOCK rules target the primary), so it has one and is `completed`.
    - `requested`: `review_status = 'pending'`, waiting in Mendari's review queue.
    - `completed`: everything else.
    """
    if refused(outcome, confidence):
        return "failed"
    if review_status == "pending":
        return "requested"
    return "completed"


def refused(outcome: str, confidence: float | None) -> bool:
    """Whether the input was refused before any provider ran (see `task_status`)."""
    return outcome == Outcome.BLOCK.value and confidence is None


def task_versions(item: Mapping[str, Any], base: str | None = None) -> list[dict[str, Any]]:
    """Every version of a decision's Task, version 1 first. `item` is a decision with all its
    events in chain order, as `AuditStore.item` (or `get` without a reader) returns it. Under the
    FHIR `base` (absolute, e.g. `https://example.org/fhir`), `relevantHistory` lists the
    Provenance of each version so far; without it there is nowhere to point, so none."""
    made = [event for event in item["events"] if event["event"] in audit_fhir.VERSION_EVENTS]
    if not made or made[0]["event"] != "decision.created":
        raise ValueError(f"decision {item['id']} has no decision.created event")
    return [_version(item, made[:number], base) for number in range(1, len(made) + 1)]


def current_task(item: Mapping[str, Any], base: str | None = None) -> dict[str, Any]:
    """The latest version of a decision's Task."""
    return task_versions(item, base)[-1]


def _version(
    item: Mapping[str, Any], made: Sequence[Mapping[str, Any]], base: str | None
) -> dict[str, Any]:
    """The Task as of the last of `made`, the version-making events so far."""
    record = item["record"]
    latest = made[-1]
    reviewed = next((e for e in made if e["event"] == "review.resolved"), None)
    review = None if reviewed is None else reviewed["payload"]
    observed = [e["payload"] for e in made if e["event"] == "outcome.recorded"]
    status = task_status(item["outcome"], item["confidence"], _review_status(item, review))
    pack = f"{item['pack_id']}@{item['pack_version']}"
    version = str(len(made))
    summary = (
        f"Decision {item['id']} by {pack}: {item['outcome']}, status {status}, version "
        f"{version} ({latest['event']}), recorded {latest['created_at']}."
    )
    period = {"start": item["created_at"]}
    if status != "requested":  # final: when the review resolved it, or at once
        period["end"] = (reviewed or made[0])["created_at"]
    task: dict[str, Any] = {
        "resourceType": "Task",
        "id": item["id"],
        "meta": {
            "versionId": version,
            "lastUpdated": latest["created_at"],
            "tag": [{"system": PACK_TAG_SYSTEM, "code": pack}],
        },
        "text": narrative(summary),
        "identifier": [{"system": DECISION_ID_SYSTEM, "value": item["id"]}],
        "status": status,
        "businessStatus": {
            "coding": [{"system": OUTCOME_SYSTEM, "code": item["outcome"]}],
            "text": item["outcome"].replace("_", " ").lower(),
        },
        "intent": "order",
        "priority": item["priority"],
        "code": {"text": f"Decide with {pack}"},
        "authoredOn": item["created_at"],
        "executionPeriod": period,
        "owner": {"display": str(review["reviewer"])} if review else audit_fhir.engine(item),
    }
    if record["reasons"] and not refused(item["outcome"], item["confidence"]):
        task["note"] = [{"text": reason} for reason in record["reasons"]]
    outputs = list(_outputs(item, review, observed[-1] if observed else None))
    if outputs:
        task["output"] = outputs
    if base is not None:
        task["relevantHistory"] = [
            {"reference": f"{base.rstrip('/')}/Provenance/{item['id']}-p{event['seq']}"}
            for event in made
        ]
    return task


def _review_status(item: Mapping[str, Any], review: Mapping[str, Any] | None) -> str:
    """The `review_status` column as it stood at a version: `resolved` from the review on, and
    before it `pending` for a decision that went to review, `not_required` for any other."""
    if review is not None:
        return "resolved"
    return "pending" if item["review_status"] in ("pending", "resolved") else "not_required"


def _outputs(
    item: Mapping[str, Any],
    review: Mapping[str, Any] | None,
    observed: Mapping[str, Any] | None,
) -> Iterator[dict[str, Any]]:
    """Recommendation and fallback from the record; the reviewer's label and whether it
    overrode the recommendation from the review; the latest observed label."""
    record = item["record"]
    system = davinci.question_system(item["pack_id"], record["primary"])
    if record.get("recommendation") is not None:
        yield _output("recommendation", {"valueCoding": _label(system, record["recommendation"])})
    if record.get("fallback"):
        yield _output("fallback", {"valueString": record["fallback"]})
    if review is not None:
        yield _output("final-label", {"valueCoding": _label(system, review["final_label"])})
        if review.get("human_override") is not None:
            yield _output("human-override", {"valueBoolean": bool(review["human_override"])})
    if observed is not None:
        yield _output("observed-label", {"valueCoding": _label(system, observed["final_label"])})


def _output(kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": {"text": kind}, **value}


def _label(system: str, code: str) -> dict[str, str]:
    return {"system": system, "code": str(code)}
