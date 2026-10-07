# SPDX-License-Identifier: Apache-2.0
"""The one ranking of a leaderboard's configurations (`mendari.rank/1`, LP-10): the card, the
command line and the dashboard all show what this writes into the rows, never their own order.

Only rows measured on the pack as it is (its digest) rank, against rows on the same records (the
dataset's hash and size). Within those, critical cases come first and accuracy never alone:

1. fewer critical positives automated;
2. a higher lower bound of the worst critical label's detection, over the critical labels with
   at least 100 positives;
3. a lower upper bound of false automation (over the labelled cases);
4. a higher lower bound of accuracy;
5. a lower cost per decision; then the configuration's name, so the order is total.

A row measured on another digest is "earlier version"; a group whose critical labels all have
fewer than 100 positives is "insufficient evidence"; neither takes a rank.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mendari.evals import stats
from mendari.evals.result import MIN_POSITIVES

METHOD = "mendari.rank/1"
RANKED, EARLIER, INSUFFICIENT = "ranked", "earlier version", "insufficient evidence"


def _group(row: Mapping[str, Any]) -> tuple[str, int]:
    return (str(row.get("dataset_sha256") or row["dataset"]), int(row["n"]))


def _key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    critical = [c for c in row.get("critical", []) if c["n"] >= MIN_POSITIVES]
    detection = min(
        (stats.clopper_pearson(c["detected"], c["n"])[0] for c in critical), default=0.0
    )
    false_automation = row.get("false_automation") or {}
    accuracy = row.get("accuracy_proportion") or {}
    cost = row.get("cost_per_decision_usd")
    return (
        sum(c["auto_missed"] for c in row.get("critical", [])),
        -detection,
        false_automation.get("high") if false_automation.get("high") is not None else 1.0,
        -(accuracy.get("low") or 0.0),
        cost if cost is not None else float("inf"),
        str(row["config"]),
    )


def rank(rows: Sequence[Mapping[str, Any]], digest: str | None) -> list[dict[str, Any]]:
    """`rows` with `standing` and `rank` (1, 2, ... within their dataset; None unranked), in the
    order they are shown: each dataset's ranked rows first, then the unranked by name."""
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group(row), []).append(dict(row))
    out: list[dict[str, Any]] = []
    for group in sorted(groups):
        members = groups[group]
        on = [digest is not None and r.get("pack_digest") == digest for r in members]
        current = [r for r, ok in zip(members, on, strict=True) if ok]
        others = [r for r, ok in zip(members, on, strict=True) if not ok]
        enough = any(c["n"] >= MIN_POSITIVES for r in current for c in r.get("critical", []))
        ranked = sorted(current, key=_key) if enough else []
        for position, row in enumerate(ranked, start=1):
            row.update(standing=RANKED, rank=position)
        unranked = [] if enough else current
        for row in unranked:
            row.update(standing=INSUFFICIENT, rank=None)
        for row in others:
            row.update(standing=EARLIER, rank=None)
        out += ranked + sorted(unranked + others, key=lambda r: str(r["config"]))
    return out
