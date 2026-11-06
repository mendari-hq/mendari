# SPDX-License-Identifier: Apache-2.0
"""A grading run's result over a hidden evaluation set (`mendari.hidden_result/1`, LP-11l,
ADR-0011): what `POST /api/evalsets/{set_id}/runs` answers an organization's grading key, and
nothing else (conflicts 35 and 51 of notes/lp11-plan.md).

**The run** (`graded`) decides each of the set's cases in the server's process, as the store's
grading read hands them over (`EvalsetStore.grade_read`, each case named by its keyed hash),
with a Decision built with local providers only and no store: no decision is recorded, no report
saved (`AuditStore.save_report` is never called), no ledger kept. Its result is an evaluation
report's (`mendari.eval/2`), computed from the run's ledger in memory (`result_from_ledger`) and
dropped with it once projected.

**The result** is a closed schema built from a site report's parts (`evals/share.py`): the pack,
providers and policy; per question, its count, accuracy with its k of n, per-label precision,
recall and F1, the confusion matrix, the Brier score with its seeded bootstrap interval, and
calibration with its equal-width bins of `MIN_GROUP` cases or more only; the outcome counts and
rates, automation and review, and each critical label's counts with its k of n; the threshold
sweep; the error counts. It adds the set's id and commitment, how many cases were graded, the
run's number and the runs the set has left, and the seeds. It never holds a case's id or hash, a
dataset's name or hash, the ledger's id, a critical label's missed cases, the misclassified
cases, subgroups or equal-mass bins: every field is listed, and anything else fails the run
rather than reach anyone. A Brier interval is kept only over `MIN_GROUP` scored cases or more,
as calibration bins are: over a few cases its bounds are single cases' scores.

Every string is a name the pack or Mendari defines (a question, label, slot, variant, provider
type or model) or an error code; every number is bounded and rounded, as a site report's are.
The result names the pack's labels beside counts and rates, never beside a case.

**The hidden-set scoreboard** (`mendari.hidden_scoreboard/1`, LP-17k, the grading method 1.0's
sections 3 to 9) is what the same route answers a request that declares its arms (`arms`, with
`declared`, the SHA-256 of the committed declaration they come from). Every declared arm is
decided by its own Decision (local providers only, no store) on the same in-memory records of
one grading read (`scoreboard_graded`), so their ledgers share one case key and pair, and the
run is one chained `evalset.graded`, one of the set's runs. Each arm's ledger gives its entry
(`scoreboard.hidden_entry`: the records' inputs give its distinct groups and their
`attributes.weight` its weighted figures), its grade (`grade.grade`) and, with the others', the
ranking (`bands.ranking`: off, "one arm graded", while only the deterministic arm is local).
All of it is computed in memory and dropped once projected onto the closed answer: per arm, its
status ("graded", "not graded here" for an arm declared nowhere whose configurations call a
remote provider, "not applicable"), its configuration and providers, its figures (each critical
label's detection and positives decided AUTO as k of n with its distinct positives, the grade's
bound and the evidence; false automation, the AUTO error rate, automation and accuracy as k of
n; macro-F1, Brier and Brier's reliability with their intervals, a Brier interval only over
`MIN_GROUP` scored cases or more; ECE as a single number; the weighted figures) and its
attestations; the insufficient-evidence banner; the ranking and its Holm family (each ranked
arm's bands without cost, which is 0 for every local configuration); the scope (the pack, the
set's id and commitment with its size and distinct inputs, the method and whether it is in
effect, the day, "engineering evaluation, not clinical validation", "no model provider" and the
declaration's SHA-256); the seeds; and, in a run block outside its body's hash, this run's
number and the runs left. So two reads of one frozen set on one day give the same body
(`scoreboard_body_sha256`), and a signature (LP-17e) covers the run's number too. It never holds
cost, latency, subgroups, equal-mass bins, a case's id or hash, the dataset's name or hash, or a
ledger's id. Its figures are 6 places, as `mendari.scoreboard/1`'s are.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mendari.core.evalsets import RUNS_CAP, GradingRead
from mendari.errors import MendariError
from mendari.evals import arms, bands, grade, method, scoreboard
from mendari.evals import ledger as ledgers
from mendari.evals.harness import record_problems
from mendari.evals.ledger import canonical
from mendari.evals.result import PREDICTED, TRUTH, result_from_ledger
from mendari.evals.share import (
    _NAME_PART,
    MIN_GROUP,
    Automation,
    Count,
    CriticalCounts,
    ErrorKey,
    Name,
    Outcome,
    Positive,
    QuestionMetrics,
    Rate,
    Review,
    SharedPack,
    SharedPolicy,
    SharedProvider,
    SweepRow,
)

if TYPE_CHECKING:
    from mendari.core.decision import Decision
    from mendari.core.pack import Pack
    from mendari.evals.harness import Dataset

HIDDEN_RESULT_FORMAT = "mendari.hidden_result/1"
HIDDEN_SCOREBOARD_FORMAT = "mendari.hidden_scoreboard/1"

SetId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
Commitment = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Runs = Annotated[int, Field(ge=0, le=RUNS_CAP)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Proportion(_Closed):
    """k of n with its exact Clopper-Pearson interval (null bounds when n is 0)."""

    k: Count
    n: Count
    low: Rate | None
    high: Rate | None

    @model_validator(mode="after")
    def _k_of_n(self) -> Proportion:
        if self.k > self.n:
            raise ValueError("k is at most n")
        return self


class BrierInterval(_Closed):
    low: Positive
    high: Positive


class HiddenQuestion(QuestionMetrics):
    """A question's metrics as a site report shares them, with its accuracy as k of n and the
    Brier score's interval (null under `MIN_GROUP` scored cases)."""

    proportion: Proportion
    brier_interval: BrierInterval | None


