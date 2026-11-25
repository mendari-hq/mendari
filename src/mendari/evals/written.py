# SPDX-License-Identifier: Apache-2.0
"""Written output (ADR-0025): what an agent wrote, graded by clinicians against a rubric and by a
model judge against the same rubric, and how far the judge is to be believed.

A run (`run`) asks the agent for each case's text (or reads it from the input), asks the judge
for its grades, and keeps a ledger (`mendari.written-ledger/1`): one line a case, holding the
case's hash, the item's id, the judge's grades and nothing of the text. Clinicians' grades come
from a file (`mendari.written-grades/1`) or from a grading round (`core/written.py`). The report
(`mendari.written/1`, `report`) is computed from the ledger, the grades and the seeds alone, so
a second run gives the same bytes. It holds no text and no case id.

**Grades bind to the text.** An item's id is a keyed hash of the case's id, the output's name
and the exact text. A grade whose item is not in the run is stale: counted, never used. A grade
given under another wording of the rubric (`rubric_sha256`) is refused.

**Agreement** is Cohen's kappa with linear weights on the levels' declared scores
(`rater_agreement.weighted_kappa`), with a percentile-bootstrap interval over cases. Per
criterion: the judge with clinicians on the outputs both graded, beside clinician with clinician
on the outputs two clinicians graded (the reference; it gates nothing). Where two clinicians
differ on an output the less favourable grade stands, and the output is counted as disputed.

What a report publishes of a judge is a pass share, so agreement over the levels is not enough:
a judge one level kinder at the pass line agrees closely on the scale and disagrees on who
passes. Three more things are therefore measured on the outputs both graded, about pass or fail:
plain Cohen's kappa on that dichotomy (the same number as the weighted kappa on a yes/no
criterion); an exact two-sided McNemar test on the discordant pairs (binomial, exact in
fractions, at `SYMMETRY_ALPHA`), which says whether the judge leans to one side; and the
judge's pass share beside the clinicians' on the same outputs.

An output the judge could not grade is never a pass and never dropped: on a must-not-fail
criterion a clinician-failed output it could not grade counts as a failure it did not find, and
the report counts, per criterion, the outputs it could not grade among those clinicians graded
and among the rest.

**The gate**, per criterion, has three states:

- *insufficient evidence* when any holds: fewer than `MIN_PAIRED` outputs graded by both; fewer
  than `MIN_EACH_SIDE` clinician verdicts on either side of the pass line; either kappa or its
  interval not estimable; for a must-not-fail criterion, fewer clinician-found failures than a
  judge that found every one would need (`min_failures`);
- *not validated* when, the evidence being sufficient, any holds: the lower bound of the
  weighted kappa, or of the pass-or-fail kappa, is under the agreement floor; for a
  must-not-fail criterion, the exact lower bound of the failures the judge found is under its
  floor; the judge leans lenient (the McNemar test rejects symmetry and it passes more outputs
  than clinicians do); or it could not grade more than `MAX_JUDGE_ERRORS` of the outputs
  clinicians graded, or of the rest;
- *validated* otherwise. A judge that leans strict stays eligible, and is flagged.

Bounds are worked out from the counts and the unrounded bootstrap, never from a stored six-place
figure, and a state is never decided on fewer than `stats.RESAMPLES` resamples: with a handful
the interval is a draw, not a bound. The judge's grades for the outputs no clinician graded
(`judged`) are reported only for a validated criterion, with how many of them it could not
grade. Otherwise they are withheld: null, with the state and its reasons. What is withheld is
the report's figure: the run's ledger still holds each grade the judge gave, by case hash, since
it is the record a result is recomputed from.

The floors are the engine's (`core/spec.py`). A manifest may raise `agreement` and
`must_pass_recall` and lower `max_judge_errors`, never the reverse, and every report repeats
the five it was decided on."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from itertools import permutations
from typing import TYPE_CHECKING, Any

from mendari import __version__
from mendari.core.spec import (
    AGREEMENT_FLOOR,
    MAX_JUDGE_ERRORS,
    MUST_PASS_RECALL_FLOOR,
    PackManifest,
)
from mendari.errors import MendariError, ProviderError
from mendari.evals import stats
from mendari.evals.ledger import CASE_HASH, canonical, case_hash
from mendari.evals.rater_agreement import kappa as plain_kappa
from mendari.evals.rater_agreement import measured, weighted_kappa
from mendari.providers.base import METER, Meter, Metered, get_path, select_fields

if TYPE_CHECKING:
    from mendari.core.decision import Decision
    from mendari.core.pack import Pack
    from mendari.providers.judge import Judge

__all__ = [
    "AGREEMENT_FLOOR",
    "FORMAT",
    "GRADES_FORMAT",
    "LEDGER_FORMAT",
    "MAX_JUDGE_ERRORS",
    "MIN_EACH_SIDE",
    "MIN_PAIRED",
    "MUST_PASS_RECALL_FLOOR",
    "Grade",
    "Grades",
    "Item",
    "WrittenLedger",
    "WrittenRun",
    "grades_problems",
    "item_id",
    "load_grades",
    "min_failures",
    "report",
    "rubric_sha256",
    "run",
    "run_report",
    "written_text",
]

FORMAT = "mendari.written/1"
LEDGER_FORMAT = "mendari.written-ledger/1"
GRADES_FORMAT = "mendari.written-grades/1"
# `AGREEMENT_FLOOR`: the judge's weighted kappa with clinicians; its interval's lower bound is
# at least this. `MUST_PASS_RECALL_FLOOR`: on a must-not-fail criterion, of the outputs a
# clinician failed, the share the judge failed too; its exact lower bound is at least this.
# Both are the manifest's to raise, so they are declared where the manifest is read.
# Fewer outputs graded by both than this is insufficient evidence, whatever the agreement.
MIN_PAIRED = 50
# So is fewer clinician verdicts than this on either side of the pass line.
MIN_EACH_SIDE = 10
# The level of the exact McNemar test that asks whether the judge leans to one side of the
# pass line: a fraction, since the test is decided exactly.
SYMMETRY_ALPHA = Fraction(1, 20)
LENIENT, STRICT = "lenient", "strict"
VALIDATED, NOT_VALIDATED, INSUFFICIENT = "validated", "not validated", "insufficient evidence"
STATES = (VALIDATED, NOT_VALIDATED, INSUFFICIENT)
MAX_LISTED = 50  # the case hashes a report lists under each heading
MAX_RESAMPLES = 100_000
# Why a case has no output: the agent wrote none; its input (or a text over the output's length)
# was refused; the slot that writes failed.
NONE, REFUSED, ERROR = "none", "refused", "error"
NO_OUTPUT = (NONE, REFUSED, ERROR)
GRADER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}\Z")
_ITEM = re.compile(r"^[0-9a-f]{32}\Z")
_ITEM_PREFIX = b"mendari-written-item-v1\x00"


def min_failures(floor: float) -> int:
    """The clinician-found failures a must-not-fail criterion needs before its floor can be met:
    the smallest n whose n-of-n lower bound (`stats.clopper_pearson`, at `stats.LEVEL`) is at or
    above `floor`; 17 for 0.80. On fewer, a judge that found every one is insufficient
    evidence. Written as `method.min_positives` is."""
    if isinstance(floor, bool) or not isinstance(floor, int | float):
        raise TypeError("floor must be a number")
    if not 0.0 < floor < 1.0:
        raise ValueError("floor must be in (0, 1)")
    # The n-of-n lower bound is ((1 - level) / 2) ** (1 / n), so n is about log(tail) / log(floor).
    # Start from there and step, so the answer is the bound `stats` computes, not this estimate.
    n = max(1, math.floor(math.log((1.0 - stats.LEVEL) / 2.0) / math.log(floor)))
    while stats.clopper_pearson(n, n, stats.LEVEL)[0] < floor:
        n += 1
    while n > 1 and stats.clopper_pearson(n - 1, n - 1, stats.LEVEL)[0] >= floor:
        n -= 1
    return n


# -- what is graded ------------------------------------------------------------------------------


def item_id(key: bytes, case_id: Any, output: str, text: str) -> str:
    """The id of one written text: an HMAC-SHA256, under the dataset's case key
    (`ledger.case_key`), of the case's id, the output's name and the exact text, cut to 128
    bits. Another text for the same case is another item, so a grade never outlives the text
    it was given for; without the key the id says nothing of the case or the text."""
    message = _ITEM_PREFIX + canonical([case_id, output, text]).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def rubric_view(manifest: PackManifest, rubric_name: str) -> dict[str, Any]:
    """What a grader of `rubric_name` reads, and nothing else of the rubric: the output's name
    and description, the context fields, and per criterion its type, instructions, levels (each
    its label, score and anchor, lowest first), pass line and whether it must not fail. Not the
    judge and not the floors: a grade does not depend on either."""
    rubric = manifest.rubrics[rubric_name]
    output = manifest.outputs[rubric.output]
    return {
        "output": {"name": rubric.output, "description": output.description},
        "context_fields": list(rubric.context_fields),
        "criteria": {
            key: {
                "type": criterion.type,
                "instructions": criterion.instructions,
                "levels": [
                    [level, score, criterion.criteria[level]] for level, score in criterion.scale()
                ],
                "pass_from": criterion.passing(),
                "must_pass": criterion.must_pass,
            }
            for key, criterion in rubric.criteria.items()
        },
    }


def rubric_sha256(manifest: PackManifest, rubric_name: str) -> str:
    """SHA-256 of `rubric_view` as canonical JSON: what grades are bound to. A reworded
    instruction or anchor, another pass line or another context is another rubric."""
    return hashlib.sha256(canonical(rubric_view(manifest, rubric_name)).encode()).hexdigest()


def written_text(
    manifest: PackManifest,
    output_name: str,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, str],
) -> str | None:
    """The text of one output of one case: the input's field for an `input` source, else what
    the agent wrote. None when there is none: no such field, a value that is not text, a blank
    one. Cleaned as an agent's response is (`providers.agent.clean_output`)."""
    from mendari.providers.agent import clean_output

    output = manifest.outputs[output_name]
    found = get_path(inputs, output.input) if output.input is not None else outputs.get(output_name)
    if not isinstance(found, str):
        return None
    return clean_output(found) or None


