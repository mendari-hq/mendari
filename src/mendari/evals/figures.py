# SPDX-License-Identifier: Apache-2.0
"""Figures as served (`mendari.figures/1`, LP-10): every share of cases the dashboard and the Hub
show, worked out here once, as the card and the command line work them out, so a browser only
formats them (`render.shown`, ported in `dashboard/ui.js` and `hub/lib/figures.ts` and checked
against tests/fixtures/shown.json).

A figure is `{k, n, low, high}`: k of n with its exact 95% interval (Clopper-Pearson, from k and
n, unrounded; `low` and `high` are null when n is 0, "no cases"). A figure is null when what it
is of is not known ("n/a"): a rate stored without its count, or a count that is not a whole
number of cases. Figures are served beside a report, a leaderboard or a registry version and
never stored: they are recomputed from what is stored on every read.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from mendari.evals import ranking, stats
from mendari.evals.result import (
    MIN_POSITIVES,
    false_automation,
    normalize,
    subgroup_labelled,
    sweep,
)

FORMAT = "mendari.figures/1"
MAX_N = 10**9  # the size of anything a figure is of; a larger one is not a dataset
OUTCOMES = ("AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK")
_SLACK = 1e-6  # how far rate x n may be from a whole k, from a rate's float rounding

Figure = dict[str, Any]
_MALFORMED = (AttributeError, IndexError, KeyError, TypeError, ValueError, ArithmeticError)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_N


def of(k: Any, n: Any) -> Figure:
    """k of n with its exact interval. Refuses anything that is not a whole k of a whole n."""
    if not (_count(k) and _count(n)) or k > n:
        raise ValueError(f"not k of n: {k!r} of {n!r}")
    if n == 0:
        return {"k": 0, "n": 0, "low": None, "high": None}
    low, high = stats.interval(k, n)
    return {"k": k, "n": n, "low": low, "high": high}


def maybe(k: Any, n: Any) -> Figure | None:
    """`of`, or null (n/a) when the counts are not a whole k of a whole n."""
    try:
        return of(k, n)
    except ValueError:
        return None


def counted(rate: Any, n: Any) -> Figure | None:
    """k of n for a rate stated over n cases, as `result.counted` does, or null when that is not
    known: n missing or not a whole count, the rate missing over some cases, or rate x n not a
    whole number of cases (a rate that is not a share of those n)."""
    if not _count(n):
        return None
    if rate is None:
        return of(0, 0) if n == 0 else None
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not math.isfinite(rate):
        return None
    k = round(rate * n)
    if not 0.0 <= rate <= 1.0 or abs(rate * n - k) > _SLACK:
        return None
    return of(k, n)


def _stored(p: Any) -> Figure | None:
    """A stored proportion's k and n, its interval worked out again (never the stored one)."""
    return maybe(p.get("k"), p.get("n")) if isinstance(p, Mapping) else None


def critical(item: Mapping[str, Any]) -> dict[str, Any]:
    n = item["n"]
    return {
        "question": item["question"],
        "label": item["label"],
        "detected": of(item["detected"], n),
        "auto_missed": item["auto_missed"],
        "insufficient_evidence": n < MIN_POSITIVES,
    }


# -- an evaluation report -----------------------------------------------------------------------