class HiddenCritical(CriticalCounts):
    """A critical label's counts as a site report shares them (never its missed cases), with its
    detection as k of n."""

    proportion: Proportion


class HiddenDecisions(_Closed):
    outcome_counts: dict[Outcome, Count]
    outcome_rates: dict[Outcome, Rate | None]
    abstention_rate: Rate | None
    automation: Automation
    review: Review
    recommendation_accuracy: Rate | None
    critical: Annotated[list[HiddenCritical], Field(max_length=64)]


class HiddenSeeds(_Closed):
    bootstrap: Annotated[int, Field(ge=0, le=2**32 - 1)]
    resamples: Annotated[int, Field(ge=1, le=100_000)]
    level: Rate


class HiddenResult(_Closed):
    """Everything a grading run over a hidden set answers, and nothing else."""

    format: Literal["mendari.hidden_result/1"]
    set: SetId
    commitment: Commitment = Field(description="the set's salted commitment, as chained at freeze")
    pack: SharedPack
    providers: Annotated[dict[Name, SharedProvider], Field(max_length=64)]
    policy: SharedPolicy
    n: Count = Field(description="the cases graded")
    run: Annotated[int, Field(ge=1, le=RUNS_CAP, description="this run's number")]
    runs_left: Runs = Field(description="the grading runs the set has left after this one")
    seeds: HiddenSeeds
    primary: Name
    questions: Annotated[dict[Name, HiddenQuestion], Field(max_length=64)]
    decisions: HiddenDecisions
    threshold_sweep: Annotated[list[SweepRow], Field(max_length=101)]
    errors: Annotated[dict[ErrorKey, Count], Field(max_length=64)]


class _Fixed(_Closed):
    """What of a grading run's result its pack and providers fix, whatever the cases, each part
    as the result's models hold it (`holds`): each question with its labels (the names its
    per-label metrics and confusion matrix are keyed by), each critical label as its question
    and label."""

    pack: SharedPack
    providers: Annotated[dict[Name, SharedProvider], Field(max_length=64)]
    policy: SharedPolicy
    primary: Name
    questions: Annotated[
        dict[Name, Annotated[list[Name], Field(max_length=64)]], Field(max_length=64)
    ]
    critical: Annotated[list[tuple[Name, Name]], Field(max_length=64)]


# What of a `mendari.eval/2` result each part keeps (`hidden_result`); nothing else is copied.
_QUESTION_KEPT = ("n", "accuracy", "macro_f1", "per_label", "confusion", "brier", "proportion")
_DECISIONS_KEPT = (
    "outcome_counts",
    "outcome_rates",
    "abstention_rate",
    "automation",
    "review",
    "recommendation_accuracy",
)
_CRITICAL_KEPT = (
    "question",
    "label",
    "n",
    "detected",
    "detection_rate",
    "auto_missed",
    "proportion",
)
_SWEEP_KEPT = ("threshold", "coverage", "accuracy", "errors")
_SEEDS_KEPT = ("bootstrap", "resamples", "level")


def graded(decision: Decision, dataset: Dataset, read: GradingRead) -> HiddenResult:
    """A grading run over the cases a grading read handed over (`dataset`, in memory), with
    `decision` (built with no store and local providers only: anything else is refused before a
    case is decided), as the grading route answers it (`hidden_result`). A dataset the pack
    cannot evaluate (`record_problems`, whose lines name cases) is a `MendariError` that names
    nothing of it, as is a result that does not fit the schema."""
    if decision.store is not None or decision.is_remote:
        raise MendariError("a hidden set is graded with no store and local providers only")
    if record_problems(decision.pack.manifest, dataset):
        raise MendariError("the set's cases are not the pack's to evaluate")
    results = [decision.decide(record["input"]) for record in dataset.records]
    return hidden_result(result_from_ledger(ledgers.build(decision, dataset, results)), read)


