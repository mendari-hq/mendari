# SPDX-License-Identifier: Apache-2.0
"""An external agent's confidence, calibrated (ADR-0024).

An `agent` variant's confidence is a number the agent reports about itself, and the policy
automates on it. `mendari calibrate` asks the live agent about labelled cases and fits, for each
question, one temperature T (`providers/calibration.py`, the fit an `llm` variant and naive
Bayes use): the reported distribution p becomes softmax(log p / T).

- When the agent reports a distribution over the labels, that is temperature scaling of it.
- When it reports only its answer's confidence c, the provider has already spread the rest
  evenly over the other labels (`providers/agent.py`), and the same formula then scales the
  log-odds of c against each other label: log(c / ((1 - c) / (K - 1))) / T. For a question
  with two labels that is the ordinary log-odds of c, divided by T. Fitting it by the true
  label's likelihood gives the same T as fitting whether the answer was right: the two
  likelihoods differ by a constant.

One parameter per question, monotone, and it never changes which answer wins: softmax keeps the
order of its scores, and the answer's label is kept as the agent gave it.

The fit is a file of the agent (`mendari.agent-calibration/1`), named by the variant's
`calibration` key as an `llm` variant's is, so the agent's digest, its archive and its signature
cover it. It records what it was fitted on (the dataset, its sha256, the number of cases, the
date) and for which agent: the variant's `agent_id` and the `agent_version` the agent's service
reported while it was asked. A question with fewer than `MIN_CASES` labelled cases is not
fitted and is left as reported; so is one whose best temperature is at the edge of the search
(an agent right on every case has no temperature: sharpening it further always fits better).

At decision time (`calibrated`, called by `AgentProvider.answer` for every call, so on every
path a decision is made by):

- canned answers (`mock_file`) are left alone: they are not the agent's confidence;
- when the service reports the version the fit was made for, each fitted question's
  distribution is rescaled, and the evidence says `calibrated T=…`;
- when it reports another version, or none, nothing is rescaled and the decision is capped
  (`signals.Cap`, `policy.lowered`): AUTO becomes HUMAN_REVIEW, with the reason
  `the agent changed since its confidence was calibrated (was X, is Y)`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mendari.core.pack import Pack
from mendari.core.spec import Question
from mendari.errors import MendariError, PackValidationError, ProviderError
from mendari.providers.base import ProviderOutput, read_problems
from mendari.providers.calibration import (
    fit_temperature,
    log_probabilities,
    rescale,
    temperature_grid,
)
from mendari.providers.signals import Cap, Signals

if TYPE_CHECKING:
    from mendari.providers.agent import AgentProvider

FORMAT = "mendari.agent-calibration/1"
# The fewest labelled cases a question's temperature is fitted on. One parameter does not
# overfit a hundred cases; fewer, and the fit says more about the sample than about the agent.
MIN_CASES = 100
NOT_REPORTED = "not reported"
NOT_CALIBRATED = "not calibrated"


@dataclass(frozen=True)
class Calibration:
    """A fitted calibration file, as a provider and a card read it."""

    agent_id: str | None
    agent_version: str
    model: str | None
    temperatures: Mapping[str, float]
    questions: Mapping[str, Mapping[str, Any]]
    not_fitted: Mapping[str, str]
    dataset: str
    dataset_sha256: str | None
    cases: int
    date: str

    def changed(self, agent_id: str | None, reported_version: str | None) -> str | None:
        """Why this calibration is not the running agent's, as the cap's reason, or None when
        it is: the variant's `agent_id` and the version the service reports are the ones it
        was fitted for. A service that reports no version is not the one it was fitted for."""
        if agent_id == self.agent_id and reported_version == self.agent_version:
            return None
        was, now = self.agent_version, reported_version or NOT_REPORTED
        if agent_id != self.agent_id:
            was = f"{self.agent_id or 'no agent_id'} {was}"
            now = f"{agent_id or 'no agent_id'} {now}"
        return changed_reason(was, now)

    def words(self) -> str:
        """What a card and a report say of it, in one clause."""
        agent = f"{self.agent_id} " if self.agent_id else ""
        return (
            f"calibrated on {self.cases:,} cases of {self.dataset} ({self.date}) for "
            f"{agent}version {self.agent_version}"
        )


def changed_reason(was: str, now: str) -> str:
    """The version cap's reason (stable: the audit trail and the review queue show it)."""
    return f"the agent changed since its confidence was calibrated (was {was}, is {now})"


