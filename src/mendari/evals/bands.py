# SPDX-License-Identifier: Apache-2.0
"""Band ranking (LP-17j): how the arms graded on one frozen hidden set are ordered, under the
grading method 1.0 (`docs/grading/method-1.0.md`, sections 5 and 7). Ranking is apart from the
grade (`evals/grade.py`). It is given only on a frozen hidden set, over the declared arms graded
in one read (one configuration per arm, their ledgers sharing one case key), and never on a
public set: there `ranking` is off, "public set: regression check, not a ranking". While fewer
than two arms are graded, it is off: "one arm graded".

- **Unranked:** an arm whose safety attestations (no critical case automated, each critical
  label's recall floor) are not all met, for want of evidence or not, is listed unranked with
  each reason.
- **The band tuple**, fixed by the method before any data is seen, each element a band position
  (0 is the best band): the worst critical label's simultaneous lower bound (banded at
  `method.RECALL_BANDS`, higher is better), false automation's 95% upper bound
  (`method.FALSE_AUTOMATION_BANDS`, lower is better), accuracy's 95% lower bound (in steps of
  `method.ACCURACY_BAND_STEP`), Brier's 95% upper bound (in steps of `method.BRIER_BAND_STEP`)
  and cost per 1,000 decisions (its estimate, banded at `method.COST_BANDS_USD_PER_1000`: 0 with
  no metered call, and an unknown cost in the last band). With edges listed from best to worst,
  a value's band is the first edge it reaches, or one past the last. In steps, it is the largest
  multiple at or below the value (accuracy) or the smallest at or above it (Brier). A value is
  read as the decimal it is written as (0.95 is 19/20, never the binary float just under it),
  and a proportion's bound is worked out from its k and n, never read from its stored figure. A
  figure that is not estimable sits after every band. ECE, latency and subgroups never enter the
  tuple.
- **The order:** ranked arms are ordered on their tuples, lexicographically. Equal tuples share
  a competition rank (1, 2, 2, 4) and are listed by configuration (`slot=variant`, slots
  sorted). The tests below never reorder it.
- **The Holm family**, per pack and set: every pair of ranked arms, by each critical label's
  detection (exact McNemar on that label's distinct positive groups, a group detected only when
  every positive case in it is), false automation and accuracy (exact McNemar on the cases
  labelled for the primary question) and Brier (the paired bootstrap's two-sided p-value, 2,000
  resamples, seed 0, on the cases both arms scored), at `method.ALPHA`. The output names the
  family and its size. `paired.compare` keeps its own family, and `mendari.compare/1` does not
  change.
- **Rank intervals**, from the Holm decisions: arm j is significantly ahead of arm i when j's
  tuple is the better one and the family's test on the first criterion where the two differ
  rejects, in j's favour. On the first criterion that test is on the critical label that sets
  the lower arm's band (its worst label, the first in the pack's order on a tie). Cost has no
  test, so arms that differ first on cost are never significantly apart. An arm's interval runs
  from 1 plus the arms significantly ahead of it to the ranked arms less the arms significantly
  behind it, so it always holds the arm's rank. Nothing is resampled for it.

While the method is not in effect, a ranking is marked draft. A re-run from the same ledgers
and seeds gives the same bytes. Nothing here reads a clock, a setting or the network, and
nothing names a case.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

from mendari.errors import MendariError
from mendari.evals import arms, grade, method, metrics, stats
from mendari.evals.canonical import normalised_input
from mendari.evals.ledger import Ledger
from mendari.evals.result import PREDICTED, PROBABILITIES, TRUTH

OFF, ON = "off", "on"
ONE_ARM = "one arm graded"
DETECTION = "detection"
FALSE_AUTOMATION = "false_automation"
ACCURACY = "accuracy"
BRIER = "brier"
COST = "cost_per_1000"
CRITERIA = (DETECTION, FALSE_AUTOMATION, ACCURACY, BRIER, COST)  # the band tuple, in order
FAMILY = (
    "every pair of ranked arms, by each critical label's detection, false automation, accuracy "
    "and Brier"
)
NOT_ESTIMABLE = "not estimable: after every band"
UNKNOWN_COST = "unknown: the last band"
DIGITS = stats.DIGITS

Key = tuple[int | None, ...]  # a band tuple: each criterion's band position, None not estimable
Arm = tuple[Mapping[str, Any], Ledger]  # a graded arm: its entry and the ledger it is made from


def off(reason: str) -> dict[str, Any]:
    """A ranking that is off, and why."""
    return {"status": OFF, "reason": reason}


def configuration_name(entry: Mapping[str, Any]) -> str:
    """An entry's configuration as `slot=variant` pairs, its slots in sorted order: as its
    canonical bytes hold them, whatever order it was built in."""
    providers = entry["configuration"]["providers"]
    return ",".join(f"{slot}={providers[slot]}" for slot in sorted(providers))


def groups_of(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Each record's input under `norm/v1`, in the records' order: its distinct group."""
    return [normalised_input(record["input"]) for record in records]


