# SPDX-License-Identifier: Apache-2.0
"""What `mendari probe` says: its report as text, a dry run's plan as text, the verdict line,
and the exit code a CI job reads.

Everything here works from the report itself (`FORMAT`, built by `evals/probe.py`) or from a
plan's summary (`PLAN_FORMAT`), so what the command prints is what `--json` holds: ids, names of
transformations, questions and labels, and counts. Never a case's text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mendari.errors import MendariError
from mendari.evals.behaviour_tests import PROBE_TESTS, PROBE_UNCONFIRMED
from mendari.providers.agent import ENDPOINT_ENV

FORMAT = "mendari.probe/1"
PLAN_FORMAT = "mendari.probe-plan/1"
EXIT_FOUND = 2  # `--fail-on`: the probe ran, and found what it was told to fail on
MAX_SHOWN = 20  # findings the text lists; the JSON report lists them all
_HEADINGS = ("transformation", "tried", "changed", "critical lost", "automated")
_COLUMNS = ("tried", "changed", "critical_lost", "automated")


def _count(number: int, what: str) -> str:
    return f"{number} {what}" if number == 1 else f"{number} {what}s"


def verdict(
    totals: Mapping[str, int], cases: int, *, canned: bool = False, nothing: str | None = None
) -> str:
    """One plain line: the worst that was found. `canned`: the answers probed were the agent's
    canned ones: the line says so, and what to probe next, since nobody fixes a fixture.
    `nothing`: why nothing could be probed."""
    said = "Verdict (canned answers)" if canned else "Verdict"
    then = "Fix the agent, then probe again."
    if canned:
        then = f"These are canned answers: set {ENDPOINT_ENV} and probe your agent."
    tried, changed = totals["tried"], totals["changed"]
    automated, lost = totals["automated"], totals["critical_lost"]
    was = "was" if lost == 1 else "were"
    if nothing is not None:
        return f"Verdict: nothing was probed: {nothing}."
    if not tried:
        return f"{said}: nothing was probed: no variant could be made of these cases."
    if automated:
        also = f", and {_count(lost, 'critical answer')} {was} lost" if lost else ""
        return (
            f"{said}: not ready. {_count(automated, 'variant')} would have been automated while "
            f"wrong or critical{also}. {then}"
        )
    if lost:
        on, read = ("a variant", "the case") if lost == 1 else ("variants", "each of those cases")
        return (
            f"{said}: {_count(lost, 'critical answer')} {was} lost on {on}; a person would still "
            f"have read {read}. {then}"
        )
    if changed:
        return (
            f"{said}: the answers changed on {changed} of {_count(tried, 'variant')}, none of "
            "them a critical answer."
        )
    return f"{said}: no answer changed on {_count(tried, 'variant')} of {_count(cases, 'case')}."


def exit_code(report: Mapping[str, Any], fail_on: str | None) -> int:
    """What a CI job gets: 0, or `EXIT_FOUND` when the probe found what `fail_on` names:
    `critical` (a critical answer lost, or a variant automated while wrong or critical) or
    `any` (any answer changed). With no `fail_on` a probe only reports. A probe that tried
    nothing cannot pass: asked to fail on findings, it is an error."""
    totals = report["totals"]
    if fail_on is None:
        return 0
    if not totals["tried"]:
        raise MendariError("nothing was probed, so --fail-on has nothing to pass")
    if fail_on == "any":
        return EXIT_FOUND if totals["changed"] else 0
    return EXIT_FOUND if totals["automated"] or totals["critical_lost"] else 0


def kept_lines(
    added: Sequence[str],
    waiting: Sequence[str],
    already: int,
    left_out: Mapping[str, int],
    agent: str,
) -> list[str]:
    """What `--write` did, a line for each thing: the tests added to those that run, those
    that wait for a person (and how they are confirmed), those there already, and the failures
    left out. `agent` is the agent as the person named it, for the command they type next."""
    lines = []
    if added:
        lines.append(
            f"Wrote {_count(len(added), 'test')} to {PROBE_TESTS}: `mendari test` fails on "
            f"{'it' if len(added) == 1 else 'them'} until the agent is fixed."
        )
    if waiting:
        one = len(waiting) == 1
        confirm = " ".join(f"--confirm {name}" for name in waiting)
        whose = "its" if one else "each one's"
        lines.append(
            f"{_count(len(waiting), 'test')} {'waits' if one else 'wait'} in {PROBE_UNCONFIRMED}: "
            f"a model changed what {'it expects' if one else 'they expect'}, so "
            f"{'it does' if one else 'they do'} not run. Read {whose} text and its `expect` "
            f"there, then `mendari probe {agent} {confirm}`."
        )
    are = "is a test" if already == 1 else "are tests"
    if not added and not waiting:
        there = f": {_count(already, 'failure')} {are} already" if already else ""
        lines.append(f"Nothing new to write{there}.")
    elif already:
        lines.append(f"{_count(already, 'failure')} more {are} already.")
    site, unheld = left_out.get("site", 0), left_out.get("no_expectation", 0)
    if site:
        lines.append(
            f"Not written: {_count(site, 'failure')} of a site's own cases, whose text stays "
            "where it is marked as a site's."
        )
    if unheld:
        lines.append(
            f"Not written: {_count(unheld, 'variant')} that changed an answer to no worse: "
            "there is nothing to hold the agent to."
        )
    if added or waiting:
        lines.append(
            "The agent's files changed, and its digest with them: it is another version now."
        )
    return lines


# -- the report, as text ---------------------------------------------------------------------------


def text(report: Mapping[str, Any], *, shown: int = MAX_SHOWN) -> str:
    """The report as the command line prints it: what was probed and what it cost, a line per
    transformation, what was left out, the worst findings (at most `shown`), and the verdict
    last."""
    if report["nothing"] is not None:
        return "\n".join([_agent(report), "", report["verdict"]])
    lines = [_agent(report), _cases(report, "Text changed"), _calls(report)]
    by_model = _by_model(report)
    if by_model:
        lines.append(by_model)
    lines += ["", *_table(report)]
    lines += _left_out(report)
    findings = report["findings"]
    if findings:
        listed = findings[:shown]
        some = str(len(findings))
        if len(listed) < len(findings):
            some = f"{len(listed)} of {len(findings)}; --json lists them all"
        lines += ["", f"Findings, worst first ({some})"]
        rows = [
            (found["harm"].replace("_", " "), found["case"], found["transformation"], _said(found))
            for found in listed
        ]
        lines += [f"  {row}" for row in _aligned(rows)]
    return "\n".join([*lines, "", report["verdict"]])


def _agent(report: Mapping[str, Any]) -> str:
    agent = report["agent"]
    providers = ", ".join(f"{slot}={variant}" for slot, variant in report["providers"].items())
    return f"{agent['name']} {agent['version']} ({agent['id']}), {providers}"


def _cases(report: Mapping[str, Any], changed: str) -> str:
    cases = report["cases"]
    held_to = f"{cases['critical']} with a critical label"
    if not cases["labelled"]:
        held_to = "no labels: each variant is held to the agent's own answers as written"
    said = (
        f"{cases['probed']} of the {_count(cases['distinct'], 'distinct case')} of "
        f"{report['source']['name']} ({held_to}), seed {report['seed']}. {changed}: "
        f"{', '.join(report['fields'])}."
    )
    if cases["refused"]:
        said += f" {_count(cases['refused'], 'case')} the agent refuses as written left out."
    if cases.get("unanswered"):
        said += f" {_count(cases['unanswered'], 'case')} it did not answer as written left out."
    return said


def _canned(report: Mapping[str, Any]) -> str:
    """`its answers are canned (<why>), read on this machine`."""
    why = str(report["canned"]).partition(": ")[2]
    return f"its answers are canned ({why}), read on this machine"


def _calls(report: Mapping[str, Any]) -> str:
    calls = report["calls"]
    decisions = _count(calls["decisions"], "decision")
    if not calls["agent"]:
        if report["canned"] is None:
            return f"{decisions}, none of them a call to an agent."
        return (
            f"{decisions}, none of them a call to an agent: {_canned(report)}. What follows is "
            "what the canned answers miss, not what your agent does: set "
            f"{ENDPOINT_ENV} and probe again."
        )
    cost = _cost(calls.get("cost_usd"))
    said = f"{decisions}: {_count(calls['agent'], 'call')} to the agent, {cost}."
    return said if report["canned"] is None else f"{said} Part of {_canned(report)}."


def _cost(cost: float | None) -> str:
    """A cost as a line says it; None is a cost nobody reported, never a free one."""
    if cost is None:
        return "at a cost it did not report"
    return f"${cost:,.2f}" if cost >= 0.01 or cost == 0 else f"${cost:.4f}"


def _by_model(report: Mapping[str, Any]) -> str:
    """The line about the model that wrote variants, if one did."""
    calls = report["calls"]
    if not calls["generator"]:
        return ""
    return (
        f"{_count(calls['generator'], 'call')} to {report['written_by']}, which wrote variants, "
        f"{_cost(calls.get('generator_cost_usd'))}."
    )


def _table(report: Mapping[str, Any]) -> list[str]:
    rows = [
        (line["name"], *(str(line[key]) for key in _COLUMNS)) for line in report["transformations"]
    ]
    rows.append(("total", *(str(report["totals"][key]) for key in _COLUMNS)))
    first = max(len(row[0]) for row in [_HEADINGS, *rows])
    lines = ["  ".join([f"{_HEADINGS[0]:<{first}}", *_HEADINGS[1:]])]
    for name, *numbers in rows:
        cells = [
            f"{number:>{len(heading)}}"
            for number, heading in zip(numbers, _HEADINGS[1:], strict=True)
        ]
        lines.append("  ".join([f"{name:<{first}}", *cells]))
    if any(not line["keeps_meaning"] for line in report["transformations"]):
        changing = [line["name"] for line in report["transformations"] if not line["keeps_meaning"]]
        lines.append(
            f"({', '.join(changing)}: a variant changes what is right, and `changed` counts the "
            "answers that did not follow it.)"
        )
    return lines


def _left_out(report: Mapping[str, Any]) -> list[str]:
    """What was not tried and why, and what got no answer: a line each, when there is any."""
    lines = []
    skipped = _reasons(report["transformations"], "skipped")
    if skipped:
        lines.append(f"Not tried: {skipped}.")
    failed = report["not_written"]
    if failed:
        why = ", ".join(f"{code} {count}" for code, count in failed.items())
        lines.append(f"The model wrote no variant {_count(sum(failed.values()), 'time')}: {why}.")
    unanswered = ", ".join(
        f"{line['name']} {line['unanswered']}"
        for line in report["transformations"]
        if line["unanswered"]
    )
    if unanswered:
        lines.append(
            f"No answer (a person decides those; probe again to see whether it repeats): "
            f"{unanswered}."
        )
    return lines


def _reasons(transformations: Sequence[Mapping[str, Any]], key: str) -> str:
    """`typos 2 (nothing to change), case 3 (same as another)`."""
    return ", ".join(
        f"{line['name']} {count} ({why.replace('_', ' ')})"
        for line in transformations
        for why, count in line[key].items()
    )


def _aligned(rows: Sequence[Sequence[str]]) -> list[str]:
    """Rows as columns two spaces apart, each as wide as its longest cell; the last as it is."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
    return [
        "  ".join(
            [*(f"{cell:<{width}}" for cell, width in zip(row, widths, strict=False)), row[-1]]
        )
        for row in rows
    ]


