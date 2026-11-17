# SPDX-License-Identifier: Apache-2.0
"""An improvement dataset from reviewers' corrections (LP-20i, ADR-0016 Proposed).

One record per decision a reviewer resolved (`final_label` on its row, `resolve_review`): its
input, the final label of its primary question, and that label's provenance: the decision's id,
the reviewer (by id, never an address), when, whether the reviewer confirmed or overrode what
the system recommended, and the digest of the version that decided. A decision kept as a hash
only, or an encrypted input without a reader, is counted as skipped, as the ratings export
counts it (`evals/feedback.py`).

These are patients' inputs wherever the server holds real data. The dataset is kept in the
organization's pack store, sealed under the PHI policy, offered to a draft as proposed behaviour
tests that stay beside it (never written into a pack's files), and may be a job's dataset. It is
never downloadable: no route returns its cases, no export, registry or site report carries it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from mendari.core import tenancy
from mendari.core.audit import AuditStore

FORMAT = "mendari.corrections/1"


def _reviewer(value: Any) -> str | None:
    """A reviewer as the dataset names them: a person's id as it is (`user:…`); anything that
    could be an address as a digest of it."""
    if value is None:
        return None
    text = str(value)
    if text.startswith("user:") and "@" not in text:
        return text
    return "reviewer:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def corrections_dataset(
    store: AuditStore,
    *,
    org: str,
    pack_ids: Iterable[str],
    limit: int = 1000,
    reader: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """(records, skipped) of `org`'s decisions of these packs that a reviewer resolved, newest
    first, `limit` in all. Each read of a kept input is chained for `reader` (as every read of
    one is). Never another organization's: the read acts for `org`."""
    records: list[dict[str, Any]] = []
    skipped = 0
    with tenancy.acting_for(org):
        for pack_id in dict.fromkeys(pack_ids):
            for item in store.labelled(pack_id=pack_id, limit=limit, reader=reader):
                if item.get("final_label") is None:
                    continue  # an outcome alone is not a reviewer's correction
                record = item["record"]
                primary = record.get("primary")
                if record.get("input") is None or not isinstance(primary, str):
                    skipped += 1
                    continue
                pack = record.get("pack") or {}
                records.append(
                    {
                        "id": str(item["id"]),
                        "input": record["input"],
                        "labels": {primary: item["final_label"]},
                        "provenance": {
                            primary: {
                                "decision": str(item["id"]),
                                "reviewer": _reviewer(item.get("reviewer")),
                                "at": item.get("resolved_at"),
                                "overrode": bool(item.get("human_override")),
                                "digest": pack.get("digest"),
                            }
                        },
                    }
                )
                if len(records) >= limit:
                    return records, skipped
    return records, skipped


def jsonl(records: list[dict[str, Any]]) -> bytes:
    """The records as JSON Lines, as a dataset is kept."""
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records).encode("utf-8")
