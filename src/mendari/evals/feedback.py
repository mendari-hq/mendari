# SPDX-License-Identifier: Apache-2.0
"""Turn thumbs-down feedback into a dataset.

Each exported line is a pack-dataset record. Where the people who rated a decision gave the right
answer (and agree), it is already in `labels`, so the record is a ready-made test case; otherwise
a clinician or engineer fills in `labels`. The file then becomes evaluation data or behaviour tests
for the next pack version. Nothing here changes a pack; improvements stay reviewed and versioned.
"""

from __future__ import annotations

from typing import Any

from mendari.core.audit import AuditStore, Rating


def feedback_dataset(
    store: AuditStore,
    *,
    pack_id: str,
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
    for item in store.feedback(pack_id=pack_id, rating=rating, limit=limit, reader=reader):
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
                "system": {
                    "outcome": record["outcome"],
                    "recommendation": record["recommendation"],
                    "priority": record["priority"],
                    "answers": {q: a["label"] for q, a in record["answers"].items()},
                    "pack_version": record["pack"]["version"],
                },
                "feedback": [],
                "attributes": {"source": "feedback", "decision_id": item["decision_id"]},
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


def _agreed_labels(feedback: list[dict[str, Any]]) -> dict[str, str]:
    given: dict[str, set[str]] = {}
    for item in feedback:
        if item["expected"]:
            given.setdefault(item["expected"]["question"], set()).add(item["expected"]["label"])
    return {question: labels.pop() for question, labels in given.items() if len(labels) == 1}
