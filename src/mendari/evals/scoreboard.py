# SPDX-License-Identifier: Apache-2.0
"""Scoreboard entries (`mendari.scoreboard/1`, LP-17d) and the board they are shown on, under the
grading method 1.0 (`mendari.evals.method`, `docs/grading/method-1.0.md`).

An entry is one configuration's figures on one set, and a pure function of five inputs: the
ledger (`mendari.ledger/1`), the dataset (by its SHA-256), the run record (`mendari.run/1`), the
price table (`mendari.prices/1`, by its SHA-256) and the method (by its SHA-256). The same inputs
give the same bytes. Its figures follow the method's section 4:

- each critical label's detection as raw k of n with its 95% interval, its distinct positives
  (cases grouped by their input under `norm/v1`: a group is a positive when any of its cases is,
  and detected only when every positive case in it is), the grade's bound (Clopper-Pearson at
  the simultaneous level 1 - 0.05/L over the distinct groups, its own field with its level), and
  its evidence (enough when its distinct positives reach `method.min_positives`); and its
  positives decided AUTO, as k of n;
- false automation (wrong AUTO over the labelled cases), the AUTO error rate (wrong AUTO over the
  AUTO cases), automation and accuracy, each k of n with its 95% interval;
- macro-F1 with its stratified bootstrap interval, Brier with its bootstrap interval, Brier's
  reliability term with its bootstrap interval when estimable, and ECE, a description only;
- cost per 1,000 decisions from the run record's tokens and the dated price table, with its
  bootstrap interval, or "0 (no metered calls)", or "unknown";
- latency p50 and p95 from the run record, each with its bootstrap interval, measured on the
  runner and not reproducible, never ranked;
- the pack's declared subgroups, and, for a set whose records carry sampling weights, the
  weighted figures beside the unweighted ones (recall is never weighted).

Every bootstrap is the method's: 2,000 resamples, seed 0, at 95%. Where `result_from_ledger`
already computes a figure, it is reused. An entry also carries its scope (the pack, the set, the
models as reported, the method and whether it is in effect, the date, the words "engineering
evaluation, not clinical validation", the disclosure and the pack's status line) and its
standing: a public set is a regression check, never ranked or graded. It may name the earlier
result it supersedes in the results store (`supersedes`, LP-17f, `evals/results_store.py`).
`body_sha256` is the SHA-256 of its canonical bytes without any signature.

`board` shows entries one table per pack and set. Freshness is worked out there, when the board
is built, and never stored: no clock is read, so every entry shows the date it goes stale. Its
ranking and each entry's grade come from `bands.ranking` and `grade.grade` (LP-17j): on a public
set, off and None. Nothing here reads a clock, a setting or the network, and nothing names a case.

`hidden_entry` is a frozen hidden set's entry (LP-17k): made in memory, inside the grading read
that decided its arm, from the ledger and the records that read handed over (their inputs give
the distinct groups, their `attributes.weight` the weighted figures), and never stored. It holds
the same figures but cost, latency and subgroups (a local configuration's cost is 0 by
construction, kept for the band tuple only), and its scope names the set by its id and freeze
commitment, never a hash of its cases. `evals/hidden.py` grades and ranks it, and answers what
`mendari.hidden_scoreboard/1` holds of it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mendari.core.pack import Pack
from mendari.core.status import NOT_REVIEWED, SYNTHETIC_EXAMPLE, status_label
from mendari.errors import MendariError
from mendari.evals import arms, bands, grade, method, metrics, runs, stats
from mendari.evals.canonical import NORM_VERSION
from mendari.evals.harness import Dataset
from mendari.evals.ledger import Ledger, canonical, case_hash
from mendari.evals.result import ERROR, PREDICTED, PROBABILITIES, TRUTH, result_from_ledger
from mendari.providers import MODEL_TYPES

FORMAT = "mendari.scoreboard/1"
PUBLIC = grade.PUBLIC  # every shipped set's kind: never ranked or graded
PUBLIC_SET = grade.PUBLIC_SET
HIDDEN = "hidden"  # a frozen hidden set's kind (`hidden_entry`): graded, and ranked when it can be
NOT_CLINICAL = "engineering evaluation, not clinical validation"
NO_MODEL_PROVIDER = "no model provider"
DISCLOSURE_PENDING = "not published: disclosure pending"
NO_METERED_CALLS = "0 (no metered calls)"
PRICED = "priced"
UNKNOWN = "unknown"
NO_PRICE = "no price table holds its model"  # none given, or one that lacks it: the same bytes
NO_PRICES = "no price table holds all their models"  # the same, for two model types or more
NOT_REPRODUCIBLE = "measured on the runner, not reproducible"
TOO_FEW = "too few"
AS_SAMPLED = "as sampled"
# A hidden set's weighted figures left out (`hidden_entry`): a weight is a stratum's (N_h / n_h),
# so beside the unweighted k of n over the same cases they could give a stratum's figures.
WITHHELD = "withheld"
WEIGHTS_WITHHELD = f"fewer than {method.SUBGROUP_MIN} cases of a share carry one of its weights"
NOT_ESTIMABLE = "not_estimable"
SUFFICIENT, INSUFFICIENT = "sufficient", grade.INSUFFICIENT
MEASURED, NOT_MEASURED = "measured", "not measured"
UNSIGNED = "unsigned"
DRAFT = "method draft, not in effect"
STALE_PACK = "stale: agent changed"
STALE_SET = "stale: set changed"
STALE_METHOD = "stale: method changed"
STALE_MODEL = "stale: another model reported"
STALE_DRIFT = "stale: canary drift"
CANARY_FORMAT = "mendari.canary/1"
CANARY_SET = "test"  # a canary is drawn from a pack's `test` set (the method's section 12)
# Not the body: what `body_sha256` is of leaves these out, and a signature (LP-17e) is added.
BODY_EXCLUDED = ("body_sha256", "signature")
_BODY_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
WEIGHT = "weight"  # the record attribute that holds a case's sampling weight
DIGITS = stats.DIGITS
STATUSES = (SYNTHETIC_EXAMPLE, NOT_REVIEWED)


@dataclass(frozen=True)
class PackScope:
    """The pack an entry is of: its id, version and digest, and its status line
    (`core.status.status_label`). An entry of an earlier digest is recomputed from its own."""

    id: str
    version: str
    digest: str
    status: str

    @classmethod
    def of(cls, pack: Pack) -> PackScope:
        return cls(pack.id, pack.version, pack.digest, status_label(pack.manifest))


def body(entry: Mapping[str, Any]) -> dict[str, Any]:
    """An entry without what its hash leaves out (`BODY_EXCLUDED`)."""
    return {key: value for key, value in entry.items() if key not in BODY_EXCLUDED}


def body_sha256(entry: Mapping[str, Any]) -> str:
    """The SHA-256 of an entry's canonical bytes (`ledger.canonical`), without any signature."""
    return "sha256:" + hashlib.sha256(canonical(body(entry)).encode()).hexdigest()


