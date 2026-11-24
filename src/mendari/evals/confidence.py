# SPDX-License-Identifier: Apache-2.0
"""What an evaluation says of the confidence the policy cuts on (ADR-0022): whether each model
variant's confidence is calibrated and what the calibration does on these cases
(`mendari.calibration-report/1`), and what a second signal does on them
(`mendari.second-signal/1`).

Both blocks are in the report's `run` (`harness.build_evaluation`), beside the operating point
and for the same reason: they are worked out from what each decision kept in memory
(`DecisionResult.signals`: an answer's distribution before its calibration, a second signal's
calls and disagreements), which a ledger does not hold. So neither is in the result that
`result_sha256` covers, and no stored, hashed or signed format changes. No case text and no
case id is in either.

**Calibration.** For each `agent` and `llm` variant the run decided with (a composite's members
too): calibrated or not, and if so on how many cases of which dataset, when, and for which agent
version or model. For the variant whose answers are a slot's own (the variant itself, or the
member of a `check` that answers), each question's figures on the labelled cases of this run,
before the calibration (the distribution the provider reported) and after (what the policy
saw): the expected calibration error over ten equal-width bins with its reliability table, the
equal-mass error and Murphy's reliability term of the Brier score where the set is large enough
for them (`stats.equal_mass_ece`, `stats.brier_reliability`), each with its number of cases. A
variant with no calibration has one column and says "not calibrated". A run on the very cases
the calibration was fitted on says so: its "after" is optimistic.

**Second signal.** For each repeated variant and each check: on how many cases it disagreed
(on a question the decision turns on), how many of those the agent got wrong or were a critical
label's cases (caught: a person now reads them), how many it got right (extra review), and the
extra calls it made. Then, over all of them, how many cases were kept from automation: the
policy applied again to each case without the second signal's caps
(`policy.PolicyInputs.apply`), and nothing else changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from mendari.core.decision import Decision, DecisionResult
from mendari.evals import metrics, stats
from mendari.providers.agent import AgentProvider
from mendari.providers.agent_calibration import NOT_CALIBRATED, Calibration
from mendari.providers.base import Provider
from mendari.providers.check import CheckProvider
from mendari.providers.llm import LlmProvider
from mendari.providers.signals import SECOND_SIGNALS, Checked

CALIBRATION_FORMAT = "mendari.calibration-report/1"
SECOND_SIGNAL_FORMAT = "mendari.second-signal/1"
CALIBRATED, UNCALIBRATED = "calibrated", "not_calibrated"
DIGITS = 4
_AUTO = "AUTO"
_INDENT = "  "


# --------------------------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------------------------


def _answering(provider: Provider) -> Provider | None:
    """The leaf whose answers are the slot's own, when one is: the variant itself, or the
    member of a check that answers. Another composite's answers are several members'."""
    if isinstance(provider, AgentProvider | LlmProvider):
        return provider
    if isinstance(provider, CheckProvider):
        first = next(iter(provider.members.values()))
        return first if isinstance(first, AgentProvider | LlmProvider) else None
    return None


def _fitted(leaf: Provider) -> tuple[dict[str, Any] | None, str | None]:
    """(what a leaf's calibration was fitted on and for, why it has none)."""
    if isinstance(leaf, AgentProvider):
        found: Calibration | None = leaf.calibration
        if found is None:
            return None, None
        return {
            "dataset": found.dataset,
            "sha256": found.dataset_sha256,
            "cases": found.cases,
            "date": found.date,
            "agent": {"id": found.agent_id, "version": found.agent_version},
            "temperatures": {key: found.temperatures[key] for key in sorted(found.temperatures)},
            "not_fitted": dict(found.not_fitted),
        }, None
    if isinstance(leaf, LlmProvider):
        if leaf.settings.calibration is None:
            return None, None
        if not leaf.temperatures:
            return None, "its calibration was fitted on another model"
        data = leaf.pack.read_json(leaf.settings.calibration)
        on = data.get("fitted_on") if isinstance(data.get("fitted_on"), dict) else {}
        return {
            "dataset": on.get("dataset"),
            "sha256": on.get("sha256"),
            "cases": on.get("records"),
            "date": on.get("date"),
            "model": leaf.model,
            "temperatures": {key: leaf.temperatures[key] for key in sorted(leaf.temperatures)},
            "not_fitted": {},
        }, None
    return None, None


def _rounded(value: Any) -> float | None:
    return None if value is None else round(float(value), DIGITS)


def _figures(
    distributions: Sequence[Mapping[str, float]],
    labels: Sequence[str],
    truths: Sequence[str],
    order: Sequence[str],
) -> dict[str, Any]:
    """The calibration of answers `labels` (each with its distribution) against `truths`."""
    confidences = [
        distribution[label] for distribution, label in zip(distributions, labels, strict=True)
    ]
    correct = [label == truth for label, truth in zip(labels, truths, strict=True)]
    plain = metrics.calibration(confidences, correct)
    equal_mass = stats.equal_mass_ece(confidences, correct)
    reliability = stats.brier_reliability(distributions, truths, order)
    return {
        "ece": _rounded(plain["ece"]),
        "equal_mass_ece": _rounded(equal_mass["ece"]),
        "brier_reliability": _rounded(reliability["reliability"]),
        "not_estimable": equal_mass.get("reason"),
        "bins": [
            {
                "low": row["low"],
                "high": row["high"],
                "n": row["n"],
                "confidence": _rounded(row["confidence"]),
                "accuracy": _rounded(row["accuracy"]),
            }
            for row in plain["bins"]
        ],
    }


def _question(
    key: str,
    order: Sequence[str],
    records: Sequence[Mapping[str, Any]],
    results: Sequence[DecisionResult],
) -> dict[str, Any] | None:
    truths, labels, after, before = [], [], [], []
    rescaled = 0
    for record, result in zip(records, results, strict=True):
        truth = (record.get("labels") or {}).get(key)
        answer = result.answers.get(key)
        if truth is None or answer is None:
            continue
        reported = result.signals.reported.get(key)
        rescaled += reported is not None
        truths.append(truth)
        labels.append(answer.label)
        after.append({label: answer.probabilities.get(label, 0.0) for label in order})
        source = answer.probabilities if reported is None else reported
        before.append({label: source.get(label, 0.0) for label in order})
    if not truths:
        return None
    found: dict[str, Any] = {"n": len(truths), "rescaled": rescaled}
    if rescaled:
        found["before"] = _figures(before, labels, truths, order)
    found["after"] = _figures(after, labels, truths, order)
    return found


def calibration(
    decision: Decision,
    dataset: Any,
    results: Sequence[DecisionResult],
) -> dict[str, Any]:
    """The run's calibration block: see the module docstring. `results` are in the order of
    `dataset.records`."""
    manifest = decision.pack.manifest
    variants = []
    for slot, provider in decision.providers.items():
        answering = _answering(provider)
        for leaf in provider.leaves():
            if not isinstance(leaf, AgentProvider | LlmProvider):
                continue
            fitted, why_not = _fitted(leaf)
            entry: dict[str, Any] = {
                "slot": slot,
                "variant": leaf.info.variant,
                "type": leaf.type,
                "status": CALIBRATED if fitted is not None else UNCALIBRATED,
            }
            if fitted is not None:
                entry["fitted"] = fitted
                entry["same_cases"] = fitted["sha256"] is not None and (
                    fitted["sha256"] == dataset.sha256
                )
            if why_not is not None:
                entry["why_not"] = why_not
            if leaf is answering:
                entry["questions"] = {
                    key: found
                    for key in leaf.questions
                    if (found := _question(key, manifest.labels(key), dataset.records, results))
                }
            variants.append(entry)
    changed = sum(bool(result.signals.reasons(("calibration",))) for result in results)
    block = {
        "format": CALIBRATION_FORMAT,
        "cases": len(results),
        "variants": variants,
        # Cases whose agent reported another version than its calibration's: nothing was
        # rescaled on them, and none was automated.
        "agent_changed": changed,
    }
    block["lines"] = _calibration_lines(block)
    return block


def _change(
    before: Mapping[str, Any] | None, after: Mapping[str, Any], figure: str, digits: int = 3
) -> str:
    """A figure after its calibration, and before it when there is one: `0.142 -> 0.031`."""

    def shown(value: Any) -> str:
        return "not estimable" if value is None else f"{value:.{digits}f}"

    now = shown(after[figure])
    return now if before is None else f"{shown(before[figure])} -> {now}"


def _fitted_words(entry: Mapping[str, Any]) -> str:
    fitted = entry["fitted"]
    cases = fitted["cases"]
    counted = f"{cases:,} cases" if isinstance(cases, int) else "cases"
    when = f" ({fitted['date']})" if fitted.get("date") else ""
    agent = fitted.get("agent")
    if agent is not None:
        name = f"{agent['id']} " if agent.get("id") else ""
        whom = f" for {name}version {agent['version']}"
    else:
        whom = f" for {fitted['model']}" if fitted.get("model") else ""
    return f"calibrated on {counted} of {fitted['dataset']}{when}{whom}"


def _calibration_lines(block: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for entry in block["variants"]:
        name = f"{entry['slot']}/{entry['variant']}"
        if entry["status"] == CALIBRATED:
            lines.append(f"{name}: {_fitted_words(entry)}")
            if entry.get("same_cases"):
                lines.append(
                    "  these are the cases it was fitted on, so the figures after it are "
                    "optimistic: evaluate on a set of its own"
                )
            for key, why in entry["fitted"]["not_fitted"].items():
                lines.append(f"  {key}: {NOT_CALIBRATED} ({why})")
        else:
            why = entry.get("why_not") or "the confidence is the model's own report"
            lines.append(f"{name}: {NOT_CALIBRATED} ({why})")
        for key, found in (entry.get("questions") or {}).items():
            before, after = found.get("before"), found["after"]
            lines.append(
                f"  {key} (n={found['n']:,}): ECE {_change(before, after, 'ece')}   "
                f"equal-mass ECE {_change(before, after, 'equal_mass_ece')}   "
                f"Brier reliability {_change(before, after, 'brier_reliability', 4)}"
            )
            lines.extend(_reliability_rows(before, after))
    if block["agent_changed"]:
        lines.append(
            f"{block['agent_changed']:,} of {block['cases']:,} cases: the agent reported another "
            "version than its calibration's, so nothing was rescaled and none was automated"
        )
    return lines


def _reliability_rows(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[str]:
    """The reliability table, a row per confidence bin that holds a case: how many cases
    stated a confidence in the bin and how many of them were right, before and after."""

    def by_bin(figures: Mapping[str, Any] | None) -> dict[float, Mapping[str, Any]]:
        return {} if figures is None else {row["low"]: row for row in figures["bins"]}

    was, now = by_bin(before), by_bin(after)

    def cell(row: Mapping[str, Any] | None) -> str:
        if row is None:
            return f"{'0':>14}  {'':>6}"
        return f"{row['n']:>14,}  {row['accuracy'] * 100:>5.1f}%"

    head = f"    {'stated':<8}"
    head += f"{'before: cases':>14}  {'right':>6}" if before is not None else ""
    head += f"{'after: cases' if before is not None else 'cases':>14}  {'right':>6}"
    rows = [head]
    for low in sorted({*was, *now}):
        high = (was.get(low) or now[low])["high"]
        line = f"    {low:.1f}-{high:.1f} "
        if before is not None:
            line += cell(was.get(low))
        line += cell(now.get(low))
        rows.append(line.rstrip())
    return rows


# --------------------------------------------------------------------------------------------
# The second signal
# --------------------------------------------------------------------------------------------


def _sorted(case: tuple[bool, bool, bool]) -> str:
    """A case a second signal disagreed on, as what the disagreement bought: `caught` (the
    agent's answer to the primary question was wrong, or the case is a critical label's),
    `extra_review` (it was right, and not critical), or `unlabelled`."""
    labelled, wrong, critical = case
    if critical or (labelled and wrong):
        return "caught"
    return "extra_review" if labelled else "unlabelled"


def second_signal(
    decision: Decision,
    ledger_cases: Sequence[Mapping[str, Any]],
    results: Sequence[DecisionResult],
    primary_truths: Sequence[str | None],
) -> dict[str, Any]:
    """The run's second-signal block: see the module docstring. `ledger_cases` (for each
    case's critical labels) and `primary_truths` are in the order of `results`."""
    manifest = decision.pack.manifest
    primary = manifest.policy.primary
    kinds: list[tuple[bool, bool, bool]] = []
    for line, result, truth in zip(ledger_cases, results, primary_truths, strict=True):
        answer = result.answers.get(primary)
        wrong = truth is not None and (answer is None or answer.label != truth)
        kinds.append((truth is not None, wrong, bool(line["k"])))

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, result in enumerate(results):
        for found in result.signals.checked:
            group = groups.setdefault((found.slot, found.variant, found.kind), _group(found))
            group["cases"] += 1
            group["extra_calls"] += found.extra_calls
            if found.canned:
                group["canned"] += 1
            elif found.disagreed:
                group["disagreed"] += 1
                group[_sorted(kinds[index])] += 1

    kept = {"k": 0, "caught": 0, "extra_review": 0, "unlabelled": 0}
    for index, result in enumerate(results):
        inputs = result.policy_inputs
        if inputs is None or not result.signals.reasons(SECOND_SIGNALS):
            continue
        without = replace(inputs, caps=result.signals.reasons(("calibration",)))
        if without.apply(manifest).outcome.value == _AUTO and result.outcome.value != _AUTO:
            kept["k"] += 1
            kept[_sorted(kinds[index])] += 1
    block: dict[str, Any] = {
        "format": SECOND_SIGNAL_FORMAT,
        "cases": len(results),
        "signals": list(groups.values()),
        "kept_from_automation": kept,
    }
    block["lines"] = _second_signal_lines(block)
    return block


def _group(found: Checked) -> dict[str, Any]:
    return {
        "slot": found.slot,
        "variant": found.variant,
        "kind": found.kind,
        "runs": found.runs,
        "cases": 0,
        "canned": 0,
        "disagreed": 0,
        "caught": 0,
        "extra_review": 0,
        "unlabelled": 0,
        "extra_calls": 0,
    }


def _bought(found: Mapping[str, Any]) -> str:
    parts = [
        f"{found['caught']:,} wrong or critical (caught)",
        f"{found['extra_review']:,} right (extra review)",
    ]
    if found["unlabelled"]:
        parts.append(f"{found['unlabelled']:,} not labelled")
    return ", ".join(parts)


def _second_signal_lines(block: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for group in block["signals"]:
        name = f"{group['slot']}/{group['variant']}"
        what = (
            f"each case asked {group['runs']} times"
            if group["kind"] == "repeat"
            else "each case checked by a second variant"
        )
        calls = f"{group['extra_calls']:,} extra call{'' if group['extra_calls'] == 1 else 's'}"
        lines.append(f"{name}: {what} ({calls})")
        checked = group["cases"] - group["canned"]
        if group["canned"]:
            lines.append(
                f"  not checked on {group['canned']:,} cases: their answers are canned and "
                "cannot vary, so no agreement is claimed"
            )
        if checked:
            lines.append(
                f"  disagreed on {group['disagreed']:,} of {checked:,} cases"
                + (f": {_bought(group)}" if group["disagreed"] else "")
            )
    kept = block["kept_from_automation"]
    if block["signals"]:
        lines.append(
            f"kept {kept['k']:,} of {block['cases']:,} cases from automation"
            + (f": {_bought(kept)}" if kept["k"] else "")
        )
    return lines


# --------------------------------------------------------------------------------------------
# As text
# --------------------------------------------------------------------------------------------


def _titled(title: str, lines: Sequence[str]) -> str:
    if not lines:
        return ""
    return "\n".join([title, *(f"{_INDENT}{line}" for line in lines)])


def text(run: Mapping[str, Any]) -> str:
    """Both blocks of a report's `run` as the terminal prints them, or "" for a run that has
    neither a model variant nor a second signal (and for a report from before them)."""
    parts = []
    found = run.get("calibration")
    if isinstance(found, Mapping) and found.get("lines"):
        parts.append(_titled("Confidence", found["lines"]))
    signal = run.get("second_signal")
    if isinstance(signal, Mapping) and signal.get("lines"):
        parts.append(_titled("Second signal", signal["lines"]))
    return "\n\n".join(parts)


def markdown(run: Mapping[str, Any]) -> str:
    """`text`, as a Markdown summary shows it: in a block that renders nothing."""
    said = text(run)
    if not said:
        return ""
    body = "\n".join(f"    {line}" for line in said.splitlines())
    return f"### Confidence and second signal\n\n{body}\n"
