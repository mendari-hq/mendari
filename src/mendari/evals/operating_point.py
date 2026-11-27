# SPDX-License-Identifier: Apache-2.0
"""An evaluation's operating point (`mendari.operating-point/1`): what the agent's automation
threshold lets through on these cases, each risk with its exact upper bound, and, when the run
is given targets, the lowest threshold this evidence supports, with a stated guarantee.

**The risks**, counted as the report counts them, so the two never disagree:

- critical misses: a critical label's cases that are automated, so no person sees them (the
  report's `auto_missed`), as a share of that label's cases and as a share of all cases. A
  target is on one of the two (the label's own cases unless it says all) and holds for each
  critical label on its own, so a rare label cannot hide behind a common one;
- false automation: cases automated with a wrong recommendation, as a share of the cases
  labelled for the primary question (the report's row of that name).

**What a threshold would do** is what the policy does at it: each case's policy is applied again
to what it decided from (`policy.PolicyInputs`, kept by the run in memory), under a manifest that
differs in `policy.thresholds.auto` alone. Rules, per-label limits, risk caps and provider
failures decide as they did, and no provider is asked again. At the manifest's own threshold
that gives the run's outcomes back case for case, which is checked before anything is
recommended. A label with a threshold of its own keeps it.

**The recommendation** is fixed-sequence testing (Bauer 1991; used for risk control by
Angelopoulos, Bates, Candès, Jordan and Lei, "Learn then Test", 2021). The thresholds tested
are fixed before a case is seen (`sequence`): 1.00, 0.99 and so on down to the review
threshold. From the strictest down, a threshold is certified when every targeted risk's exact
one-sided upper bound (Clopper-Pearson, `upper_bound`) is within its target, and the walk
stops at the first that is not. A threshold is reached only through every stricter one, so the
chance that any certified threshold's true risk is above its target is at most the error level,
however many thresholds were tried on the same cases: the first threshold whose true risk is
above a target is certified only if that one risk's own test errs.

With two targets the error level is split between them (each is tested at half of it). Within
the critical target every critical label is tested at that target's level and all must pass;
that needs no further split, by the same argument.

The lowest certified threshold automates the most. Where several certified thresholds automate
the same number of these cases, the strictest of them is the one recommended: the set holds no
case between them. The manifest's own threshold counts as supported when it is at or above the
lowest certified one (the policy automates no more at a higher threshold).

**What the guarantee assumes**, and says: the cases resemble the ones the agent will see, the
labels are right, and the agent, its configuration and its version do not change. A case that
is in the set twice counts twice; the block says how many cases repeat another (`repeated`).

The block is advice. Nothing here edits a manifest; `change` is the one line an author would.
It is kept in the report's `run` (`harness.build_evaluation`), outside the result that
`result_sha256` covers: it depends on the run's options and on answers the ledger rounds, so it
is not something a reader of the ledger can recompute. No case text and no case id is in it.

Standard library only.
"""

from __future__ import annotations

import math
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from mendari.core.policy import PolicyInputs
from mendari.core.spec import AgentManifest, Thresholds
from mendari.errors import MendariError
from mendari.evals import stats
from mendari.evals.ledger import Ledger
from mendari.evals.result import TRUTH, false_automation

FORMAT = "mendari.operating-point/1"
CONFIDENCE = 0.95
OF_CRITICAL, OF_ALL = "critical", "all"
STEPS = 100  # the thresholds tested are hundredths
SETTING = "policy.thresholds.auto"
# A run's status: what it can say about a threshold.
NO_TARGET = "no_target"  # only the bound at the manifest's threshold
SUPPORTED = "supported"  # the manifest's threshold is certified, and none certified automates more
RECOMMENDED = "recommended"  # another threshold is the one these cases support
NONE_CERTIFIED = "none_certified"  # no threshold that automates a case is certified
NOTHING_AUTOMATED = "nothing_automated"  # the policy automates no case at any threshold
NO_REPLAY = "no_replay"  # the run did not keep what its policy decided from
CRITICAL_MISS, FALSE_AUTOMATION = "critical_miss", "false_automation"
# What each of a block's lines is (its `kinds`, in the order of its `lines`): a threshold's
# counts; where the walk ended; critical cases automated at a threshold chosen for another risk;
# the guarantee; the line to change; labels with a threshold of their own; the cases a target
# needs; what no threshold fixes; cases that repeat an input; how each bound is taken.
POINT, WALK, UNHELD, GUARANTEE, CHANGE = "point", "walk", "unheld", "guarantee", "change"
LABELS, NEEDS, CANNOT_FIX, REPEATED, METHOD = "labels", "needs", "cannot_fix", "repeated", "method"
_AUTO = "AUTO"
_FIXED = ("ABSTAIN", "BLOCK")  # what a case is given when no policy runs on it
_MIN_SHARE = 1e-6  # a target under one in a million needs more cases than a set holds
_MAX_CONFIDENCE = 0.9999
_WIDTH = 100