def report(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every share of cases in a report (either format), each over the cases the card and the
    command line show it over. Raises on a report that is not one."""
    r = normalize(data)
    primary = r["primary"]
    q = r["questions"].get(primary)
    d = r["decisions"]
    counts = d["outcome_counts"]
    total = sum(counts.values())
    automation = d["automation"]
    labelled_wrong = false_automation(r)
    out: dict[str, Any] = {
        "format": FORMAT,
        "min_positives": MIN_POSITIVES,
        "accuracy": counted(q["accuracy"], q["n"]) if q else None,
        "outcomes": {o: of(counts.get(o, 0), total) for o in OUTCOMES},
        "automated_correct": counted(automation["accuracy"], automation["n"]),
        "false_automation": None
        if labelled_wrong is None
        else of(labelled_wrong.k, labelled_wrong.n),
        "expected_override": counted(d["review"]["expected_override_rate"], d["review"].get("n")),
        "critical": [critical(item) for item in d["critical"]],
        "per_label": {},
        "confusion": {},
        "calibration": [],
        "sweep": [],
        "subgroups": {},
        "comparison": None,
    }
    if q:
        confusion = q["confusion"]
        for label, m in q["per_label"].items():
            predicted = sum(row.get(label, 0) for row in confusion.values())
            out["per_label"][label] = {
                "precision": counted(m["precision"], predicted),
                "recall": counted(m["recall"], m["support"]),
            }
        for truth, row in confusion.items():
            whole = sum(row.values())
            out["confusion"][truth] = {answer: of(c, whole) for answer, c in row.items()}
        out["calibration"] = [counted(b["accuracy"], b["n"]) for b in q["calibration"]["bins"]]
        out["sweep"] = [
            {
                "automated": maybe(row["chosen"], row["scored"]),
                "correct": None
                if row["chosen"] is None
                else maybe(row["chosen"] - row["errors"], row["chosen"]),
            }
            for row in sweep(r)
        ]
    for attribute, values in r["subgroups"].items():
        out["subgroups"][attribute] = {
            value: {
                "accuracy": counted(m["accuracy"], subgroup_labelled(r, m)),
                "automation": counted(m["auto_rate"], m["n"]),
                "abstention": counted(m["abstention_rate"], m["n"]),
            }
            for value, m in values.items()
        }
    if isinstance(r.get("comparison"), Mapping):
        out["comparison"] = comparison(r["comparison"])
    return out


def comparison(c: Mapping[str, Any]) -> dict[str, Any] | None:
    """A local validation's figures: each side's accuracy and each finding's two figures. A
    finding stored before its counts were (LP-10b) shows its benchmark side as n/a; its local
    side is over the cases the finding is about where that is exact."""
    if c.get("status") == "unknown":
        return None
    base, local = c["baseline"], c["local"]
    findings: list[dict[str, Any] | None] = []
    for f in c["findings"]:
        kind = f["kind"]
        if kind == "critical":
            findings.append(
                {
                    "automated": of(f["local"], f["n"]),
                    "insufficient_evidence": f["n"] < MIN_POSITIVES,
                }
            )
        elif kind == "calibration":
            findings.append(None)  # an ECE, not a share of cases
        else:
            exact = kind in ("label_recall", "false_automation")  # `n` is their local count
            local_n = f.get("local_n", f["n"] if exact else None)
            findings.append(
                {
                    "baseline": counted(f["baseline"], f.get("baseline_n")),
                    "local": counted(f["local"], local_n),
                    "overall": f.get("baseline_scope") == "overall",
                }
            )
    return {
        "baseline": counted(base["accuracy"], base.get("n")),
        "local": counted(local["accuracy"], local.get("n")),
        "findings": findings,
    }


# -- a leaderboard ------------------------------------------------------------------------------


def board(data: Mapping[str, Any], digest: str | None, version: str | None) -> dict[str, Any]:
    """A leaderboard as served: its rows in `ranking.grouped`'s order, each with its rank,
    standing and the reason in words, the set of records it is in (`group`) and its figures;
    the sets of records with their captions; and the ranking method. Raises on a board whose
    rows are not leaderboard rows (`_check`)."""
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        raise TypeError("rows")
    for row in rows:
        _check(row)
    groups = ranking.grouped(rows, digest)
    sizes = [(name, n) for (name, _, n), _ in groups]
    served_groups, served_rows = [], []
    for index, (group, members) in enumerate(groups):
        name, sha, n = group
        twin = sizes.count((name, n)) > 1
        about = ranking.about(group, limited=any(m.get("limit") for m in members), twin=twin)
        served_groups.append(
            {
                "dataset": name,
                "sha256": sha or None,
                "n": n,
                "about": about,
                "label": f"{name} n={n}" + (f" ({sha[:8]})" if twin and sha else ""),
            }
        )
        for row in members:
            served_rows.append(
                {**row, "group": index, **_standing(row, version), "figures": _row(row)}
            )
    return {
        **data,
        "ranking": {"method": ranking.METHOD, "min_positives": MIN_POSITIVES},
        "figures": FORMAT,
        "groups": served_groups,
        "rows": served_rows,
    }


def _standing(row: Mapping[str, Any], version: str | None) -> dict[str, Any]:
    if row["rank"] is not None:
        return {"standing_text": None}
    if row["standing"] == ranking.EARLIER:
        why = ranking.measured_on(row.get("pack_version"), row.get("pack_digest"), version)
        return {"standing_text": why}
    return {"standing_text": row["standing"]}


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": _stored(row.get("accuracy_proportion")),
        "false_automation": _stored(row.get("false_automation")),
        "automation": counted(row.get("auto_rate"), row["n"]),
        "critical": [critical(c) for c in row["critical"] if c["n"]],
        "questions": {
            key: counted(m.get("accuracy"), m.get("n"))
            for key, m in (row.get("questions") or {}).items()
        },
    }


def _check(row: Any) -> None:
    """A leaderboard row's shape, as the ranking and the figures read it."""
    if not isinstance(row, Mapping):
        raise TypeError("a row is not an object")
    if not isinstance(row.get("config"), str) or not isinstance(row.get("dataset"), str):
        raise TypeError("a row's config and dataset are names")
    if not _count(row.get("n")):
        raise ValueError("a row's n is not a count")
    if row.get("dataset_sha256") is not None and not isinstance(row["dataset_sha256"], str):
        raise TypeError("a row's dataset hash is a string")
    items = row.get("critical")
    if not isinstance(items, list):
        raise TypeError("a row's critical labels are a list")
    for c in items:
        if not isinstance(c, Mapping) or not all(
            isinstance(c.get(key), str) for key in ("question", "label")
        ):
            raise TypeError("a critical label is named")
        if not (_count(c.get("n")) and _count(c.get("detected")) and _count(c.get("auto_missed"))):
            raise ValueError("a critical label's counts are not counts")
        if c["detected"] > c["n"] or c["auto_missed"] > c["n"]:
            raise ValueError("a critical label counts more cases than it has")
    for key in ("accuracy_proportion", "false_automation"):
        p = row.get(key)
        if p is not None and _stored(p) is None:
            raise ValueError(f"a row's {key} is not k of n")
    cost = row.get("cost_per_decision_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float)):
        raise TypeError("a row's cost is a number")
    questions = row.get("questions")
    if questions is not None and not (
        isinstance(questions, Mapping) and all(isinstance(m, Mapping) for m in questions.values())
    ):
        raise TypeError("a row's questions are objects")


# -- a registry version -------------------------------------------------------------------------


def registry(version: Mapping[str, Any], *, with_leaderboard: bool = True) -> dict[str, Any]:
    """A published version's figures, worked out on read from what the registry stored (so a
    version published before needs no migration): its benchmark accuracy and (unless left out,
    as a search does) its leaderboard ranked against its own digest. Never raises: what is not
    a figure is null."""
    benchmark = version.get("benchmark")
    accuracy = None
    if isinstance(benchmark, Mapping):
        accuracy = counted(benchmark.get("accuracy"), benchmark.get("n"))
    leaderboard = None
    stored = version.get("leaderboard") if with_leaderboard else None
    if isinstance(stored, Mapping):
        try:
            leaderboard = board(stored, version.get("digest"), version.get("version"))
        except _MALFORMED:
            leaderboard = None
    return {"format": FORMAT, "benchmark": accuracy, "leaderboard": leaderboard}


# -- the overview's periods ---------------------------------------------------------------------

REVIEWS = ("reviewed", "overridden", "labelled", "agreed")


def periods(daily: Sequence[Mapping[str, Any]], today: date) -> dict[str, Any]:
    """The overview's windows, each with the window before it: the last 7 and 30 days (today
    included, local days) and all time. A window's figures: automated of its decisions,
    overridden of reviewed, agreed of labelled, and each outcome of its decisions."""

    def window(start: date | None, end: date | None) -> dict[str, Any]:
        sums = dict.fromkeys((*OUTCOMES, *REVIEWS), 0)
        for day in daily:
            when = date.fromisoformat(day["date"])
            if (start and when < start) or (end and when > end):
                continue
            for key in sums:
                sums[key] += int(day.get(key) or 0)
        decisions = sum(sums[o] for o in OUTCOMES)
        return {
            **sums,
            "decisions": decisions,
            "figures": {
                "automated": of(sums["AUTO"], decisions),
                "overrides": of(sums["overridden"], sums["reviewed"]),
                "accuracy": of(sums["agreed"], sums["labelled"]),
                "outcomes": {o: of(sums[o], decisions) for o in OUTCOMES},
            },
        }

    out: dict[str, Any] = {}
    for key, days in (("7", 7), ("30", 30), ("all", None)):
        if days is None:
            out[key] = {"days": None, "from": None, "now": window(None, None), "before": None}
            continue
        start = today - timedelta(days=days - 1)
        before = window(start - timedelta(days=days), start - timedelta(days=1))
        out[key] = {
            "days": days,
            "from": start.isoformat(),
            "now": window(start, None),
            "before": before,
        }
    return out


def guarded(make: Callable[[], Any]) -> Any:
    """What `make` gives, or null when what it reads is malformed: one odd stored report shows
    its figures as n/a and never fails a page."""
    try:
        return make()
    except _MALFORMED:
        return None
