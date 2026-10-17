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
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mendari.core.evalsets import RUNS_CAP, GradingRead
from mendari.errors import MendariError
from mendari.evals import ledger as ledgers
from mendari.evals.harness import record_problems
from mendari.evals.result import result_from_ledger
from mendari.evals.share import (
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
    from mendari.evals.harness import Dataset

HIDDEN_RESULT_FORMAT = "mendari.hidden_result/1"

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
