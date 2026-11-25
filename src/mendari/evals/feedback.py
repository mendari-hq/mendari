# SPDX-License-Identifier: Apache-2.0
"""Turn what people said about decisions into datasets.

Two sources, each exported as agent-dataset records (JSON Lines), one per decision:

- **Ratings** (`feedback_dataset`): thumbs-down feedback. Where the people who rated a decision
  gave the right answer (and agree), it is already in `labels`, so the record is a case ready
  to evaluate the agent on; otherwise a clinician or engineer fills in `labels`.
- **Reviews** (`reviewed_dataset`): decisions a person resolved in the review queue, and
  decisions a downstream system recorded an outcome for. `labels` holds the primary question's
  label: what really happened when an outcome says, else what the reviewer chose
  (`core.audit.truth_label`, as observed accuracy counts it).

A file then is evaluation data (`mendari eval --dataset`), and a confirmed miss becomes a
behaviour test a person writes for the agent. Nothing here changes an agent; improvements stay
reviewed and versioned.

Every record says it was exported (`attributes.exported_by`, `core/site_cases.py`). It is a
site's case wherever the file goes, and may be patient text: no archive is made of an agent that
holds one, as the file an export wrote or as records copied into the agent's datasets.

Reviewed cases are not a sample of what an agent sees. They are the decisions it sent to a person
(the ones it was unsure of, or a rule held), labelled by someone who saw its suggestion. They
are cases to learn from and to compare two models on; an agent's accuracy on all its decisions is
not theirs.
"""

from __future__ import annotations

from typing import Any

from mendari.core.audit import AuditStore, Rating, truth_label
from mendari.core.site_cases import EXPORT, EXPORTED_BY


def feedback_dataset(
    store: AuditStore,
    *,
    agent_id: str,
    rating: Rating = "down",
    limit: int = 1000,
    reader: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """(records, skipped): one record per rated decision, from each person's current rating.
    Decisions whose input was stored only as a hash cannot be exported and are counted in
    `skipped`, and so are encrypted inputs without a `reader`. A question is labelled only when
    every rating that answers it gives the same label; a disagreement stays visible in the
    record's `feedback` list for a person to settle."""
    by_decision: dict[str, dict[str, Any]] = {}
    skipped: set[str] = set()
    for item in store.feedback(agent_id=agent_id, rating=rating, limit=limit, reader=reader):
        record = item["record"]
        if record.get("input") is None:
            skipped.add(item["decision_id"])
            continue
        entry = by_decision.setdefault(
            item["decision_id"],
            {
                "id": f"feedback-{item['decision_id']}",
                "input": record["input"],
                "labels": {},
                "system": _system(record),
                "feedback": [],
                "attributes": _attributes("feedback", item["decision_id"]),
            },
        )
        entry["feedback"].append(
            {
                "rating": item["rating"],
                "note": item["note"],
                "reporter": item["reporter"],
                "created_at": item["created_at"],
                "expected": item["expected"],
            }
        )
    for entry in by_decision.values():
        entry["labels"] = _agreed_labels(entry["feedback"])
    return list(by_decision.values()), len(skipped)


def reviewed_dataset(
    store: AuditStore,
    *,
    agent_id: str,
    limit: int = 1000,
    reader: str | None = None,
    only_overrides: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """(records, skipped): one record per decision of the agent that a reviewer resolved or a
    downstream system recorded an outcome for (`AuditStore.labelled`), the newest `limit`.
    When `only_overrides` is true, keeps only cases where a clinician overrode the agent's
    recommendation (for closed-loop regression testing and recalibration).
    `labels` holds its primary question's label (`truth_label`: the outcome when there is one,
    else the reviewer's), `attributes.source` says which (`outcome` or `review`), and `review`
    and `outcome` what each said. A decision whose input was stored only as a hash is counted in
    `skipped`, and so is an encrypted input without a `reader`. A record names no person: not
    the reviewer, not who recorded the outcome, and no note."""
    records = []
    skipped = 0
    for item in store.labelled(agent_id=agent_id, limit=limit, reader=reader):
        if only_overrides and not item.get("human_override"):
            continue
        record = item["record"]
        if record.get("input") is None:
            skipped += 1
            continue
        entry: dict[str, Any] = {
            "id": f"reviewed-{item['id']}",
            "input": record["input"],
            "labels": {record["primary"]: truth_label(item)},
            "system": _system(record),
        }
        if item["final_label"] is not None:
            entry["review"] = {
                "label": item["final_label"],
                "overridden": item["human_override"],
                "resolved_at": item["resolved_at"],
            }
        if item["observed_label"] is not None:
            entry["outcome"] = {"label": item["observed_label"]}
        entry["attributes"] = _attributes(
            "outcome" if item["observed_label"] is not None else "review", item["id"]
        )
        records.append(entry)
    return records, skipped


def _attributes(source: str, decision_id: str) -> dict[str, str]:
    """Where a record came from, the decision it was, and that an export wrote it: the mark
    that keeps a site's cases out of an agent's archive (`core/site_cases.py`)."""
    return {"source": source, "decision_id": decision_id, EXPORTED_BY: EXPORT}


def _system(record: dict[str, Any]) -> dict[str, Any]:
    """What the agent decided, beside what people said of it."""
    return {
        "outcome": record["outcome"],
        "recommendation": record["recommendation"],
        "priority": record["priority"],
        "answers": {q: a["label"] for q, a in record["answers"].items()},
        "agent_version": record["agent"]["version"],
    }


def _agreed_labels(feedback: list[dict[str, Any]]) -> dict[str, str]:
    given: dict[str, set[str]] = {}
    for item in feedback:
        if item["expected"]:
            given.setdefault(item["expected"]["question"], set()).add(item["expected"]["label"])
    return {question: labels.pop() for question, labels in given.items() if len(labels) == 1}