def share(text: str, *, whole: bool = False) -> float:
    """A share as people write one, `1%` or `0.01`: a number above 0 and below 1, which is what
    a target is. With `whole`, 0 and 1 are shares too (`0%`, `100%`): what a limit on a run's
    own result may be (`mendari eval --min-...`, `--max-...`), read the same way."""
    raw = text.strip()
    try:
        value = float(raw[:-1]) / 100.0 if raw.endswith("%") else float(raw)
    except ValueError:
        raise ValueError("a share is written 1% or 0.01") from None
    if whole:
        if not (math.isfinite(value) and 0.0 <= value <= 1.0):
            raise ValueError("a share is from 0 to 100%: write 90% or 0.9")
    elif not (math.isfinite(value) and 0.0 < value < 1.0):
        raise ValueError("a share is above 0 and below 100%: write 1% or 0.01")
    return value


@dataclass(frozen=True)
class Targets:
    """What a recommended threshold must keep to: the most critical cases that may be automated
    (`critical_miss`, a share of each critical label's cases, or of all cases with
    `critical_miss_of="all"`), the most cases that may be automated with a wrong recommendation
    (`false_automation`, a share of the labelled cases), and the confidence all of it is stated
    with. Each target is optional; with none, nothing is recommended."""

    critical_miss: float | None = None
    critical_miss_of: str = OF_CRITICAL
    false_automation: float | None = None
    confidence: float = CONFIDENCE

    def __post_init__(self) -> None:
        for name in (CRITICAL_MISS, FALSE_AUTOMATION):
            value = getattr(self, name)
            if value is not None and not _MIN_SHARE <= value < 1.0:
                raise ValueError("a target is a share of cases, from 0.0001% to under 100%")
        if self.critical_miss_of not in (OF_CRITICAL, OF_ALL):
            raise ValueError(f"critical_miss_of is {OF_CRITICAL!r} or {OF_ALL!r}")
        if not 0.5 <= self.confidence <= _MAX_CONFIDENCE:
            raise ValueError("the confidence level is from 50% to 99.99%")

    @property
    def given(self) -> int:
        """How many targets there are: 0, 1 or 2."""
        return (self.critical_miss is not None) + (self.false_automation is not None)

    @property
    def level(self) -> float:
        """The one-sided confidence each bound is taken at: the error level, split between the
        targets when there are two."""
        return 1.0 - (1.0 - self.confidence) / max(1, self.given)

    def to_dict(self) -> dict[str, Any]:
        return {
            CRITICAL_MISS: None
            if self.critical_miss is None
            else {"at_most": self.critical_miss, "of": self.critical_miss_of},
            FALSE_AUTOMATION: None
            if self.false_automation is None
            else {"at_most": self.false_automation},
        }


# --------------------------------------------------------------------------------------------
# The exact bound, and how many cases a target needs
# --------------------------------------------------------------------------------------------


@lru_cache(maxsize=8192, typed=True)
def upper_bound(k: int, n: int, level: float) -> float:
    """The exact one-sided upper bound of a proportion seen as k of n (Clopper & Pearson 1934):
    the rate at which k or fewer of n has probability 1 - level, the Beta(k + 1, n - k)
    quantile at `level`; 1.0 when k = n. With confidence `level` the true rate is at most it.

    `upper_bound(k, n, level) <= target` is the exact binomial test of "the rate is above
    `target`" at error level 1 - level: P(Bin(n, target) <= k) <= 1 - level."""
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (k, n)):
        raise TypeError("k and n must be integers")
    if n < 1 or not 0 <= k <= n:
        raise ValueError("need n >= 1 and 0 <= k <= n")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    return 1.0 if k == n else stats.beta_ppf(k + 1, n - k, level)


def cases_needed(target: float, level: float, k: int = 0) -> int:
    """The fewest cases of which k may have the risk for its upper bound to be within
    `target`: with none (k = 0), the smallest n with 1 - (1 - level)^(1/n) <= target, which is
    299 for 1% at 95%."""
    if not 0.0 < target < 1.0:
        raise ValueError("target must be in (0, 1)")
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError("k must be a count")
    if k == 0:
        # The closed form, then held to the bound itself either side of a rounding.
        n = max(1, math.ceil(math.log(1.0 - level) / math.log1p(-target)))
        while upper_bound(0, n, level) > target:
            n += 1
        while n > 1 and upper_bound(0, n - 1, level) <= target:
            n -= 1
        return n
    low, high = k, 2 * (k + 1)  # k of k is a bound of 1: never within a target
    while upper_bound(k, high, level) > target:
        low, high = high, 2 * high
    while high - low > 1:  # the bound falls as n grows
        middle = (low + high) // 2
        if upper_bound(k, middle, level) <= target:
            high = middle
        else:
            low = middle
    return high