def holds(decision: Decision) -> bool:
    """Whether `mendari.hidden_result/1` holds what a run with `decision` would answer of its
    pack and providers, whatever the cases: the pack, each slot's variant, type and model, the
    policy, the primary question, each question with its labels and each critical label, as
    names the result's models take and as many as they hold. The grading route asks before the
    set's read is chained, so a pack none of whose results could be answered uses none of a
    set's runs."""
    pack = decision.pack
    manifest = pack.manifest
    try:
        _Fixed.model_validate(
            {
                "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
                "providers": {
                    slot: {"variant": info.variant, "type": info.type, "model": info.model}
                    for slot, info in decision.provider_info.items()
                },
                "policy": {
                    "risk_level": int(manifest.risk.level),
                    "thresholds": manifest.policy.thresholds.model_dump(),
                    "governance_override": decision.governance is not None,
                },
                "primary": manifest.policy.primary,
                "questions": {
                    question: list(manifest.labels(question)) for question in manifest.questions
                },
                "critical": [(item.question, item.label) for item in manifest.evaluation.critical],
            }
        )
    except ValidationError:
        return False
    return True


def hidden_result(result: Mapping[str, Any], read: GradingRead) -> HiddenResult:
    """`result` (a `mendari.eval/2` result of a grading run) projected onto
    `mendari.hidden_result/1`, with what its read chained (`read`): only the fields listed,
    calibration bins and Brier intervals over fewer than `MIN_GROUP` cases left out, each value
    validated. A result that does not fit is a `MendariError` that names nothing of it (a
    validation error would quote a value)."""
    try:
        questions = {}
        for name, question in result["questions"].items():
            calibration = question["calibration"]
            scored = sum(int(found["n"]) for found in calibration["bins"])
            questions[name] = {
                **{key: question[key] for key in _QUESTION_KEPT},
                "calibration": {
                    "ece": calibration["ece"],
                    "bins": [found for found in calibration["bins"] if found["n"] >= MIN_GROUP],
                },
                "brier_interval": question["brier_interval"] if scored >= MIN_GROUP else None,
            }
        decisions = result["decisions"]
        policy = result["policy"]
        return HiddenResult.model_validate(
            {
                "format": HIDDEN_RESULT_FORMAT,
                "set": read.set,
                "commitment": read.commitment,
                "pack": result["pack"],
                "providers": result["providers"],
                "policy": {
                    "risk_level": policy["risk_level"],
                    "thresholds": policy["thresholds"],
                    "governance_override": policy["governance_override"] is not None,
                },
                "n": read.n,
                "run": read.run,
                "runs_left": read.runs_left,
                "seeds": {key: result["seeds"][key] for key in _SEEDS_KEPT},
                "primary": result["primary"],
                "questions": questions,
                "decisions": {
                    **{key: decisions[key] for key in _DECISIONS_KEPT},
                    "critical": [
                        {key: item[key] for key in _CRITICAL_KEPT} for item in decisions["critical"]
                    ],
                },
                "threshold_sweep": [
                    {key: row[key] for key in _SWEEP_KEPT} for row in result["threshold_sweep"]
                ],
                "errors": result["errors"],
            }
        )
    except (ValidationError, KeyError, TypeError, AttributeError):
        raise MendariError(f"the run's result does not fit {HIDDEN_RESULT_FORMAT}") from None


# --------------------------------------------------------------------------------------------
# The hidden-set scoreboard (`mendari.hidden_scoreboard/1`, LP-17k)
# --------------------------------------------------------------------------------------------

GRADED = "graded"
NOT_GRADED_HERE = "not graded here"  # an arm whose configurations call a remote provider
ARM_STATUSES = (GRADED, NOT_GRADED_HERE, arms.NOT_APPLICABLE)
# What the body's SHA-256 leaves out: the run block (this run's number and the runs left), so
# two reads of one set give the same body; and what a store adds (`body_sha256`, a signature).
SCOREBOARD_BODY_EXCLUDED = ("body_sha256", "signature", "run")
MAX_CRITICAL = 64  # critical labels an answer holds, at most (as `mendari.hidden_result/1`)
_ARMS = len(arms.ARMS)
_CONFIGURATION = rf"{_NAME_PART}={_NAME_PART}(?:,{_NAME_PART}={_NAME_PART}){{0,63}}"
_ARM = r"[a-z][a-z_]{0,31}"
_NUMBER = r"[0-9]+(?:\.[0-9]+)?"

