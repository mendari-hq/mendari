"""Turn thumbs-down feedback into a dataset to label.

Each exported line is a pack-dataset record whose `labels` are empty: a clinician or engineer
fills in the correct answers, and the file then becomes evaluation data or behaviour tests for
the next pack version. Nothing here changes a pack; improvements stay reviewed and versioned.
"""

from __future__ import annotations

from typing import Any

from openhealth.core.audit import AuditStore, Rating


def feedback_dataset(
    store: AuditStore, *, pack_id: str, rating: Rating = "down", limit: int = 1000
) -> tuple[list[dict[str, Any]], int]:
    """(records, skipped): one record per rated decision; decisions whose input was stored
    only as a hash cannot be exported and are counted in `skipped`."""
    by_decision: dict[str, dict[str, Any]] = {}
    skipped: set[str] = set()
    for item in store.feedback(pack_id=pack_id, rating=rating, limit=limit):
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
            }
        )
    return list(by_decision.values()), len(skipped)