def entry(
    ledger: Ledger,
    dataset: Dataset,
    run: Mapping[str, Any],
    *,
    pack: PackScope,
    arm: str,
    configuration: arms.Configuration,
    prices: runs.Prices | None,
    disclosures: runs.Disclosures | None,
    supersedes: str | None = None,
    replicates: Sequence[tuple[Ledger, Mapping[str, Any]]] = (),
    declared: str | None = None,
) -> dict[str, Any]:
    """The `mendari.scoreboard/1` entry of `configuration` (in `arm`) of `pack` on `dataset`, from
    its `ledger` and its `run` record, priced from `prices` (None: a metered cost is unknown),
    with the statements of `disclosures` (None: every remote type's is pending). `supersedes`
    names the earlier result it replaces in the results store (`sha256:` and its body's SHA-256,
    LP-17f), or None: the link is part of the body, so its hash and any signature cover it.

    A remote configuration's entry may hold its replicates (LP-17h; the method's section 10):
    `ledger` and `run` are replicate 1, the headline, and `replicates` the others (each a ledger
    and its run record, replicate 2 first). Its figures are replicate 1's; its inputs list every
    replicate's ledger and run record, and `figures.replicates` holds their count, the case flip
    rate and each figure's spread (`runs.replicates`). `declared` is the SHA-256 of the
    declaration its configuration was declared in (`mendari.declared/1`), or None. Neither field
    is written when it is not given, so an entry of one replicate is as it was before them.

    Refused, in a `MendariError` that names ids and hashes and never a case: a ledger of another
    pack (id, version or digest) or dataset (name, SHA-256, size, limit, or cases that are not
    the dataset's in its order), another configuration or arm, a run record that disagrees
    with the ledger (`runs.disagreement`) or holds tokens of a type the configuration does not
    call, a `supersedes` that is not a body's SHA-256, replicates of a local configuration (it
    is deterministic: one replicate), more than `method.MAX_REPLICATES`, a headline that is not
    replicate 1 or replicates out of order, and a `declared` that is not 64 hex digits."""
    record = runs.check_run(run)
    _refuse_another(ledger, dataset, pack, arm, configuration, record)
    if supersedes is not None and not _BODY_SHA256.fullmatch(supersedes):
        raise MendariError("supersedes names a result by its body's SHA-256 (sha256:<64 hex>)")
    if declared is not None and not _HEX.fullmatch(declared):
        raise MendariError("declared names a declaration by its SHA-256 (64 hex digits)")
    if replicates and configuration.local:
        raise MendariError(
            f"{configuration.label} is local, so deterministic: one replicate, no flips"
        )
    if replicates and record["replicate"] != runs.HEADLINE:
        raise MendariError(
            f"the headline is replicate {runs.HEADLINE}, as declared before the run (this run "
            f"record is replicate {record['replicate']})"
        )
    figures = _figures(ledger, dataset, record, configuration, prices)
    cost = figures["cost_per_1000"]
    measured_on = datetime.date.fromisoformat(record["date"])
    replicated: list[dict[str, Any]] = []
    if replicates:
        ledgers, records, scalars = [ledger], [record], [_scalars(figures)]
        for other, other_run in replicates:
            checked = runs.check_run(other_run)
            _refuse_another(other, dataset, pack, arm, configuration, checked)
            if checked["date"] != record["date"]:
                raise MendariError("every replicate of a run is dated the day of its headline")
            ledgers.append(other)
            records.append(checked)
            # Priced from the table only when the headline is: a result no table priced is
            # the same whichever table is at hand (its inputs cite none).
            table = prices if cost["status"] == PRICED else None
            scalars.append(_scalars(_figures(other, dataset, checked, configuration, table)))
        figures["replicates"] = runs.replicates(ledgers, records, scalars)
        replicated = [
            {"replicate": found["replicate"], "ledger": one.id, "run": runs.run_sha256(found)}
            for one, found in zip(ledgers, records, strict=True)
        ]
    header = ledger.header
    group_of = bands.groups_of(dataset.records)
    scope = {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "status": pack.status,
        "set": {
            "kind": PUBLIC,
            "name": dataset.name,
            "sha256": dataset.sha256,
            "n": len(ledger.cases),
            "distinct_inputs": len(set(group_of)),
            "norm": NORM_VERSION,
        },
        "providers": header["providers"],
        "models": {"answered": header["models"], "by_type": record["models"]},
        "method": {
            "version": method.METHOD,
            "sha256": method.METHOD_SHA256,
            "in_effect": method.in_effect(measured_on),
        },
        "measured_on": record["date"],
        "evaluation": NOT_CLINICAL,
        "disclosure": _disclosure(configuration, disclosures),
    }
    inputs: dict[str, Any] = {
        "ledger": ledger.id,
        "dataset": dataset.sha256,
        "run": {
            "sha256": runs.run_sha256(record),
            "replicate": record["replicate"],
            "engine": record["engine"],
        },
        # Cited only when the cost is priced from it: an entry it did not price (no metered
        # call, a failed call, a model it lacks) is the same whichever table is at hand.
        "prices": {"sha256": prices.sha256, "date": prices.date}
        if prices is not None and cost["status"] == PRICED
        else None,
        "method": method.METHOD_SHA256,
    }
    # Written only when given, so an entry of one replicate is as it was before them (LP-17h).
    if replicated:
        inputs["replicates"] = replicated
    if declared is not None:
        inputs["declared"] = declared
    found = {
        "format": FORMAT,
        "arm": arm,
        "configuration": configuration.to_dict(),
        "inputs": inputs,
        "seeds": {"bootstrap": method.SEED, "resamples": method.RESAMPLES, "level": method.LEVEL},
        "scope": scope,
        "ranking": bands.off(PUBLIC_SET),
        "grade": None,  # a public set is never graded (`grade.grade`)
        "figures": figures,
        "supersedes": supersedes,
    }
    return {**found, "body_sha256": body_sha256(found)}