Share = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Score = Annotated[float, Field(ge=0.0, le=2.0, allow_inf_nan=False)]  # a Brier score: 0 to 2
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BodySha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Day = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
ConfigurationName = Annotated[str, Field(pattern=rf"^{_CONFIGURATION}$")]
State = Literal["met", "not met", "insufficient evidence"]
# `stats`' words for a calibration term it cannot estimate: counts, never a case.
NotEstimableReason = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{1,10} scored cases; [0-9]{1,4} equal-mass bins of at least [0-9]{1,6} "
        r"need [0-9]{1,10}$"
    ),
]
BandWords = Annotated[
    str,
    Field(
        pattern=rf"^(?:at or (?:above|below) {_NUMBER}(?: USD)?|(?:above|below) {_NUMBER}"
        rf"(?: USD)?|not estimable: after every band|unknown: the last band)$"
    ),
]
TestName = Annotated[
    str,
    Field(
        pattern=rf"^{_ARM} against {_ARM}: (?:detection {_NAME_PART}={_NAME_PART}|"
        r"false_automation|accuracy|brier)$"
    ),
]


class Figure(_Closed):
    """k of n with its exact Clopper-Pearson 95% interval, 6 places (null bounds when n is 0)."""

    k: Count
    n: Count
    low: Share | None
    high: Share | None

    @model_validator(mode="after")
    def _k_of_n(self) -> Figure:
        if self.k > self.n:
            raise ValueError("k is at most n")
        if (self.low is None) != (self.n == 0) or (self.high is None) != (self.n == 0):
            raise ValueError("a figure has its interval when it has cases, and only then")
        return self


class Bound(Figure):
    """The grade's bound: detected groups of the positive groups, at the simultaneous level."""

    level: Share
    labels: Annotated[int, Field(ge=1, le=MAX_CRITICAL)]


class Distinct(_Closed):
    positives: Count
    detected: Count


class Evidence(_Closed):
    status: Literal["sufficient", "insufficient evidence"]
    needed: Count


class ScoredCritical(_Closed):
    """A critical label's figures: detection and its positives decided AUTO (raw k of n), its
    distinct positives, the grade's bound and its evidence. Never a missed case."""

    question: Name
    label: Name
    detected: Figure
    automated: Figure
    distinct: Distinct
    bound: Bound
    evidence: Evidence


class Mean(_Closed):
    """A mean with its seeded bootstrap interval; a Brier score's is null over fewer than
    `MIN_GROUP` scored cases, whose bounds would be single cases' scores."""

    estimate: Score
    low: Score | None
    high: Score | None


class Term(Mean):
    """Brier's reliability term with its bootstrap interval."""

    status: Literal["ok"]


class NotEstimable(_Closed):
    status: Literal["not_estimable"]
    reason: NotEstimableReason


class Ece(_Closed):
    """ECE, equal-mass, as one number: a description with no interval, never its bins."""

    status: Literal["ok"]
    value: Share


class WeightedShare(_Closed):
    """A Korn-Graubard share: the Hájek estimate, Kish's effective n and the interval."""

    n: Count
    estimate: Share | None
    effective_n: Annotated[float, Field(ge=0.0, le=1e9, allow_inf_nan=False)] | None
    low: Share | None
    high: Share | None


class AsSampled(_Closed):
    status: Literal["as sampled"]


class Weighted(_Closed):
    """The weighted figures, beside the unweighted ones: detection is never weighted."""

    status: Literal["weighted"]
    accuracy: WeightedShare
    false_automation: WeightedShare
    automation: WeightedShare
    auto_error_rate: WeightedShare


class ScoredFigures(_Closed):
    """An arm's figures on a hidden set: never cost, latency or subgroups."""

    critical: Annotated[list[ScoredCritical], Field(min_length=1, max_length=MAX_CRITICAL)]
    false_automation: Figure
    auto_error_rate: Figure
    automation: Figure
    accuracy: Figure
    macro_f1: Mean | None
    brier: Mean | None
    brier_reliability: Term | NotEstimable | None
    ece: Ece | NotEstimable | None
    weighted: AsSampled | Weighted


class GradeMethod(_Closed):
    version: Annotated[str, Field(pattern=r"^mendari\.grading/[0-9]{1,4}\.[0-9]{1,4}$")]
    sha256: Sha256
    in_effect: bool


class AutomatedLabel(_Closed):
    question: Name
    label: Name
    automated: Figure
    distinct_positives: Count


class NoCriticalAutomated(_Closed):
    attestation: Literal["no_critical_automated"]
    state: State
    labels: Annotated[list[AutomatedLabel], Field(min_length=1, max_length=MAX_CRITICAL)]
    needed: Count
    draft: bool


class RecallFloor(_Closed):
    attestation: Literal["recall_floor"]
    question: Name
    label: Name
    state: State
    floor: Share
    bound: Bound
    needed: Count
    draft: bool