@dataclass(frozen=True)
class Item:
    """One written text to grade, with the context a grader reads beside it. In memory, or
    sealed in a grading round: never in a ledger, a report or a log."""

    case: str  # the case's ledger hash
    item: str  # `item_id`
    text: str = field(repr=False)
    context: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Grade:
    """One grader's grades of one item: a level for each criterion of the rubric."""

    item: str
    grader: str
    values: dict[str, str]


@dataclass(frozen=True)
class Grades:
    """Clinicians' grades and the rubric wording they were given under (`rubric_sha256`).
    `simulated`: the graders are no clinicians (a demonstration's, made by a script); the report
    says so wherever it is shown."""

    rubric: str
    grades: tuple[Grade, ...]
    simulated: bool = False


# -- the ledger ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class WrittenLedger:
    """The per-case ledger of a written-output run (`mendari.written-ledger/1`): canonical JSON
    Lines, its id the sha256 of its bytes. Line 1 is the header: the agent, the dataset, the
    case-hash scheme, the rubric (its hash, its output and each criterion's scale), the floors,
    the providers, the judge and the models. Then one line a case, in the dataset's order:

        {"c": <case hash>, "i": <item id or null>, "n": <why there is no output, or null>,
         "j": {<criterion>: [<level>, <confidence>]}, "e": {<criterion>: <error code>},
         "m": [<agent's model>, <judge's model>], "u": [cost_usd, input_tokens, output_tokens]}

    `m` holds indices into the header's `models` (null: none named); `u` is the judge's call."""

    header: dict[str, Any]
    cases: list[dict[str, Any]]

    def dumps(self) -> bytes:
        return "".join(canonical(line) + "\n" for line in [self.header, *self.cases]).encode()

    @property
    def id(self) -> str:
        return "sha256:" + hashlib.sha256(self.dumps()).hexdigest()

    @property
    def file_name(self) -> str:
        return self.id.removeprefix("sha256:") + ".jsonl"

    @classmethod
    def loads(cls, data: bytes, *, where: str = "ledger") -> WrittenLedger:
        """A ledger from its bytes, refused in one line when it is not one."""
        try:
            lines = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise MendariError(f"{where}: not a ledger (JSON Lines)") from None
        if not lines or not isinstance(lines[0], dict) or lines[0].get("format") != LEDGER_FORMAT:
            raise MendariError(f"{where}: not a {LEDGER_FORMAT} ledger")
        header, cases = lines[0], lines[1:]
        if not _header_ok(header):
            raise MendariError(f"{where}: its header is not a {LEDGER_FORMAT} header")
        if not all(isinstance(case, dict) and _case_ok(case, header) for case in cases):
            raise MendariError(f"{where}: a case line is not a written-output ledger case")
        if header["cases"].get("n") != len(cases):
            raise MendariError(f"{where}: its header and its cases disagree on how many there are")
        if len({case["c"] for case in cases}) != len(cases):
            raise MendariError(f"{where}: two cases have the same hash")
        items = [case["i"] for case in cases if case["i"] is not None]
        if len(set(items)) != len(items):
            raise MendariError(f"{where}: two cases have the same item")
        return cls(header, cases)


