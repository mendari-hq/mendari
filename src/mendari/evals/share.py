# SPDX-License-Identifier: Apache-2.0
"""What of an evaluation a site can share: counts, never cases.

`mendari.site-report/1` (LP-07) was a closed schema, the same on both sides. It held the
agent, the providers and policy, the metrics per question, the outcome counts and rates, the
critical counts, the threshold sweep, latency, cost, error counts and the dataset's size. It
never holds a case id, case text, the dataset's name or hash, who approved a governance
override (only that one applied), or subgroup cells: a subgroup's values come from a site's
data (a clinic, a clinician, a date), and nothing can tell a category from a name until an agent
declares each subgroup's values. Every string is a name the agent or Mendari defines (a
question, label, slot, variant, composite member, provider type or model), short and ASCII, or
one of Mendari's error codes (a closed set, `FAILURE_CODES`); every number is bounded and
rounded. A composite provider (`check`) is shared as it is recorded: its model lists each
member's as `member:model`, joined as the composite joins them (" + "), and a failed member's
error is `member:code`. Calibration bins with fewer than `MIN_GROUP` cases are dropped.

Of it, only the names a shared record holds are kept (`SharedAgent`, `SharedProvider`, `Name`);
a registry takes none, and nothing here builds one. A site shares `mendari.site-report/1`
(LP-22c, ADR-0017), built by `site_report` (`shareable`, as sent): whole counts only, counted
from the run's ledger, small cells suppressed by one function (`suppress`) that both sides run.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from mendari.errors import FAILURE_CODES, MendariError
from mendari.evals.ledger import Ledger
from mendari.evals.result import CONFIDENCE, OUTCOMES, PREDICTED, TRUTH
from mendari.providers import PROVIDER_TYPES
from mendari.providers.composite import CompositeProvider

MIN_GROUP = 10
MAX_MEMBERS = 16  # the most members a shared composite may list

_NAME_PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
_MODEL_PART = r"[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,127}"
_NAME = rf"^{_NAME_PART}$"
# An error code (Mendari's closed set), or a composite's failed members as `member:code` (", "
# between several, as `CompositeProvider.output` records them).
_CODE = f"(?:{'|'.join(map(re.escape, sorted(FAILURE_CODES)))})"
_MEMBER_CODE = rf"{_NAME_PART}:{_CODE}"
_ERROR_KEY = rf"^(?:{_CODE}|{_MEMBER_CODE}(?:, {_MEMBER_CODE}){{0,{MAX_MEMBERS - 1}}})$"

Name = Annotated[str, Field(pattern=_NAME)]
ErrorKey = Annotated[str, Field(pattern=_ERROR_KEY)]
Count = Annotated[int, Field(ge=0, le=10**9)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SharedAgent(_Closed):
    """The agent a shared record is about (its `agent` key): its id, version and digest."""

    # What a JSON schema made from it calls it; the class and the `agent` key it types keep their
    # names, as the signed records that hold them do.
    model_config = ConfigDict(title="SharedAgent")

    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$")]
    version: Annotated[str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


# How each composite type joins its members' `member:model` in its own model.
JOINERS: dict[str, str] = {
    name: kind.joiner
    for name, kind in PROVIDER_TYPES.items()
    if issubclass(kind, CompositeProvider)
}
_MODEL = re.compile(_MODEL_PART)
_COMPOSITE_MODEL = {
    name: re.compile(
        rf"{_NAME_PART}:{_MODEL_PART}"
        rf"(?:{re.escape(joiner)}{_NAME_PART}:{_MODEL_PART}){{1,{MAX_MEMBERS - 1}}}"
    )
    for name, joiner in JOINERS.items()
}


class SharedProvider(_Closed):
    variant: Name
    type: Name
    model: str | None

    @field_validator("model")
    @classmethod
    def _a_model_name(cls, model: str | None, info: ValidationInfo) -> str | None:
        """A model name; a composite's lists its members' (each a name, a colon and a model
        name), joined as that type joins them, and only a composite's does."""
        pattern = _COMPOSITE_MODEL.get(info.data.get("type", ""), _MODEL)
        if model is not None and not pattern.fullmatch(model):
            raise ValueError("must be a model name, or a composite's members and their models")
        return model