class FalseAutomationCeiling(_Closed):
    attestation: Literal["false_automation_ceiling"]
    state: State
    ceiling: Share
    false_automation: Figure
    level: Share
    needed: Count
    draft: bool


Attestation = NoCriticalAutomated | RecallFloor | FalseAutomationCeiling


class ScoredGrade(_Closed):
    """A grade: its attestations, each met, not met or insufficient evidence, under the method
    named. No letter, no score, no composite."""

    method: GradeMethod
    attestations: Annotated[list[Attestation], Field(min_length=3, max_length=MAX_CRITICAL + 2)]


class GradedConfiguration(_Closed):
    """The configuration an arm ran: each slot's variant, local, calling nothing remote."""

    providers: Annotated[dict[Name, Name], Field(min_length=1, max_length=64)]
    local: Literal[True]
    remote_types: Annotated[list[Name], Field(max_length=0)]


class ScoredArm(_Closed):
    """One arm of the pack (`arms.ARMS`): graded here, with its configuration, providers,
    figures and grade; or "not graded here" or "not applicable", with none of them."""

    arm: Name
    name: Annotated[str, Field(max_length=64)]
    status: Literal["graded", "not graded here", "not applicable"]
    configuration: GradedConfiguration | None
    providers: Annotated[dict[Name, SharedProvider], Field(max_length=64)] | None
    figures: ScoredFigures | None
    grade: ScoredGrade | None

    @model_validator(mode="after")
    def _an_arm(self) -> ScoredArm:
        if arms.ARMS.get(self.arm) != self.name:
            raise ValueError("an arm is one of the scoreboard's, with its name")
        parts = (self.configuration, self.providers, self.figures, self.grade)
        if (self.status == GRADED) != all(part is not None for part in parts) or (
            self.status != GRADED and any(part is not None for part in parts)
        ):
            raise ValueError("a graded arm has its configuration, providers, figures and grade")
        return self


class ShortLabel(_Closed):
    """A critical label of a graded arm short of evidence: its distinct positives and the
    method's minimum."""

    arm: Name
    configuration: ConfigurationName
    question: Name
    label: Name
    positives: Count
    needed: Count


class Banner(_Closed):
    short: Annotated[list[ShortLabel], Field(min_length=1, max_length=_ARMS * MAX_CRITICAL)]


class RankingOff(_Closed):
    status: Literal["off"]
    reason: Literal["one arm graded"]


class DetectionBand(_Closed):
    question: Name
    label: Name
    value: Share | None
    position: Annotated[int, Field(ge=0, le=100)] | None
    band: BandWords


class Band(_Closed):
    value: Score | None
    position: Annotated[int, Field(ge=0, le=100)] | None
    band: BandWords


class RankBands(_Closed):
    """A ranked arm's bands, in the tuple's order, without cost (0 for every local arm)."""

    detection: DetectionBand
    false_automation: Band
    accuracy: Band
    brier: Band


Place = Annotated[int, Field(ge=1, le=_ARMS)]  # a rank, or a bound of a rank interval


class Ranked(_Closed):
    rank: Place
    interval: Annotated[list[Place], Field(min_length=2, max_length=2)]
    arm: Name
    configuration: ConfigurationName
    bands: RankBands


class UnrankedReason(_Closed):
    attestation: Literal["no_critical_automated", "recall_floor"]
    question: Name | None
    label: Name | None
    state: State


class Unranked(_Closed):
    arm: Name
    configuration: ConfigurationName
    reasons: Annotated[list[UnrankedReason], Field(min_length=1, max_length=MAX_CRITICAL + 1)]


class FamilyTest(_Closed):
    """One test of the Holm family: its pair, criterion (and label), what it was decided on (the
    discordant pairs, or Brier's paired difference), its p-values and the arm it favours."""

    name: TestName
    arms: Annotated[list[Name], Field(min_length=2, max_length=2)]
    criterion: Literal["detection", "false_automation", "accuracy", "brier"]
    question: Name | None
    label: Name | None
    n: Count
    discordant: Annotated[list[Count], Field(min_length=2, max_length=2)] | None
    difference: Annotated[float, Field(ge=-2.0, le=2.0, allow_inf_nan=False)] | None
    p: Share
    adjusted: Share
    reject: bool
    favours: Name | None


class FamilySeeds(_Closed):
    bootstrap: Annotated[int, Field(ge=0, le=2**32 - 1)]
    resamples: Annotated[int, Field(ge=1, le=100_000)]


