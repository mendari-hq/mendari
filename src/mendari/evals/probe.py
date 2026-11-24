# SPDX-License-Identifier: Apache-2.0
"""`mendari probe`: find an agent's failures by changing its own cases.

An evaluation says how an agent does on the cases someone wrote. The probe takes those cases
(`evals/probe_cases.py`: a labelled dataset, or the agent's behaviour tests) and makes variants
of the text of each (`evals/probe_variants.py`: offline transformations that keep the meaning,
variants a model writes, variants a file gives), then decides the original and each variant on
the real decision path (`core/decision.py`), so every outcome is the policy's.

**What counts as a failure** (metamorphic testing, so it works without labels): a variant that
keeps the meaning must get the answers the original got. With labels the expected answer is the
label; without, the agent's own answer on the original. A variant that changes what is right
(a denial of the critical finding, with its own `labels`) must get those answers instead. A
finding is what the change made worse: a case the agent already gets wrong as written is the
evaluation's to report, once, not the probe's once per variant.

**How bad** (`HARMS`, worst first):

- `automated`: the variant would be automated (AUTO) while its recommendation is wrong or its
  case critical, and the original was not.
- `critical_lost`: an answer the agent's manifest calls critical (`evaluation.critical`) was
  given on the original and not on the variant; a person still reads the case.
- `wrong`: another answer went from the expected one to another (or did not follow a variant
  that changes it).
- `changed`: an answer changed, to no worse (the original was wrong already).

**Cost**: a case is what the agent reads of it (`probe_cases.identity_fields`), so the same
given twice is probed once; `Options.limit` and `Options.variants` bound the calls, and `plan`
says how many there will be before any is made (`Plan.summary`). Answers an agent reads from
its mock file are canned: they cost no call, the report says they are canned, and where they
cannot differ from one input to another nothing is decided at all (`Plan.nothing`).

The report (`probe_report.FORMAT`) holds ids, names of transformations, questions and labels,
and counts: never a case's text. The text of a variant goes only into the agent's own tests
(`keep`, `evals/probe_tests.py`) or a file the caller names (`dump_variants`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mendari.core.decision import Decision, DecisionResult
from mendari.core.pack import Pack
from mendari.core.spec import Outcome, PackManifest
from mendari.errors import MendariError
from mendari.evals import probe_tests
from mendari.evals.canonical import canonical_input
from mendari.evals.probe_cases import (
    Case,
    Source,
    critical,
    distinct_cases,
    field_of,
    in_turn,
    is_critical,
    leaves,
    source,
)
from mendari.evals.probe_model import Writer
from mendari.evals.probe_report import FORMAT, PLAN_FORMAT, verdict
from mendari.evals.probe_transforms import OFFLINE
from mendari.evals.probe_variants import (
    NEGATION,
    NO_FINDING,
    NOT_WRITTEN,
    Given,
    Job,
    Variant,
    by_a_model,
    denials,
    dump_variants,
    jobs,
    load_variants,
    name_problem,
    offline,
    written,
)
from mendari.parallel import parallel_map
from mendari.providers.agent import ENDPOINT_ENV, AgentProvider

__all__ = [
    "Finding",
    "Kept",
    "Options",
    "Plan",
    "Probed",
    "as_test",
    "dump_variants",
    "keep",
    "load_variants",
    "plan",
    "run",
    "source",
]

DEFAULT_LIMIT = 50  # cases
MAX_VARIANTS = 10  # of one case by one transformation
REMOTE_WORKERS = 8  # decisions, or variants a model writes, at once
# How bad a finding is, worst first (the module's docstring).
AUTOMATED, CRITICAL_LOST, WRONG, CHANGED = "automated", "critical_lost", "wrong", "changed"
HARMS = (AUTOMATED, CRITICAL_LOST, WRONG, CHANGED)
# Why a variant was not tried, as the report counts it (`probe_variants` has a model's).
NOTHING_TO_CHANGE = "nothing_to_change"  # the variant says what the original says
SAME_AS_ANOTHER = "same_as_another"  # the same text as an earlier variant of the case
NOT_VALID_INPUT = "not_valid_input"  # the agent's input schema refuses the variant
CASE_NOT_PROBED = "case_not_probed"  # a file's variant of a case the probe did not decide
# Where a finding's expected answers come from (`Finding.expected_from`).
FROM_LABELS, FROM_ORIGINAL, FROM_MODEL, FROM_VARIANT = "labels", "original", "model", "variant"


@dataclass(frozen=True)
class Options:
    """What a probe does: which transformations, on how many cases, how many variants of each
    case by each transformation, from which seed. `limit=None` is every case. `fields` names
    the text fields to change, in place of those the agent reads."""

    transformations: tuple[str, ...] = tuple(OFFLINE)
    seed: int = 0
    limit: int | None = DEFAULT_LIMIT
    variants: int = 1
    fields: tuple[str, ...] | None = None
    workers: int | None = None


@dataclass(frozen=True)
class Finding:
    """What a variant made worse. `answers`: each question whose answer changed or is not the
    expected one, as the labels on the original, on the variant, and expected. `outcome`: the
    original's and the variant's. `expect`: the behaviour-test expectation the variant fails
    now (None for `changed`, which has no right answer to hold it to). `expected_from`: where
    its answers come from: the `labels`, the agent's own answers on the `original` case, the
    `model` that wrote a variant changing them, or such a `variant` a person wrote."""

    variant: Variant
    harm: str
    answers: dict[str, dict[str, str | None]]
    outcome: tuple[str, str]
    expect: dict[str, Any] | None
    expected_from: str


@dataclass(frozen=True)
class Kept:
    """What `keep` wrote into the agent: the ids of the tests added to those that run, of those
    that wait for a person, how many findings were tests there already, and how many were left
    out and why (`site`: a site's own case; `no_expectation`: nothing was worse)."""

    added: tuple[str, ...]
    waiting: tuple[str, ...]
    already: int
    left_out: dict[str, int]


@dataclass
class _Counts:
    """One transformation's line of the report."""

    name: str
    keeps_meaning: bool = True
    tried: int = 0
    changed: int = 0
    critical_lost: int = 0
    automated: int = 0
    unanswered: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


@dataclass
class Plan:
    """What a probe will do, worked out without a call to an agent or a model: the cases, the
    variants built already, those a model is to write (`jobs`), and how many decisions and
    calls running it makes. `nothing` says why nothing can be probed, when the agent's answers
    cannot differ between inputs. A plan is run once."""

    decision: Decision
    source: Source
    options: Options
    counts: dict[str, _Counts]
    in_source: int
    calls_per_decision: int
    canned: str | None
    nothing: str | None
    writer: Writer | None = None
    fields: tuple[str, ...] = ()
    cases: tuple[Case, ...] = ()
    variants: tuple[Variant, ...] = ()
    jobs: tuple[Job, ...] = ()
    distinct: int = 0
    refused: int = 0
    # What each case's variants say, by transformation: a second one saying the same is left out.
    said: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    @property
    def decisions(self) -> int:
        """The most decisions a run makes: each case as written, and each variant."""
        return len(self.cases) + len(self.variants) + len(self.jobs)

    def take(self, made: Variant | None, case: Case, name: str) -> bool:
        """Whether a variant is one to decide; counted with why not, otherwise: nothing was
        changed, it says what an earlier variant of the case says, or the input schema refuses
        it."""
        counts = self.counts[name]
        original = canonical_input(case.input)
        said = self.said.setdefault((case.key, name), {original})
        if made is None:
            counts.skip(NOTHING_TO_CHANGE)
            return False
        what = canonical_input(made.input)
        if what in said:
            counts.skip(NOTHING_TO_CHANGE if what == original else SAME_AS_ANOTHER)
            return False
        if self.decision.validate(made.input):
            counts.skip(NOT_VALID_INPUT)
            return False
        said.add(what)
        return True

    def summary(self) -> dict[str, Any]:
        """What `--dry-run` says (`probe_report.PLAN_FORMAT`): the cases, the variants by
        transformation, and the most decisions and calls a run makes."""
        variants = dict.fromkeys(self.counts, 0)
        names = [variant.transformation for variant in self.variants]
        for name in (*names, *(job.transformation for job in self.jobs)):
            variants[name] += 1
        return {
            "format": PLAN_FORMAT,
            **_header(self),
            "cases": _case_counts(self),
            "transformations": [
                {
                    "name": name,
                    "variants": variants[name],
                    "skipped": dict(sorted(counts.skipped.items())),
                }
                for name, counts in self.counts.items()
            ],
            "calls": {
                "decisions": self.decisions,
                "agent": self.decisions * self.calls_per_decision,
                "generator": len(self.jobs),
            },
        }


@dataclass(frozen=True)
class Probed:
    """A probe's result: its report (`probe_report.FORMAT`: ids, names and counts), the
    findings worst first, and every variant that was decided."""

    report: dict[str, Any]
    findings: list[Finding]
    variants: list[Variant]


# -- the plan --------------------------------------------------------------------------------------


def plan(
    decision: Decision,
    cases: Source,
    options: Options,
    *,
    given: Sequence[Given] = (),
    writer: Writer | None = None,
) -> Plan:
    """What a probe of `cases` will do, with no call to an agent or a model: which cases, each
    offline variant and each `given` one of a file built and checked against the input schema,
    the variants `writer` (a model) is to write, and the calls it will take.

    The cases a file's variants name come first, in the file's order; then, when a
    transformation is named, the others (`probe_cases.in_turn`); `Options.limit` in all."""
    manifest = decision.pack.manifest
    _check(options, given, writer)
    by_model = [name for name in options.transformations if name not in OFFLINE]
    counts = {name: _Counts(name, name != NEGATION) for name in options.transformations}
    for variant in given:
        counts.setdefault(variant.transformation, _Counts(variant.transformation))
        if variant.labels:
            counts[variant.transformation].keeps_meaning = False
    planned = Plan(
        decision=decision,
        source=cases,
        options=options,
        counts=counts,
        in_source=len(cases.records),
        calls_per_decision=_remote_calls(decision),
        canned=_canned(decision),
        nothing=_cannot_vary(decision),
        writer=writer if by_model else None,
    )
    if planned.nothing is not None:
        return planned  # no variant can change the agent's answers: nothing is decided
    deniable = denials(manifest)
    if NEGATION in by_model and not deniable:
        raise MendariError(
            f"{NEGATION} denies a critical answer of a question with two labels (yes or no): "
            "this agent has none"
        )
    if planned.writer is not None:
        planned.writer.preflight()
    by_key, by_id, planned.refused = distinct_cases(decision, cases, options.fields)
    known = {record.id for record in cases.records}
    unknown = [variant for variant in given if variant.case not in known]
    if unknown:
        raise MendariError(
            f"{unknown[0].file}: {len(unknown)} variant{'' if len(unknown) == 1 else 's'} "
            f"name{'s' if len(unknown) == 1 else ''} a case that {cases.name} does not have (the "
            f"first: {unknown[0].case})"
        )
    named = {
        by_id[variant.case].key: by_id[variant.case] for variant in given if variant.case in by_id
    }
    chosen = list(named.values())
    if options.transformations:
        others = [case for case in by_key.values() if case.key not in named]
        chosen += in_turn(others, manifest, options.seed)
    if options.limit is not None:
        chosen = chosen[: options.limit]
    variants: list[Variant] = []
    asked: list[Job] = []
    for case in chosen:
        for name in options.transformations:
            for index in range(options.variants):
                if name in OFFLINE:
                    made = offline(case, name, index, options.seed, manifest)
                    if planned.take(made, case, name) and made is not None:
                        variants.append(made)
                    continue
                for job in jobs(case, name, index, manifest, deniable):
                    if isinstance(job, str):
                        counts[name].skip(job)
                    else:
                        asked.append(job)
        for variant in given:
            if by_id.get(variant.case) is case:
                made = variant.of(case)
                if planned.take(made, case, made.transformation):
                    variants.append(made)
    probed = {case.key for case in chosen}
    for variant in given:
        if variant.case not in by_id or by_id[variant.case].key not in probed:
            counts[variant.transformation].skip(CASE_NOT_PROBED)
    planned.fields = tuple(
        dict.fromkeys(field_of(place) for case in chosen for place in case.places)
    )
    planned.cases, planned.variants, planned.jobs = tuple(chosen), tuple(variants), tuple(asked)
    planned.distinct = len(by_key)
    return planned


def _check(options: Options, given: Sequence[Given], writer: Writer | None) -> None:
    for name in options.transformations:
        problem = name_problem(name)
        if problem is not None:
            raise MendariError(problem)
        if by_a_model(name) is not None and writer is None:
            raise MendariError(f"{name} is written by a model: none was given to ask")
    if not options.transformations and not given:
        raise MendariError("name at least one transformation, or give a file of variants")
    if len(set(options.transformations)) != len(options.transformations):
        raise MendariError("a transformation is named once")
    if options.seed < 0:
        raise MendariError("the seed is 0 or more")
    if options.limit is not None and options.limit < 1:
        raise MendariError("the limit is 1 case or more")
    if not 1 <= options.variants <= MAX_VARIANTS:
        raise MendariError(f"1 to {MAX_VARIANTS} variants of a case by a transformation")


def _canned_agents(decision: Decision) -> list[AgentProvider]:
    """The agents that answer from their mock file now."""
    return [
        leaf
        for leaf in leaves(decision)
        if isinstance(leaf, AgentProvider) and leaf.canned is not None
    ]


def _canned(decision: Decision) -> str | None:
    """Why answers of this configuration are canned (`providers.agent.CANNED`), or None."""
    found = _canned_agents(decision)
    return found[0].canned if found else None


def _cannot_vary(decision: Decision) -> str | None:
    """Why no variant can change this configuration's answers, or None: every provider answers
    from a mock file that has no `cases` to match an input against, so every input gets its
    `default`."""
    fixed = [leaf for leaf in _canned_agents(decision) if not leaf.canned_answers_vary]
    if not fixed or len(fixed) != len(list(leaves(decision))):
        return None
    return (
        "the agent's answers are canned and the same whatever a case says "
        f"({fixed[0].settings.mock_file} has no `cases`): set {ENDPOINT_ENV} and probe your agent"
    )


def _remote_calls(decision: Decision) -> int:
    """The most calls one decision makes to an agent or a model: one for each provider that
    answers over the network, those that share a call counted once. Canned answers are read on
    this machine and are no call."""
    canned = _canned_agents(decision)
    shared, calls = set(), 0
    for leaf in leaves(decision):
        if not leaf.remote or any(leaf is found for found in canned):
            continue
        key = leaf.batch_key()
        if key is None:
            calls += 1
        elif key not in shared:
            shared.add(key)
            calls += 1
    return calls


# -- the run ---------------------------------------------------------------------------------------


def run(planned: Plan) -> Probed:
    """Decide each case as written, have the model write its variants of the cases the agent
    answered, decide each variant, and report what the variants made worse."""
    if planned.nothing is not None:
        return Probed(_report(planned, [], 0, [], {}, 0), [], [])
    decision, writer = planned.decision, planned.writer
    manifest = decision.pack.manifest
    workers = planned.options.workers or (REMOTE_WORKERS if planned.calls_per_decision else 1)
    before = parallel_map(decision.decide, [case.input for case in planned.cases], workers)
    answered = {
        case.key: result
        for case, result in zip(planned.cases, before, strict=True)
        if _answered(manifest, result)
    }
    if not answered:
        codes = sorted({code for result in before for code in result.errors.values()})
        raise MendariError(
            f"the agent answered none of the {len(before)} cases as written "
            f"({', '.join(codes) or 'refused'}): there is nothing to compare a variant with"
        )
    tried = [variant for variant in planned.variants if variant.case.key in answered]
    not_written: dict[str, int] = {}
    asked = writer.calls if writer is not None else 0
    if writer is not None:
        tried += _written(planned, writer, answered, not_written)
    after = parallel_map(decision.decide, [variant.input for variant in tried], workers)
    findings: list[Finding] = []
    decided: list[Variant] = []
    for variant, result in zip(tried, after, strict=True):
        counts = planned.counts[variant.transformation]
        if result.rejected:
            counts.skip(NOT_VALID_INPUT)
            continue
        decided.append(variant)
        counts.tried += 1
        if not _answered(manifest, result):
            counts.unanswered += 1  # no answer: the policy holds it for a person
            continue
        finding = _judged(manifest, variant, answered[variant.case.key], result)
        if finding is not None:
            findings.append(finding)
            counts.changed += 1
            counts.automated += finding.harm == AUTOMATED
            counts.critical_lost += bool(_lost(manifest, finding))
    order = {name: number for number, name in enumerate(planned.counts)}
    # Worst first; among variants automated, those that also lost a critical answer.
    findings.sort(
        key=lambda found: (
            HARMS.index(found.harm),
            not _lost(manifest, found),
            found.variant.case.id,
            order[found.variant.transformation],
            found.variant.index,
        )
    )
    asked = writer.calls - asked if writer is not None else 0
    report = _report(planned, findings, len(answered), [*before, *after], not_written, asked)
    return Probed(report, findings, decided)


def _answered(manifest: PackManifest, result: DecisionResult) -> bool:
    """Whether a decision answered every question: the input was taken, and no provider failed."""
    whole = all(question in result.answers for question in manifest.questions)
    return whole and not result.errors and not result.rejected


def _written(
    planned: Plan,
    writer: Writer,
    answered: Mapping[str, DecisionResult],
    not_written: dict[str, int],
) -> list[Variant]:
    """The variants the model wrote, of the cases the agent answered: those to decide. A
    denial of a finding whose label is not known is asked only where the agent itself gave the
    critical answer. What the model did not write is counted by why (`not_written`)."""
    asked = []
    for job in planned.jobs:
        before = answered.get(job.case.key)
        if before is None:
            continue
        if job.denies is not None and job.denies[0] not in job.case.labels:
            question, label, _ = job.denies
            if before.answers[question].label != label:
                planned.counts[job.transformation].skip(NO_FINDING)
                continue
        asked.append(job)
    seed = planned.options.seed
    made = parallel_map(lambda job: written(job, writer, seed), asked, REMOTE_WORKERS)
    taken = []
    for job, variant in zip(asked, made, strict=True):
        if isinstance(variant, str):
            planned.counts[job.transformation].skip(NOT_WRITTEN)
            not_written[variant] = not_written.get(variant, 0) + 1
        elif planned.take(variant, job.case, job.transformation):
            taken.append(variant)
    return taken


def _lost(manifest: PackManifest, finding: Finding) -> list[str]:
    """The critical answers a finding lost: given on the original, expected of the variant,
    and not given on it."""
    return [
        question
        for question, label in critical(manifest)
        if question in finding.answers
        and finding.answers[question]["expected"] == label
        and finding.answers[question]["original"] == label
        and finding.answers[question]["variant"] != label
    ]


def _judged(
    manifest: PackManifest, variant: Variant, before: DecisionResult, after: DecisionResult
) -> Finding | None:
    """What the variant made worse than the original, or None (the module's docstring)."""
    case = variant.case
    questions = list(manifest.questions)
    critical_answers = critical(manifest)
    primary = manifest.policy.primary
    was = {question: before.answers[question].label for question in questions}
    now = {question: after.answers[question].label for question in questions}
    # What is right as written, and what the variant itself changes of it (a denial).
    written_right = {question: case.labels.get(question, was[question]) for question in questions}
    changes = dict(variant.labels or {})
    expected = {question: changes.get(question, written_right[question]) for question in questions}
    # An answer the agent had as expected on the original and has otherwise on the variant, or
    # one the variant changes that the agent did not follow.
    worse = [
        question
        for question in questions
        if now[question] != expected[question]
        and (question in changes or was[question] == written_right[question])
    ]
    lost = [q for q, label in critical_answers if q in worse and expected[q] == label]
    a_critical_case = any(expected[q] == label for q, label in critical_answers)
    critical_as_written = any(written_right[q] == label for q, label in critical_answers)
    automated_badly = after.outcome is Outcome.AUTO and (
        a_critical_case or now[primary] != expected[primary]
    )
    # The original's own bad automation is the evaluation's finding, unless the variant
    # changes what is right about the automated answer.
    as_written = (
        before.outcome is Outcome.AUTO
        and (critical_as_written or was[primary] != written_right[primary])
        and primary not in changes
        and a_critical_case == critical_as_written
    )
    if automated_badly and not as_written:
        harm = AUTOMATED
    elif lost:
        harm = CRITICAL_LOST
    elif worse:
        harm = WRONG
    elif any(now[q] != was[q] for q in questions if q not in changes):
        harm = CHANGED
    else:
        return None
    answers: dict[str, dict[str, str | None]] = {
        question: {
            "original": was[question],
            "variant": now[question],
            "expected": expected[question],
        }
        for question in questions
        if now[question] != was[question] or question in worse
    }
    expect: dict[str, Any] | None = None
    if harm != CHANGED:
        held = {question: expected[question] for question in worse}
        if harm == AUTOMATED and now[primary] != expected[primary]:
            held[primary] = expected[primary]
        expect = {"answers": held} if held else {}
        if a_critical_case:
            expect["not_outcome"] = Outcome.AUTO.value
    return Finding(
        variant=variant,
        harm=harm,
        answers=answers,
        outcome=(before.outcome.value, after.outcome.value),
        expect=expect,
        expected_from=_expected_from(variant, expect, expected, critical_answers),
    )


def _expected_from(
    variant: Variant,
    expect: Mapping[str, Any] | None,
    expected: Mapping[str, str],
    critical_answers: Sequence[tuple[str, str]],
) -> str:
    """Whose word a finding's expected answers are (`Finding.expected_from`)."""
    if variant.labels:
        return FROM_MODEL if variant.model is not None else FROM_VARIANT
    relied_on = set((expect or {}).get("answers", {}))
    if (expect or {}).get("not_outcome"):
        relied_on |= {q for q, label in critical_answers if expected[q] == label}
    labelled = all(question in variant.case.labels for question in relied_on or expected)
    return FROM_LABELS if labelled else FROM_ORIGINAL


# -- the report ------------------------------------------------------------------------------------


def _header(planned: Plan) -> dict[str, Any]:
    """What a report and a plan's summary both say first: the agent, its configuration, whether
    its answers are canned, the model that writes variants, the source, the seed and the text
    fields."""
    pack = planned.decision.pack
    return {
        "agent": {
            "id": pack.id,
            "name": pack.manifest.name,
            "version": pack.version,
            "digest": pack.digest,
        },
        "providers": {slot: info.variant for slot, info in planned.decision.provider_info.items()},
        "canned": planned.canned,
        "nothing": planned.nothing,
        "written_by": None if planned.writer is None else planned.writer.model,
        "source": {"kind": planned.source.kind, "name": planned.source.name},
        "seed": planned.options.seed,
        "fields": list(planned.fields),
    }


def _case_counts(planned: Plan) -> dict[str, int]:
    """How many cases the source has, how many texts, and of those probed how many have a label
    at all (none: every variant is held to the agent's own answers) and a critical one."""
    manifest = planned.decision.pack.manifest
    return {
        "in_source": planned.in_source,
        "distinct": planned.distinct,
        "probed": len(planned.cases),
        "labelled": sum(bool(case.labels) for case in planned.cases),
        "critical": sum(is_critical(case.labels, manifest) for case in planned.cases),
        "refused": planned.refused,
    }


def _report(
    planned: Plan,
    findings: list[Finding],
    answered: int,
    results: Sequence[DecisionResult],
    not_written: Mapping[str, int],
    asked: int,
) -> dict[str, Any]:
    lines = list(planned.counts.values())
    totals = {
        "tried": sum(line.tried for line in lines),
        "changed": sum(line.changed for line in lines),
        "critical_lost": sum(line.critical_lost for line in lines),
        "automated": sum(line.automated for line in lines),
    }
    calls: dict[str, Any] = {
        "decisions": len(results),
        "agent": sum(metered.calls for result in results for metered in result.usage.values()),
        "generator": asked,
    }
    if calls["agent"]:
        # A call no answer priced has a cost nobody knows: never counted as free.
        costs = [result.cost_usd for result in results]
        known = [cost for cost in costs if cost is not None]
        calls["cost_usd"] = round(sum(known), 6) if len(known) == len(costs) else None
    if asked and planned.writer is not None:
        cost = planned.writer.cost_usd
        calls["generator_cost_usd"] = None if cost is None else round(cost, 6)
    report = {
        "format": FORMAT,
        **_header(planned),
        "cases": {**_case_counts(planned), "unanswered": len(planned.cases) - answered},
        "calls": calls,
        "transformations": [
            {
                "name": line.name,
                "keeps_meaning": line.keeps_meaning,
                "tried": line.tried,
                "changed": line.changed,
                "critical_lost": line.critical_lost,
                "automated": line.automated,
                "unanswered": line.unanswered,
                "skipped": dict(sorted(line.skipped.items())),
            }
            for line in lines
        ],
        "totals": totals,
        "not_written": dict(sorted(not_written.items())),
        "findings": [_finding_json(finding) for finding in findings],
    }
    report["verdict"] = verdict(
        totals, answered, canned=planned.canned is not None, nothing=planned.nothing
    )
    return report


def _finding_json(finding: Finding) -> dict[str, Any]:
    variant = finding.variant
    return {
        "id": variant.id,
        "case": variant.case.id,
        "transformation": variant.transformation,
        "detail": variant.detail,
        "harm": finding.harm,
        "answers": finding.answers,
        "outcome": {"original": finding.outcome[0], "variant": finding.outcome[1]},
        "expected_from": finding.expected_from,
    }


# -- each failure, kept as a behaviour test --------------------------------------------------------


def as_test(finding: Finding) -> dict[str, Any]:
    """The behaviour test a finding is written as (`evals/probe_tests.py`): the variant's input,
    what the agent must decide of it, and where it came from."""
    variant, case = finding.variant, finding.variant.case
    found: dict[str, Any] = {
        "id": variant.id,
        "found_by": probe_tests.FOUND_BY,
        "transformation": variant.transformation,
    }
    if variant.detail:
        found["detail"] = variant.detail
    if variant.seed is not None:
        found["seed"] = variant.seed
    found |= {
        "variant": variant.index,
        "original": {"source": case.source, "case": case.id},
        "harm": finding.harm,
        "expected_from": finding.expected_from,
    }
    if variant.model is not None:
        found["model"] = variant.model
    return {
        "name": f"{variant.transformation} variant of {case.source} case {case.id} ({variant.id})",
        "input": variant.input,
        "expect": finding.expect,
        probe_tests.PROBE_KEY: found,
    }


def keep(pack: Pack, findings: Sequence[Finding]) -> Kept:
    """Write each failure into the agent as a behaviour test `mendari test` runs, once
    (`probe_tests.write`). A finding whose expected answer a model changed is written among
    those that wait for a person. Left out: a finding with nothing worse to hold the agent to
    (`changed`), and any of a site's own case, whose text stays where it is marked as one."""
    found: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    left_out: dict[str, int] = {}
    for finding in findings:
        if finding.expect is None:
            left_out["no_expectation"] = left_out.get("no_expectation", 0) + 1
        elif finding.variant.case.site:
            left_out["site"] = left_out.get("site", 0) + 1
        elif finding.expected_from == FROM_MODEL:
            unconfirmed.append(as_test(finding))
        else:
            found.append(as_test(finding))
    kept = probe_tests.write(pack, found, unconfirmed)
    return Kept(kept.added, kept.waiting, kept.already, left_out)
