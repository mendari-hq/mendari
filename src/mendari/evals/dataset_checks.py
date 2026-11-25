# SPDX-License-Identifier: Apache-2.0
"""Checks of a labelled dataset against its agent (`mendari.dataset-check/1`, LP-20c).

Pure functions over an agent's manifest and schema and a dataset's records: nothing here reads a
file or a database but `training_records`, so `mendari data check`, the agents workflow and the
agent store (LP-20f) run the same checks on the same records.

- `schema_problems`: each case's input against the agent's input schema, as a decision checks it
  (`Decision.validate`: where and which rule, never the value), named by case id.
- `label_problems`: each case's labels are the agent's questions and their declared answers, its
  id is not repeated and its attributes are a mapping (`harness.record_problems`).
- `label_counts`: per question and label, the number of cases and its exact interval as a share
  of the cases labelled for that question (`figures.of`, Clopper-Pearson). A label with fewer
  than `MIN_CASES` cases is marked `too_few` ("too few to estimate"): reported, never refused.
- `duplicates`: groups of cases equal under the strict form (`canonical_input`) and under
  `norm/v1` (`normalised_input`), LP-11's functions, imported and never copied.
- `training_overlap`: cases equal, under either form, to a record of the agent's own training
  data (its `train` and `calibration` datasets).

A report holds case ids, labels, counts and intervals, never an input. Only schema and label
problems fail a check (`Report.passed`): duplicates, overlap and thin labels are for the reader,
and there is no hard minimum per label."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mendari.core.agent import Agent
from mendari.core.decision import Decision
from mendari.core.spec import AgentManifest
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals.canonical import NORM_VERSION, canonical_input, normalised_input
from mendari.evals.harness import Dataset, record_problems
from mendari.evals.render import shown
from mendari.evals.result import MIN_POSITIVES

FORMAT = "mendari.dataset-check/1"
# Under this many cases a label's share is "too few to estimate": the floor the evaluation
# report holds a critical label to (`result.MIN_POSITIVES`).
MIN_CASES = MIN_POSITIVES
# The datasets an agent is trained or calibrated on: a case in them and in a set it is measured on
# overstates how well it does.
TRAINING = ("train", "calibration")
# At most this many problems, groups or ids of each kind are listed; all are counted.
MAX_LISTED = 50
STRICT = "exact"

Records = Sequence[Mapping[str, Any]]


def _id(record: Mapping[str, Any]) -> str:
    return str(record.get("id"))


def schema_problems(decision: Decision, records: Records) -> list[dict[str, str]]:
    """Each case whose input the agent's input schema refuses, by id, with the first reason a
    decision gives (where and which rule, never the value). An agent the deployment does not own
    is checked within its limits and time budget, as a decision is."""
    found = []
    for record in records:
        reasons = decision.validate(record.get("input"))
        if reasons:
            found.append({"case": _id(record), "reason": reasons[0]})
    return found


def label_problems(manifest: AgentManifest, records: Records) -> list[dict[str, str]]:
    """Each case whose labels are not the agent's questions and declared answers, whose id another
    case has, or whose attributes are not a mapping (`harness.record_problems`, which names
    only what the agent defines, never a label it does not)."""
    dataset = Dataset(name="", path=Path(), records=[dict(r) for r in records], sha256="")
    found = []
    for line in record_problems(manifest, dataset):
        head, _, reason = line.partition(": ")
        found.append({"case": head.removeprefix(" case "), "reason": reason})
    return found


def label_counts(manifest: AgentManifest, records: Records) -> dict[str, Any]:
    """Per question: how many cases are labelled for it with a declared label, and per declared
    label k of those n with its exact interval, marked `too_few` under `MIN_CASES`. A label the
    agent does not declare is not counted here (`label_problems` names its case)."""
    counts: dict[str, Any] = {}
    for question in manifest.questions:
        declared = manifest.labels(question)
        labelled = [
            labels.get(question)
            for record in records
            if isinstance(labels := record.get("labels", {}), Mapping)
            and labels.get(question) in declared
        ]
        n = len(labelled)
        counts[question] = {
            "labelled": n,
            "labels": {
                label: {
                    "share": figures.of(labelled.count(label), n),
                    "too_few": labelled.count(label) < MIN_CASES,
                }
                for label in declared
            },
        }
    return counts


def _groups(records: Records, form: Callable[[Any], str]) -> list[list[str]]:
    keyed: dict[str, list[str]] = {}
    for record in records:
        keyed.setdefault(form(record.get("input")), []).append(_id(record))
    return [ids for ids in keyed.values() if len(ids) > 1]


def duplicates(records: Records) -> dict[str, list[list[str]]]:
    """Groups of case ids whose inputs are equal: `exact` under `canonical_input`, and
    `norm/v1` under `normalised_input` (each exact group lies inside one of these)."""
    return {
        STRICT: _groups(records, canonical_input),
        NORM_VERSION: _groups(records, normalised_input),
    }


def training_overlap(
    records: Records, training: Iterable[Mapping[str, Any]]
) -> dict[str, list[str]]:
    """The ids of cases whose input equals a training record's input, under each form."""
    seen = list(training)
    strict = {canonical_input(record.get("input")) for record in seen}
    folded = {normalised_input(record.get("input")) for record in seen}
    return {
        STRICT: [_id(r) for r in records if canonical_input(r.get("input")) in strict],
        NORM_VERSION: [_id(r) for r in records if normalised_input(r.get("input")) in folded],
    }