class Family(_Closed):
    of: Annotated[str, Field(max_length=200)]
    alpha: Share
    size: Count
    seeds: FamilySeeds
    tests: Annotated[list[FamilyTest], Field(max_length=6 * (MAX_CRITICAL + 3))]

    @model_validator(mode="after")
    def _the_family(self) -> Family:
        if self.of != bands.FAMILY or self.size != len(self.tests):
            raise ValueError("the family is the method's, and its size is its tests'")
        return self


class RankingOn(_Closed):
    status: Literal["on"]
    method: GradeMethod
    draft: bool
    graded: Annotated[int, Field(ge=2, le=_ARMS)]
    ranked: Annotated[list[Ranked], Field(max_length=_ARMS)]
    unranked: Annotated[list[Unranked], Field(max_length=_ARMS)]
    family: Family


class ScoredSet(_Closed):
    """A frozen hidden set, as its scope names it: its id and freeze commitment, never a name
    or a hash of its cases."""

    kind: Literal["hidden"]
    id: SetId
    commitment: Commitment
    n: Count
    distinct_inputs: Count
    norm: Annotated[str, Field(pattern=r"^norm/v[0-9]{1,4}$")]


class ScoredDisclosure(_Closed):
    status: Literal["no model provider"]
    statements: Annotated[dict[Name, str], Field(max_length=0)]


class ScoredScope(_Closed):
    """What the answer is of (the grading method's section 9)."""

    pack: SharedPack
    status: Literal[
        "Synthetic example. Not clinically reviewed. Not for patient care.",
        "Not clinically reviewed.",
    ]
    set: ScoredSet
    method: GradeMethod
    measured_on: Day
    evaluation: Literal["engineering evaluation, not clinical validation"]
    disclosure: ScoredDisclosure
    declared: Sha256 = Field(description="the SHA-256 of the declaration the arms come from")


class RunBlock(_Closed):
    """This run's number (its chained `evalset.graded`) and the runs the set has left after it:
    outside the body's SHA-256."""

    run: Annotated[int, Field(ge=1, le=RUNS_CAP)]
    runs_left: Runs


class HiddenScoreboard(_Closed):
    """Everything a scoreboard run over a hidden set answers, and nothing else."""

    format: Literal["mendari.hidden_scoreboard/1"]
    scope: ScoredScope
    seeds: HiddenSeeds
    arms: Annotated[list[ScoredArm], Field(min_length=_ARMS, max_length=_ARMS)]
    banner: Banner | None
    ranking: RankingOff | RankingOn
    supersedes: BodySha256 | None = Field(
        description="the earlier stored result this one supersedes (`mendari scoreboard add`)"
    )
    run: RunBlock
    body_sha256: BodySha256

    @model_validator(mode="after")
    def _every_arm_once(self) -> HiddenScoreboard:
        if [found.arm for found in self.arms] != list(arms.ARMS):
            raise ValueError("every arm, once, in the scoreboard's order")
        if not any(found.status == GRADED for found in self.arms):
            raise ValueError("at least one arm is graded")
        return self

    @property
    def n(self) -> int:
        """The cases graded."""
        return self.scope.set.n


@dataclass(frozen=True)
class Declared:
    """One declared arm of a scoreboard run, as the grading route built it before any case is
    read: the arm, the configuration it runs (`arms.Configuration`, local) and the Decision that
    runs it (local providers, no store)."""

    arm: str
    configuration: arms.Configuration
    decision: Decision


def configuration_of(decision: Decision) -> arms.Configuration | None:
    """The configuration of its pack (`arms.configurations`) that `decision` runs, slot by slot;
    None when there is none, or the pack has more than the scoreboard lists."""
    running = {slot: info.variant for slot, info in decision.provider_info.items()}
    try:
        found = arms.configurations(decision.pack)
    except MendariError:
        return None
    return next((each for each in found if each.providers == running), None)


def scoreboard_holds(pack: Pack) -> bool:
    """Whether `mendari.hidden_scoreboard/1` can answer a run of `pack`, whatever the cases,
    beyond what `holds` asks of each arm's Decision: a grade attests its critical labels, so the
    pack has 1 to `MAX_CRITICAL`."""
    return 1 <= len(pack.manifest.evaluation.critical) <= MAX_CRITICAL


def arm_statuses(pack: Pack, declared: Sequence[str]) -> dict[str, str]:
    """Each arm's status, in `arms.ARMS`'s order: "graded" when declared; else "not applicable"
    when no configuration of `pack` reaches it, and "not graded here" otherwise (a hidden set is
    graded with local providers only, and every other arm's configurations call a remote
    one)."""
    found = arms.arms(pack)
    return {
        arm: GRADED
        if arm in declared
        else arms.NOT_APPLICABLE
        if found[arm].status == arms.NOT_APPLICABLE
        else NOT_GRADED_HERE
        for arm in arms.ARMS
    }


