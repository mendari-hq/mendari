# SPDX-License-Identifier: Apache-2.0
"""Figures as served (`mendari.figures/1`, LP-10): every share of cases the dashboard and the Hub
show, worked out here once, as the card and the command line work them out, so a browser only
formats them: a port of `render.shown`, checked against tests/fixtures/shown.json.

A figure is `{k, n, low, high}`: k of n with its exact 95% interval (Clopper-Pearson, from k and
n, unrounded; `low` and `high` are null when n is 0, "no cases"). A figure is null when what it
is of is not known ("n/a"): a rate stored without its count, or a count that is not a whole
number of cases. Figures are served beside a report, a leaderboard or a registry version and
never stored: they are recomputed from what is stored on every read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from mendari.evals import ranking, result, stats
from mendari.evals.result import MAX_N, MIN_POSITIVES, false_automation

FORMAT = "mendari.figures/1"
OUTCOMES = ("AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK")

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
    """k of n for a rate stated over n cases, by the card's own rule (`result.counted`): null
    when that is not known (n missing or not a whole count, or the rate not a share of those n
    cases), so the card and the served figures never disagree."""
    p = result.counted(rate, n)
    return None if p is None else of(p.k, p.n)


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


def report(r: Mapping[str, Any]) -> dict[str, Any]:
    """Every share of cases in a report, each over the cases the card and the command line show
    it over. Raises on a report that is not one."""
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
                "correct": maybe(row["chosen"] - row["errors"], row["chosen"]),
            }
            for row in r["threshold_sweep"]
        ]
    for attribute, values in r["subgroups"].items():
        out["subgroups"][attribute] = {
            value: {
                "accuracy": counted(m["accuracy"], m["labelled"]),
                "automation": counted(m["auto_rate"], m["n"]),
                "abstention": counted(m["abstention_rate"], m["n"]),
            }
            for value, m in values.items()
        }
    if isinstance(r.get("comparison"), Mapping):
        out["comparison"] = comparison(r["comparison"])
    return out


SITE_SWEEP = (5, 6, 7, 8, 9)  # deciles: the sweep's thresholds a site report's bins give


def site(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every figure the registry shows of one site report (`mendari.site-report/1`, LP-22c),
    computed from its counts: none is a rate the site sent. A figure over a suppressed (null)
    count is null ("n/a").

    - per question: accuracy (right of n), and each calibration decile as right of its cases;
    - outcomes: each outcome's share of the dataset;
    - false automation: automated with a wrong recommendation, of the primary question's n;
    - per critical label: detected of its n, and its cases automated (auto_missed of n);
    - the threshold sweep at 0.5, 0.6, 0.7, 0.8 and 0.9 from the primary's deciles: the cases
      at or above the threshold of every answered case, and how many of those were right. The
      finer thresholds of a full report (0.85, 0.95, 0.97, 0.99) stay with the site: their
      bands would let a suppressed decile be worked out by subtraction.
    """
    n = data["dataset"]["n"]
    questions = {}
    for name, question in data["questions"].items():
        questions[name] = {
            "accuracy": of(question["right"], question["n"]),
            "calibration": [
                {
                    "bin": b["bin"],
                    "accuracy": None
                    if b["right"] is None or b["wrong"] is None
                    else of(b["right"], b["right"] + b["wrong"]),
                }
                for b in question["calibration"]
            ],
        }
    decisions = data["decisions"]
    primary = data["questions"].get(data["primary"])
    sweep: list[dict[str, Any]] = []
    if primary is not None:
        bins = primary["calibration"]
        known = all(b["right"] is not None and b["wrong"] is not None for b in bins)
        answered = sum(b["right"] + b["wrong"] for b in bins) if known else None
        for decile in SITE_SWEEP:
            above = bins[decile:]
            if answered is None:
                sweep.append({"threshold_bp": decile * 1000, "coverage": None, "accuracy": None})
                continue
            right = sum(b["right"] for b in above)
            chosen = right + sum(b["wrong"] for b in above)
            sweep.append(
                {
                    "threshold_bp": decile * 1000,
                    "coverage": of(chosen, answered),
                    "accuracy": of(right, chosen),
                }
            )
    return {
        "format": FORMAT,
        "n": n,
        "primary": data["primary"],
        "questions": questions,
        "outcomes": {key: maybe(count, n) for key, count in decisions["outcomes"].items()},
        "false_automation": of(decisions["false_automation"], primary["n"] if primary else 0),
        "critical": [
            {
                "question": item["question"],
                "label": item["label"],
                "detected": of(item["detected"], item["n"]),
                "automated": of(item["auto_missed"], item["n"]),
                "insufficient_evidence": item["n"] < MIN_POSITIVES,
            }
            for item in decisions["critical"]
        ],
        "sweep": sweep,
    }


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
    sizes = Counter((name, n) for (name, _, n), _ in groups)
    served_groups, served_rows = [], []
    for index, (group, members) in enumerate(groups):
        name, sha, n = group
        twin = sizes[(name, n)] > 1
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
        why = ranking.measured_on(row.get("agent_version"), row.get("agent_digest"), version)
        return {"standing_text": why}
    return {"standing_text": row["standing"]}


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "accuracy": _stored(row.get("accuracy_proportion")),
        "false_automation": _stored(row.get("false_automation")),
        "automation": counted(row.get("auto_rate"), row["n"]),
        "critical": [critical(c) for c in row["critical"] if c["n"]],
        # Each critical figure says its label when the agent has several, as the card does
        # (`leaderboard.critical_text`), whether or not they all have positives here.
        "critical_named": len(row["critical"]) > 1,
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


MAX_BOARD_FIGURES = 5000  # what a registry works out for one uploaded leaderboard, at most


def registry(version: Mapping[str, Any], *, with_leaderboard: bool = True) -> dict[str, Any]:
    """A published version's figures, worked out on read from what the registry stored (so a
    version published before needs no migration): its benchmark accuracy and (unless left out,
    as a search does) its leaderboard ranked against its own digest. Never raises: what is not
    a figure is null, and so is a leaderboard with more than MAX_BOARD_FIGURES figures (the
    shipped ones have a few hundred), so one upload cannot make every read slow."""
    benchmark = version.get("benchmark")
    accuracy = None
    if isinstance(benchmark, Mapping):
        accuracy = guarded(lambda: counted(benchmark.get("accuracy"), benchmark.get("n")))
    leaderboard = None
    stored = version.get("leaderboard") if with_leaderboard else None
    if isinstance(stored, Mapping) and _figure_count(stored) <= MAX_BOARD_FIGURES:
        leaderboard = guarded(lambda: board(stored, version.get("digest"), version.get("version")))
    return {"format": FORMAT, "benchmark": accuracy, "leaderboard": leaderboard}


def _figure_count(stored: Mapping[str, Any]) -> int:
    """How many figures a board would be worked out into (each row's own three, its critical
    labels and its other questions), counted without working any out; past the cap, any."""
    rows = stored.get("rows")
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if isinstance(row, Mapping):
            total += 3 + _length(row.get("critical")) + _length(row.get("questions"))
        if total > MAX_BOARD_FIGURES:
            break
    return total


def _length(value: Any) -> int:
    return len(value) if isinstance(value, list | Mapping) else 0


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