def _figures(
    ledger: Ledger,
    dataset: Dataset,
    record: Mapping[str, Any],
    configuration: arms.Configuration,
    prices: runs.Prices | None,
) -> dict[str, Any]:
    """One run's figures (the method's section 4), from its ledger and run record."""
    header, cases = ledger.header, ledger.cases
    result = result_from_ledger(ledger, seed=method.SEED, resamples=method.RESAMPLES)
    group_of = bands.groups_of(dataset.records)
    metered = sorted(MODEL_TYPES.intersection(configuration.remote_types))
    return {
        "critical": _critical(header, cases, result, group_of),
        **_proportions(result),
        **_means(header, cases, result),
        "cost_per_1000": _cost(cases, record, metered, prices),
        "latency_ms": _latency(record),
        "subgroups": _subgroups(header, cases),
        "weighted": _weighted(header, cases, dataset),
    }


def _share(found: Mapping[str, Any]) -> float | None:
    return None if not found["n"] else found["k"] / found["n"]


def _scalars(figures: Mapping[str, Any]) -> dict[str, float | None]:
    """Each figure of one run whose spread across replicates is shown, by the name its row has
    on the board, with its estimate (k/n for a proportion), or None where it has none:
    detection and critical cases automated per critical label, false automation, the AUTO error
    rate, automation, accuracy, macro-F1, Brier, Brier's reliability term, ECE, cost and
    latency: the rows of the entry's own column. Subgroups and weighted figures are left out,
    and the page says so. A subgroup's figures are shares of its cases, which move between
    replicates only where its cases flip, and the case flip rate counts every such case; it has
    a cell per declared value, most of them "too few" on these sets. Weighted figures are shown
    only for a set whose records carry sampling weights, and the public sets are as sampled."""
    found: dict[str, float | None] = {}
    for critical in figures["critical"]:
        name = f"{critical['question']}={critical['label']}"
        found[f"Detected {name}"] = _share(critical["detected"])
        found[f"Critical automated {name}"] = _share(critical["automated"])
    for key, title in (
        ("false_automation", "False automation"),
        ("auto_error_rate", "AUTO error rate"),
        ("automation", "Automation"),
        ("accuracy", "Accuracy"),
    ):
        found[title] = _share(figures[key])
    for key, title in (("macro_f1", "Macro-F1"), ("brier", "Brier")):
        found[title] = None if figures[key] is None else figures[key]["estimate"]
    reliability, ece = figures["brier_reliability"], figures["ece"]
    ok = "ok"
    found["Brier reliability"] = (
        reliability["estimate"] if reliability and reliability["status"] == ok else None
    )
    found["ECE (equal-mass)"] = ece["value"] if ece and ece["status"] == ok else None
    cost = figures["cost_per_1000"]
    found["Cost per 1,000 decisions"] = cost["estimate"] if cost["status"] == PRICED else None
    for name in ("p50", "p95"):
        found[f"Latency {name}"] = figures["latency_ms"][name]["estimate"]
    return found


def hidden_entry(
    ledger: Ledger,
    dataset: Dataset,
    *,
    pack: PackScope,
    arm: str,
    configuration: arms.Configuration,
    set_id: str,
    commitment: str,
    measured_on: datetime.date,
) -> dict[str, Any]:
    """The in-memory entry of `configuration` (in `arm`, a local configuration) of `pack` on a
    frozen hidden set (LP-17k): `dataset` is what the set's grading read handed over (its id
    `set_id`, its freeze `commitment`), `ledger` the run's ledger over it, and `measured_on` the
    day of the read. Its figures are an entry's but cost, latency and subgroups: no run record
    is kept of a hidden set, and a hidden set's answer holds none of them. Its weighted figures
    are withheld when fewer than `method.SUBGROUP_MIN` cases of a share carry one of its weights
    (`_weighted`), whose stratum they would show beside the unweighted ones. `cost_per_1000` is
    "0 (no metered calls)", which a local configuration's is by construction, for the band tuple
    only. Its scope's set is `{kind: "hidden", id, commitment, n, distinct_inputs, norm}`: never
    a name or a hash of the cases. Its `inputs` name the ledger's id, for `bands.ranking` to
    pair the arms' ledgers in memory; nothing answers it (`evals/hidden.py`).

    Refused, in a `MendariError` that names no case: a remote configuration, a ledger of another
    pack or of other cases than the dataset's (in its order), and another configuration or arm."""
    if not configuration.local:
        raise MendariError(
            f"a hidden set is graded with local configurations only, not {configuration.label}"
        )
    _refuse_pack(ledger, pack)
    on = ledger.header["dataset"]
    if (on.get("name"), on.get("n")) != (dataset.name, len(dataset.records)):
        raise MendariError(f"the ledger {ledger.id} is of another set (its name or n differs)")
    _refuse_cases(ledger, dataset, arm, configuration)
    header, cases = ledger.header, ledger.cases
    result = result_from_ledger(ledger, seed=method.SEED, resamples=method.RESAMPLES)
    group_of = bands.groups_of(dataset.records)
    figures = {
        "critical": _critical(header, cases, result, group_of),
        **_proportions(result),
        **_means(header, cases, result),
        # Local only: no metered call, by construction. The band tuple's last element; a hidden
        # set's answer holds no cost.
        "cost_per_1000": {"status": "none", "text": NO_METERED_CALLS},
        "weighted": _weighted(header, cases, dataset, min_class=method.SUBGROUP_MIN),
    }
    scope = {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "status": pack.status,
        "set": {
            "kind": HIDDEN,
            "id": set_id,
            "commitment": commitment,
            "n": len(cases),
            "distinct_inputs": len(set(group_of)),
            "norm": NORM_VERSION,
        },
        "providers": header["providers"],
        "models": {"answered": header["models"]},
        "method": {
            "version": method.METHOD,
            "sha256": method.METHOD_SHA256,
            "in_effect": method.in_effect(measured_on),
        },
        "measured_on": measured_on.isoformat(),
        "evaluation": NOT_CLINICAL,
        "disclosure": _disclosure(configuration, None),
    }
    return {
        "format": FORMAT,
        "arm": arm,
        "configuration": configuration.to_dict(),
        "inputs": {"ledger": ledger.id, "method": method.METHOD_SHA256},
        "seeds": {"bootstrap": method.SEED, "resamples": method.RESAMPLES, "level": method.LEVEL},
        "scope": scope,
        "figures": figures,
    }