_HEADER = {
    "pack": dict, "dataset": dict, "cases": dict, "rubric": dict, "floors": dict,
    "providers": dict, "judge": dict, "models": list,
}  # fmt: skip
_FLOORS = ("agreement", "must_pass_recall", "max_judge_errors", "min_paired", "min_each_side")


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _header_ok(header: Mapping[str, Any]) -> bool:
    if not all(isinstance(header.get(key), kind) for key, kind in _HEADER.items()):
        return False
    rubric, floors = header["rubric"], header["floors"]
    criteria = rubric.get("criteria")
    if not (
        isinstance(rubric.get("name"), str)
        and isinstance(rubric.get("sha256"), str)
        and isinstance(rubric.get("output"), dict)
        and isinstance(criteria, dict)
        and criteria
        and isinstance(header["pack"].get("id"), str)
        and isinstance(header["dataset"].get("sha256"), str)
        and all(isinstance(model, str) for model in header["models"])
        and all(_number(floors.get(name)) for name in _FLOORS)
        and 0.0 < floors["agreement"] < 1.0
        and 0.0 < floors["must_pass_recall"] < 1.0
        and 0.0 <= floors["max_judge_errors"] <= 1.0
    ):
        return False
    for scale in criteria.values():
        if not isinstance(scale, dict):
            return False
        levels, scores = scale.get("levels"), scale.get("scores")
        if not (
            isinstance(levels, list)
            and isinstance(scores, list)
            and len(levels) == len(scores) >= 2
            and len(set(levels)) == len(levels)
            and all(isinstance(level, str) for level in levels)
            and all(_number(score) for score in scores)
            and scores == sorted(set(scores))
            and scale.get("pass_from") in levels
            and isinstance(scale.get("must_pass"), bool)
        ):
            return False
    return True


def _case_ok(case: Mapping[str, Any], header: Mapping[str, Any]) -> bool:
    criteria = header["rubric"]["criteria"]
    models = len(header["models"])
    item, reason, judged, errors = case.get("i"), case.get("n"), case.get("j"), case.get("e")
    if not isinstance(case.get("c"), str) or not isinstance(judged, dict):
        return False
    if not isinstance(errors, dict) or not isinstance(case.get("u"), list) or len(case["u"]) != 3:
        return False
    if (item is None) == (reason is None):
        return False  # an output or a reason there is none, never both and never neither
    if item is not None and not (isinstance(item, str) and _ITEM.match(item)):
        return False
    if reason is not None and (reason not in NO_OUTPUT or judged or errors):
        return False
    for key, grade in judged.items():
        scale = criteria.get(key)
        if scale is None or not isinstance(grade, list) or len(grade) != 2:
            return False
        if grade[0] not in scale["levels"] or not (_number(grade[1]) and 0.0 <= grade[1] <= 1.0):
            return False
    if not all(key in criteria and isinstance(code, str) for key, code in errors.items()):
        return False
    if set(judged) & set(errors):
        return False
    used = case.get("m")
    return (
        isinstance(used, list)
        and len(used) == 2
        and all(
            index is None
            or (isinstance(index, int) and not isinstance(index, bool) and 0 <= index < models)
            for index in used
        )
    )


@dataclass(frozen=True)
class WrittenRun:
    """A run's ledger, the items it found (their texts, in memory only) and its remote calls by
    provider type (the agent's and the judge's, for the usage ledger)."""

    ledger: WrittenLedger
    items: list[Item] = field(repr=False)
    usage: dict[str, Metered] = field(default_factory=dict)


def floors_of(manifest: PackManifest, rubric_name: str) -> dict[str, float | int]:
    """The floors a run of `rubric_name` is decided on: the engine's, or the rubric's own where
    it raises one."""
    rubric = manifest.rubrics[rubric_name]
    return {
        "agreement": rubric.agreement_floor(),
        "must_pass_recall": rubric.must_pass_recall_floor(),
        "max_judge_errors": rubric.max_judge_errors(),
        "min_paired": MIN_PAIRED,
        "min_each_side": MIN_EACH_SIDE,
    }