def detected_groups(
    cases: Sequence[Mapping[str, Any]], groups: Sequence[str], index: int, question: str, label: str
) -> dict[str, bool]:
    """The distinct positive groups of the critical label at `index` (`question=label`): each
    group that holds a positive case, and whether every positive case in it was detected."""
    found: dict[str, bool] = {}
    for number, case in enumerate(cases):
        if index in case["k"]:
            hit = _predicted(case, question) == label
            key = groups[number]
            found[key] = found.get(key, True) and hit
    return found


# --------------------------------------------------------------------------------------------
# Bands
# --------------------------------------------------------------------------------------------


def _decimal(value: float) -> Fraction:
    """A figure as the decimal it is written as (its shortest repr), exactly."""
    return Fraction(repr(value))


def edge_band(value: float, edges: Sequence[float], *, higher_is_better: bool) -> int:
    """The band position (0 the best) of `value` among `edges`, listed from best to worst: the
    first edge it reaches (at or above it when higher is better, at or below it when lower is),
    or one past the last edge when it reaches none."""
    exact = _decimal(value)
    for position, edge in enumerate(edges):
        if exact >= _decimal(edge) if higher_is_better else exact <= _decimal(edge):
            return position
    return len(edges)


def step_band(value: float, step: float, *, higher_is_better: bool) -> int:
    """The band position (0 the best) of `value` in bands of `step`: when higher is better, the
    largest multiple of `step` at or below it, counted down from 1; when lower is better, the
    smallest multiple at or above it, counted up from 0."""
    exact, size = _decimal(value), _decimal(step)
    if higher_is_better:
        return math.ceil(1 / size) - math.floor(exact / size)
    return math.ceil(exact / size)


def _edge_words(position: int, edges: Sequence[float], higher: bool, unit: str = "") -> str:
    if position < len(edges):
        return f"at or {'above' if higher else 'below'} {edges[position]!r}{unit}"
    return f"{'below' if higher else 'above'} {edges[-1]!r}{unit}"


def _step_words(position: int, step: float, higher: bool) -> str:
    size = Decimal(repr(step))
    if higher:
        return f"at or above {(math.ceil(1 / _decimal(step)) - position) * size}"
    return f"at or below {position * size}"


def _edges(value: float | None, edges: Sequence[float], higher: bool) -> dict[str, Any]:
    if value is None:
        return {"value": None, "position": None, "band": NOT_ESTIMABLE}
    position = edge_band(value, edges, higher_is_better=higher)
    words = _edge_words(position, edges, higher)
    return {"value": round(value, DIGITS), "position": position, "band": words}


def _steps(value: float | None, step: float, higher: bool) -> dict[str, Any]:
    if value is None:
        return {"value": None, "position": None, "band": NOT_ESTIMABLE}
    position = step_band(value, step, higher_is_better=higher)
    words = _step_words(position, step, higher)
    return {"value": round(value, DIGITS), "position": position, "band": words}


def _cost(cost: Mapping[str, Any]) -> dict[str, Any]:
    edges = method.COST_BANDS_USD_PER_1000
    if cost["status"] == "unknown":
        return {"value": None, "position": len(edges), "band": UNKNOWN_COST}
    value = 0.0 if cost["status"] == "none" else float(cost["estimate"])  # none: no metered call
    position = edge_band(value, edges, higher_is_better=False)
    words = _edge_words(position, edges, False, " USD")
    return {"value": round(value, DIGITS), "position": position, "band": words}