def _refuse_pack(ledger: Ledger, pack: PackScope) -> None:
    """A ledger of another pack (id, version or digest), or a status line Mendari does not
    write."""
    if pack.status not in STATUSES:
        raise MendariError("the agent's status line is not one Mendari writes")
    of = ledger.header["pack"]
    if (of.get("id"), of.get("version"), of.get("digest")) != (pack.id, pack.version, pack.digest):
        raise MendariError(
            f"the ledger {ledger.id} is of {of.get('id')}@{of.get('version')} "
            f"({of.get('digest')}), not {pack.id}@{pack.version} ({pack.digest})"
        )


def _refuse_another(
    ledger: Ledger,
    dataset: Dataset,
    pack: PackScope,
    arm: str,
    configuration: arms.Configuration,
    record: Mapping[str, Any],
) -> None:
    header = ledger.header
    _refuse_pack(ledger, pack)
    on = header["dataset"]
    expected = {
        "name": dataset.name,
        "sha256": dataset.sha256,
        "n": len(dataset.records),
        "limit": dataset.limit,
    }
    for key, value in expected.items():
        if on.get(key) != value:
            raise MendariError(f"the ledger {ledger.id} is of another dataset (its {key} differs)")
    _refuse_cases(ledger, dataset, arm, configuration)
    problem = runs.disagreement(record, ledger)
    if problem is not None:
        raise MendariError(f"the run record does not agree with the ledger: {problem}")
    called = set(record["models"]) | {kind for case in record["cases"] for kind in case["tokens"]}
    if not called <= set(configuration.remote_types):
        raise MendariError(
            f"the run record names a type the configuration {configuration.label} does not call "
            f"({', '.join(sorted(called - set(configuration.remote_types)))})"
        )


def _refuse_cases(
    ledger: Ledger, dataset: Dataset, arm: str, configuration: arms.Configuration
) -> None:
    """A ledger of other cases than the dataset's, in its order, or of another configuration;
    a configuration that is not in `arm`."""
    header = ledger.header
    if len(ledger.cases) != len(dataset.records):
        raise MendariError(
            f"the ledger {ledger.id} holds {len(ledger.cases)} cases and {dataset.name} "
            f"{len(dataset.records)}"
        )
    if not dataset.case_key:
        raise MendariError(f"{dataset.name}: no case key (load the dataset with load_dataset)")
    for number, (found, line) in enumerate(zip(dataset.records, ledger.cases, strict=True), 1):
        if case_hash(dataset.case_key, found["id"]) != line["c"]:
            raise MendariError(
                f"the ledger {ledger.id}'s case {number} is not {dataset.name}'s case {number}"
            )
    if arm not in arms.ARMS or arm != configuration.arm:
        raise MendariError(f"the configuration {configuration.label} is not in the arm {arm}")
    variants = {slot: found["variant"] for slot, found in header["providers"].items()}
    if variants != configuration.providers:
        raise MendariError(
            f"the ledger {ledger.id} is of another configuration than {configuration.label}"
        )


def _p(k: int, n: int, level: float = method.LEVEL) -> dict[str, Any]:
    return stats.proportion(k, n, level).to_dict()


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, DIGITS)


def _interval(estimate: float, low: float | None, high: float | None) -> dict[str, Any]:
    return {"estimate": _rounded(estimate), "low": _rounded(low), "high": _rounded(high)}


def _predicted(case: Mapping[str, Any], question: str) -> str | None:
    answered = case["q"].get(question)
    return None if answered is None else answered[PREDICTED]


def _truth(case: Mapping[str, Any], question: str) -> str | None:
    answered = case["q"].get(question)
    return None if answered is None else answered[TRUTH]


def _critical(
    header: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any],
    group_of: Sequence[str],
) -> list[dict[str, Any]]:
    """Each critical label: detection (raw), its positives decided AUTO, its distinct positives,
    the grade's bound over distinct groups at the simultaneous level, and its evidence."""
    labels = len(header["critical"])
    if not labels:
        return []
    level = method.simultaneous_level(labels)
    needed = method.min_positives(method.RECALL_FLOOR, labels)
    found = []
    for index, (question, label) in enumerate(header["critical"]):
        counted = result["decisions"]["critical"][index]
        detected_groups = bands.detected_groups(cases, group_of, index, question, label)
        distinct = len(detected_groups)
        distinct_detected = sum(detected_groups.values())
        found.append(
            {
                "question": question,
                "label": label,
                "detected": _p(counted["detected"], counted["n"]),
                "automated": _p(counted["auto_missed"], counted["n"]),
                "distinct": {"positives": distinct, "detected": distinct_detected},
                "bound": {
                    **_p(distinct_detected, distinct, level),
                    "level": level,
                    "labels": labels,
                },
                "evidence": {
                    "status": SUFFICIENT if distinct >= needed else INSUFFICIENT,
                    "needed": needed,
                },
            }
        )
    return found


def _proportions(result: Mapping[str, Any]) -> dict[str, Any]:
    """False automation, the AUTO error rate, automation and accuracy, as `result` has them."""
    held = result["proportions"]
    right = held["automated_correct"]
    return {
        "false_automation": held["false_automation"],
        "auto_error_rate": _p(right["n"] - right["k"], right["n"]),
        "automation": held["automation"],
        "accuracy": held["accuracy"],
    }