def _listed(items: Sequence[Any]) -> dict[str, Any]:
    return {"count": len(items), "listed": list(items[:MAX_LISTED])}


@dataclass(frozen=True)
class Report:
    """A dataset's check: `data` is the `mendari.dataset-check/1` report."""

    data: dict[str, Any]

    @property
    def passed(self) -> bool:
        """No schema or label problem: the only ones that fail a check."""
        return not (self.data["schema_problems"]["count"] or self.data["label_problems"]["count"])


def check(
    agent: Agent, dataset: Dataset, *, training: Mapping[str, Records] | None = None
) -> Report:
    """Every check of `dataset` against `agent`. `training` is the agent's training records by
    dataset name; by default those of its own `train` and `calibration` datasets other than
    `dataset` itself (`training_records`)."""
    records = dataset.records
    if training is None:
        training = training_records(agent, skip=dataset.name)
    trained = [record for found in training.values() for record in found]
    overlap = training_overlap(records, trained) if trained else None
    data = {
        "format": FORMAT,
        "agent": agent.ref,
        "dataset": dataset.name,
        "sha256": dataset.sha256,
        "cases": len(records),
        "min_cases": MIN_CASES,
        "schema_problems": _listed(schema_problems(Decision(agent), records)),
        "label_problems": _listed(label_problems(agent.manifest, records)),
        "labels": label_counts(agent.manifest, records),
        "duplicates": {form: _listed(found) for form, found in duplicates(records).items()},
        "training": sorted(training),
        "training_overlap": None
        if overlap is None
        else {form: _listed(found) for form, found in overlap.items()},
    }
    return Report(data)


def training_records(agent: Agent, *, skip: str | None = None) -> dict[str, Records]:
    """The records of the agent's `train` and `calibration` datasets, by name, other than `skip`
    (a set is not compared with itself). One that does not read is left out: `mendari
    validate` names it."""
    from mendari.evals.harness import resolve_dataset

    found: dict[str, Records] = {}
    for name in TRAINING:
        if name == skip or name not in agent.manifest.evaluation.datasets:
            continue
        try:
            found[name] = resolve_dataset(agent, name).records
        except MendariError:
            continue
    return found


def text(report: Report) -> str:
    """The report as the command line prints it: counts and ids, never an input."""
    data = report.data
    lines = [f"{data['agent']}  {data['dataset']}  {data['cases']} cases  sha256 {data['sha256']}"]
    for key, title in (("schema_problems", "schema"), ("label_problems", "labels")):
        listed = data[key]
        lines.append(f"{title}: {listed['count']} problem(s)")
        lines += [f"  case {p['case']}: {p['reason']}" for p in listed["listed"]]
    for question, counted in data["labels"].items():
        lines.append(f"{question}: {counted['labelled']} labelled")
        for label, item in counted["labels"].items():
            thin = "  too few to estimate" if item["too_few"] else ""
            lines.append(f"  {label}: {shown(item['share'])}{thin}")
    for form, listed in data["duplicates"].items():
        lines.append(f"duplicates ({form}): {listed['count']} group(s)")
        lines += [f"  {', '.join(group)}" for group in listed["listed"]]
    overlap = data["training_overlap"]
    if overlap is None:
        lines.append("training overlap: no training data to compare with")
    else:
        for form, listed in overlap.items():
            lines.append(f"training overlap ({form}): {listed['count']} case(s)")
            if listed["listed"]:
                lines.append(f"  {', '.join(listed['listed'])}")
    lines.append("passed" if report.passed else "failed: schema or label problems")
    return "\n".join(lines)
