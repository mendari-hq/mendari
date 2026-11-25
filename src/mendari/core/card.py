# SPDX-License-Identifier: Apache-2.0
"""Render an agent's Decision Card (Markdown) from its manifest, policy and published benchmark."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Literal

from mendari import notices
from mendari.core import clock
from mendari.core.agent import Agent
from mendari.core.policy import GATE_TEXT, gate_of
from mendari.core.spec import AgentManifest, Condition, Question
from mendari.evals import leaderboard, operating_point, ranking, stats
from mendari.evals.render import INTERVALS, equal_mass, evidence, num, shown
from mendari.evals.result import (
    counted,
    false_automation,
    normalize,
    subgroup_labelled,
    sweep,
)

RISK_NAMES = {
    0: "administrative",
    1: "clinical information processing",
    2: "clinical support",
    3: "patient-specific prediction",
    4: "time-critical / diagnostic / treatment",
}
RISK_AUTOMATION = {
    0: "AUTO permitted by policy thresholds.",
    1: "AUTO permitted by policy thresholds; every decision is audited.",
    2: "Human review required by default; AUTO only with a recorded governance override.",
    3: "Human review required by default; AUTO only with a recorded governance override.",
    4: "No autonomous deployment: every decision requires human review.",
}


# What a malformed attestation raises when the card reads it (a missing key, a wrong type).
_SHAPE_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError, ArithmeticError)


def benchmark(agent: Agent) -> dict[str, Any] | None:
    """The agent's published benchmark (`evaluation.benchmark`), or None: none named, none
    there, or not a report this card can show (`read_benchmark` says why)."""
    return read_benchmark(agent)[0]


def read_benchmark(agent: Agent) -> tuple[dict[str, Any] | None, str | None]:
    """The benchmark, or None and why it is not shown. As the registry does, one that is not
    JSON, holds what a JSON response cannot (NaN, a lone surrogate) or is not the evaluation
    report the card shows is treated as absent, never an error."""
    relative = agent.manifest.evaluation.benchmark
    if relative is None:
        return None, None
    report, problem = agent.attestation_json(relative)
    if report is None:
        return None, problem
    if isinstance(report, dict):
        report = normalize(report)  # either format; v1's time and latency in its run block
    return _shown(report)


def _shown(report: Any) -> tuple[dict[str, Any] | None, str | None]:
    """`report`, or None and why the card cannot show it: everything the card reads of it
    (`_last_validation`, `_validation`, and the version it was measured on) is read once here,
    so a missing key or a wrong type is never met while the card is rendered."""
    try:
        _last_validation(report)
        _validation(report)
        _measured_on(report)
    except _SHAPE_ERRORS as error:
        return None, f"not an evaluation report ({type(error).__name__})"
    return report, None


def _measured_on(report: Mapping[str, Any]) -> tuple[Any, Any]:
    """The version and digest of the agent a benchmark was measured on."""
    measured = report["agent"]
    return measured.get("version"), measured.get("digest")


# What `render_card` reads an attestation from: the agent's own file, or what the caller gives
# (None: none shown). The registry gives none (ADR-0012).
FROM_AGENT: Literal["agent"] = "agent"
Attested = Mapping[str, Any] | None | Literal["agent"]
RenderedBy = Literal["mendari card", "registry", "store"]


def card_of(agent: Agent) -> str:
    """The card Mendari shows for an agent it serves or names: an agent from a registry as the
    registry renders it (its uploaded benchmark and leaderboard are its publisher's, and its
    text is plain), a version from an agent store as the store renders it (LP-20e: nothing uploaded
    about it is kept, and its text is plain), any other agent with its own attestations."""
    if agent.origin == "hub":
        return render_card(agent, report=None, board=None, rendered_by="registry")
    if agent.origin == "store":
        return render_card(agent, report=None, board=None, rendered_by="store")
    return render_card(agent)


def render_card(
    agent: Agent,
    *,
    report: Attested = FROM_AGENT,
    board: Attested = FROM_AGENT,
    rendered_by: RenderedBy = "mendari card",
) -> str:
    """The Decision Card. Its attestations (the files the digest leaves out: the benchmark
    `report` and the leaderboard `board`) are explicit inputs: by default the agent's own
    (`mendari card`, the engine's own agents); None shows none. Everything else comes from the
    files the digest covers: the manifest, policy, input schema and changelog.
    `rendered_by="registry"` is the registry's card (ADR-0012): the risk level declared by the
    publisher, and a validation section that says no uploaded figure is shown; `"store"` an agent
    store's card (LP-20e), its text plain too."""
    token = _PLAIN.set(rendered_by in ("registry", "store"))
    try:
        return _render(agent, report, board, rendered_by)
    finally:
        _PLAIN.reset(token)


def _render(agent: Agent, report: Attested, board: Attested, rendered_by: RenderedBy) -> str:
    m = agent.manifest
    level = int(m.risk.level)
    report, report_problem = _attested_benchmark(agent, report)
    board, board_problem = _attested_leaderboard(agent, board)
    registry = rendered_by == "registry"
    category = f" ({_t(m.risk.category)})" if m.risk.category != RISK_NAMES[level] else ""
    # Where the agent comes from, by origin and never by its manifest: a card written for an agent
    # of one's own (local) or pulled from a registry claims nothing.
    source = [f"| Source | {agent.origin_label} |"] if agent.origin == "maintained" else []
    automation = RISK_AUTOMATION[level] + _required(m.policy.automation_requires)
    out: list[str] = [
        f"# Decision Card: {_t(m.name)}",
        "",
        f"**{agent.status_label}**",
        "",
        f"*{notices.NOTICE}*",
        "",
        f"`{m.id}` · version **{m.version}** · {_t(m.license)} · "
        f"published by {_t(m.publisher.name)} ({_t(m.publisher.type)}, self-declared)",
        "",
        f"> {_t(m.summary)}",
        "",
        "| | |",
        "|---|---|",
        *source,
        f"| Risk level | **{level}**, {RISK_NAMES[level]}{category}"
        + (" (declared by the publisher)" if registry else "")
        + " |",
        f"| Automation | {automation} |",
        f"| Agent digest | `{agent.digest}` |",
    ]
    if report:
        out.append(_last_validation(report))
    out += [
        "",
        "## Intended use",
        "",
        _t(m.card.intended_use),
        "",
        f"**Why risk level {level}:** {_t(m.risk.rationale)}",
        "",
        "## Intended users",
        "",
        *[f"- {_t(user)}" for user in m.card.intended_users],
        "",
        "## Patient population",
        "",
        _t(m.card.patient_population),
        "",
        "## Out of scope",
        "",
        *[f"- {_t(item)}" for item in m.card.out_of_scope],
        "",
        "## Inputs",
        "",
        "| Field | Type | Required |",
        "|---|---|---|",
        *_schema_rows(agent.input_schema),
        "",
        f"FHIR R4 mapping: {_c(m.inputs.fhir)}." if m.inputs.fhir else "No FHIR mapping.",
        *_code_systems(m),
        "",
        "## Outputs",
        "",
    ]
    for key, question in m.questions.items():
        out += _question(key, question, primary=key == m.policy.primary)
    p = m.policy
    out += [
        "Every decision is one of `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a priority "
        "(`routine`, `urgent`, `asap`, `stat`) and an audit record.",
        "",
        *_written(agent),
        "## Decision policy",
        "",
    ]
    out.append(
        f"- AUTO when {_c(p.primary)} confidence ≥ **{p.thresholds.auto:.2f}**; human review "
        f"≥ **{p.thresholds.review:.2f}**; abstain below."
    )
    for label, label_policy in p.labels.items():
        if not label_policy.allow_auto:
            out.append(f"- {_c(label)} is never automated.")
        elif label_policy.auto_threshold is not None:
            out.append(f"- {_c(label)} needs ≥ **{label_policy.auto_threshold:.2f}** for AUTO.")
    for slot, variants in p.automation_requires.items():
        running = " or ".join(_c(variant) for variant in variants)
        out.append(f"- AUTO only while {_c(slot)} is {running}; otherwise a person reviews.")
    for rule in p.rules:
        target = f" → {_c(rule.recommendation)}" if rule.recommendation else ""
        priority = f", priority `{rule.priority.value}`" if rule.priority else ""
        out.append(
            f"- Rule **{_t(rule.name)}**: {_condition(rule.when)} ⇒ "
            f"`{rule.outcome.value}`{target}{priority}. {_t(rule.reason)}"
        )
    out += [f"- Fallback (ABSTAIN / BLOCK): {_t(p.fallback)}", ""]
    out += _actions(agent)
    out += ["## Providers", ""]
    out += ["| Slot | Answers | Default | Alternatives |", "|---|---|---|---|"]
    for slot, spec in m.providers.items():
        alternatives = ", ".join(
            f"{_c(name)} ({_t(v.type)})" for name, v in spec.variants.items() if name != spec.use
        )
        default = spec.variants[spec.use]
        out.append(
            f"| {_c(slot)} | {_t(', '.join(spec.answers))} | {_c(spec.use)} ({_t(default.type)}) | "
            f"{alternatives or 'none'} |"
        )
    out += _confidence(agent)
    out += ["", "## Validation", ""]
    if report:
        out += _validation(report)
    elif registry:
        out.append(
            "The registry shows no figure it did not compute itself: the publisher's benchmark "
            "and leaderboard are left out. Validate the agent on your own data."
        )
    elif rendered_by == "store":
        out.append(
            "The store shows no figure it did not compute itself: a benchmark or leaderboard "
            "uploaded with the agent is not kept. Its evaluations in this organization's store "
            "are listed beside it."
        )
    else:
        out.append("No published benchmark.")
    if report and (on := _measured_on(report))[1] != agent.digest:
        measured = ranking.measured_on(*on, agent.version)
        out += [
            "",
            f"This benchmark was measured on {measured}; evaluate it again before relying on it.",
        ]
    unread = [(m.evaluation.benchmark, report_problem), (leaderboard.PATH, board_problem)]
    for relative, why in unread:
        if why:
            out += ["", f"{relative} is not shown: {why}."]
    if board and board.get("rows"):
        out += [
            "",
            "## Provider comparison",
            "",
            leaderboard.preface(),
            "",
            "Each table below is one dataset at one size, and the configurations in a table that "
            "records the dataset's hash ran on exactly the same records. Remote models are named "
            "because they are a deployment choice. Synthetic and hand-written data: compare "
            "configurations with each other, not with performance at a real site. A row measured "
            "on an earlier version of the agent says so beside its configuration: its AUTO and "
            "false automation followed that version's policy.",
            "",
            *leaderboard.markdown(board, agent.digest, agent.version),
        ]
    out += ["", "## Known failure modes", ""]
    out += [f"- {_t(item)}" for item in m.card.known_failure_modes]
    out += ["", "## Evidence and provenance", ""]
    for cited in m.card.evidence:
        out.append(
            f"- **{_cited(cited.title, cited.url)}**"
            + (f": {_t(cited.note)}" if cited.note else "")
        )
    if agent.path("CHANGELOG.md").is_file():
        history = agent.read_text("CHANGELOG.md").split("\n", 1)[-1].strip()
        out += [
            "",
            "## Version history",
            "",
            _history(history),
        ]
    out += [
        "",
        "---",
        "",
        (
            "*Rendered by the registry from the agent's own files (the files its digest covers). "
            "What it says about the agent is the publisher's. It is not evidence of performance "
            "at your site; run local validation first.*"
            if registry
            else "*Generated by `mendari card`. A Decision Card describes validation on the data "
            "named above. It is not evidence of performance at your site; run local validation "
            "first.*"
        ),
        "",
    ]
    return "\n".join(out)


# While the registry renders a card, every string the publisher wrote is plain text: on one line,
# with Markdown's punctuation escaped, so it can neither start a heading, table, list or quote nor
# make a link, image or emphasis that reads as the registry's own words (ADR-0012). `mendari card`
# renders an agent of one's own as its author wrote it.
_PLAIN: ContextVar[bool] = ContextVar("card_plain_text", default=False)
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")  # every ASCII punctuation character
_LINK = re.compile(r"^https?://[^\s()<>\[\]`\\|*_]+$")


def _written(agent: Agent) -> list[str]:
    """The card's Written outputs section (spec 0.4 sections 16 to 19): each text the agent
    writes, each rubric it is graded against (every criterion's instructions and every level's
    anchor, as a clinician and the judge read them) and the floors a judge is held to. Text
    only: the card shows no figure of a judge. Nothing for an agent that declares none, so its
    card is what it always was."""
    from mendari.evals import written

    manifest = agent.manifest
    if not manifest.outputs:
        return []
    out = [
        "## Written outputs",
        "",
        "What the agent writes besides its answers. A text is returned with the decision and "
        "is not kept with it. Each is graded against a rubric: clinicians grade a sample "
        "blind, a model judge grades every text in the same words, and the judge's grades are "
        "reported for the texts no clinician graded only where its agreement with clinicians "
        "clears the floors below. Otherwise they are withheld.",
        "",
    ]
    for name, output in manifest.outputs.items():
        source = (
            f"written by the agent behind slot {_c(output.slot)}"
            if output.slot is not None
            else f"read from the input's field {_c(output.input)}"
        )
        out += [
            f"### {_c(name)}",
            "",
            f"{_t(output.description)} It is {source}, at most {output.max_chars:,} characters.",
            "",
        ]
        rubrics = {key: r for key, r in manifest.rubrics.items() if r.output == name}
        if not rubrics:
            out += ["No rubric: it is not graded.", ""]
        for key, rubric in rubrics.items():
            beside = ", ".join(_c(field) for field in rubric.context_fields) or "nothing else"
            out += [
                f"Rubric {_c(key)}. Beside the text, a grader reads: {beside}.",
                "",
                "| Criterion | Asks | Levels, lowest first | Passes from | Must not fail |",
                "|---|---|---|---|---|",
            ]
            for criterion_key, criterion in rubric.criteria.items():
                levels = "; ".join(
                    f"{_c(level)}: {_t(criterion.criteria[level])}"
                    for level, _ in criterion.scale()
                )
                out.append(
                    f"| {_c(criterion_key)} | {_t(criterion.instructions)} | {levels} | "
                    f"{_c(criterion.passing())} | {'yes' if criterion.must_pass else 'no'} |"
                )
            floors = written.floors_of(manifest, key)
            out += [
                "",
                "A judge is believed on a criterion when the lower bound of its kappa with "
                f"clinicians is at least {floors['agreement']}, over the levels and on pass or "
                "fail alike; it does not pass more outputs than clinicians do by more than "
                "chance; it could grade all but "
                f"{floors['max_judge_errors']} of the outputs; and, on a criterion that must "
                "not fail, the lower bound of the share of clinician-found failures it finds "
                f"is at least {floors['must_pass_recall']}. That is decided on at least "
                f"{floors['min_paired']} outputs graded by both, with {floors['min_each_side']} "
                "on each side of the pass line. On less, the evidence is insufficient.",
                "",
            ]
    return out


def _actions(agent: Agent) -> list[str]:
    """The card's Actions section (spec 0.4): each action the agent may propose, what it does,
    its effect and how the runtime gates it (`policy.gate_of`, in `policy.GATE_TEXT`'s words).
    Nothing for an agent that declares none, so its card is what it always was."""
    declared = agent.manifest.actions
    if not declared:
        return []
    out = [
        "## Actions",
        "",
        "What the agent may ask to have done, besides answer. Mendari authorizes each action "
        "and runs none: the caller runs an action only when it was released, and every "
        "proposal and its release or denial is in the audit trail. An action the agent "
        "proposes that is not listed here is blocked, and its decision goes to a person. "
        "Where the deployment has not vouched for the agent (one from a registry before its "
        "organization approves its digest), nothing it proposes is released without a person.",
        "",
        "| Action | Does | Effect | Released | Arguments |",
        "|---|---|---|---|---|",
    ]
    for name, spec in declared.items():
        gate = gate_of(spec.effect, allow_auto=spec.allow_auto)
        schema = spec.json_schema
        named = list((schema or {}).get("properties") or {})
        if schema is None:
            arguments = "not checked"
        elif named:
            arguments = "checked: " + ", ".join(_c(key) for key in named)
        else:
            arguments = "checked: none taken"
        out.append(
            f"| {_c(name)} | {_t(spec.description)} | `{spec.effect.value}` | "
            f"{GATE_TEXT[gate]} | {arguments} |"
        )
    return [*out, ""]


def _confidence(agent: Agent) -> list[str]:
    """The card's Confidence section (ADR-0024), from the manifest and the calibration files
    the digest covers: for each `agent` variant (a composite's member too), whether its
    confidence is calibrated, on how many cases and when, or that it is not; and each second
    signal a variant carries. Nothing for an agent with neither."""
    from mendari.providers import agent_calibration

    lines = []
    for slot, spec in agent.manifest.providers.items():
        for name, variant in spec.variants.items():
            config = {"type": variant.type, **(variant.model_extra or {})}
            members = config.get("members")
            members = members if isinstance(members, dict) else {}
            configs = [(name, config)]
            configs += [(f"{name}.{key}", m) for key, m in members.items() if isinstance(m, dict)]
            for label, item in configs:
                where = f"{_c(slot)} / {_c(label)}"
                if item.get("type") == "agent":
                    found = agent_calibration.status(agent, item)
                    if isinstance(found, str):
                        lines.append(
                            f"- {where}: **{_t(found)}**. Its confidence is the agent's own "
                            "report, and the automation threshold cuts on it as reported."
                        )
                    else:
                        service = f"{_c(found.service_id)} " if found.service_id else ""
                        fitted = ", ".join(_c(key) for key in sorted(found.temperatures))
                        lines.append(
                            f"- {where}: confidence **calibrated** on {found.cases:,} cases of "
                            f"{_c(found.dataset)} on {_t(found.date)}, for {service}version "
                            f"{_c(found.service_version)} (questions: {fitted or 'none'}). When "
                            "the agent's service reports another version, no decision is "
                            "automated until it is calibrated again."
                        )
                        lines += [
                            f"  - {_c(key)}: not calibrated ({_t(why)})"
                            for key, why in found.not_fitted.items()
                        ]
                if item.get("type") in ("agent", "llm") and "repeat" in item:
                    lines.append(
                        f"- {where}: asks each case {_t(item['repeat'])} times; the answer is "
                        "the majority's with the lowest confidence among the runs that agree, "
                        "and a disagreement is never automated."
                    )
            if variant.type == "check" and len(members) == 2:
                first, second = members
                lines.append(
                    f"- {_c(slot)} / {_c(name)}: {_c(first)} answers and {_c(second)} checks; "
                    "where they disagree, the decision is never automated."
                )
    return ["", "## Confidence", "", *lines] if lines else []


def _t(value: Any) -> str:
    """Publisher text as the card shows it."""
    text = str(value)
    if not _PLAIN.get():
        return text
    return _PUNCTUATION.sub(r"\\\1", " ".join(text.split()))


def _c(value: Any) -> str:
    """A name the publisher chose, as code: its own backticks cannot close the span, and a
    pipe cannot end a table cell."""
    text = str(value)
    if not _PLAIN.get():
        return f"`{text}`"
    text = " ".join(text.split()).replace("|", "\\|")
    fence = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _cited(title: str, url: str | None) -> str:
    """An evidence entry's title, linked when its address is one: in the registry's card only an
    http or https address with nothing Markdown could read as structure."""
    if not url:
        return _t(title)
    if not _PLAIN.get():
        return f"[{title}]({url})"
    if _LINK.match(url):
        return f"[{_t(title)}]({url})"
    return f"{_t(title)} ({_c(url)})"


def _history(history: str) -> str:
    """The changelog under the card's own heading: its `##` headings one level down. In the
    registry's card each line is plain text: a version heading, a list item or a line."""
    if not _PLAIN.get():
        return history.replace("\n## ", "\n### ").replace("## ", "### ", 1)
    lines = []
    for line in history.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
        elif line.startswith("## "):  # a version, as the spec's changelog writes it
            lines.append("### " + _t(line[3:]))
        elif stripped.startswith(("- ", "* ")):
            lines.append("- " + _t(stripped[2:]))
        else:
            lines.append(_t(stripped))
    return "\n".join(lines)


def _attested_benchmark(agent: Agent, given: Attested) -> tuple[dict[str, Any] | None, str | None]:
    """The benchmark to show and why one is not: the agent's own (`read_benchmark`), none, or
    the caller's, checked as the agent's is."""
    if given == FROM_AGENT:
        return read_benchmark(agent)
    if given is None:
        return None, None
    return _shown(normalize(dict(given)))


def _attested_leaderboard(
    agent: Agent, given: Attested
) -> tuple[dict[str, Any] | None, str | None]:
    if given == FROM_AGENT:
        return leaderboard.read(agent)
    return (None, None) if given is None else (dict(given), None)


def _question(key: str, question: Question, *, primary: bool) -> list[str]:
    marker = " (primary)" if primary else ""
    kind = f" *({_t(question.type)})*" if question.type != "choice" else ""
    out = [f"**{_c(key)}**{marker}{kind}: {_t(question.instructions)}", ""]
    levels = question.levels
    if levels:
        out += ["| Label | Score | Criteria |", "|---|---|---|"]
        out += [
            f"| {_c(label)} | {levels[label].score:g} | {_t(question.criteria[label])} |"
            for label in question.labels
        ]
    else:
        out += ["| Label | Criteria |", "|---|---|"]
        out += [f"| {_c(label)} | {_t(text)} |" for label, text in question.criteria.items()]
    return [*out, ""]


def _condition(when: Condition) -> str:
    parts = []
    if when.min_probability is not None:
        labels = " or ".join(_c(x) for x in when.labels)
        parts.append(f"P({_c(when.question)} is {labels}) ≥ {when.min_probability:.2f}")
    elif when.labels:
        parts.append(f"{_c(when.question)} is " + " or ".join(_c(x) for x in when.labels))
    if when.min_score is not None:
        parts.append(f"{_c(when.question)} expected score ≥ {when.min_score:g}")
    if when.max_score is not None:
        parts.append(f"{_c(when.question)} expected score ≤ {when.max_score:g}")
    confidence = f" (p ≥ {when.min_confidence:.2f})" if when.min_confidence else ""
    return " and ".join(parts) + confidence


def _schema_rows(schema: dict[str, Any], prefix: str = "") -> list[str]:
    rows = []
    required = set(schema.get("required", []))
    for name, prop in schema.get("properties", {}).items():
        path = f"{prefix}{name}"
        if not isinstance(prop, dict):  # JSON Schema's `true` / `false`: anything, or nothing
            rows.append(f"| {_c(path)} | any | {'yes' if name in required else 'no'} |")
            continue
        if prop.get("type") == "object":
            rows.append(f"| {_c(path)} | object | {'yes' if name in required else 'no'} |")
            rows += _schema_rows(prop, f"{path}.")
            continue
        kind = (
            " \\| ".join(_c(v) for v in prop["enum"])
            if "enum" in prop
            else _t(prop.get("type", "any"))
        )
        rows.append(f"| {_c(path)} | {kind} | {'yes' if name in required else 'no'} |")
    return rows


def _required(requires: Mapping[str, list[str]]) -> str:
    """The providers the policy needs before anything is automated (`automation_requires`)."""
    needs = [
        f"{_c(slot)} is " + " or ".join(_c(variant) for variant in allowed)
        for slot, allowed in requires.items()
    ]
    return f" Only while {' and '.join(needs)}; otherwise a person reviews." if needs else ""


def _last_validation(report: dict[str, Any]) -> str:
    dataset = report["dataset"]
    when = clock.local_date(report["run"]["created_at"])
    return (
        f"| Last validation | {when} on `{dataset['name']}` "
        f"(n={dataset['n']}, sha256 `{dataset['sha256'][:12]}…`) |"
    )


def _validation(report: dict[str, Any]) -> list[str]:
    primary = report["primary"]
    q = report["questions"][primary]
    d = report["decisions"]
    counts = d["outcome_counts"]
    total = sum(counts.values())
    automation = d["automation"]
    providers = ", ".join(f"{slot}=`{p['variant']}`" for slot, p in report["providers"].items())
    rows = [
        f"Benchmark `{report['dataset']['name']}` (n={report['dataset']['n']}) with {providers}. "
        f"Each proportion: {INTERVALS}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `{primary}` accuracy | {shown(counted(q['accuracy'], q['n']))} |",
        f"| Macro-F1 | {num(q['macro_f1'])} |",
        f"| Expected calibration error | {num(q['calibration']['ece'])} |",
        f"| Expected calibration error, equal-mass bins | {equal_mass(q)} |",
        f"| Brier score | {num(q['brier'])}{_brier_interval(q)} |",
        *[f"| {k} | {shown(stats.proportion(v, total))} |" for k, v in counts.items()],
        f"| AUTO decisions correct | {shown(counted(automation['accuracy'], automation['n']))} |",
        f"| False automation (of labelled cases) | {shown(false_automation(report))} |",
        f"| Expected human override (of reviewed) | "
        f"{shown(counted(d['review']['expected_override_rate'], d['review']['n']))} |",
        f"| Latency p50 / p95 | {num(report['run']['latency_ms']['p50'], 2)} / "
        f"{num(report['run']['latency_ms']['p95'], 2)} ms |",
    ]
    cost = report["cost"]["per_decision_usd"]
    rows.append(f"| Cost per decision | {'unknown' if cost is None else f'${cost:.5f}'} |")
    for item in d["critical"]:
        rows.append(
            f"| Critical `{item['question']}={item['label']}` | "
            f"{shown(stats.proportion(item['detected'], item['n']))} detected, "
            f"{item['auto_missed']} automated{evidence(item['n'])} |"
        )
    rows += _operating_point(report)
    confusion = q["confusion"]
    rows += [
        "",
        "Per-label performance:",
        "",
        "| Label | Precision | Recall | F1 |",
        "|---|---|---|---|",
    ]
    for label, m in q["per_label"].items():
        predicted = sum(row[label] for row in confusion.values())
        rows.append(
            f"| `{label}` | {shown(counted(m['precision'], predicted))} "
            f"| {shown(counted(m['recall'], m['support']))} | {num(m['f1'])} |"
        )
    rows += [
        "",
        "Subgroups:",
        "",
        "| Subgroup | n | Accuracy | AUTO | Abstention |",
        "|---|---|---|---|---|",
    ]
    for attribute, values in report["subgroups"].items():
        for value, m in values.items():
            labelled = subgroup_labelled(report, m)
            rows.append(
                f"| {attribute}={value} | {m['n']} | {shown(counted(m['accuracy'], labelled))} "
                f"| {shown(counted(m['auto_rate'], m['n']))} "
                f"| {shown(counted(m['abstention_rate'], m['n']))} |"
            )
    rows += [
        "",
        "Calibration (reliability):",
        "",
        "| Confidence bin | Mean confidence | Correct |",
        "|---|---|---|",
    ]
    for b in q["calibration"]["bins"]:
        rows.append(
            f"| {b['low']:.1f} to {b['high']:.1f} | {num(b['confidence'])} "
            f"| {shown(counted(b['accuracy'], b['n']))} |"
        )
    rows += [
        "",
        "Threshold sweep (if everything ≥ threshold were automated):",
        "",
        "| Threshold | Would be automated | Of those, correct |",
        "|---|---|---|",
    ]
    for row in sweep(report):
        scored, chosen = row["scored"], row["chosen"]
        automated = None if scored is None else stats.proportion(chosen, scored)
        correct = None if chosen is None else stats.proportion(chosen - row["errors"], chosen)
        rows.append(f"| {row['threshold']:.2f} | {shown(automated)} | {shown(correct)} |")
    return rows


def _operating_point(report: Mapping[str, Any]) -> list[str]:
    """What the benchmark shows at the automation threshold it ran with: how many cases it
    automated, and each risk as k of n with its exact one-sided bound
    (`evals/operating_point.py`). Every figure is worked out here from the counts the report's
    result states, never read from the run's own block, which only says that the run worked one
    out: a card rendered from a benchmark written before that is as it was. A threshold the
    cases would support is advice for the agent's author (`mendari eval`), not the card's."""
    try:
        if not isinstance(report["run"].get("operating_point"), Mapping):
            return []
        point = operating_point.of_report(report)
        if point is None:
            return []
        automated = point["automated"]
        rows = [
            "",
            "Operating point, at the automation threshold "
            f"**{operating_point.threshold_text(point['threshold'])}** this benchmark ran with. "
            'Each "at most" is an exact one-sided bound (Clopper-Pearson) at '
            f"{operating_point.CONFIDENCE:.0%} confidence, for cases like the benchmark's:",
            "",
            "| At this threshold | Cases | Bound |",
            "|---|---|---|",
            f"| Automated | {automated['k']} of {automated['n']} | |",
        ]
        return rows + [f"| {' | '.join(row)} |" for row in operating_point.table(point)]
    except _SHAPE_ERRORS:  # a report whose counts are not counts shows none
        return []


def _brier_interval(question: Mapping[str, Any]) -> str:
    interval = question.get("brier_interval")
    if not interval:
        return ""
    return f" [{num(interval['low'])}-{num(interval['high'])}, bootstrap]"


def _code_systems(m: AgentManifest) -> list[str]:
    """The code systems the agent declares (`code_systems`), each licensed one with its terms."""
    if not m.code_systems:
        return []
    rows = ["", "Code systems:", ""]
    for use in m.code_systems:
        terms = f" Licence: {_t(' '.join(use.licence_note.split()))}" if use.licence_note else ""
        rows.append(f"- {_t(use.name)} ({_c(use.url)}).{terms}")
    return rows