def _temperature(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def shape_problems(where: str, data: Any) -> list[str]:
    """Why `data` is not a calibration file, or nothing."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return [f"{where}: not an agent calibration file ({FORMAT})"]
    agent, fitted_on = data.get("agent"), data.get("fitted_on")
    temperatures = data.get("temperatures")
    problems = []
    if not isinstance(agent, dict) or not isinstance(agent.get("version"), str):
        problems.append(f"{where}: the calibration file does not say which agent version it is for")
    elif agent.get("id") is not None and not isinstance(agent["id"], str):
        problems.append(f"{where}: the calibration file's agent id is not a name")
    if not isinstance(temperatures, dict) or not all(
        isinstance(key, str) and _temperature(value) for key, value in temperatures.items()
    ):
        problems.append(
            f"{where}: the calibration file's temperatures are not positive numbers by question"
        )
    if not (
        isinstance(fitted_on, dict)
        and isinstance(fitted_on.get("dataset"), str)
        and isinstance(fitted_on.get("date"), str)
        and isinstance(fitted_on.get("cases"), int)
        and not isinstance(fitted_on.get("cases"), bool)
    ):
        problems.append(
            f"{where}: the calibration file does not say what it was fitted on (dataset, cases, "
            "date)"
        )
    return problems


def file_problems(pack: Pack, where: str, relative: str) -> list[str]:
    """Why the agent's file `relative` cannot be read as a calibration, or nothing."""
    data, problems = read_problems(lambda: pack.read_json(relative))
    return problems or shape_problems(where, data)


def of(data: Mapping[str, Any], questions: Mapping[str, Any] | None = None) -> Calibration:
    """A calibration file's content (one `shape_problems` passes), with the temperatures of
    `questions` alone when given."""
    agent, fitted_on = data["agent"], data["fitted_on"]
    model = agent.get("model")
    sha256 = fitted_on.get("sha256")
    stated = data.get("questions")
    not_fitted = data.get("not_fitted")
    return Calibration(
        agent_id=agent.get("id"),
        agent_version=agent["version"],
        model=model if isinstance(model, str) else None,
        temperatures={
            key: float(value)
            for key, value in data["temperatures"].items()
            if questions is None or key in questions
        },
        questions=stated if isinstance(stated, dict) else {},
        not_fitted=not_fitted if isinstance(not_fitted, dict) else {},
        dataset=fitted_on["dataset"],
        dataset_sha256=sha256 if isinstance(sha256, str) else None,
        cases=fitted_on["cases"],
        date=fitted_on["date"],
    )


def load(pack: Pack, where: str, relative: str, questions: Mapping[str, Question]) -> Calibration:
    """The calibration an `agent` variant names, or `PackValidationError`."""
    problems = file_problems(pack, where, relative)
    if problems:
        raise PackValidationError(pack.id, problems)
    return of(pack.read_json(relative), questions)


def status(pack: Pack, config: Mapping[str, Any]) -> Calibration | str:
    """What a card says of an `agent` variant's (or member's) confidence, from its config
    alone: its calibration, or why it has none (`NOT_CALIBRATED`, or the file's problem)."""
    relative = config.get("calibration")
    if not isinstance(relative, str):
        return NOT_CALIBRATED
    problems = file_problems(pack, "its calibration", relative)
    if problems:
        return f"{NOT_CALIBRATED} ({problems[0]})"
    return of(pack.read_json(relative))


def calibrated(
    calibration: Calibration,
    output: ProviderOutput,
    questions: Mapping[str, Question],
    agent_id: str | None,
) -> ProviderOutput:
    """`output` as the policy will see it: see the module docstring."""
    if output.canned is not None:
        return output
    reported_version = output.extra.get("agent_version")
    reason = calibration.changed(agent_id, reported_version)
    if reason is not None:
        cap = Signals(caps=(Cap("calibration", reason),))
        note = f"{NOT_CALIBRATED}: {reason}"
        answers = {
            key: replace(answer, evidence=(*answer.evidence, note))
            for key, answer in output.answers.items()
        }
        return replace(output, answers=answers, signals=output.signals.merge(cap))
    answers = {}
    reported: dict[str, Mapping[str, float]] = {}
    for key, answer in output.answers.items():
        temperature = calibration.temperatures.get(key)
        if temperature is None:
            answers[key] = answer
            continue
        probabilities = rescale(answer.probabilities, temperature)
        reported[key] = dict(answer.probabilities)
        answers[key] = replace(
            answer,
            confidence=probabilities[answer.label],
            probabilities=probabilities,
            evidence=(*answer.evidence, f"calibrated T={temperature:.2f}"),
            value=questions[key].expected_score(probabilities),
        )
    return replace(
        output, answers=answers, signals=output.signals.merge(Signals(reported=reported))
    )


def fit(
    provider: AgentProvider,
    records: list[dict[str, Any]],
    *,
    dataset: str,
    sha256: str,
    date: str,
    workers: int = 8,
) -> dict[str, Any]:
    """Ask the live agent about every labelled record, once each and uncalibrated, and fit one
    temperature per question on its answers. Returns the calibration file's content.

    Refused (`MendariError`): an agent that answers from its canned examples (they are not its
    confidence), a service that reports no `agent_version` (a calibration is for a version), and
    one that reported more than one version while it was asked (the cases are of two agents)."""
    from mendari.evals import metrics
    from mendari.parallel import parallel_map

    where = f"{provider.info.slot}/{provider.info.variant}"
    provider.preflight()
    if provider.live() is None:
        raise MendariError(
            f"{where} answers from its canned examples: a calibration is fitted on the live "
            "agent's own confidence (set MENDARI_AGENT_URL, or the agent's connection)"
        )

    def ask(record: dict[str, Any]) -> ProviderOutput | str:
        try:
            return provider.answer_once(record["input"])
        except ProviderError as error:
            return error.code

    outputs = parallel_map(ask, records, workers)
    failures: dict[str, int] = {}
    cost: float | None = 0.0
    versions: dict[str | None, int] = {}
    models: dict[str, int] = {}
    for output in outputs:
        if isinstance(output, str):
            failures[output] = failures.get(output, 0) + 1
            continue
        version = output.extra.get("agent_version")
        versions[version] = versions.get(version, 0) + 1
        if isinstance(output.extra.get("model"), str):
            models[output.extra["model"]] = models.get(output.extra["model"], 0) + 1
        if cost is not None:
            cost = None if output.cost_usd is None else cost + output.cost_usd
    if not versions:
        raise MendariError(f"{where}: the agent answered no case ({failures})")
    if None in versions:
        raise MendariError(
            f"{where}: the agent's service reports no agent_version, and a calibration is "
            "fitted for a version: have the service report one (`agent_version` in its answer)"
        )
    if len(versions) > 1:
        raise MendariError(
            f"{where}: the agent's service reported more than one version while it was asked "
            f"({', '.join(sorted(str(v) for v in versions))}): fit a calibration on one version"
        )

    temperatures: dict[str, float] = {}
    questions: dict[str, Any] = {}
    not_fitted: dict[str, str] = {}
    grid = temperature_grid()
    edges = (grid[0], grid[-1])
    for key in provider.questions:
        answered = [
            (output.answers[key], record["labels"][key])
            for record, output in zip(records, outputs, strict=True)
            if not isinstance(output, str) and key in (record.get("labels") or {})
        ]
        if len(answered) < MIN_CASES:
            not_fitted[key] = f"{len(answered)} labelled cases answered; {MIN_CASES} needed"
            continue
        scored = [(log_probabilities(answer.probabilities), truth) for answer, truth in answered]
        temperature, before, after = fit_temperature(scored)
        if temperature in edges:
            # No temperature is the best one: the likelihood keeps improving past the search
            # (an agent right on every case can always be sharpened further). The cases do not
            # place its confidence, so it is left as reported.
            not_fitted[key] = (
                f"its {len(answered)} answers place no temperature: too few of them are "
                "wrong, or too few are right"
            )
            continue
        temperature = round(temperature, 4)
        correct = [answer.label == truth for answer, truth in answered]
        confident = [answer.confidence for answer, _ in answered]
        rescaled = [
            rescale(answer.probabilities, temperature)[answer.label] for answer, _ in answered
        ]
        temperatures[key] = temperature
        questions[key] = {
            "cases": len(answered),
            "nll_before": round(before, 4),
            "nll_after": round(after, 4),
            # On the fitting cases themselves: an evaluation reports it on cases of its own.
            "ece_before": round(metrics.calibration(confident, correct)["ece"], 4),
            "ece_after": round(metrics.calibration(rescaled, correct)["ece"], 4),
        }
    (version,) = versions
    return {
        "format": FORMAT,
        "agent": {
            "id": provider.settings.agent_id,
            "version": version,
            "model": max(models, key=lambda name: models[name]) if models else None,
        },
        "method": "temperature",
        "temperatures": temperatures,
        "questions": questions,
        "not_fitted": not_fitted,
        "fitted_on": {"dataset": dataset, "sha256": sha256, "cases": len(records), "date": date},
        "failures": failures,
        "cost_usd": None if cost is None else round(cost, 6),
    }