def sequence(review: float) -> list[float]:
    """The thresholds tested, in the order tested, fixed before any case is seen: 1.00 down to
    the review threshold in hundredths (a lower one would decide nothing: under the review
    threshold a case is never automated)."""
    lowest = math.ceil(review * STEPS - 1e-9)
    return [step / STEPS for step in range(STEPS, lowest - 1, -1) if step / STEPS >= review]


# --------------------------------------------------------------------------------------------
# Cases, and what a threshold does to them
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One case as the operating point reads it: its label for the primary question, the
    critical labels it is a case of (by their place in the manifest's list), what the run decided
    and what its policy decided from. No text and no id."""

    truth: str | None
    critical: tuple[int, ...]
    outcome: str
    recommendation: str | None
    inputs: PolicyInputs | None = None

    @property
    def replays(self) -> bool:
        """Whether its outcome at another threshold is known: its policy can be applied again,
        or no policy ran (an input refused, a record not read) and no threshold changes it."""
        return self.inputs is not None or self.outcome in _FIXED


@dataclass(frozen=True)
class Tally:
    """The counts at one threshold: cases, how many are automated, the cases labelled for the
    primary question and how many of those are automated with a wrong recommendation, and per
    critical label (its cases automated, its cases)."""

    cases: int
    automated: int
    labelled: int
    wrong: int
    critical: tuple[tuple[int, int], ...]


Decided = tuple[str, str | None]  # an outcome and a recommendation


def cases_of(ledger: Ledger, results: Sequence[Any]) -> list[Case]:
    """A run's cases: what its ledger counts each as, with what each decision kept
    (`DecisionResult.policy_inputs`), in the ledger's order."""
    primary = ledger.header["primary"]
    cases = []
    for line, result in zip(ledger.cases, results, strict=True):
        entry = line["q"].get(primary)
        cases.append(
            Case(
                truth=None if entry is None else entry[TRUTH],
                critical=tuple(line["k"]),
                outcome=line["o"],
                recommendation=line["r"],
                inputs=getattr(result, "policy_inputs", None),
            )
        )
    return cases


def at_threshold(manifest: AgentManifest, threshold: float) -> AgentManifest:
    """The manifest with one setting changed: `policy.thresholds.auto`."""
    thresholds = Thresholds(auto=threshold, review=manifest.policy.thresholds.review)
    policy = manifest.policy.model_copy(update={"thresholds": thresholds})
    return manifest.model_copy(update={"policy": policy})


def decided(manifest: AgentManifest, cases: Sequence[Case]) -> list[Decided]:
    """Each case's outcome and recommendation under `manifest`: its policy applied again to what
    it decided from, or what the run gave it when no policy ran."""
    out: list[Decided] = []
    for case in cases:
        if case.inputs is None:
            out.append((case.outcome, case.recommendation))
        else:
            result = case.inputs.apply(manifest)
            out.append((result.outcome.value, result.recommendation))
    return out


def tally(cases: Sequence[Case], outcomes: Sequence[Decided], labels: int) -> Tally:
    """The counts of `cases` decided as `outcomes`, as `result.result_from_ledger` counts a
    run: automated is AUTO, and wrong is a recommendation that is not the case's label."""
    automated = wrong = labelled = 0
    critical = [[0, 0] for _ in range(labels)]
    for case, (outcome, recommendation) in zip(cases, outcomes, strict=True):
        auto = outcome == _AUTO
        automated += auto
        if case.truth is not None:
            labelled += 1
            wrong += auto and recommendation != case.truth
        for index in case.critical:
            critical[index][0] += auto
            critical[index][1] += 1
    return Tally(len(cases), automated, labelled, wrong, tuple((k, n) for k, n in critical))


def tally_of(report: Mapping[str, Any]) -> Tally:
    """The counts a report states at the threshold it ran with."""
    decisions = report["decisions"]
    counts = decisions["outcome_counts"]
    wrong = false_automation(report)
    return Tally(
        cases=sum(counts.values()),
        automated=counts.get(_AUTO, 0),
        labelled=wrong.n,
        wrong=wrong.k,
        critical=tuple((item["auto_missed"], item["n"]) for item in decisions["critical"]),
    )


# --------------------------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    """One risk held to its target at one threshold: k of n, of a critical label (`label`, its
    place in the manifest's list) or of false automation (None)."""

    label: int | None
    k: int
    n: int
    target: float

    def passes(self, level: float) -> bool:
        """Whether its upper bound is within the target. With no case to count, it is not."""
        return self.n > 0 and upper_bound(self.k, self.n, level) <= self.target

    @property
    def above(self) -> bool:
        """Whether the share seen is itself above the target: more cases alone would not help."""
        return self.k > self.target * self.n + 1e-9


def held(found: Tally, targets: Targets) -> list[Held]:
    """Every risk a target is on, with its counts in `found`: each critical label, then false
    automation."""
    risks = []
    if targets.critical_miss is not None:
        for index, (k, n) in enumerate(found.critical):
            of = n if targets.critical_miss_of == OF_CRITICAL else found.cases
            risks.append(Held(index, k, of, targets.critical_miss))
    if targets.false_automation is not None:
        risks.append(Held(None, found.wrong, found.labelled, targets.false_automation))
    return risks


def certified(found: Tally, targets: Targets) -> bool:
    """Whether a threshold with these counts is certified: every targeted risk's upper bound,
    at the targets' level, is within its target."""
    return all(risk.passes(targets.level) for risk in held(found, targets))


def walk(tallies: Callable[[int], Tally], length: int, targets: Targets) -> int:
    """How many thresholds are certified, from the strictest down, before the first that is
    not: `tallies(j)` is the counts at the j-th threshold of the sequence. The certified ones
    are the first that many."""
    count = 0
    while count < length and certified(tallies(count), targets):
        count += 1
    return count


@dataclass(frozen=True)
class Found:
    """What a run's cases say about a threshold. `tested` is the sequence, `passed` how many of
    it are certified, and `tallies` the counts of each certified threshold and of the one the
    walk stopped at. With a threshold to state (`SUPPORTED`, `RECOMMENDED`): `threshold` and
    its `tally`, and whether the manifest's own is `supported`. With none (`NONE_CERTIFIED`):
    the risks that `failed` where the walk stopped, and those `beyond` any threshold's reach,
    counted at `first`, the strictest threshold that automates a case."""

    status: str
    tested: Sequence[float] = ()
    passed: int = 0
    tallies: Sequence[Tally] = ()
    threshold: float | None = None
    tally: Tally | None = None
    supported: bool | None = None
    failed: Sequence[Held] = ()
    first: float | None = None
    beyond: Sequence[Held] = ()

    @property
    def stopped_at(self) -> float | None:
        return self.tested[self.passed] if self.passed < len(self.tested) else None


def recommend(manifest: AgentManifest, cases: Sequence[Case], targets: Targets) -> Found:
    """Walk the sequence on `cases` and say what it found."""
    labels = len(manifest.evaluation.critical)
    recorded: list[Decided] = [(case.outcome, case.recommendation) for case in cases]
    # Before anything is recommended: the policy, applied again under the manifest as it is,
    # gives every case the outcome and the recommendation the run gave it.
    if not all(case.replays for case in cases) or decided(manifest, cases) != recorded:
        return Found(NO_REPLAY)
    now = manifest.policy.thresholds.auto
    current = tally(cases, recorded, labels)
    tested = sequence(manifest.policy.thresholds.review)
    tallies: dict[int, Tally] = {}

    def at(index: int) -> Tally:
        if index not in tallies:
            there = at_threshold(manifest, tested[index])
            tallies[index] = tally(cases, decided(there, cases), labels)
        return tallies[index]

    passed = walk(at, len(tested), targets)
    walked = [at(index) for index in range(min(passed + 1, len(tested)))]
    most = max((at(index).automated for index in range(passed)), default=0)
    if most:
        # Of the certified thresholds, the strictest that automates the most of these cases.
        pick = next(index for index in range(passed) if at(index).automated == most)
        supported = now >= tested[passed - 1]
        if supported and current.automated >= most:
            return Found(SUPPORTED, tested, passed, walked, now, current, supported)
        return Found(RECOMMENDED, tested, passed, walked, tested[pick], at(pick), supported)
    everywhere = [at(index) for index in range(len(tested))]
    automating = [index for index, found in enumerate(everywhere) if found.automated]
    if cases and not automating:
        # No threshold automates any of these cases (a cap, a rule or a label's limit sends each
        # to a person): there is nothing to hold to a target, whatever the cases could show.
        return Found(NOTHING_AUTOMATED, tested, passed, walked)
    failed = [risk for risk in held(at(passed), targets) if not risk.passes(targets.level)]
    # A risk whose share is above its target at every threshold that automates a case is one
    # no threshold fixes: said with its counts at the strictest of those thresholds.
    first = None
    beyond: list[Held] = []
    if automating:
        first = tested[automating[0]]
        beyond = [
            risk
            for place, risk in enumerate(held(everywhere[automating[0]], targets))
            if all(held(everywhere[index], targets)[place].above for index in automating)
        ]
    return Found(
        NONE_CERTIFIED,
        tested,
        passed,
        walked,
        supported=False,
        failed=failed,
        first=first,
        beyond=beyond,
    )


# --------------------------------------------------------------------------------------------
# The block
# --------------------------------------------------------------------------------------------


def check(manifest: AgentManifest, targets: Targets | None) -> None:
    """Refuse a target the agent has nothing to hold to it, before a run is paid for: a target
    on critical misses for an agent that declares no critical label."""
    if targets is None or targets.critical_miss is None or manifest.evaluation.critical:
        return
    raise MendariError(
        f"{manifest.id} declares no critical label (`evaluation.critical` in its manifest), so "
        "there is no critical miss to hold to a target"
    )


def build(
    manifest: AgentManifest,
    ledger: Ledger,
    results: Sequence[Any],
    targets: Targets | None = None,
) -> dict[str, Any]:
    """The operating point of one run, whose decisions are `results` in the ledger's order.
    With no target, the bound at the manifest's threshold; with any, also the threshold the
    cases support, or why none is."""
    check(manifest, targets)
    targets = targets or Targets()
    names = [(str(question), str(label)) for question, label in ledger.header["critical"]]
    cases = cases_of(ledger, results)
    recorded: list[Decided] = [(case.outcome, case.recommendation) for case in cases]
    current = tally(cases, recorded, len(names))
    found = recommend(manifest, cases, targets) if targets.given else Found(NO_TARGET)
    # A case whose input is another's, to the byte, is not new evidence: counted, and said.
    hashes = [h for h in (getattr(result, "input_hash", None) for result in results) if h]
    repeated = len(hashes) - len(set(hashes))
    return block_of(manifest, names, targets, current, found, repeated)


def _figure(k: int, n: int, level: float) -> dict[str, Any]:
    upper = None if n == 0 else round(upper_bound(k, n, level), stats.DIGITS)
    return {"k": k, "n": n, "upper": upper}


def _point(
    threshold: float, found: Tally, names: Sequence[tuple[str, str]], level: float
) -> dict[str, Any]:
    """A threshold's counts as the block holds them: each risk as k of n with its upper bound."""
    return {
        "threshold": threshold,
        "automated": {"k": found.automated, "n": found.cases},
        "critical": [
            {
                "question": question,
                "label": label,
                "of_critical": _figure(k, n, level),
                "of_all": _figure(k, found.cases, level),
            }
            for (question, label), (k, n) in zip(names, found.critical, strict=True)
        ],
        FALSE_AUTOMATION: _figure(found.wrong, found.labelled, level),
    }


def block_of(
    manifest: AgentManifest,
    names: Sequence[tuple[str, str]],
    targets: Targets,
    current: Tally,
    found: Found,
    repeated: int = 0,
) -> dict[str, Any]:
    """The block: the counts at the manifest's threshold (`current`), what the walk `found`, and
    the sentences that say both. `names` are the critical labels, as (question, label), and
    `repeated` how many cases have the same input as an earlier one."""
    level = targets.level
    now = manifest.policy.thresholds.auto
    recommended = change = None
    if found.threshold is not None and found.tally is not None:
        recommended = _point(found.threshold, found.tally, names, level)
        if found.status == RECOMMENDED:
            line = f"auto: {threshold_text(found.threshold)}"
            change = {"setting": SETTING, "from": now, "to": found.threshold, "line": line}
    words = _Words(names, targets)
    block: dict[str, Any] = {
        "format": FORMAT,
        "status": found.status,
        "confidence": targets.confidence,
        "level": level,
        "targets": targets.to_dict(),
        "current": _point(now, current, names, level),
        "current_supported": found.supported,
        "recommended": recommended,
        "change": change,
        "walk": _walked(found),
        # With no threshold certified: per risk that lacks cases, how many would show it; and
        # the risks no threshold fixes, counted at the strictest threshold that automates a case.
        "needs": [
            {**words.named(risk), "cases": cases_needed(risk.target, level, risk.k)}
            for risk in found.failed
            if not risk.above
        ],
        "cannot_fix": [{**words.named(risk), "threshold": found.first} for risk in found.beyond],
        "repeated": repeated,
    }
    own = sorted(
        n for n, label in manifest.policy.labels.items() if label.auto_threshold is not None
    )
    block.update(words.say(block, found, own))
    return block


def _walked(found: Found) -> dict[str, Any] | None:
    """The thresholds the walk tested, in order, with the counts each was judged on: every
    certified one, then the one it stopped at."""
    if not found.tested:
        return None
    return {
        "from": found.tested[0],
        "to": found.tested[-1],
        "step": 1 / STEPS,
        "certified": found.passed,
        "stopped_at": found.stopped_at,
        "tested": [
            {
                "threshold": found.tested[index],
                "automated": item.automated,
                "critical": [k for k, _ in item.critical],
                FALSE_AUTOMATION: item.wrong,
                "certified": index < found.passed,
            }
            for index, item in enumerate(found.tallies)
        ],
    }


# --------------------------------------------------------------------------------------------
# The words
# --------------------------------------------------------------------------------------------


def threshold_text(value: float) -> str:
    """A threshold as a manifest writes it: two decimals, more only when it has more."""
    if abs(value * STEPS - round(value * STEPS)) < 1e-9:
        return f"{value:.2f}"
    return f"{value:.6f}".rstrip("0")


def _percent(value: float) -> str:
    """A target or a level as a percentage, with the decimals it has: 1%, 0.5%, 97.5%."""
    return f"{value * 100:.4f}".rstrip("0").rstrip(".") + "%"


def _at_most(upper: float) -> str:
    """An upper bound as a percentage, rounded up and never down: it stays a bound, and one
    within a target never reads as above it. One decimal from 1% up, more below."""
    percent = upper * 100.0
    digits = 1 if percent >= 1.0 else 2 if percent >= 0.1 else 3 if percent >= 0.01 else 4
    scale = 10**digits
    return f"{math.ceil(percent * scale - 1e-9) / scale:.{digits}f}%"


def _bounded(k: int, n: int, level: float) -> str:
    """`(at most 13.5%)`, from k and n: never from a stored bound."""
    return f"(at most {_at_most(upper_bound(k, n, level))})"


class _Words:
    """A block's sentences: its summary, the lines under it and the guarantee."""

    def __init__(self, names: Sequence[tuple[str, str]], targets: Targets):
        self.names = names
        self.targets = targets
        self.level = targets.level

    def named(self, risk: Held) -> dict[str, Any]:
        """Which risk this is, with its counts and its target, as the block's lists hold it."""
        if risk.label is None:
            return {"risk": FALSE_AUTOMATION, "k": risk.k, "n": risk.n, "at_most": risk.target}
        question, label = self.names[risk.label]
        return {
            "risk": CRITICAL_MISS,
            "question": question,
            "label": label,
            "of": self.targets.critical_miss_of,
            "k": risk.k,
            "n": risk.n,
            "at_most": risk.target,
        }

    def aim(self, joined: str) -> str:
        """The targets as the summary says them: `critical misses at most 1%` (of each critical
        label's cases, unless it says of all cases)."""
        said = []
        if self.targets.critical_miss is not None:
            of = " of all cases" if self.targets.critical_miss_of == OF_ALL else ""
            said.append(f"critical misses at most {_percent(self.targets.critical_miss)}{of}")
        if self.targets.false_automation is not None:
            said.append(f"false automation at most {_percent(self.targets.false_automation)}")
        return joined.join(said)

    def promise(self) -> str:
        """The targets as the guarantee says them, each with what it is a share of."""
        said = []
        if self.targets.critical_miss is not None:
            if self.targets.critical_miss_of == OF_ALL:
                of = "all cases"
            elif len(self.names) > 1:
                of = "each critical label's cases"
            else:
                of = "critical cases"
            said.append(
                f"critical misses stay at or below {_percent(self.targets.critical_miss)} of {of}"
            )
        if self.targets.false_automation is not None:
            said.append(
                "false automation stays at or below "
                f"{_percent(self.targets.false_automation)} of labelled cases"
            )
        return " and ".join(said)

    def counted(self, item: Mapping[str, Any]) -> str:
        """A risk's counts in words: `3 of 114 critical cases (q=label) are automated`."""
        k, n = item["k"], item["n"]
        are = "is" if k == 1 else "are"
        if item["risk"] == FALSE_AUTOMATION:
            return f"{k} of {n} labelled cases {are} automated with a wrong answer"
        named = f"({item['question']}={item['label']})"
        if item["of"] == OF_ALL:
            return f"{k} of {n} cases {are} critical {named} and automated"
        return f"{k} of {n} critical cases {named} {are} automated"

    def needed(self, item: Mapping[str, Any]) -> str:
        """How many cases would show a risk within its target, and how many the set has."""
        few = "none" if item["k"] == 0 else f"no more than {item['k']}"
        if item["risk"] == FALSE_AUTOMATION:
            what = f"labelled cases with {few} automated with a wrong answer"
        elif item["of"] == OF_ALL:
            named = f"({item['question']}={item['label']})"
            what = f"cases with {few} both critical {named} and automated"
        else:
            what = f"critical cases ({item['question']}={item['label']}) with {few} automated"
        has = f"this set has {item['n']}" if item["n"] else "this set has none"
        return (
            f"Showing at most {_percent(item['at_most'])} with {_percent(self.level)} confidence "
            f"needs at least {item['cases']} {what}; {has}."
        )

    def point(self, point: Mapping[str, Any], opening: str) -> str:
        """One threshold in a sentence: how many cases it automates, then each risk."""
        risks = []
        for item in point["critical"]:
            name = f"critical {item['question']}={item['label']}"
            k, n, cases = item["of_critical"]["k"], item["of_critical"]["n"], item["of_all"]["n"]
            if not n:
                risks.append(f"{name}: no such case in this set")
                continue
            risks.append(
                f"{name} automated {k} of {n} critical cases {_bounded(k, n, self.level)}, "
                f"{k} of all {cases} cases {_bounded(k, cases, self.level)}"
            )
        wrong = point[FALSE_AUTOMATION]
        if wrong["n"]:
            bound = _bounded(wrong["k"], wrong["n"], self.level)
            risks.append(f"false automation {wrong['k']} of {wrong['n']} labelled cases {bound}")
        else:
            risks.append("false automation: no labelled case in this set")
        return f"{opening}: {'; '.join(risks)}."

    def stop(self, found: Found) -> str:
        """Where the walk ended, in a sentence."""
        stopped = found.stopped_at
        if stopped is None:
            return (
                "Every threshold tested is certified, down to the review threshold "
                f"{threshold_text(found.tested[-1])}."
            )
        said = []
        for risk in held(found.tallies[found.passed], self.targets):
            if not risk.passes(self.level):
                bound = f" {_bounded(risk.k, risk.n, self.level)}" if risk.n else ""
                said.append(f"{self.counted(self.named(risk))}{bound}")
        where = (
            f"The strictest threshold, {threshold_text(stopped)}, cannot be certified"
            if found.passed == 0
            else f"The walk down from 1.00 stopped at {threshold_text(stopped)}, which cannot be "
            "certified"
        )
        return f"{where}: {'; '.join(said)}."

    def say(self, block: Mapping[str, Any], found: Found, own: Sequence[str]) -> dict[str, Any]:
        """The block's `summary`, its `lines` with what each is (`kinds`), and the `guarantee`."""
        targets, status = self.targets, found.status
        confidence = _percent(targets.confidence)
        current = block["current"]
        now = threshold_text(current["threshold"])
        automated = f"{current['automated']['k']} of {current['automated']['n']} cases"
        here = self.point(
            current, f"At the agent's automation threshold {now}, {automated} are automated"
        )
        said: list[tuple[str, str]] = []  # each line, with its kind
        guarantee = None
        if status == NO_TARGET:
            summary = here
        elif status in (SUPPORTED, RECOMMENDED):
            point = block["recommended"]
            to = threshold_text(point["threshold"])
            portion = point["automated"]["k"] / point["automated"]["n"]
            reached = f"{self.aim(', ')}, {confidence} confidence, {portion:.1%} automated"
            if status == SUPPORTED:
                summary = f"The agent's automation threshold {now} is supported: {reached}."
            else:
                standing = (
                    "also supported" if found.supported else "which these cases do not support"
                )
                summary = (
                    f"Recommended automation threshold {to} (now {now}, {standing}): {reached}."
                )
                would = f"{point['automated']['k']} of {point['automated']['n']} cases"
                said.append((POINT, self.point(point, f"At {to}, {would} would be automated")))
            said += [(POINT, here), (WALK, self.stop(found))]
            unheld = [
                f"{item['of_critical']['k']} of {item['of_critical']['n']} critical cases "
                f"({item['question']}={item['label']})"
                for item in point["critical"]
                if targets.critical_miss is None and item["of_critical"]["k"]
            ]
            if unheld:  # said apart: a threshold chosen for another risk still automates these
                said.append(
                    (
                        UNHELD,
                        f"No target was given for critical misses: at {to}, "
                        f"{' and '.join(unheld)} are automated, and no person sees them.",
                    )
                )
            guarantee = (
                f"With {confidence} confidence, at the automation threshold {to} "
                f"{self.promise()}, provided the cases the agent will see resemble these, the "
                "labels are right, and the agent, its configuration and its version do not "
                "change; after any of those changes, evaluate again."
            )
            said.append((GUARANTEE, f"Guarantee: {guarantee[0].lower()}{guarantee[1:]}"))
            if block["change"] is not None:
                said.append(
                    (
                        CHANGE,
                        "To adopt it, change one line of the agent's manifest, under "
                        f"`policy.thresholds`: `{block['change']['line']}` (now {now}). Nothing "
                        "changes it for you.",
                    )
                )
            if own:
                said.append(
                    (
                        LABELS,
                        f"A label with a threshold of its own ({', '.join(own)}) keeps it: only "
                        f"`{SETTING}` was varied.",
                    )
                )
        elif status == NONE_CERTIFIED:
            summary = (
                f"No automation threshold can be certified for {self.aim(' and ')} with "
                f"{confidence} confidence."
            )
            said.append((POINT, here))
            # A risk the walk stopped on with cases of it automated is said with its counts,
            # unless the sentence on what no threshold fixes says them at that same threshold.
            covered = (
                {_key(item) for item in block["cannot_fix"]}
                if found.first == found.stopped_at
                else set()
            )
            if any(risk.k and _key(self.named(risk)) not in covered for risk in found.failed):
                said.append((WALK, self.stop(found)))
            said += [(NEEDS, self.needed(item)) for item in block["needs"]]
            for item in block["cannot_fix"]:
                said.append(
                    (
                        CANNOT_FIX,
                        f"The threshold cannot fix this: at {threshold_text(item['threshold'])}, "
                        f"the strictest threshold that automates any case, {self.counted(item)} "
                        f"({item['k'] / item['n']:.1%}, above the {_percent(item['at_most'])} "
                        "target), and every lower threshold is above it too. What can: the agent "
                        "itself, a rule that sends such cases to a person, or a second signal.",
                    )
                )
        else:
            summary = (
                "The policy automates no case at any threshold (a rule, a label's limit or a cap "
                "sends each to a person), so there is no threshold to recommend."
                if status == NOTHING_AUTOMATED
                else "No threshold is recommended: this run did not keep what its policy decided "
                "from, so the policy cannot be applied again at another threshold."
            )
            said.append((POINT, here))
        if block["repeated"]:
            repeat = "repeats" if block["repeated"] == 1 else "repeat"
            said.append(
                (
                    REPEATED,
                    f"{block['repeated']} of the {current['automated']['n']} cases {repeat} "
                    "another case's input. Each counts as its own evidence here, so the bounds "
                    "are narrower than the distinct cases alone would support.",
                )
            )
        split = (
            f": the two targets share the {_percent(1.0 - targets.confidence)} error level"
            if targets.given == 2
            else ""
        )
        # "Of its count": at a threshold the walk chose, what holds with the stated confidence
        # is the guarantee (the risk is within its target), not the tighter bound of the count.
        said.append(
            (
                METHOD,
                'Each "at most" is the exact one-sided bound (Clopper-Pearson) of its count at '
                f"{_percent(self.level)} confidence{split}. It is about cases like these, "
                "labelled as these are, and about this version and configuration of the agent.",
            )
        )
        return {
            "summary": summary,
            "lines": [line for _, line in said],
            "kinds": [kind for kind, _ in said],
            "guarantee": guarantee,
        }


def _key(item: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """Which risk an item of the block's lists is of."""
    return item["risk"], item.get("question"), item.get("label")


def text(block: Mapping[str, Any], *more: str) -> str:
    """The block for a terminal: its heading, its summary and its lines, each wrapped, then
    any lines the caller adds (`more`)."""

    def wrapped(line: str) -> str:
        return textwrap.fill(
            line,
            _WIDTH,
            initial_indent="  ",
            subsequent_indent="    ",
            break_on_hyphens=False,
            break_long_words=False,
        )

    lines = [block["summary"], *block["lines"], *more]
    return "\n".join(["Operating point", *map(wrapped, lines)])


def markdown(block: Mapping[str, Any]) -> str:
    """The block as Markdown, for a CI job summary (after `render.report_markdown`): its
    heading, its summary in bold, then its lines as a list. The sentences are the block's own,
    as `text` prints them: they hold counts and the manifest's names for questions and labels
    (which a manifest holds to lower-case letters, digits and `_`), never a case's text or id,
    and what one writes between backticks is code here too."""
    lines = ["### Operating point", "", f"**{block['summary']}**"]
    if block["lines"]:
        lines += ["", *(f"- {line}" for line in block["lines"])]
    return "\n".join(lines) + "\n"


def of_report(report: Mapping[str, Any], confidence: float = CONFIDENCE) -> dict[str, Any]:
    """The point at the threshold a report ran with, from the counts its result states alone (so
    a card shows nothing it did not work out from them): each risk as k of n with its bound at
    `confidence`."""
    found = tally_of(report)
    names = [(str(c["question"]), str(c["label"])) for c in report["decisions"]["critical"]]
    return _point(float(report["policy"]["thresholds"]["auto"]), found, names, confidence)


def table(point: Mapping[str, Any], confidence: float = CONFIDENCE) -> list[tuple[str, str, str]]:
    """A point as rows for a table: what is counted, k of n, and its bound in words (worked out
    from k and n at `confidence`), for a card."""

    def row(what: str, figure: Mapping[str, Any]) -> tuple[str, str, str]:
        k, n = int(figure["k"]), int(figure["n"])
        if not n:
            return what, "no cases", "no bound"
        return what, f"{k} of {n}", f"at most {_at_most(upper_bound(k, n, confidence))}"

    rows = []
    for item in point["critical"]:
        name = f"Critical `{item['question']}={item['label']}` automated"
        rows.append(row(f"{name}, of its cases", item["of_critical"]))
        rows.append(row(f"{name}, of all cases", item["of_all"]))
    rows.append(row("False automation, of labelled cases", point[FALSE_AUTOMATION]))
    return rows