def scoreboard_graded(
    declared: Sequence[Declared],
    dataset: Dataset,
    read: GradingRead,
    *,
    declaration: str,
    measured_on: datetime.date,
) -> HiddenScoreboard:
    """A scoreboard run over the cases one grading read handed over (`dataset`, in memory):
    each declared arm (one per arm, of one pack, each local with no store) decides every case,
    in `arms.ARMS`'s order, on the same records, so their ledgers pair; then each arm's entry,
    grade and the ranking, computed in memory and projected onto the closed answer
    (`hidden_scoreboard`), which cites `declaration` (the declaration's SHA-256) and is dated
    `measured_on`. A dataset the pack cannot evaluate is a `MendariError` that names nothing of
    it, as is an answer that does not fit the schema."""
    if not declared:
        raise MendariError("a scoreboard run grades at least one declared arm")
    order = list(arms.ARMS)
    chosen = sorted(declared, key=lambda found: order.index(found.arm))
    pack = chosen[0].decision.pack
    if len({found.arm for found in chosen}) != len(chosen):
        raise MendariError("one configuration per arm is graded")
    for found in chosen:
        decision = found.decision
        if decision.store is not None or decision.is_remote or not found.configuration.local:
            raise MendariError("a hidden set is graded with no store and local providers only")
        if decision.pack.digest != pack.digest or found.configuration.arm != found.arm:
            raise MendariError("every arm is of one pack, each in the arm it is declared under")
    if record_problems(pack.manifest, dataset):
        raise MendariError("the set's cases are not the pack's to evaluate")
    scope = scoreboard.PackScope.of(pack)
    graded = []
    for found in chosen:
        results = [found.decision.decide(record["input"]) for record in dataset.records]
        ledger = ledgers.build(found.decision, dataset, results)
        entry = scoreboard.hidden_entry(
            ledger,
            dataset,
            pack=scope,
            arm=found.arm,
            configuration=found.configuration,
            set_id=read.set,
            commitment=read.commitment,
            measured_on=measured_on,
        )
        graded.append((entry, ledger))
    ranking = bands.ranking(
        [entry for entry, _ in graded],
        ledgers=[ledger for _, ledger in graded],
        records=dataset.records,
    )
    statuses = arm_statuses(pack, [found.arm for found in chosen])
    return hidden_scoreboard(graded, ranking, read, statuses=statuses, declaration=declaration)


def scoreboard_body(answer: Mapping[str, Any]) -> dict[str, Any]:
    """An answer without what its body's SHA-256 leaves out (`SCOREBOARD_BODY_EXCLUDED`)."""
    return {key: value for key, value in answer.items() if key not in SCOREBOARD_BODY_EXCLUDED}


def scoreboard_body_sha256(answer: Mapping[str, Any]) -> str:
    """`sha256:` and the SHA-256 of an answer's canonical bytes (`ledger.canonical`) without its
    run block, its `body_sha256` and any signature: the same for two reads of one set."""
    return "sha256:" + hashlib.sha256(canonical(scoreboard_body(answer)).encode()).hexdigest()


def _scored(ledger: ledgers.Ledger) -> int:
    """The cases scored for the primary question: labelled and answered."""
    primary = ledger.header["primary"]
    return sum(
        1
        for case in ledger.cases
        if (found := case["q"].get(primary)) is not None
        and found[TRUTH] is not None
        and found[PREDICTED] is not None
    )


def _figures(entry: Mapping[str, Any], ledger: ledgers.Ledger) -> dict[str, Any]:
    """What an answer holds of an entry's figures: every one but cost (and an entry of a hidden
    set has no latency or subgroups); a Brier interval only over `MIN_GROUP` scored cases."""
    figures = entry["figures"]
    kept = {key: figures[key] for key in ScoredFigures.model_fields}
    brier = kept["brier"]
    if brier is not None and _scored(ledger) < MIN_GROUP:
        kept["brier"] = {**brier, "low": None, "high": None}
    return kept


def _ranking(found: Mapping[str, Any]) -> dict[str, Any]:
    """A ranking as an answer holds it: off with its reason; or on, each ranked arm's bands
    without cost and without its key, every reason and test with each field named."""
    if found["status"] != bands.ON:
        return dict(found)
    shown = [bands.DETECTION, bands.FALSE_AUTOMATION, bands.ACCURACY, bands.BRIER]
    return {
        **{key: found[key] for key in ("status", "method", "draft", "graded")},
        "ranked": [
            {
                **{key: row[key] for key in ("rank", "interval", "arm", "configuration")},
                "bands": {criterion: row["bands"][criterion] for criterion in shown},
            }
            for row in found["ranked"]
        ],
        "unranked": [
            {
                "arm": row["arm"],
                "configuration": row["configuration"],
                "reasons": [
                    {
                        "attestation": reason["attestation"],
                        "question": reason.get("question"),
                        "label": reason.get("label"),
                        "state": reason["state"],
                    }
                    for reason in row["reasons"]
                ],
            }
            for row in found["unranked"]
        ],
        "family": {
            **{key: found["family"][key] for key in ("of", "alpha", "size", "seeds")},
            "tests": [
                {key: test.get(key) for key in FamilyTest.model_fields}
                for test in found["family"]["tests"]
            ],
        },
    }