def run(
    decision: Decision,
    dataset: Any,
    rubric_name: str,
    *,
    judge: Judge,
    workers: int = 1,
) -> WrittenRun:
    """Run every case of `dataset` (a `harness.Dataset`): the agent writes (a `slot` source: one
    decision a case) or the input is read (an `input` source: nothing is called), then the judge
    grades the text. `decision` is a store-less one made as `mendari eval` makes it: nothing is
    recorded. Refused first: a rubric the agent does not have, a slot that runs no `agent`
    variant, a dataset whose records the agent cannot read."""
    from mendari.evals.harness import record_problems
    from mendari.parallel import parallel_map
    from mendari.providers.deadline import decision_deadline

    pack = decision.pack
    manifest = pack.manifest
    rubric = manifest.rubrics.get(rubric_name)
    if rubric is None:
        known = ", ".join(manifest.rubrics) or "none"
        raise MendariError(f"{pack.id} has no rubric {rubric_name!r} (its rubrics: {known})")
    output = manifest.outputs[rubric.output]
    if output.slot is not None:
        info = decision.provider_info[output.slot]
        if info.type != "agent":
            raise MendariError(
                f"output {rubric.output} is written by slot {output.slot}, which runs its "
                f"{info.variant} variant (type {info.type}) here: only an agent writes. Choose "
                "the slot's agent variant"
            )
    if not getattr(dataset, "case_key", b""):
        raise MendariError(f"{dataset.name}: no case key (load the dataset with load_dataset)")
    problems = record_problems(manifest, dataset)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise MendariError(f"{problems[0]}{more}")
    try:
        judge.preflight()
    except ProviderError as error:
        raise MendariError(f"the judge cannot be asked: {error}") from None
    writes = set(manifest.providers[output.slot].answers) if output.slot is not None else set()

    def one(record: Mapping[str, Any]) -> tuple[dict[str, Any], Item | None, dict[str, Metered]]:
        inputs = record["input"]
        hashed = case_hash(dataset.case_key, record["id"])
        usage: dict[str, Metered] = {}
        agent_model: str | None = None
        text: str | None = None
        reason: str | None = None
        if output.slot is not None:
            result = decision.decide(inputs)
            usage = dict(result.usage)
            answered = next((result.answers[key] for key in writes if key in result.answers), None)
            agent_model = answered.provider.model if answered is not None else None
            text = written_text(manifest, rubric.output, inputs, result.outputs)
            if result.rejected:
                reason = REFUSED
            elif writes & set(result.errors):
                reason = ERROR
            else:
                reason = NONE
        else:
            raw = get_path(inputs, str(output.input))
            if decision.validate(inputs) or (isinstance(raw, str) and len(raw) > output.max_chars):
                reason = REFUSED
            else:
                text = written_text(manifest, rubric.output, inputs, {})
                reason = NONE
        line: dict[str, Any] = {"c": hashed, "i": None, "n": reason, "j": {}, "e": {}}
        if text is None:
            return {**line, "m": [agent_model, None], "u": [0.0, 0, 0]}, None, usage
        context = select_fields(inputs, list(rubric.context_fields))
        item = Item(
            hashed, item_id(dataset.case_key, record["id"], rubric.output, text), text, context
        )
        meter = Meter()
        metering = METER.set(meter)
        try:
            with decision_deadline(decision.deadline_s):
                judged = judge.grade(text, context)
        finally:
            METER.reset(metering)
        for kind, metered in meter.totals().items():
            usage.setdefault(kind, Metered()).add(metered)
        line.update(
            i=item.item,
            n=None,
            j={key: [level, round(conf, 6)] for key, (level, conf) in judged.grades.items()},
            e=dict(judged.errors),
            m=[agent_model, judged.model],
            u=[
                None if judged.cost_usd is None else round(judged.cost_usd, 6),
                judged.input_tokens,
                judged.output_tokens,
            ],
        )
        return line, item, usage

    done = parallel_map(one, dataset.records, workers)
    models: list[str] = []
    cases, items = [], []
    usage: dict[str, Metered] = {}
    seen: set[str] = set()
    for line, item, used in done:
        if line["c"] in seen:
            raise MendariError(f"{dataset.name}: two cases have the same id")
        seen.add(line["c"])
        indices = []
        for model in line["m"]:
            if model is not None and model not in models:
                models.append(model)
            indices.append(None if model is None else models.index(model))
        cases.append({**line, "m": indices})
        if item is not None:
            items.append(item)
        for kind, metered in used.items():
            usage.setdefault(kind, Metered()).add(metered)
    header = {
        "format": LEDGER_FORMAT,
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "dataset": {
            "name": dataset.name,
            "sha256": dataset.sha256,
            "n": len(dataset.records),
            "limit": dataset.limit,
        },
        "cases": {"hash": CASE_HASH, "key": "dataset/v1", "n": len(cases)},
        "rubric": {
            "name": rubric_name,
            "sha256": rubric_sha256(manifest, rubric_name),
            "output": {"name": rubric.output, "source": output.source},
            "criteria": {
                key: {
                    "levels": [level for level, _ in criterion.scale()],
                    "scores": [score for _, score in criterion.scale()],
                    "pass_from": criterion.passing(),
                    "must_pass": criterion.must_pass,
                }
                for key, criterion in rubric.criteria.items()
            },
        },
        "floors": floors_of(manifest, rubric_name),
        "providers": {
            slot: {"variant": info.variant, "type": info.type, "model": info.model}
            for slot, info in decision.provider_info.items()
        },
        "judge": judge.describe(),
        "models": models,
    }
    return WrittenRun(WrittenLedger(header, cases), items, dict(sorted(usage.items())))


# -- clinicians' grades in a file ------------------------------------------------------------------