def _means(
    header: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Macro-F1, Brier, Brier's reliability term and ECE, of the primary question."""
    primary = header["primary"]
    labels = header["questions"][primary]
    question = result["questions"].get(primary)
    if question is None:  # no case is labelled for the primary question
        return {"macro_f1": None, "brier": None, "brier_reliability": None, "ece": None}
    pairs = [
        (_truth(case, primary), _predicted(case, primary))
        for case in cases
        if _truth(case, primary) is not None
    ]

    def macro_f1(sample: Sequence[tuple[Any, Any]]) -> float:
        f1 = metrics.classification([t for t, _ in sample], [p for _, p in sample], labels)
        return float(f1["macro_f1"])

    estimate, low, high = stats.stratified_bootstrap(
        pairs,
        [truth for truth, _ in pairs],
        macro_f1,
        resamples=method.RESAMPLES,
        seed=method.SEED,
        level=method.LEVEL,
    )
    brier = question["brier"]
    held = question["brier_interval"]
    ece = question["calibration"]["equal_mass"]
    return {
        "macro_f1": _interval(estimate, low, high),
        "brier": None
        if brier is None or held is None
        else _interval(brier, held["low"], held["high"]),
        "brier_reliability": _reliability(cases, primary, labels),
        # A description: no interval, never a criterion.
        "ece": {"status": "ok", "value": _rounded(ece["ece"])}
        if ece["status"] == "ok"
        else {"status": NOT_ESTIMABLE, "reason": ece["reason"]},
    }


def _reliability(
    cases: Sequence[Mapping[str, Any]], primary: str, labels: Sequence[str]
) -> dict[str, Any] | None:
    scored = [
        case["q"][primary]
        for case in cases
        if _truth(case, primary) is not None and _predicted(case, primary) is not None
    ]
    if not scored:
        return None
    rows = [
        (dict(zip(labels, found[PROBABILITIES], strict=True)), found[TRUTH]) for found in scored
    ]

    def term(sample: Sequence[tuple[dict[str, float], str]]) -> float | None:
        found = stats.brier_reliability(
            [d for d, _ in sample],
            [t for _, t in sample],
            labels,
            bins=method.CALIBRATION_BINS,
            min_bin_size=method.CALIBRATION_MIN_BIN,
        )
        value = found["reliability"]
        return value if isinstance(value, float) else None

    whole = stats.brier_reliability(
        [d for d, _ in rows],
        [t for _, t in rows],
        labels,
        bins=method.CALIBRATION_BINS,
        min_bin_size=method.CALIBRATION_MIN_BIN,
    )
    if whole["status"] != "ok":
        return {"status": NOT_ESTIMABLE, "reason": whole["reason"]}
    estimate, low, high, _ = stats.bootstrap_defined(
        rows, term, resamples=method.RESAMPLES, seed=method.SEED, level=method.LEVEL
    )
    assert estimate is not None  # estimable on every case, so on every resample of their size
    return {"status": "ok", **_interval(estimate, low, high)}


def _cost(
    cases: Sequence[Mapping[str, Any]],
    record: Mapping[str, Any],
    metered: Sequence[str],
    prices: runs.Prices | None,
) -> dict[str, Any]:
    """Cost per 1,000 decisions, from each case's tokens by type and the dated table: "0 (no
    metered calls)" for a configuration that calls no model, or whose model types made no call
    on this set; else "unknown" when any call failed (a provider error in the ledger), or a type
    that spent tokens reported no model (a composite kept none of its answers) or more than one
    (whose tokens the record does not split), or no price table holds its model (there is none,
    or it lacks the model). A type that spent no token costs nothing, whatever its model. Never
    a price the ledger, a pack or a provider holds.

    An unknown cost reads the same with no table as with any table: an entry cites a table only
    when it priced the cost (`entry`), so the store recomputes an unpriced entry with none and
    must get the same bytes (LP-17h: a run priced with a table that lacks its model is stored)."""
    none = {"status": "none", "text": NO_METERED_CALLS}
    if not metered:
        return none

    def unknown(reason: str) -> dict[str, Any]:
        return {"status": UNKNOWN, "reason": reason}

    if any(found[ERROR] is not None for case in cases for found in case["q"].values()):
        return unknown("a provider call failed")
    called = sorted({kind for case in record["cases"] for kind in case["tokens"]})
    if not called:
        return none
    spent = [
        kind
        for kind in called
        if any(any(case["tokens"].get(kind, ())) for case in record["cases"])
    ]
    # What no table changes is asked of every type before any price is looked up, and a missing
    # price names every type that spent tokens: an unpriced entry cites no table and is
    # recomputed with none, so its reason must be the same whichever table is at hand (a
    # configuration of Jev and an LLM included).
    for kind in spent:
        models = record["models"].get(kind, [])
        if not models:
            return unknown(f"{kind}: tokens spent, and no answer kept names its model")
        if len(models) != 1:
            return unknown(f"{kind}: {len(models)} models reported, not one")
    rates = {kind: (0.0, 0.0) for kind in called}  # a type that spent no token: nothing to price
    for kind in spent:
        rate = None if prices is None else prices.rate(kind, record["models"][kind][0])
        if rate is None:
            if len(spent) == 1:
                return unknown(f"{kind}: {NO_PRICE}")
            return unknown(f"{', '.join(spent)}: {NO_PRICES}")
        rates[kind] = rate
    per_case = [
        1000.0
        * math.fsum(
            tokens[0] / 1000.0 * rates[kind][0] + tokens[1] / 1000.0 * rates[kind][1]
            for kind, tokens in sorted(case["tokens"].items())
        )
        for case in record["cases"]
    ]
    estimate, low, high = stats.bootstrap(
        per_case, resamples=method.RESAMPLES, seed=method.SEED, level=method.LEVEL
    )
    return {"status": PRICED, "currency": "USD", **_interval(estimate, low, high)}


def _latency(record: Mapping[str, Any]) -> dict[str, Any]:
    """Latency p50 and p95 (Hyndman and Fan's definition 7) from the run record, each with its
    bootstrap interval: measured on the runner, not reproducible, never ranked."""
    values = [float(case["latency_ms"]) for case in record["cases"]]

    def at(q: float) -> Callable[[Sequence[float]], float]:
        return lambda sample: stats.percentile(sorted(sample), q)

    found: dict[str, Any] = {"note": NOT_REPRODUCIBLE}
    for name, q in (("p50", 0.5), ("p95", 0.95)):
        estimate, low, high = stats.bootstrap(
            values, at(q), resamples=method.RESAMPLES, seed=method.SEED, level=method.LEVEL
        )
        found[name] = _interval(estimate, low, high)
    return found


def _counted(k: int, n: int) -> dict[str, Any]:
    """k of n with its interval, or "too few" (and its n) under the method's subgroup minimum."""
    return _p(k, n) if n >= method.SUBGROUP_MIN else {"status": TOO_FEW, "n": n}


def _subgroups(
    header: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The pack's declared subgroups (the ledger's `a`): per value, its cases and its accuracy,
    automation and AUTO error rate, each "too few" over fewer than 10 cases."""
    primary = header["primary"]
    found: dict[str, dict[str, Any]] = {}
    for attribute in header["subgroups"]:
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for case in cases:
            buckets.setdefault(case["a"].get(attribute, "unknown"), []).append(case)
        found[attribute] = {}
        for value, members in sorted(buckets.items()):
            labelled = [case for case in members if _truth(case, primary) is not None]
            auto = [case for case in labelled if case["o"] == "AUTO"]
            if len(members) < method.SUBGROUP_MIN:
                found[attribute][value] = {"n": len(members), "status": TOO_FEW}
                continue
            found[attribute][value] = {
                "n": len(members),
                "accuracy": _counted(
                    sum(_predicted(c, primary) == _truth(c, primary) for c in labelled),
                    len(labelled),
                ),
                "automation": _counted(sum(c["o"] == "AUTO" for c in members), len(members)),
                "auto_error_rate": _counted(
                    sum(c["r"] != _truth(c, primary) for c in auto), len(auto)
                ),
            }
    return found


def _weighted(
    header: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    dataset: Dataset,
    *,
    min_class: int | None = None,
) -> dict[str, Any]:
    """Korn-Graubard versions of accuracy, false automation, automation and the AUTO error rate
    when every record carries a sampling weight (`attributes.weight`), aligned to the ledger's
    order; "as sampled" when none does. Recall is never weighted. With `min_class` (a hidden
    set's), "withheld" when fewer than `min_class` cases of a share (all, labelled or AUTO) carry
    one of its weights: a weight is a stratum's, and the pair of weighted and unweighted figures
    over few cases of one weight could be solved for that stratum's figures."""
    weights = [found.get("attributes", {}).get(WEIGHT) for found in dataset.records]
    if all(weight is None for weight in weights):
        return {"status": AS_SAMPLED}
    if any(weight is None for weight in weights):
        raise MendariError(f"{dataset.name}: some records carry a sampling weight and some do not")
    for weight in weights:
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise MendariError(f"{dataset.name}: a sampling weight is not a number")
        if not (math.isfinite(weight) and weight > 0):
            raise MendariError(f"{dataset.name}: a sampling weight is not finite and positive")
    primary = header["primary"]
    numbered = list(zip(cases, (float(w) for w in weights), strict=True))
    labelled = [(c, w) for c, w in numbered if _truth(c, primary) is not None]
    auto = [(c, w) for c, w in labelled if c["o"] == "AUTO"]
    if min_class is not None and any(
        count < min_class
        for members in (numbered, labelled, auto)
        for count in Counter(w for _, w in members).values()
    ):
        return {"status": WITHHELD, "reason": WEIGHTS_WITHHELD}

    def share(members: Sequence[tuple[Mapping[str, Any], float]], hit: Callable[..., bool]) -> Any:
        found = stats.weighted_proportion([hit(c) for c, _ in members], [w for _, w in members])
        return found.to_dict()

    return {
        "status": "weighted",
        "accuracy": share(labelled, lambda c: _predicted(c, primary) == _truth(c, primary)),
        "false_automation": share(
            labelled, lambda c: c["o"] == "AUTO" and c["r"] != _truth(c, primary)
        ),
        "automation": share(numbered, lambda c: c["o"] == "AUTO"),
        "auto_error_rate": share(auto, lambda c: c["r"] != _truth(c, primary)),
    }


def _disclosure(
    configuration: arms.Configuration, disclosures: runs.Disclosures | None
) -> dict[str, Any]:
    """For a local configuration, "no model provider"; else each remote type's statement, and
    "not published: disclosure pending" while any is missing."""
    if not configuration.remote_types:
        return {"status": NO_MODEL_PROVIDER, "statements": {}}
    held = {} if disclosures is None else disclosures.statements
    statements = {kind: held.get(kind) for kind in configuration.remote_types}
    pending = any(statement is None for statement in statements.values())
    return {"status": DISCLOSURE_PENDING if pending else "stated", "statements": statements}


# --------------------------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Current:
    """What is current for one pack when a board is built: its digest, its arms' statuses
    (`arms.APPLICABLE` or `arms.NOT_APPLICABLE`), its sets' SHA-256, and each arm's
    configurations, written `slot=variant` with their slots sorted (none: the board names no
    configuration as not measured)."""

    digest: str
    arms: Mapping[str, str]
    sets: Mapping[str, str]
    configurations: Mapping[str, Sequence[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, pack: Pack) -> Current:
        datasets = pack.manifest.evaluation.datasets
        chosen = arms.arms(pack).values()
        return cls(
            digest=pack.digest,
            arms={arm.id: arm.status for arm in chosen},
            sets={
                name: hashlib.sha256(pack.read_bytes(path)).hexdigest()
                for name, path in sorted(datasets.items())
            },
            configurations={
                arm.id: tuple(_written(c.providers) for c in arm.configurations) for arm in chosen
            },
        )


def stale_after(found: Mapping[str, Any]) -> str:
    """The date an entry goes stale, `STALE_AFTER_DAYS` after it was measured."""
    measured = datetime.date.fromisoformat(found["scope"]["measured_on"])
    return (measured + datetime.timedelta(days=method.STALE_AFTER_DAYS)).isoformat()


def short_of_evidence(found: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The critical labels of an entry whose distinct positives are short of the method's
    minimum: each with its question, label, distinct positives and the minimum."""
    return [
        {
            "question": item["question"],
            "label": item["label"],
            "positives": item["distinct"]["positives"],
            "needed": item["evidence"]["needed"],
        }
        for item in found["figures"]["critical"]
        if item["evidence"]["status"] != SUFFICIENT
    ]


def board(
    entries: Sequence[Mapping[str, Any]],
    current: Mapping[str, Current],
    canaries: Sequence[Mapping[str, Any]] = (),
    *,
    disclosures: runs.Disclosures | None = None,
) -> dict[str, Any]:
    """The board of `entries`: one table per pack and set (by pack id, then set name), each with
    its ranking (`bands.ranking`: off on a public set, a regression check, not a ranking), the
    insufficient-evidence banner when any published entry's critical label is short, each arm's
    status (measured, "not measured" or "not applicable", from `current`), the configurations
    of a measured arm that have no entry here (`not_measured`, from `current`), and its entries
    in a listing order (by arm, then configuration, then date), never an order of any figure,
    each with its grade (`grade.grade`: None on a public set) and whether it is published
    (`published`, against `disclosures`, the disclosures file as it is now; None: there is
    none, so no remote entry is). An unpublished entry shows its scope and none of its figures.
    A hidden set's arms are ranked only from their ledgers, which a board does not hold, so a
    hidden table of two or more entries is refused. Each entry's marks are worked out here and
    never stored: "unsigned", "method draft, not in effect", why it is stale (its pack, set or
    method is not `current`'s; another model reported, `_another_model_marks`; a canary's drift,
    `_drift_marks`, from `canaries`, `mendari.canary/1`) and the date it goes stale.

    Each entry is read as its canonical bytes hold it (`ledger.canonical`: keys sorted), so the
    board of entries just built is the board of the same entries read back from a store: a
    configuration is written `slot=variant`, its slots in sorted order."""
    entries = [json.loads(canonical(dict(found))) for found in entries]
    canaries = [json.loads(canonical(dict(canary))) for canary in canaries]
    for canary in canaries:
        if canary.get("format") != CANARY_FORMAT:
            raise MendariError(f"not a {CANARY_FORMAT} canary")
    reported = _runs_reported(entries, canaries)
    drifts = _drifts(canaries)
    tables: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for found in entries:
        if found.get("format") != FORMAT:
            raise MendariError(f"not a {FORMAT} entry")
        scope = found["scope"]
        tables.setdefault((scope["pack"]["id"], scope["set"]["name"]), []).append(found)
    shown = []
    for (pack_id, set_name), members in sorted(tables.items()):
        now = current.get(pack_id)
        listed = sorted(members, key=_listing)
        short = [
            {**label, "configuration": _label(found)}
            for found in listed
            if published(found, disclosures)
            for label in short_of_evidence(found)
        ]
        measured = {found["arm"] for found in listed}
        shown_configurations = {(found["arm"], _label(found)) for found in listed}
        statuses = []
        not_measured = []
        for arm, name in arms.ARMS.items():
            if arm in measured:
                status = MEASURED
                held = () if now is None else now.configurations.get(arm, ())
                not_measured += [
                    {"arm": arm, "name": name, "configuration": label}
                    for label in held
                    if (arm, label) not in shown_configurations
                ]
            elif now is not None and now.arms.get(arm) == arms.NOT_APPLICABLE:
                status = arms.NOT_APPLICABLE
            else:
                status = NOT_MEASURED
            statuses.append({"arm": arm, "name": name, "status": status})
        shown.append(
            {
                "pack": pack_id,
                "set": set_name,
                "ranking": bands.ranking(listed),
                "evaluation": NOT_CLINICAL,
                "banner": {"short": short} if short else None,
                "arms": statuses,
                "not_measured": not_measured,
                "entries": [
                    {
                        "entry": dict(found),
                        "arm_name": arms.ARMS.get(found["arm"], found["arm"]),
                        "configuration": _label(found),
                        "grade": grade.grade(found),
                        "marks": _marks(found, now, reported, drifts),
                        "stale_after": stale_after(found),
                        "published": published(found, disclosures),
                    }
                    for found in listed
                ],
            }
        )
    return {"method": {"version": method.METHOD, "sha256": method.METHOD_SHA256}, "tables": shown}


def _label(found: Mapping[str, Any]) -> str:
    """`slot=variant` pairs, its slots in sorted order: as an entry's canonical bytes hold them,
    whatever order it was built in."""
    return _written(found["configuration"]["providers"])


def _written(providers: Mapping[str, str]) -> str:
    """`slot=variant` pairs, the slots sorted, as `bands.configuration_name` writes an entry's."""
    return ",".join(f"{slot}={providers[slot]}" for slot in sorted(providers))


def _listing(found: Mapping[str, Any]) -> tuple[Any, ...]:
    order = list(arms.ARMS)
    position = order.index(found["arm"]) if found["arm"] in order else len(order)
    return (position, _label(found), found["scope"]["measured_on"], found["body_sha256"])


def published(found: Mapping[str, Any], disclosures: runs.Disclosures | None) -> bool:
    """Whether an entry's figures may be shown: a local configuration's always ("no model
    provider"); a remote one's when every remote type it calls had a statement when it was
    stored, and `disclosures` (the disclosures file as it is now; None: there is none) still
    holds that statement, word for word. An entry whose statement is pending, or was taken out
    or rewritten since (a commercial relationship begun, say), shows its scope and none of its
    figures, on the page and in `web/`, until it is run again with the file's statement."""
    disclosure = found["scope"]["disclosure"]
    if disclosure["status"] == DISCLOSURE_PENDING:
        return False
    now = {} if disclosures is None else disclosures.statements
    return all(now.get(kind) == text for kind, text in disclosure["statements"].items())


def _reported(found: Mapping[str, Any]) -> dict[str, list[str]]:
    """The models each model type reported in an entry's runs: across its replicates when it
    holds them (`figures.replicates.models`), else in its one run. A type none of its kept
    answers named (its calls failed, or a composite kept none of them) is not listed."""
    held = found["figures"].get("replicates")
    by_type = found["scope"]["models"]["by_type"] if held is None else held["models"]
    return {kind: list(models) for kind, models in by_type.items()}


def _another_model(
    reported: Mapping[str, Sequence[str]], held: Mapping[str, Sequence[str]]
) -> bool:
    """Whether `reported` names other models than `held` for a model type both name. A run that
    named no model of a type reports no other model of it: no answer, no model."""
    return any(list(reported[kind]) != list(held[kind]) for kind in reported if kind in held)


def _runs_reported(
    entries: Sequence[Mapping[str, Any]], canaries: Sequence[Mapping[str, Any]]
) -> list[tuple[str, dict[str, list[str]]]]:
    """Every run the board knows of, each entry's and each canary's, with its day and the
    models each model type reported in it, of whatever pack or configuration: the same type
    calls the same deployment's model (`JEV_MODEL`, `LLM_MODEL`)."""
    found = [(entry["scope"]["measured_on"], _reported(entry)) for entry in entries]
    found += [
        (canary["measured_on"], {kind: list(models) for kind, models in canary["models"].items()})
        for canary in canaries
    ]
    return found


def _drifts(canaries: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str, str]]:
    """Each canary that shows drift: its pack, its arm, the day of the replicates it was
    compared with and its own day."""
    return [
        (
            canary["pack"]["id"],
            canary["arm"],
            canary["against"]["measured_on"],
            canary["measured_on"],
        )
        for canary in canaries
        if canary["drift"]
    ]


def _another_model_marks(
    found: Mapping[str, Any], reported: Sequence[tuple[str, Mapping[str, list[str]]]]
) -> bool:
    """Whether an entry is "stale: another model reported" (the method's section 12): its own
    runs (its replicates) reported more than one model of a type, or a run of the same model
    type, of any pack or configuration, a result's or a canary's, measured the same day or
    later (a day holds no order), reported another model of a type the entry's runs reported.
    A run that reported no model of a type (every call failed) reports no other."""
    own, on = _reported(found), found["scope"]["measured_on"]
    if any(len(models) > 1 for models in own.values()):
        return True
    return any(day >= on and _another_model(other, own) for day, other in reported)


def _drift_marks(found: Mapping[str, Any], drifts: Sequence[tuple[str, str, str, str]]) -> bool:
    """Whether an entry is "stale: canary drift": a canary of its pack's arm (the arm's declared
    remote configuration, now or before) shows drift, and the entry is a remote configuration's
    result of that arm, on any set, measured before the canary's day, or no later than the
    replicates the canary was compared with. A local configuration is deterministic and never
    drifts; a result run again from the canary's day on is not marked by it."""
    if found["configuration"]["local"]:
        return False
    pack_id, on = found["scope"]["pack"]["id"], found["scope"]["measured_on"]
    return any(
        (pack_id, found["arm"]) == (of, arm) and (on < canary_on or on <= against_on)
        for of, arm, against_on, canary_on in drifts
    )


def _marks(
    found: Mapping[str, Any],
    now: Current | None,
    reported: Sequence[tuple[str, Mapping[str, list[str]]]] = (),
    drifts: Sequence[tuple[str, str, str, str]] = (),
) -> list[str]:
    scope = found["scope"]
    marks = []
    if "signature" not in found:
        marks.append(UNSIGNED)
    if not scope["method"]["in_effect"]:
        marks.append(DRAFT)
    if now is None or scope["pack"]["digest"] != now.digest:
        marks.append(STALE_PACK)
    if now is None or now.sets.get(scope["set"]["name"]) != scope["set"]["sha256"]:
        marks.append(STALE_SET)
    if scope["method"]["sha256"] != method.METHOD_SHA256:
        marks.append(STALE_METHOD)
    if _drift_marks(found, drifts):
        marks.append(STALE_DRIFT)
    if _another_model_marks(found, reported):
        marks.append(STALE_MODEL)
    marks.append(f"stale after {stale_after(found)}")
    return marks


# --------------------------------------------------------------------------------------------
# The canary
# --------------------------------------------------------------------------------------------


def canary(
    ledger: Ledger,
    run: Mapping[str, Any],
    *,
    against: Mapping[str, Any],
    headline: Ledger,
    dataset: Dataset | None,
    size: int,
    seed: int,
) -> dict[str, Any]:
    """The `mendari.canary/1` record of a canary (the method's section 12): a seeded draw of
    `size` cases (`method.CANARY_SIZE`) from a pack's `test` set, decided once by hand outside
    CI, by a remote configuration whose current result on that set (`against`) holds replicates;
    `headline` is that result's replicate 1 ledger. From its `ledger` and its `run` record:

    - its flip rate, k of n drawn cases whose primary prediction or outcome differs from
      replicate 1's on the same case (matched by ledger hash), with its 95% interval;
    - `drift`: that interval's lower bound is above the upper bound of the replicates' flip
      rate's (`runs.drift`);
    - `another_model`: for a model type both it and the replicates name, it names other models
      than they did (`figures.replicates.models`). A type it named no model of (its calls
      failed) is not another model.

    A board marks "stale: canary drift" the remote results of the canary's pack and arm measured
    before its day, or no later than `against`; and "stale: another model reported" every
    result, of any pack or configuration, measured no later than the canary whose runs named
    other models of a type it names (`board`). `dataset` is the pack's `test` set as it is now,
    when it is still the set `against` was measured on: the canary's cases must then be its
    seeded draw (`runs.draw`). Refused, naming hashes and never a case: a size that is not the
    method's, an `against` with no replicates or not on `test`, a `headline` that is not its
    ledger, a run record that is not the canary's ledger's, or a canary of another pack, set or
    configuration, or of cases its replicates do not hold."""
    record = runs.check_run(run)
    if size != method.CANARY_SIZE:
        raise MendariError(
            f"a canary under {method.METHOD} is {method.CANARY_SIZE} cases, not {size}"
        )
    held = against.get("figures", {}).get("replicates")
    scope = against.get("scope", {})
    if against.get("format") != FORMAT or held is None:
        raise MendariError("a canary is compared with a result that holds replicates")
    if scope.get("set", {}).get("name") != CANARY_SET:
        raise MendariError(f"a canary is drawn from the `{CANARY_SET}` set's replicates")
    if headline.id != against["inputs"]["ledger"]:
        raise MendariError(f"{headline.id} is not the replicate 1 ledger of the result")
    problem = runs.disagreement(record, ledger)
    if problem is not None:
        raise MendariError(f"the canary's run record is not its ledger's: {problem}")
    if len(ledger.cases) != size:
        raise MendariError(f"the canary holds {len(ledger.cases)} cases, not {size}")
    if dataset is not None and dataset.sha256 == scope["set"]["sha256"]:
        hashes = [case_hash(dataset.case_key, found["id"]) for found in dataset.records]
        drawn = [hashes[at] for at in runs.draw(hashes, size, seed)]
        if drawn != [case["c"] for case in ledger.cases]:
            raise MendariError("the canary's cases are not its seed's draw from the set")
    k, n = runs.canary_flips(ledger, headline)
    replicated = held["flip_rate"]
    models = record["models"]
    # Only a type both name: a canary whose calls all failed names no model, which is not
    # another one, and a type the replicates named none of has none to differ from.
    another = _another_model(models, held["models"])
    found = {
        "format": CANARY_FORMAT,
        "pack": dict(scope["pack"]),
        "set": {"name": CANARY_SET, "sha256": scope["set"]["sha256"]},
        "arm": against["arm"],
        "configuration": dict(against["configuration"]),
        "draw": {"size": size, "seed": seed},
        "measured_on": record["date"],
        "inputs": {
            "ledger": ledger.id,
            "run": runs.run_sha256(record),
            "method": method.METHOD_SHA256,
        },
        "against": {
            "result": against["body_sha256"],
            "measured_on": scope["measured_on"],
            "flip_rate": replicated,
        },
        "models": models,
        "flip_rate": stats.proportion(k, n, method.LEVEL).to_dict(),
        "drift": runs.drift((k, n), (int(replicated["k"]), int(replicated["n"]))),
        "another_model": another,
        "evaluation": NOT_CLINICAL,
    }
    if found["measured_on"] < scope["measured_on"]:
        raise MendariError("a canary is run after the replicates it is compared with")
    return {**found, "body_sha256": body_sha256(found)}