def hidden_scoreboard(
    graded: Sequence[tuple[Mapping[str, Any], ledgers.Ledger]],
    ranking: Mapping[str, Any],
    read: GradingRead,
    *,
    statuses: Mapping[str, str],
    declaration: str,
    supersedes: str | None = None,
) -> HiddenScoreboard:
    """The graded arms (each in-memory entry with its ledger, `scoreboard.hidden_entry`) and
    their `ranking` (`bands.ranking`) projected onto `mendari.hidden_scoreboard/1`, with what the
    read chained (`read`), each arm's status (`arm_statuses`) and the declaration's SHA-256: only
    the fields listed, each value validated, and the body's SHA-256 computed over what the
    answer holds. One that does not fit is a `MendariError` that names nothing of it (a
    validation error would quote a value)."""
    try:
        by_arm = {entry["arm"]: (entry, ledger) for entry, ledger in graded}
        first = graded[0][0]["scope"]
        listed: list[dict[str, Any]] = []
        short: list[dict[str, Any]] = []
        for arm, name in arms.ARMS.items():
            if arm not in by_arm:
                shown = {"configuration": None, "providers": None, "figures": None, "grade": None}
                listed.append({"arm": arm, "name": name, "status": statuses[arm], **shown})
                continue
            entry, ledger = by_arm[arm]
            written = bands.configuration_name(entry)
            short += [
                {"arm": arm, "configuration": written, **label}
                for label in scoreboard.short_of_evidence(entry)
            ]
            listed.append(
                {
                    "arm": arm,
                    "name": name,
                    "status": statuses[arm],
                    "configuration": entry["configuration"],
                    "providers": entry["scope"]["providers"],
                    "figures": _figures(entry, ledger),
                    "grade": grade.grade(entry),
                }
            )
        scope = {
            **{key: first[key] for key in ("pack", "status", "method", "measured_on")},
            "set": {**first["set"], "id": read.set, "commitment": read.commitment},
            "evaluation": first["evaluation"],
            "disclosure": first["disclosure"],
            "declared": declaration,
        }
        body = {
            "format": HIDDEN_SCOREBOARD_FORMAT,
            "scope": scope,
            "seeds": {
                "bootstrap": method.SEED,
                "resamples": method.RESAMPLES,
                "level": method.LEVEL,
            },
            "arms": listed,
            "banner": {"short": short} if short else None,
            "ranking": _ranking(ranking),
            "supersedes": supersedes,
        }
        run = {"run": read.run, "runs_left": read.runs_left}
        # Validated before it is hashed, so the hash is of what the answer holds.
        checked = HiddenScoreboard.model_validate(
            {**body, "run": run, "body_sha256": "sha256:" + "0" * 64}
        ).model_dump(mode="json")
        return HiddenScoreboard.model_validate(
            {**checked, "body_sha256": scoreboard_body_sha256(checked)}
        )
    except (ValidationError, KeyError, TypeError, AttributeError, IndexError, MendariError):
        raise MendariError(f"the run's result does not fit {HIDDEN_SCOREBOARD_FORMAT}") from None


def scoreboard_problem(result: Any) -> str | None:
    """Why `result` (a stored hidden-set scoreboard result, with or without its signature) does
    not fit `mendari.hidden_scoreboard/1` as an answer holds it, or None: the closed schema,
    every value as the schema keeps it, and a `body_sha256` that is its body's. Names fields,
    never a value."""
    if not isinstance(result, Mapping) or result.get("format") != HIDDEN_SCOREBOARD_FORMAT:
        return f"not a {HIDDEN_SCOREBOARD_FORMAT} result"
    body = {key: value for key, value in result.items() if key != "signature"}
    try:
        held = HiddenScoreboard.model_validate(body).model_dump(mode="json")
    except ValidationError as error:
        where = sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]})[:3]
        return f"it does not fit {HIDDEN_SCOREBOARD_FORMAT} (in {', '.join(where)})"
    if canonical(held) != canonical(body):
        return f"it is not {HIDDEN_SCOREBOARD_FORMAT} as an answer holds it"
    if body["body_sha256"] != scoreboard_body_sha256(body):
        return "its body_sha256 is not its body's SHA-256"
    return None