def grades_header(
    manifest: PackManifest, rubric_name: str, dataset: Any, *, simulated: bool = False
) -> dict[str, Any]:
    """Line 1 of a grades file for `rubric_name` over `dataset` (a `harness.Dataset`).
    `simulated`: its graders are no clinicians, which the file then says in its first line."""
    header: dict[str, Any] = {
        "format": GRADES_FORMAT,
        "rubric": {"name": rubric_name, "sha256": rubric_sha256(manifest, rubric_name)},
        "dataset": {"name": dataset.name, "sha256": dataset.sha256},
    }
    if simulated:
        header["simulated"] = True
    return header


def load_grades(
    raw: bytes, *, where: str, scales: Mapping[str, Sequence[str]] | None = None
) -> tuple[dict[str, Any], Grades]:
    """A grades file (`mendari.written-grades/1`): its header and its grades. Line 1 names the
    rubric (name and `rubric_sha256`) and the dataset (name and sha256); every other line is one
    grader's grades of one item, `{"item", "grader", "grades": {<criterion>: <level>}}`, at most
    one line an item and grader. Line 1 may also hold `"simulated": true`: the graders are no
    clinicians, and every report made from the file says so. With `scales` (each criterion's
    levels), every line answers exactly those criteria, each with one of its levels. A problem
    names the file and the line, never a value: a grader's id or a level may be anything a file
    holds."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MendariError(f"{where}: not UTF-8 text") from None
    lines = [(number, line) for number, line in enumerate(text.split("\n"), 1) if line.strip()]
    parsed: list[tuple[int, Any]] = []
    for number, line in lines:
        try:
            parsed.append((number, json.loads(line)))
        except (ValueError, RecursionError):
            raise MendariError(f"{where}:{number}: invalid JSON") from None
    if not parsed or not isinstance(parsed[0][1], dict):
        raise MendariError(f"{where}: not a {GRADES_FORMAT} file")
    header = parsed[0][1]
    rubric, dataset = header.get("rubric"), header.get("dataset")
    if (
        header.get("format") != GRADES_FORMAT
        or not {"format", "rubric", "dataset"} <= set(header) <= {*_GRADES_HEADER, "simulated"}
        or not isinstance(header.get("simulated", False), bool)
        or not isinstance(rubric, dict)
        or not isinstance(dataset, dict)
        or not all(isinstance(rubric.get(key), str) for key in ("name", "sha256"))
        or not all(isinstance(dataset.get(key), str) for key in ("name", "sha256"))
    ):
        raise MendariError(f"{where}: its first line is not a {GRADES_FORMAT} header")
    grades: list[Grade] = []
    seen: set[tuple[str, str]] = set()
    for number, line in parsed[1:]:
        at = f"{where}:{number}"
        if not isinstance(line, dict) or set(line) != {"item", "grader", "grades"}:
            raise MendariError(f"{at}: a grade is {{item, grader, grades}}")
        item, grader, values = line["item"], line["grader"], line["grades"]
        if not isinstance(item, str) or not _ITEM.match(item):
            raise MendariError(f"{at}: `item` is not an item id")
        if not isinstance(grader, str) or not GRADER.match(grader):
            raise MendariError(f"{at}: `grader` is not a grader's id ({GRADER.pattern})")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(level, str) for key, level in values.items()
        ):
            raise MendariError(f"{at}: `grades` is a mapping of criterion to level")
        if (item, grader) in seen:
            raise MendariError(f"{at}: this grader's grades of this item are given twice")
        seen.add((item, grader))
        if scales is not None:
            problem = _grade_problem(values, scales)
            if problem is not None:
                raise MendariError(f"{at}: {problem}")
        grades.append(Grade(item, grader, dict(values)))
    return header, Grades(rubric["sha256"], tuple(grades), bool(header.get("simulated", False)))


_GRADES_HEADER = ("format", "rubric", "dataset")


def _grade_problem(values: Mapping[str, str], scales: Mapping[str, Sequence[str]]) -> str | None:
    """Why one grader's grades of one item are not the rubric's, naming a criterion of the
    rubric and never a value given."""
    if set(values) - set(scales):
        return "it grades a criterion the rubric does not have"
    for key, levels in scales.items():
        if key not in values:
            return f"it leaves {key} ungraded"
        if values[key] not in levels:
            return f"its grade of {key} is not one of its levels"
    return None


def grades_problems(pack: Pack) -> list[str]:
    """Why any grades file the agent declares (`evaluation.grades`) could not be used: it does
    not read as one, its grades are not of the rubric's criteria and levels, or it names another
    rubric wording or another dataset than the agent's now. Each problem names the file."""
    from mendari.errors import PackValidationError
    from mendari.evals.harness import resolve_dataset

    manifest = pack.manifest
    problems: list[str] = []
    for name, declared in manifest.evaluation.grades.items():
        where = declared.file
        rubric = manifest.rubrics[name]
        scales = {
            key: [level for level, _ in criterion.scale()]
            for key, criterion in rubric.criteria.items()
        }
        try:
            header, _ = load_grades(pack.read_bytes(where), where=where, scales=scales)
            dataset = resolve_dataset(pack, declared.dataset)
        except PackValidationError as error:
            problems += [f"{where}: {problem}" for problem in error.problems]
            continue
        except MendariError as error:
            problems.append(str(error))
            continue
        if header["rubric"]["name"] != name:
            problems.append(f"{where}: its grades are of another rubric than {name}")
        elif header["rubric"]["sha256"] != rubric_sha256(manifest, name):
            problems.append(
                f"{where}: its grades were given under another wording of rubric {name}: grades "
                "bind to the rubric's text, so they are graded again or the file is made again"
            )
        if header["dataset"]["sha256"] != dataset.sha256:
            problems.append(
                f"{where}: its grades are of another dataset than {declared.dataset} as it is now"
            )
    return problems