def composite_members(provider: SharedProvider) -> list[str] | None:
    """The members a composite's model lists, in order; None for any other provider."""
    joiner = JOINERS.get(provider.type)
    if joiner is None or provider.model is None:
        return None
    return [part.split(":", 1)[0] for part in provider.model.split(joiner)]


Outcome = Literal["AUTO", "HUMAN_REVIEW", "ABSTAIN", "BLOCK"]


def _field_names() -> frozenset[str]:
    models: list[type[BaseModel]] = [SiteReportV1]
    names: set[str] = {"report", "site", "body", "query", "path"}
    while models:
        model = models.pop()
        for name, field in model.model_fields.items():
            names.add(name)
            for kind in _models_in(field.annotation):
                if kind.__name__ not in {m.__name__ for m in models}:
                    models.append(kind)
    return frozenset(names)


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for argument in getattr(annotation, "__args__", ()) or ():
        found += _models_in(argument)
    return found


def where(loc: tuple[Any, ...] | list[Any]) -> str:
    """A validation error's location without the data in it: list positions and the share
    models' field names are shown, anything else (a mapping key, an unknown field) is `<key>`."""
    known = _FIELD_NAMES
    shown = [
        str(part) if isinstance(part, int) or part in known else "<key>"
        for part in loc
        if part != "[key]"
    ]
    return ".".join(shown)


# --------------------------------------------------------------------------------------------
# mendari.site-report/1: counts in, rates out (LP-22c)
# --------------------------------------------------------------------------------------------

SITE_REPORT_V1 = "mendari.site-report/1"
MIN_DATASET = 100  # owner decision 3 (plan): a run on fewer cases is not shared
BASIS = 10_000  # a threshold as a whole number of basis points: 0.85 is 8500
BINS = 10  # calibration: confidence deciles, as `metrics.calibration` cuts them

BasisPoints = Annotated[int, Field(ge=0, le=BASIS)]
Cell = Annotated[int | None, Field(ge=0, le=10**9)]  # a count of cases, or null: suppressed


class CountedThresholds(_Closed):
    auto_bp: BasisPoints
    review_bp: BasisPoints


class CountedPolicy(_Closed):
    risk_level: Annotated[int, Field(ge=0, le=4)]
    thresholds: CountedThresholds
    governance_override: bool


class CountedDataset(_Closed):
    n: Annotated[int, Field(ge=MIN_DATASET, le=10**9)]


class CountedBin(_Closed):
    bin: Annotated[int, Field(ge=0, le=BINS - 1)]
    right: Cell
    wrong: Cell


class CountedQuestion(_Closed):
    n: Count
    right: Count
    confusion: Annotated[
        dict[Name, Annotated[dict[Name, Cell], Field(max_length=64)]], Field(max_length=64)
    ]
    calibration: Annotated[list[CountedBin], Field(min_length=BINS, max_length=BINS)]

    @model_validator(mode="after")
    def _whole(self) -> CountedQuestion:
        if self.right > self.n:
            raise ValueError("right must be at most n")
        if [b.bin for b in self.calibration] != list(range(BINS)):
            raise ValueError("calibration must hold each decile once, in order")
        shown = sum(c for row in self.confusion.values() for c in row.values() if c is not None)
        if shown > self.n:
            raise ValueError("the confusion cells must add up to at most n")
        # Each part at most its total, so no stated count is past what it is of (the
        # adversarial review of LP-22f: counts that could not add up kept `suppress` running).
        wrong = self.n - self.right
        diagonal = sum(
            row.get(t) or 0 for t, row in self.confusion.items() if row.get(t) is not None
        )
        off = sum(c for t, row in self.confusion.items() for p, c in row.items() if p != t and c)
        rights = sum(b.right for b in self.calibration if b.right is not None)
        wrongs = sum(b.wrong for b in self.calibration if b.wrong is not None)
        if diagonal > self.right or rights > self.right:
            raise ValueError("the right answers' cells must add up to at most right")
        if off > wrong or wrongs > wrong:
            raise ValueError("the wrong answers' cells must add up to at most n - right")
        return self


