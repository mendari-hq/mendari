# SPDX-License-Identifier: Apache-2.0
"""The one ranking of a leaderboard's configurations (`mendari.rank/1`, LP-10): the card, the
command line and the dashboard all list the rows in the order this writes, never their own.
A pack's own sets are public, in the repository and in the package, and a board can also hold a
site's own file (`mendari leaderboard --dataset PATH`), never a frozen hidden set: so since
LP-17g that order is shown as a listing order and no surface prints a rank, each table a
regression check, not a ranking. `rank` and `standing` stay in the rows and the API as data.

Only rows measured on the pack as it is (its digest) rank, against rows on the same records (the
dataset's name, hash and size; a row that did not record the hash is never on the same records as
one that did). Within those, critical cases come first and accuracy never alone:

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


Records = tuple[str, str, int]


def records(row: Mapping[str, Any]) -> Records:
    """What a row was measured on: the dataset's name, hash ("" when not recorded) and size."""
    return (str(row["dataset"]), str(row.get("dataset_sha256") or ""), int(row["n"]))


def about(group: Records, *, limited: bool, twin: bool) -> list[str]:
    """What a set of records is, in the words the card, the dashboard and the Hub all use:
    its size, whether it is the first cases of a file (a `--limit` run), whether its hash was
    recorded, and the hash's start when another set has the same name and size (`twin`)."""
    _, sha, n = group
    words = [f"n={n}"]
    if limited:
        words.append(f"the first {n} cases of the file")
    if not sha:
        words.append("dataset hash not recorded")
    elif twin:
        words.append(f"records sha256:{sha[:12]}…")
    return words


def measured_on(version: Any, digest: Any, current: str | None) -> str:
    """Why a row (or a benchmark) is not of the pack as it is, in words."""
    if not digest:
        return f"version {version}, its agent digest not recorded"
    if current is not None and version == current:
        return f"version {version} before its last change"
    return f"earlier version {version}"


def _bound(p: Any, side: int, missing: float) -> float:
    """A bound of a stored proportion, worked out again from its k and n: a stored bound is
    never trusted (a board can come from a registry upload)."""
    if not isinstance(p, Mapping) or not p.get("n"):
        return missing
    return stats.interval(int(p["k"]), int(p["n"]))[side]


def _key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    critical = [c for c in row.get("critical", []) if c["n"] >= MIN_POSITIVES]
    detection = min((stats.interval(c["detected"], c["n"])[0] for c in critical), default=0.0)
    cost = row.get("cost_per_decision_usd")
    return (
        sum(c["auto_missed"] for c in row.get("critical", [])),
        -detection,
        _bound(row.get("false_automation"), 1, 1.0),
        -_bound(row.get("accuracy_proportion"), 0, 0.0),
        cost if cost is not None else float("inf"),
        str(row["config"]),
    )


# What a row is ordered by before cost and its name, in order: the names of `measures`' values.
MEASURES = ("critical_automated", "critical_detection", "false_automation", "accuracy")


def measures(row: Mapping[str, Any]) -> tuple[float, ...]:
    """The four values `row` is ordered by before cost and name, each smaller for the row that
    comes first (`MEASURES` names them): the critical positives it automated, then the lower
    bound of its worst critical label's detection, the upper bound of its false automation and
    the lower bound of its accuracy, the two that should be high negated. Two rows with the
    same four are told apart by nothing a count says."""
    automated, detection, false_automation, accuracy = _key(row)[:4]
    return (float(automated), float(detection), float(false_automation), float(accuracy))


def worst_critical(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The critical label whose detection decides `measures`' second value: of those with at
    least `MIN_POSITIVES` positives, the one with the lowest lower bound (the first of equals);
    None when the row has none."""
    enough = [c for c in row.get("critical", []) if c["n"] >= MIN_POSITIVES]
    return min(enough, key=lambda c: stats.interval(c["detected"], c["n"])[0], default=None)


def rank(rows: Sequence[Mapping[str, Any]], digest: str | None) -> list[dict[str, Any]]:
    """`rows` with `standing` and `rank` (1, 2, ... within their records; None unranked), in the
    order they are shown (`grouped`)."""
    return [row for _, members in grouped(rows, digest) for row in members]


def grouped(
    rows: Sequence[Mapping[str, Any]], digest: str | None
) -> list[tuple[Records, list[dict[str, Any]]]]:
    """The rows by the records they were measured on (by dataset name, then the largest size,
    then the recorded hashes), each group's ranked rows first, then its unranked by name."""
    groups: dict[Records, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(records(row), []).append(dict(row))
    out: list[tuple[Records, list[dict[str, Any]]]] = []
    for group in sorted(groups, key=lambda g: (g[0], -g[2], not g[1], g[1])):
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
        out.append((group, ranked + sorted(unranked + others, key=lambda r: str(r["config"]))))
    return out