def _said(finding: Mapping[str, Any]) -> str:
    """What a finding changed, in the agent's own names: `urgent yes -> no; AUTO -> ...`."""
    answers = []
    for question, labels in finding["answers"].items():
        said = f"{question} {labels['original']} -> {labels['variant']}"
        if labels["expected"] not in (labels["original"], labels["variant"]) or (
            labels["original"] == labels["variant"]
        ):
            said += f" (expected {labels['expected']})"
        answers.append(said)
    outcome = finding["outcome"]
    if outcome["original"] != outcome["variant"]:
        answers.append(f"{outcome['original']} -> {outcome['variant']}")
    return "; ".join(answers)


# -- a dry run's plan, as text ---------------------------------------------------------------------


def planned(summary: Mapping[str, Any]) -> str:
    """What a probe would do, as `--dry-run` prints it: the cases, the variants by
    transformation, and how many decisions and calls running it makes."""
    lines = [
        _agent(summary),
        "A dry run: nothing was decided, and no agent or model was called.",
    ]
    if summary["nothing"] is not None:
        return "\n".join([*lines, f"Nothing to probe: {summary['nothing']}."])
    lines.append(_cases(summary, "Text to change"))
    variants = ", ".join(
        f"{line['name']} {line['variants']}" for line in summary["transformations"]
    )
    lines.append(f"Variants: {variants}.")
    skipped = _reasons(summary["transformations"], "skipped")
    if skipped:
        lines.append(f"Not built: {skipped}.")
    calls, cases = summary["calls"], summary["cases"]["probed"]
    decisions = (
        f"{_count(calls['decisions'], 'decision')} ({_count(cases, 'case')} as written and "
        f"{_count(calls['decisions'] - cases, 'variant')})"
    )
    if calls["generator"]:
        lines.append(
            f"At most {_count(calls['generator'], 'call')} to {summary['written_by']}, to write "
            "variants."
        )
    if calls["agent"]:
        lines.append(f"{decisions}: at most {_count(calls['agent'], 'call')} to the agent.")
    elif summary["canned"] is None:
        lines.append(f"{decisions}, none of them a call to an agent.")
    else:
        lines.append(f"{decisions}, none of them a call to an agent: {_canned(summary)}.")
    return "\n".join(lines)