class CountedCritical(_Closed):
    question: Name
    label: Name
    n: Count
    detected: Count
    auto_missed: Count

    @model_validator(mode="after")
    def _of_n(self) -> CountedCritical:
        if self.detected > self.n or self.auto_missed > self.n:
            raise ValueError("detected and auto_missed must be at most n")
        return self


class CountedDecisions(_Closed):
    outcomes: dict[Outcome, Cell]
    false_automation: Count  # automated with a wrong recommendation, of the primary's n
    critical: Annotated[list[CountedCritical], Field(max_length=64)]


class Tokens(_Closed):
    input: Count
    output: Count


class SiteReportV1(_Closed):
    """Everything a site may share about one evaluation: whole counts, names and booleans.
    No rate, no fraction, no case. The registry computes every figure it shows from these."""

    format: Literal["mendari.site-report/1"]
    agent: SharedAgent
    providers: Annotated[dict[Name, SharedProvider], Field(max_length=64)]
    policy: CountedPolicy
    remote_consent: bool
    dataset: CountedDataset
    primary: Name
    questions: Annotated[dict[Name, CountedQuestion], Field(max_length=64)]
    decisions: CountedDecisions
    tokens: Tokens

    @model_validator(mode="after")
    def _whole(self) -> SiteReportV1:
        if set(self.decisions.outcomes) != set(OUTCOMES):
            raise ValueError("outcomes must name each outcome once")
        shown = sum(c for c in self.decisions.outcomes.values() if c is not None)
        if shown > self.dataset.n or any(q.n > self.dataset.n for q in self.questions.values()):
            raise ValueError("no count may be more than the dataset's n")
        primary = self.questions.get(self.primary)
        if self.decisions.false_automation > (primary.n if primary else 0):
            raise ValueError("false_automation must be at most the primary question's n")
        return self


# The rule each field of mendari.site-report/1 is shared by (spec: docs/spec/
# evaluation-reports.md, "Shared site reports"). A test holds every field to one.
TOTAL = "total: shared exactly"
CELL = "cell: null when 1 to 9, and complementary suppression"
SAFETY = "safety count: shared exactly, never suppressed"
NAME = "name, version, digest or flag: as it is"
SUPPRESSION_RULES: dict[str, str] = {
    "format": NAME,
    "agent": NAME,
    "providers": NAME,
    "policy": NAME,
    "remote_consent": NAME,
    "dataset.n": TOTAL + f"; under {MIN_DATASET} cases nothing is shared",
    "primary": NAME,
    "questions.n": TOTAL,
    "questions.right": TOTAL,
    "questions.confusion": CELL
    + "; a critical label's own cell is its detected count, so it is shown",
    "questions.calibration.bin": NAME,
    "questions.calibration.right": CELL,
    "questions.calibration.wrong": CELL,
    "decisions.outcomes": CELL,
    "decisions.false_automation": SAFETY,
    "decisions.critical.question": NAME,
    "decisions.critical.label": NAME,
    "decisions.critical.n": SAFETY,
    "decisions.critical.detected": SAFETY,
    "decisions.critical.auto_missed": SAFETY,
    "tokens.input": TOTAL,
    "tokens.output": TOTAL,
}