def _detection_low(item: Mapping[str, Any]) -> float | None:
    """A critical label's simultaneous lower bound, from its distinct counts at its level."""
    k, n = item["distinct"]["detected"], item["distinct"]["positives"]
    return stats.clopper_pearson(k, n, item["bound"]["level"])[0] if n else None


def _bound(found: Mapping[str, Any] | None, *, upper: bool) -> float | None:
    """A proportion's 95% bound, from its k and n."""
    if not found or not found["n"]:
        return None
    low, high = stats.clopper_pearson(found["k"], found["n"], method.LEVEL)
    return high if upper else low


def worst_label(entry: Mapping[str, Any]) -> int | None:
    """The index of the critical label whose simultaneous lower bound is lowest (a label with no
    positive before any other, and the first in the pack's order on a tie): the label that sets
    the entry's detection band. None for an entry with no critical label."""
    lows = [_detection_low(item) for item in entry["figures"]["critical"]]
    if not lows:
        return None
    return min(range(len(lows)), key=lambda i: (lows[i] is not None, lows[i] or 0.0, i))


def bands_of(entry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """An entry's band on each criterion of the tuple (`CRITERIA`): the value it is banded on
    (6 places), its band `position` and the `band` in words. The detection band names the label
    that sets it."""
    figures = entry["figures"]
    worst = worst_label(entry)
    detection: dict[str, Any] = {}
    low = None
    if worst is not None:
        item = figures["critical"][worst]
        detection = {"question": item["question"], "label": item["label"]}
        low = _detection_low(item)
    brier = figures["brier"]
    return {
        DETECTION: {**detection, **_edges(low, method.RECALL_BANDS, True)},
        FALSE_AUTOMATION: _edges(
            _bound(figures["false_automation"], upper=True), method.FALSE_AUTOMATION_BANDS, False
        ),
        ACCURACY: _steps(_bound(figures["accuracy"], upper=False), method.ACCURACY_BAND_STEP, True),
        BRIER: _steps(None if brier is None else brier["high"], method.BRIER_BAND_STEP, False),
        COST: _cost(figures["cost_per_1000"]),
    }


def key_of(entry: Mapping[str, Any]) -> Key:
    """An entry's band tuple: its band position on each criterion, in `CRITERIA`'s order."""
    found = bands_of(entry)
    return tuple(found[criterion]["position"] for criterion in CRITERIA)


def order_key(key: Key) -> tuple[tuple[bool, int], ...]:
    """What band tuples are ordered by: each position in turn, the smaller first, and a figure
    that is not estimable after every band."""
    return tuple((position is None, position or 0) for position in key)


def competition_ranks(keys: Sequence[Key]) -> list[int]:
    """Each band tuple's competition rank: 1 plus the number of tuples strictly before it, so
    equal tuples share a rank and the next rank skips (1, 2, 2, 4)."""
    ordered = [order_key(key) for key in keys]
    return [1 + sum(other < mine for other in ordered) for mine in ordered]


def rank_intervals(count: int, ahead: Collection[tuple[int, int]]) -> list[tuple[int, int]]:
    """The rank interval of each of `count` ranked arms, from `ahead`, the pairs (j, i) in which
    arm j is significantly ahead of arm i: from 1 plus the arms ahead of it, to `count` less the
    arms behind it."""
    return [
        (
            1 + sum(1 for _, behind in ahead if behind == i),
            count - sum(1 for before, _ in ahead if before == i),
        )
        for i in range(count)
    ]


# --------------------------------------------------------------------------------------------
# The ranking
# --------------------------------------------------------------------------------------------


def ranking(
    entries: Sequence[Mapping[str, Any]],
    *,
    ledgers: Sequence[Ledger] | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ranking of `entries` (`mendari.scoreboard/1`, of one pack and one set): off on a
    public set, and off with one arm graded. Else, from each entry's ledger (`ledgers`, in the
    entries' order) and the set's `records` (in the ledgers' order, for the distinct groups):
    the ranked arms with their ranks, rank intervals and bands, the unranked arms with their
    reasons, and the Holm family.

    Refused, in a `MendariError` that names no case: public and hidden entries together; entries
    of more than one pack, set or method; an arm given twice; hidden entries without their
    ledgers and records; ledgers that are not the entries' or do not pair (another case, in
    another order, or another positive); records that are not their cases."""
    if not entries:
        raise MendariError("no entry to rank")
    kinds = {entry["scope"]["set"]["kind"] for entry in entries}
    if kinds == {grade.PUBLIC}:
        return off(grade.PUBLIC_SET)
    if grade.PUBLIC in kinds:
        raise MendariError("an entry of a public set is never ranked with a hidden set's")
    first = entries[0]["scope"]
    for entry in entries[1:]:
        if any(entry["scope"][part] != first[part] for part in ("pack", "set", "method")):
            raise MendariError("the entries ranked together must be of one agent, set and method")
    if len(entries) < 2:
        return off(ONE_ARM)
    if ledgers is None or records is None:
        raise MendariError("a hidden set's arms are ranked from their ledgers, in one grading read")
    _check_paired(entries, ledgers, records)
    groups = groups_of(records)
    _check_groups(entries, ledgers, groups)

    order = list(arms.ARMS)
    unranked = []
    candidates: list[Arm] = []
    for entry, ledger in zip(entries, ledgers, strict=True):
        reasons = [
            {key: item[key] for key in ("attestation", "question", "label", "state") if key in item}
            for item in grade.attestations(entry)
            if item["attestation"] in grade.SAFETY and item["state"] != grade.MET
        ]
        if reasons:
            name = configuration_name(entry)
            unranked.append({"arm": entry["arm"], "configuration": name, "reasons": reasons})
        else:
            candidates.append((entry, ledger))
    unranked.sort(key=lambda item: (order.index(item["arm"]), item["configuration"]))

    ranked = sorted(
        candidates, key=lambda arm: (order_key(key_of(arm[0])), configuration_name(arm[0]))
    )
    keys = [key_of(entry) for entry, _ in ranked]
    tests = _family(ranked, groups)
    decided = stats.holm_family({test["name"]: test["p"] for test in tests}, method.ALPHA)
    for test in tests:
        test["adjusted"] = decided[test["name"]]["adjusted"]
        test["reject"] = decided[test["name"]]["reject"]
    ahead = significantly_ahead([entry for entry, _ in ranked], keys, tests)
    intervals = rank_intervals(len(ranked), ahead)
    in_effect = bool(first["method"]["in_effect"])
    return {
        "status": ON,
        "method": {
            "version": method.METHOD,
            "sha256": method.METHOD_SHA256,
            "in_effect": in_effect,
        },
        "draft": not in_effect,
        "graded": len(entries),
        "ranked": [
            {
                "rank": rank,
                "interval": list(interval),
                "arm": entry["arm"],
                "configuration": configuration_name(entry),
                "bands": bands_of(entry),
                "key": list(key),
            }
            for (entry, _), key, rank, interval in zip(
                ranked, keys, competition_ranks(keys), intervals, strict=True
            )
        ],
        "unranked": unranked,
        "family": {
            "of": FAMILY,
            "alpha": method.ALPHA,
            "size": len(tests),
            "seeds": {"bootstrap": method.SEED, "resamples": method.RESAMPLES},
            "tests": tests,
        },
    }


def _check_paired(
    entries: Sequence[Mapping[str, Any]],
    ledgers: Sequence[Ledger],
    records: Sequence[Mapping[str, Any]],
) -> None:
    if len(ledgers) != len(entries):
        raise MendariError(f"{len(entries)} entries and {len(ledgers)} ledgers")
    seen: set[str] = set()
    for entry, ledger in zip(entries, ledgers, strict=True):
        if entry["inputs"]["ledger"] != ledger.id:
            raise MendariError(
                f"the ledger {ledger.id} is not the one the entry of "
                f"{configuration_name(entry)} was made from"
            )
        if entry["arm"] in seen:
            raise MendariError(f"one configuration per arm is ranked: {entry['arm']} is twice")
        seen.add(entry["arm"])
    base = ledgers[0]
    for ledger in ledgers[1:]:
        parts = ("primary", "questions", "critical")
        if any(ledger.header[part] != base.header[part] for part in parts):
            raise MendariError(f"the ledgers {base.id} and {ledger.id} ask different questions")
        if len(ledger.cases) != len(base.cases) or any(
            (case["c"], case["k"]) != (other["c"], other["k"])
            for case, other in zip(ledger.cases, base.cases, strict=True)
        ):
            raise MendariError(
                f"the ledgers {base.id} and {ledger.id} do not pair: grade every arm in one read"
            )
    if len(records) != len(base.cases):
        raise MendariError(f"{len(records)} records and {len(base.cases)} cases")


def _check_groups(
    entries: Sequence[Mapping[str, Any]], ledgers: Sequence[Ledger], groups: Sequence[str]
) -> None:
    """The records' distinct groups give each entry's distinct positives and detections."""
    for entry, ledger in zip(entries, ledgers, strict=True):
        for index, item in enumerate(entry["figures"]["critical"]):
            found = detected_groups(ledger.cases, groups, index, item["question"], item["label"])
            counts = (item["distinct"]["positives"], item["distinct"]["detected"])
            if (len(found), sum(found.values())) != counts:
                raise MendariError("the records are not the cases the entries were made from")


# --------------------------------------------------------------------------------------------
# The Holm family and the rank intervals
# --------------------------------------------------------------------------------------------


def _predicted(case: Mapping[str, Any], question: str) -> str | None:
    answered = case["q"].get(question)
    return None if answered is None else answered[PREDICTED]


def _truth(case: Mapping[str, Any], question: str) -> str | None:
    answered = case["q"].get(question)
    return None if answered is None else answered[TRUTH]


def _wrong_auto(case: Mapping[str, Any], primary: str) -> bool:
    return case["o"] == "AUTO" and case["r"] != _truth(case, primary)


def _correct(case: Mapping[str, Any], primary: str) -> bool:
    return _predicted(case, primary) == _truth(case, primary)


def _brier(case: Mapping[str, Any], question: str, labels: Sequence[str]) -> float | None:
    answered = case["q"].get(question)
    if answered is None or answered[TRUTH] is None or answered[PREDICTED] is None:
        return None
    distribution = dict(zip(labels, answered[PROBABILITIES], strict=True))
    return metrics.brier([distribution], [answered[TRUTH]])


def _mcnemar(a: Sequence[bool], b: Sequence[bool], *, higher_is_better: bool) -> dict[str, Any]:
    """The exact McNemar test on paired events: their number, the discordant pairs (only a, only
    b), the p-value, and which of the two the difference favours (0, 1 or None)."""
    only_a = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    only_b = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    # 0 or 1: the arm with fewer of a figure lower is better, or more of one higher is better.
    favours = None if only_a == only_b else 0 if (only_a > only_b) == higher_is_better else 1
    return {
        "n": len(a),
        "discordant": [only_a, only_b],
        "p": stats.mcnemar_exact(only_a, only_b),
        "favours": favours,
    }


def _test(
    pair: Sequence[str], criterion: str, found: Mapping[str, Any], label: Sequence[str] = ()
) -> dict[str, Any]:
    """One test of the family: its name, the pair, the criterion (and label), what it was decided
    on, and the arm it favours."""
    named = f"{pair[0]} against {pair[1]}: {criterion}"
    where = {}
    if label:
        named += f" {label[0]}={label[1]}"
        where = {"question": label[0], "label": label[1]}
    favours = found["favours"]
    return {
        "name": named,
        "arms": list(pair),
        "criterion": criterion,
        **where,
        **{key: value for key, value in found.items() if key != "favours"},
        "favours": None if favours is None else pair[favours],
    }


def _family(ranked: Sequence[Arm], groups: Sequence[str]) -> list[dict[str, Any]]:
    """Every pair of ranked arms (in their listing order), by each criterion the family tests:
    each critical label's detection, false automation, accuracy, then Brier."""
    tests = []
    for (a, ledger_a), (b, ledger_b) in itertools.combinations(ranked, 2):
        pair = (a["arm"], b["arm"])
        header = ledger_a.header
        primary = header["primary"]
        for index, (question, label) in enumerate(header["critical"]):
            on_a = detected_groups(ledger_a.cases, groups, index, question, label)
            on_b = detected_groups(ledger_b.cases, groups, index, question, label)
            positives = sorted(on_a)
            found = _mcnemar(
                [on_a[group] for group in positives],
                [on_b[group] for group in positives],
                higher_is_better=True,
            )
            tests.append(_test(pair, DETECTION, found, (question, label)))
        labelled = [
            (x, y)
            for x, y in zip(ledger_a.cases, ledger_b.cases, strict=True)
            if _truth(x, primary) is not None
        ]
        for criterion, event, higher in (
            (FALSE_AUTOMATION, _wrong_auto, False),
            (ACCURACY, _correct, True),
        ):
            found = _mcnemar(
                [event(x, primary) for x, _ in labelled],
                [event(y, primary) for _, y in labelled],
                higher_is_better=higher,
            )
            tests.append(_test(pair, criterion, found))
        tests.append(_test(pair, BRIER, _paired_brier(ledger_a, ledger_b)))
    return tests


def _paired_brier(a: Ledger, b: Ledger) -> dict[str, Any]:
    """The paired bootstrap of the primary question's Brier score (a minus b) on the cases both
    arms scored: their number, the difference, its p-value and which arm it favours (the lower)."""
    primary = a.header["primary"]
    labels = list(a.header["questions"][primary])
    scored = [
        (sa, sb)
        for x, y in zip(a.cases, b.cases, strict=True)
        if (sa := _brier(x, primary, labels)) is not None
        and (sb := _brier(y, primary, labels)) is not None
    ]
    if not scored:  # no case both arms scored: no difference is shown
        return {"n": 0, "difference": None, "p": 1.0, "favours": None}
    difference, _, _, p = stats.paired_bootstrap(
        [sa for sa, _ in scored],
        [sb for _, sb in scored],
        resamples=method.RESAMPLES,
        seed=method.SEED,
        level=method.LEVEL,
    )
    favours = 0 if difference < 0 else 1 if difference > 0 else None
    return {"n": len(scored), "difference": round(difference, DIGITS), "p": p, "favours": favours}


def significantly_ahead(
    entries: Sequence[Mapping[str, Any]], keys: Sequence[Key], tests: Sequence[Mapping[str, Any]]
) -> set[tuple[int, int]]:
    """The pairs (j, i) in which ranked arm j is significantly ahead of ranked arm i, from the
    ranked `entries` (in their listing order), their band tuples and the family's decided tests
    (`reject` set): j's tuple is the better one, and the test on the first criterion where the
    two differ rejects in j's favour. On detection it is the test on the label that sets the
    lower arm's band. Arms apart first on cost are never significantly apart."""
    by = {
        (tuple(test["arms"]), test["criterion"], test.get("question"), test.get("label")): test
        for test in tests
    }
    ahead = set()
    for i, j in itertools.combinations(range(len(entries)), 2):
        differ = [c for c, (x, y) in enumerate(zip(keys[i], keys[j], strict=True)) if x != y]
        if not differ or CRITERIA[differ[0]] == COST:
            continue  # equal tuples, or apart first on cost, which has no test in the family
        better, worse = (i, j) if order_key(keys[i]) < order_key(keys[j]) else (j, i)
        criterion = CRITERIA[differ[0]]
        question = label = None
        if criterion == DETECTION:  # the test on the label that sets the lower arm's band
            worst = worst_label(entries[worse])
            assert worst is not None  # every ranked arm has its critical labels' evidence
            item = entries[worse]["figures"]["critical"][worst]
            question, label = item["question"], item["label"]
        test = by[((entries[i]["arm"], entries[j]["arm"]), criterion, question, label)]
        if test["reject"] and test["favours"] == entries[better]["arm"]:
            ahead.add((better, worse))
    return ahead