# -- the report ------------------------------------------------------------------------------------


def _proportion(k: int, n: int) -> dict[str, Any]:
    return stats.proportion(k, n).to_dict()


def seed_problem(seed: Any) -> str | None:
    """Why `seed` is no bootstrap seed, or None."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        return "the bootstrap seed is a whole number, 0 or more"
    return None


def resamples_problem(resamples: Any) -> str | None:
    """Why a report may not be decided on `resamples` resamples, or None: a state is decided
    on a bound, and with fewer than `stats.RESAMPLES` the percentile interval is a few draws,
    so a judge under the floor passes on most seeds and an interval that is not estimable
    reads as one. Whatever decides a state asks this first, before it changes anything."""
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        return f"the resamples are a whole number, {stats.RESAMPLES} to {MAX_RESAMPLES}"
    if not stats.RESAMPLES <= resamples <= MAX_RESAMPLES:
        return (
            f"the resamples are {stats.RESAMPLES} to {MAX_RESAMPLES}: a judge's state is never "
            f"decided on fewer than {stats.RESAMPLES}"
        )
    return None


def symmetry_p(judge_only_pass: int, judge_only_fail: int) -> Fraction:
    """The exact two-sided McNemar p-value on the discordant pass-or-fail pairs, as a fraction:
    min(1, 2 P(X <= min(b, c))) with X ~ Bin(b + c, 1/2). `stats.mcnemar_exact` is its float;
    1 with no discordant pair."""
    for value in (judge_only_pass, judge_only_fail):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("the discordant counts are whole numbers, 0 or more")
    n, m = judge_only_pass + judge_only_fail, min(judge_only_pass, judge_only_fail)
    term = total = 1  # C(n, 0)
    for j in range(m):
        term = term * (n - j) // (j + 1)  # C(n, j + 1), exact
        total += term
    return min(Fraction(1), Fraction(2 * total, 1 << n))


def _model_name(model: str) -> str:
    """A model's name as two reports of it compare: its last path segment, whatever its case,
    so `vendor/model-x` and `Model-X` are one model."""
    return model.rsplit("/", 1)[-1].casefold()


def report(
    ledger: WrittenLedger,
    grades: Grades | None = None,
    *,
    seed: int = 0,
    resamples: int = stats.RESAMPLES,
) -> dict[str, Any]:
    """The report of one run (`mendari.written/1`), from its ledger, clinicians' grades and the
    seeds: pure, so a second call gives the same bytes. See the module docstring for what each
    figure is and how the gate decides. Refused: grades given under another wording of the
    rubric, a grade that is not of the rubric's criteria and levels, one grader's grades of one
    item given twice; fewer than `stats.RESAMPLES` resamples.

    `seed` is this function's and `WrittenStore.create_round`'s alone: no command and no route
    takes one once grades exist. `mendari written eval` leaves it at 0, and a round's closing
    passes the seed chained when the round was made. A caller in Python that passes another is
    computing a different report, and the report states the seed it was decided on."""
    problem = seed_problem(seed) or resamples_problem(resamples)
    if problem is not None:
        raise MendariError(problem)
    header = ledger.header
    rubric = header["rubric"]
    floors = {name: header["floors"][name] for name in _FLOORS}
    scales: dict[str, list[str]] = {
        key: scale["levels"] for key, scale in rubric["criteria"].items()
    }
    given = grades.grades if grades is not None else ()
    if grades is not None and grades.rubric != rubric["sha256"]:
        raise MendariError(
            f"the grades were given under another wording of rubric {rubric['name']} than this "
            "run's: grades bind to the rubric's text, and are never carried to another"
        )
    with_output = [case for case in ledger.cases if case["i"] is not None]
    known = {case["i"] for case in with_output}
    by_item: dict[str, dict[str, dict[str, str]]] = {}  # item -> grader -> its grades
    stale = 0
    graders: Counter[str] = Counter()
    for grade in given:
        problem = _grade_problem(grade.values, scales)
        if problem is not None:
            raise MendariError(f"a grade is not of rubric {rubric['name']}: {problem}")
        if grade.item not in known:
            stale += 1
            continue
        own = by_item.setdefault(grade.item, {})
        if grade.grader in own:
            raise MendariError("one grader's grades of one item are given twice")
        own[grade.grader] = dict(grade.values)
        graders[grade.grader] += 1
    judge_errors: Counter[str] = Counter()
    for case in with_output:
        judge_errors.update(set(case["e"].values()))
    costs = [case["u"][0] for case in with_output]
    judge = header["judge"]
    named = [_model_name(model) for model in header["models"]]
    same_model = any(
        case["m"][0] is not None
        and case["m"][1] is not None
        and named[case["m"][0]] == named[case["m"][1]]
        for case in with_output
    )
    found = {
        "format": FORMAT,
        "pack": header["pack"],
        "dataset": header["dataset"],
        "providers": header["providers"],
        "rubric": {"name": rubric["name"], "sha256": rubric["sha256"], "output": rubric["output"]},
        "ledger": {"id": ledger.id, "cases": len(ledger.cases)},
        "judge": {**judge, "same_model_as_agent": same_model},
        "seeds": {"bootstrap": seed, "resamples": resamples, "level": stats.LEVEL},
        "floors": floors,
        "cases": {
            "n": len(ledger.cases),
            "with_output": len(with_output),
            "no_output": {
                reason: sum(1 for case in ledger.cases if case["n"] == reason)
                for reason in NO_OUTPUT
            },
            "judged": sum(1 for case in with_output if case["j"]),
            "judge_errors": dict(sorted(judge_errors.items())),
            "graded": len(by_item),
            "graded_twice": sum(1 for own in by_item.values() if len(own) >= 2),
            "stale_grades": stale,
        },
        "graders": dict(sorted(graders.items())),
        # The graders are no clinicians (a demonstration's grades file says so of itself).
        "simulated_graders": bool(grades is not None and grades.simulated),
        "criteria": {
            key: _criterion(key, scale, with_output, by_item, floors, seed, resamples)
            for key, scale in rubric["criteria"].items()
        },
        "cost": {
            "judge_usd": None
            if any(cost is None for cost in costs)
            else round(math.fsum(costs), 6),
            "input_tokens": sum(case["u"][1] for case in with_output),
            "output_tokens": sum(case["u"][2] for case in with_output),
        },
    }
    return found


def _criterion(
    key: str,
    scale: Mapping[str, Any],
    with_output: Sequence[Mapping[str, Any]],
    by_item: Mapping[str, Mapping[str, Mapping[str, str]]],
    floors: Mapping[str, Any],
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    levels: list[str] = scale["levels"]
    scores = dict(zip(levels, scale["scores"], strict=True))
    line = scores[scale["pass_from"]]
    must_pass: bool = scale["must_pass"]

    def passes(level: str) -> bool:
        return bool(scores[level] >= line)

    def counted(found: Sequence[str]) -> dict[str, int]:
        counts = Counter(found)
        return {level: counts[level] for level in levels}

    # Each graded output's grades, in the ledger's order; its verdict is the least favourable.
    graded = [
        (case, [own[key] for _, own in sorted(by_item[case["i"]].items())])
        for case in with_output
        if case["i"] in by_item
    ]
    verdicts = {case["i"]: min(given, key=lambda level: scores[level]) for case, given in graded}
    disputed = [case for case, given in graded if len(set(given)) > 1]
    judged = {case["i"]: case["j"][key][0] for case in with_output if key in case["j"]}
    paired = [case for case, _ in graded if case["i"] in judged]
    # What the judge could not grade: among the outputs clinicians graded, and among the rest.
    unjudged = [case for case, _ in graded if case["i"] not in judged]
    rest = [case for case in with_output if case["i"] not in verdicts]
    ungraded = [case for case in rest if case["i"] in judged]
    errors = {
        "paired": {"k": len(unjudged), "n": len(graded)},
        "ungraded": {"k": len(rest) - len(ungraded), "n": len(rest)},
    }

    # Clinician with clinician: each pair of graders of an output, in both orders.
    several = [given for _, given in graded if len(given) >= 2]

    def clinician_kappa(sample: Sequence[Sequence[str]]) -> float | None:
        pairs = [pair for given in sample for pair in permutations(given, 2)]
        value = weighted_kappa(pairs, scores)
        return None if value is None else float(value)

    both_orders = [pair for given in several for pair in permutations(given, 2)]
    clinician_agreement = {
        "cases": len(several),
        "agreement": _proportion(sum(1 for given in several if len(set(given)) == 1), len(several)),
        "kappa": measured(
            weighted_kappa(both_orders, scores),
            clinician_kappa,
            several,
            seed,
            resamples,
            "no output was graded by two clinicians"
            if not several
            else "the clinicians gave every output one and the same level",
        )[0],
    }

    # Judge with clinicians: (the clinicians' verdict, the judge's grade), outputs both graded.
    pairs = [(verdicts[case["i"]], judged[case["i"]]) for case in paired]
    nothing = "no output was graded by both a clinician and the judge"

    def judge_kappa(sample: Sequence[tuple[str, str]]) -> float | None:
        value = weighted_kappa(sample, scores)
        return None if value is None else float(value)

    kappa, low = measured(
        weighted_kappa(pairs, scores),
        judge_kappa,
        pairs,
        seed,
        resamples,
        nothing
        if not pairs
        else "the clinicians and the judge gave every output one and the same level",
    )
    # The same pairs as pass or fail: what the published share is about. On a yes/no criterion
    # (two levels, the higher one passing) it is the weighted kappa, to the last digit.
    sides = [(passes(a), passes(b)) for a, b in pairs]
    two_sided = len(levels) == 2 and scale["pass_from"] == levels[1]
    if two_sided:
        pass_kappa, pass_low = kappa, low
    else:
        named = [(str(a), str(b)) for a, b in sides]

        def side_kappa(sample: Sequence[tuple[str, str]]) -> float | None:
            value = plain_kappa(sample)
            return None if value is None else float(value)

        pass_kappa, pass_low = measured(
            plain_kappa(named),
            side_kappa,
            named,
            seed,
            resamples,
            nothing
            if not pairs
            else "the clinicians and the judge put every output on one and the same side",
        )
    pass_fail = {
        "both_pass": sum(1 for a, b in sides if a and b),
        "both_fail": sum(1 for a, b in sides if not a and not b),
        "judge_only_fail": sum(1 for a, b in sides if a and not b),
        "judge_only_pass": sum(1 for a, b in sides if not a and b),
    }
    failed = pass_fail["both_fail"] + pass_fail["judge_only_pass"]  # the clinicians' failures
    passed = len(pairs) - failed
    # A clinician-failed output the judge could not grade is a failure it did not find.
    failed_unjudged = sum(1 for case in unjudged if not passes(verdicts[case["i"]]))
    failed_in_all = failed + failed_unjudged
    # Does the judge lean to one side of the pass line? Exact, on the discordant pairs.
    lenient_pairs, strict_pairs = pass_fail["judge_only_pass"], pass_fail["judge_only_fail"]
    p_value = symmetry_p(lenient_pairs, strict_pairs)
    leaning = None
    if p_value < SYMMETRY_ALPHA:
        leaning = LENIENT if lenient_pairs > strict_pairs else STRICT

    # The gate. First what no result on these outputs could settle, then the floors.
    needed = min_failures(floors["must_pass_recall"])
    reasons = []
    if len(pairs) < floors["min_paired"]:
        reasons.append(
            f"{len(pairs)} outputs were graded by both a clinician and the judge; "
            f"{floors['min_paired']} are needed"
        )
    if min(passed, failed) < floors["min_each_side"]:
        reasons.append(
            f"clinicians passed {passed} and failed {failed} of them; "
            f"{floors['min_each_side']} on each side of the pass line are needed"
        )
    if kappa["estimate"] is None:
        reasons.append(f"kappa is not estimable: {kappa['reason']}")
    elif low is None:
        reasons.append(f"kappa's interval is not estimable: {kappa['interval']['reason']}")
    if not two_sided:
        if pass_kappa["estimate"] is None:
            reasons.append(f"kappa on pass or fail is not estimable: {pass_kappa['reason']}")
        elif pass_low is None:
            reasons.append(
                "the interval of kappa on pass or fail is not estimable: "
                f"{pass_kappa['interval']['reason']}"
            )
    if must_pass and failed_in_all < needed:
        reasons.append(
            f"clinicians failed {failed_in_all} outputs; {needed} are needed before a floor of "
            f"{floors['must_pass_recall']} on the failures the judge finds can be met"
        )
    state = INSUFFICIENT
    if not reasons:
        if low is not None and low < floors["agreement"]:
            reasons.append(
                f"the lower bound of the judge's kappa with clinicians is under the floor of "
                f"{floors['agreement']}"
            )
        if not two_sided and pass_low is not None and pass_low < floors["agreement"]:
            reasons.append(
                "the lower bound of the judge's kappa with clinicians on pass or fail, which is "
                f"what a pass share is about, is under the floor of {floors['agreement']}"
            )
        if (
            must_pass
            and stats.clopper_pearson(pass_fail["both_fail"], failed_in_all)[0]
            < floors["must_pass_recall"]
        ):
            could_not = (
                f" (it could not grade {failed_unjudged} of them, which count as not found)"
                if failed_unjudged
                else ""
            )
            reasons.append(
                f"the judge failed {pass_fail['both_fail']} of the {failed_in_all} outputs "
                f"clinicians failed{could_not}; the lower bound is under the floor of "
                f"{floors['must_pass_recall']}"
            )
        if leaning == LENIENT:
            reasons.append(
                f"the judge leans lenient: it passed {lenient_pairs} outputs clinicians failed "
                f"and failed {strict_pairs} they passed (exact McNemar test, p under "
                f"{float(SYMMETRY_ALPHA)})"
            )
        ceiling = Fraction(str(floors["max_judge_errors"]))
        for where, words in (
            ("paired", "outputs clinicians graded"),
            ("ungraded", "outputs no clinician graded"),
        ):
            could, of = errors[where]["k"], errors[where]["n"]
            if of and Fraction(could, of) > ceiling:
                reasons.append(
                    f"the judge could not grade {could} of the {of} {words}, more than "
                    f"{floors['max_judge_errors']} of them"
                )
        state = NOT_VALIDATED if reasons else VALIDATED

    judge_grades = [judged[case["i"]] for case in ungraded]
    return {
        "scale": {
            "levels": levels,
            "scores": list(scale["scores"]),
            "pass_from": scale["pass_from"],
        },
        "must_pass": must_pass,
        "clinicians": {
            "passing": _proportion(
                sum(1 for verdict in verdicts.values() if passes(verdict)), len(verdicts)
            ),
            "levels": counted(list(verdicts.values())),
            "disputed": len(disputed),
        },
        "clinician_agreement": clinician_agreement,
        "judge_agreement": {
            "cases": len(pairs),
            "agreement": _proportion(sum(1 for a, b in pairs if a == b), len(pairs)),
            "kappa": kappa,
            # Agreement on pass or fail, which is what a pass share is about.
            "pass_fail_kappa": pass_kappa,
            "pass_fail": pass_fail,
            # On the same outputs: how many the clinicians passed, and how many the judge did.
            "passing": {
                "clinicians": _proportion(passed, len(pairs)),
                "judge": _proportion(sum(1 for _, b in sides if b), len(pairs)),
            },
            "symmetry": {
                "p": round(float(p_value), stats.DIGITS),
                "alpha": float(SYMMETRY_ALPHA),
                "leaning": leaning,
            },
            # Of the outputs clinicians failed, those the judge failed too; one it could not
            # grade is not found.
            "failures_found": (
                _proportion(pass_fail["both_fail"], failed_in_all) if must_pass else None
            ),
        },
        # The outputs the judge could not grade, of those clinicians graded and of the rest.
        "judge_errors": errors,
        "validation": {"state": state, "reasons": reasons},
        # Reported only when the criterion is validated. Otherwise withheld, not shown with a
        # caveat: the state and its reasons are beside it.
        "judged": {
            "passing": _proportion(
                sum(1 for level in judge_grades if passes(level)), len(judge_grades)
            ),
            "levels": counted(judge_grades),
            "could_not_grade": errors["ungraded"]["k"],
        }
        if state == VALIDATED
        else None,
        "cases": {
            "failed_by_clinicians": [
                case["c"] for case, _ in graded if not passes(verdicts[case["i"]])
            ][:MAX_LISTED],
            "judge_missed": [
                case["c"]
                for case in paired
                if not passes(verdicts[case["i"]]) and passes(judged[case["i"]])
            ][:MAX_LISTED],
            "disputed": [case["c"] for case in disputed][:MAX_LISTED],
        },
    }


def result_sha256(found: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(found).encode()).hexdigest()


def run_report(found: Mapping[str, Any], ledger: WrittenLedger) -> dict[str, Any]:
    """A report as a run keeps it: the result `report` computed, the run that made it (its id,
    when, the engine) and the result's hash, as an evaluation report is (`harness`)."""
    if found.get("ledger", {}).get("id") != ledger.id:
        raise MendariError("the report is of another ledger than this run's")
    run_block = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine": __version__,
    }
    return {**found, "run": run_block, "result_sha256": result_sha256(found)}