def _basis_points(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value  # refused by the model
    return round(value * BASIS)


def _decile(confidence: float) -> int:
    """The calibration bin a confidence falls in, as `metrics.calibration` cuts them: [i/10,
    (i+1)/10), the last including 1. Clamped into [0, 1], so every answered case has a bin."""
    return min(BINS - 1, max(0, int(min(1.0, max(0.0, confidence)) * BINS)))


def counted_report(ledger: Ledger, *, remote_consent: bool = False) -> dict[str, Any]:
    """The counts of one run, from its ledger, before suppression: each one counted from the
    cases, never a rate times n."""
    header, cases = ledger.header, ledger.cases
    primary = header["primary"]
    questions: dict[str, Any] = {}
    for question, labels in header["questions"].items():
        labelled = [
            c["q"][question] for c in cases if c["q"].get(question, [None])[TRUTH] is not None
        ]
        if not labelled:
            continue
        confusion = {truth: dict.fromkeys(labels, 0) for truth in labels}
        bins = [[0, 0] for _ in range(BINS)]
        for entry in labelled:
            predicted = entry[PREDICTED]
            if predicted is None:
                continue
            confusion[entry[TRUTH]][predicted] += 1
            right = predicted == entry[TRUTH]
            bins[_decile(entry[CONFIDENCE])][0 if right else 1] += 1
        questions[question] = {
            "n": len(labelled),
            "right": sum(e[PREDICTED] == e[TRUTH] for e in labelled),
            "confusion": confusion,
            "calibration": [
                {"bin": i, "right": right, "wrong": wrong} for i, (right, wrong) in enumerate(bins)
            ],
        }

    def truth(case: Mapping[str, Any], q: str = primary) -> Any:
        entry = case["q"].get(q)
        return None if entry is None else entry[TRUTH]

    outcomes = Counter(c["o"] for c in cases)
    critical = []
    for index, (question, label) in enumerate(header["critical"]):
        positives = [c for c in cases if index in c["k"]]
        critical.append(
            {
                "question": question,
                "label": label,
                "n": len(positives),
                "detected": sum(
                    (c["q"].get(question) or [None, None])[PREDICTED] == label for c in positives
                ),
                "auto_missed": sum(c["o"] == "AUTO" for c in positives),
            }
        )
    policy = header["policy"]
    thresholds = policy.get("thresholds") or {}
    return {
        "format": SITE_REPORT_V1,
        "agent": header["agent"],
        "providers": header["providers"],
        "policy": {
            "risk_level": policy.get("risk_level"),
            "thresholds": {
                "auto_bp": _basis_points(thresholds.get("auto")),
                "review_bp": _basis_points(thresholds.get("review")),
            },
            "governance_override": policy.get("governance") is not None,
        },
        "remote_consent": remote_consent,
        "dataset": {"n": len(cases)},
        "primary": primary,
        "questions": questions,
        "decisions": {
            "outcomes": {key: outcomes.get(key, 0) for key in OUTCOMES},
            "false_automation": sum(
                c["o"] == "AUTO" and truth(c) is not None and c["r"] != truth(c) for c in cases
            ),
            "critical": critical,
        },
        "tokens": {
            "input": sum(c["u"][1] for c in cases),
            "output": sum(c["u"][2] for c in cases),
        },
    }


@dataclass
class _Ref:
    """A cell of a payload: the mapping or bin that holds it, its key there, and the most it
    could be given the totals the payload shares (`_bound`)."""

    holder: dict[str, Any]
    key: str
    kind: str  # diagonal, off, right, wrong, outcome
    question: str = ""
    row: str = ""
    exempt: bool = False  # a cell a safety count states: never suppressed

    @property
    def value(self) -> Any:
        return self.holder[self.key]


Equation = tuple[list[_Ref], list[_Ref], int]


def _equations(report: Mapping[str, Any]) -> list[Equation]:
    """What a payload states about its cells: each (added, subtracted, total) says the added
    cells less the subtracted ones come to `total`, a number the payload shares. No cell is in
    two equations.

    Per question: the diagonal of its confusion matrix adds up to `right`; so do the calibration
    bins' `right`; the off-diagonal cells (answered wrongly) and the bins' `wrong` count the
    same cases. The outcomes add up to the dataset's n."""
    exempt = {(c["question"], c["label"]) for c in report["decisions"]["critical"]}
    equations: list[Equation] = []
    for name in sorted(report["questions"]):
        question = report["questions"][name]
        confusion = question["confusion"]
        diagonal, off = [], []
        for truth in sorted(confusion):
            row = confusion[truth]
            for predicted in sorted(row):
                if predicted == truth:
                    diagonal.append(
                        _Ref(row, predicted, "diagonal", name, truth, (name, truth) in exempt)
                    )
                else:
                    off.append(_Ref(row, predicted, "off", name, truth))
        bins = question["calibration"]
        equations.append((diagonal, [], question["right"]))
        equations.append(([_Ref(b, "right", "right", name) for b in bins], [], question["right"]))
        equations.append((off, [_Ref(b, "wrong", "wrong", name) for b in bins], 0))
    outcomes = report["decisions"]["outcomes"]
    added = [_Ref(outcomes, key, "outcome") for key in OUTCOMES]
    equations.append((added, [], report["dataset"]["n"]))
    return equations


def _bound(report: Mapping[str, Any], ref: _Ref) -> int:
    """The most a cell could be, from the totals the payload shares: a critical label's row
    holds its n cases (its own cell is `detected`, its shown cells are known); every other
    cell at most what its question's right or wrong answers, or the dataset, hold."""
    if ref.kind == "outcome":
        return int(report["dataset"]["n"])
    question = report["questions"][ref.question]
    wrong = question["n"] - question["right"]
    if ref.kind == "off":
        critical = [
            c
            for c in report["decisions"]["critical"]
            if c["question"] == ref.question and c["label"] == ref.row
        ]
        if critical:
            row = question["confusion"][ref.row]
            shown = sum(v for k, v in row.items() if k not in (ref.row, ref.key) and v is not None)
            return max(0, min(wrong, critical[0]["n"] - critical[0]["detected"] - shown))
        return max(0, wrong)
    return max(0, question["right"] if ref.kind in ("diagonal", "right") else wrong)


def _known(report: Mapping[str, Any], equation: Equation, null: _Ref) -> int | None:
    """The one value a null cell can take, or None when it can take more than one: from its
    equation, the other cells' bounds and that no count is negative."""
    added, subtracted, total = equation
    rest = total
    rest -= sum(r.value for r in added if r.value is not None)
    rest += sum(r.value for r in subtracted if r.value is not None)
    others_added = [r for r in added if r.value is None and r is not null]
    others_subtracted = [r for r in subtracted if r.value is None and r is not null]
    if any(r is null for r in added):  # null = rest - others_added + others_subtracted
        low = rest - sum(_bound(report, r) for r in others_added)
        high = rest + sum(_bound(report, r) for r in others_subtracted)
    else:  # null = others_added - others_subtracted - rest
        low = -rest - sum(_bound(report, r) for r in others_subtracted)
        high = -rest + sum(_bound(report, r) for r in others_added)
    low, high = max(low, 0), min(high, _bound(report, null))
    return low if low >= high else None


def _range(report: Mapping[str, Any], equation: Equation, null: _Ref) -> tuple[int, int]:
    """The values a null cell can take (`_known`'s bounds before it chooses one)."""
    added, subtracted, total = equation
    rest = total
    rest -= sum(r.value for r in added if r.value is not None)
    rest += sum(r.value for r in subtracted if r.value is not None)
    others_added = [r for r in added if r.value is None and r is not null]
    others_subtracted = [r for r in subtracted if r.value is None and r is not null]
    if any(r is null for r in added):
        low = rest - sum(_bound(report, r) for r in others_added)
        high = rest + sum(_bound(report, r) for r in others_subtracted)
    else:
        low = -rest - sum(_bound(report, r) for r in others_subtracted)
        high = -rest + sum(_bound(report, r) for r in others_added)
    return max(low, 0), min(high, _bound(report, null))


def suppress(report: Mapping[str, Any]) -> dict[str, Any]:
    """The one suppression function (owner decision 3 of the plan: 1 to 9 suppressed, zeros
    shown, nothing shared under 100 cases). The sender runs it before writing or sending; the
    registry refuses a payload it would change. Applied to its own output it changes nothing.

    | Field | Rule |
    |---|---|
    | `dataset.n` | shared exactly; under `MIN_DATASET` cases the run is not shared at all |
    | a question's `n` and `right` | totals: shared exactly |
    | confusion cells, calibration `right` and `wrong`, outcomes | cells: 1 to 9 is null |
    | `decisions.critical` (`n`, `detected`, `auto_missed`), `false_automation` | safety counts:
      shared exactly, never suppressed, so a missed critical case never disappears |
    | `tokens` | totals: shared exactly |

    Then complementary suppression: while a null cell can take only one value, given the
    equation it is in (`_equations`), the other cells' bounds (`_bound`) and that no count is
    negative, the smallest shown cell of that equation is made null too (a zero first; never
    a cell a safety count states). A null whose equation has no shown cell left to suppress
    is known from the others whatever is done, so it is shown.
    """
    out = copy.deepcopy(dict(report))
    n = (out.get("dataset") or {}).get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < MIN_DATASET:
        raise MendariError(
            f"this run cannot be shared: it has fewer than {MIN_DATASET} cases "
            "(too few for its counts to describe no one)"
        )

    def small(count: Any) -> bool:
        return isinstance(count, int) and not isinstance(count, bool) and 0 < count < MIN_GROUP

    for added, subtracted, _ in _equations(out):
        for ref in (*added, *subtracted):
            if small(ref.value) and not ref.exempt:
                ref.holder[ref.key] = None

    _add_up(out)
    # Each pass nulls a shown cell or shows the one null left in its equation: never more passes
    # than there are cells (the adversarial review of LP-22f: counts that could not add up made
    # this loop run for good).
    passes = 1 + sum(len(added) + len(subtracted) for added, subtracted, _ in _equations(out))
    changed = True
    while changed:
        passes -= 1
        if passes < 0:
            raise MendariError(NOT_COUNTS)
        changed = False
        for equation in _equations(out):
            added, subtracted, _ = equation
            refs = [*added, *subtracted]
            nulls = [r for r in refs if r.value is None]
            for null in nulls:
                known = _known(out, equation, null)
                if known is None:
                    continue
                candidates = [r for r in refs if r.value is not None and not r.exempt]
                if candidates:
                    chosen = min(candidates, key=lambda r: r.value)
                    chosen.holder[chosen.key] = None
                elif len(nulls) == 1:
                    null.holder[null.key] = known
                else:  # two or more nulls, nothing left to suppress, one pinned: not counts
                    raise MendariError(NOT_COUNTS)
                changed = True
                break
            if changed:
                break
    _add_up(out)
    return out


NOT_COUNTS = "these counts cannot be shared: they do not add up to the totals they state"


def _add_up(report: Mapping[str, Any]) -> None:
    """Refuse counts that cannot be one run's: an equation the payload states that does not
    hold where every cell is shown, or that no values of its null cells could make hold."""
    for added, subtracted, total in _equations(report):
        refs = [*added, *subtracted]
        nulls = [r for r in refs if r.value is None]
        if not nulls:
            plus = sum(r.value for r in added)
            minus = sum(r.value for r in subtracted)
            if plus - minus != total:
                raise MendariError(NOT_COUNTS)
            continue
        for null in nulls:
            low, high = _range(report, (added, subtracted, total), null)
            if low > high:
                raise MendariError(NOT_COUNTS)


def site_report(ledger: Ledger, *, remote_consent: bool = False) -> SiteReportV1:
    """The payload a site shares about one run: counted from its ledger, suppressed, checked."""
    return SiteReportV1.model_validate(
        suppress(counted_report(ledger, remote_consent=remote_consent))
    )


def shareable(ledger: Ledger, *, remote_consent: bool = False) -> dict[str, Any]:
    """`site_report` as the JSON-ready dict that is sent. A run that cannot be shared is a
    `MendariError` naming the fields (never their values)."""
    try:
        return site_report(ledger, remote_consent=remote_consent).model_dump(mode="json")
    except ValidationError as error:
        fields = sorted({where(e["loc"]) for e in error.errors()})
        shown = ", ".join(fields[:5]) + (f" and {len(fields) - 5} more" if len(fields) > 5 else "")
        raise MendariError(
            f"this run cannot be shared: {shown} must be a short name or a count in range"
        ) from None


def unchanged_by_suppression(report: SiteReportV1) -> bool:
    """What the registry checks: the payload is what `suppress` makes of it."""
    shared = report.model_dump(mode="json")
    try:
        return suppress(shared) == shared
    except MendariError:
        return False


def adds_up(report: SiteReportV1) -> bool:
    """Whether a payload's counts are one run's: every equation it states holds where its cells
    are shown, and can hold where they are null (`_add_up`). What the registry checks of a
    publisher's evaluation, whose counts are not suppressed (`hub/evaluations.py`)."""
    try:
        _add_up(report.model_dump(mode="json"))
    except MendariError:
        return False
    return True


_FIELD_NAMES = _field_names()
