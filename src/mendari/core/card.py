# SPDX-License-Identifier: Apache-2.0
"""Render a pack's Decision Card (Markdown) from its manifest, policy and published benchmark."""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Literal

from mendari import notices
from mendari.core import clock
from mendari.core.origin import COMMUNITY_AUTOMATION
from mendari.core.pack import Pack
from mendari.core.spec import Condition, DeriveRow, Outcome, PackManifest, Question
from mendari.evals import leaderboard, ranking, stats
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


def benchmark(pack: Pack) -> dict[str, Any] | None:
    """The pack's published benchmark (`evaluation.benchmark`), or None: none named, none
    there, or not a report this card can show (`read_benchmark` says why)."""
    return read_benchmark(pack)[0]


def read_benchmark(pack: Pack) -> tuple[dict[str, Any] | None, str | None]:
    """The benchmark, or None and why it is not shown. As the registry does, one that is not
    JSON, holds what a JSON response cannot (NaN, a lone surrogate) or is not the evaluation
    report the card shows is treated as absent, never an error."""
    relative = pack.manifest.evaluation.benchmark
    if relative is None:
        return None, None
    report, problem = pack.attestation_json(relative)
    if report is None:
        return None, problem
    if isinstance(report, dict):
        report = normalize(report)  # either format; v1's time and latency in its run block
    try:
        _last_validation(report)
        _validation(report)
    except _SHAPE_ERRORS as error:
        return None, f"not an evaluation report ({type(error).__name__})"
    return report, None


# What `render_card` reads an attestation from: the pack's own file, or what the caller gives
# (None: none shown). The registry gives none (ADR-0012).
FROM_PACK: Literal["pack"] = "pack"
Attested = Mapping[str, Any] | None | Literal["pack"]
RenderedBy = Literal["mendari card", "registry"]


def card_of(pack: Pack) -> str:
    """The card Mendari shows for a pack it serves or names: a pack from a registry as the
    registry renders it (its uploaded benchmark and leaderboard are its publisher's, and its
    text is plain), any other pack with its own attestations."""
    if pack.origin == "hub":
        return render_card(pack, report=None, board=None, rendered_by="registry")
    return render_card(pack)


def render_card(
    pack: Pack,
    *,
    report: Attested = FROM_PACK,
    board: Attested = FROM_PACK,
    rendered_by: RenderedBy = "mendari card",
) -> str:
    """The Decision Card. Its attestations (the files the digest leaves out: the benchmark
    `report` and the leaderboard `board`) are explicit inputs: by default the pack's own
    (`mendari card`, the engine's own packs); None shows none. Everything else comes from the
    files the digest covers: the manifest, policy, input schema and changelog.
    `rendered_by="registry"` is the registry's card (ADR-0012): the risk level declared by the
    publisher, and a validation section that says no uploaded figure is shown."""
    token = _PLAIN.set(rendered_by == "registry")
    try:
        return _render(pack, report, board, rendered_by)
    finally:
        _PLAIN.reset(token)


def _render(pack: Pack, report: Attested, board: Attested, rendered_by: RenderedBy) -> str:
    m = pack.manifest
    level = int(m.risk.level)
    report, report_problem = _attested_benchmark(pack, report)
    board, board_problem = _attested_leaderboard(pack, board)
    registry = rendered_by == "registry"
    category = f" ({_t(m.risk.category)})" if m.risk.category != RISK_NAMES[level] else ""
    # Where the pack comes from, by origin and never by its manifest: a card written for a pack
    # of one's own (local) or pulled from a registry claims nothing.
    source = (
        [f"| Source | {pack.origin_label} |"]
        if pack.shown_origin in ("maintained", "community")
        else []
    )
    automation = (
        COMMUNITY_AUTOMATION
        if pack.community
        else RISK_AUTOMATION[level] + _required(m.policy.automation_requires)
    )
    out: list[str] = [
        f"# Decision Card: {_t(m.name)}",
        "",
        f"**{pack.status_label}**",
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
        f"| Pack digest | `{pack.digest}` |",
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
        *_schema_rows(pack.input_schema),
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
        "## Decision policy",
        "",
    ]
    if pack.community:
        # Never AUTO, whatever the thresholds say (`core/policy.py`): the card says what runs.
        out.append(
            f"- Never automated: a person reviews from {_c(p.primary)} confidence "
            f"≥ **{p.thresholds.review:.2f}**; abstain below."
        )
    else:
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
        outcome = rule.outcome
        if pack.community and outcome in (Outcome.AUTO, Outcome.BLOCK):
            outcome = Outcome.HUMAN_REVIEW  # what the policy makes of it for a community pack
        out.append(
            f"- Rule **{_t(rule.name)}**: {_condition(rule.when)} ⇒ "
            f"`{outcome.value}`{target}{priority}. {_t(rule.reason)}"
        )
    out += [f"- Fallback (ABSTAIN / BLOCK): {_t(p.fallback)}", "", "## Providers", ""]
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
    out += ["", "## Validation", ""]
    if report:
        out += _validation(report)
    elif registry:
        out.append(
            "The registry shows no figure it did not compute itself: the publisher's benchmark "
            "and leaderboard are left out. Validate the pack on your own data."
        )
    else:
        out.append("No published benchmark.")
    if report and report["pack"].get("digest") != pack.digest:
        measured = ranking.measured_on(
            report["pack"].get("version"), report["pack"].get("digest"), pack.version
        )
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
            f"{leaderboard.PUBLIC_SET[:1].upper()}{leaderboard.PUBLIC_SET[1:]}. Every set below "
            "is public: it is in the repository and in the package, so a configuration can be "
            "tuned to it. The rows are listed with the critical cases caught first, never "
            "accuracy alone, and nothing here is graded: grades and rankings are given only on "
            "frozen hidden sets. Mendari's "
            f"[scoreboard]({leaderboard.SCOREBOARD}) shows its reference packs' configurations "
            "on their public sets, each figure with its interval and scope.",
            "",
            "Each table below is one dataset at one size, and the configurations in a table that "
            "records the dataset's hash ran on exactly the same records. Remote models are named "
            "because they are a deployment choice. Synthetic and hand-written data: compare "
            "configurations with each other, not with performance at a real site. A row measured "
            "on an earlier version of the pack says so beside its configuration: its AUTO and "
            "false automation followed that version's policy.",
            "",
            *leaderboard.markdown(board, pack.digest, pack.version),
        ]
    out += ["", "## Known failure modes", ""]
    out += [f"- {_t(item)}" for item in m.card.known_failure_modes]
    out += ["", "## Evidence and provenance", ""]
    for cited in m.card.evidence:
        out.append(
            f"- **{_cited(cited.title, cited.url)}**"
            + (f": {_t(cited.note)}" if cited.note else "")
        )
    if pack.path("CHANGELOG.md").is_file():
        history = pack.read_text("CHANGELOG.md").split("\n", 1)[-1].strip()
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
            "*Rendered by the registry from the pack's own files (the files its digest covers). "
            "What it says about the pack is the publisher's. It is not evidence of performance "
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
# renders a pack of one's own as its author wrote it.
_PLAIN: ContextVar[bool] = ContextVar("card_plain_text", default=False)
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")  # every ASCII punctuation character
_LINK = re.compile(r"^https?://[^\s()<>\[\]`\\|*_]+$")


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


def _attested_benchmark(pack: Pack, given: Attested) -> tuple[dict[str, Any] | None, str | None]:
    """The benchmark to show and why one is not: the pack's own (`read_benchmark`), none, or
    the caller's, checked as the pack's is."""
    if given == FROM_PACK:
        return read_benchmark(pack)
    if given is None:
        return None, None
    report = normalize(dict(given))
    try:
        _last_validation(report)
        _validation(report)
    except _SHAPE_ERRORS as error:
        return None, f"not an evaluation report ({type(error).__name__})"
    return report, None


def _attested_leaderboard(pack: Pack, given: Attested) -> tuple[dict[str, Any] | None, str | None]:
    if given == FROM_PACK:
        return leaderboard.read(pack)
    return (None, None) if given is None else (dict(given), None)


def _question(key: str, question: Question, *, primary: bool) -> list[str]:
    marker = " (primary)" if primary else ""
    kind = f" *({_t(question.type)})*" if question.type != "choice" else ""
    out = [f"**{_c(key)}**{marker}{kind}: {_t(question.instructions)}", ""]
    if question.derive is not None:
        members = ", ".join(_c(member) for member in question.derive.members)
        out += [
            f"Derived from {members} by a decision table (the first matching row wins; the "
            "members' answers are treated as independent):",
            "",
            *[f"{n}. {_row(row)}" for n, row in enumerate(question.derive.table, start=1)],
            "",
        ]
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


def _row(row: DeriveRow) -> str:
    if row.otherwise is not None:
        return f"otherwise → {_c(row.result)}"
    among = f"of {', '.join(_c(m) for m in row.scope)}" if row.scope else "member"
    value = row.any if row.any is not None else row.all
    labels = [value] if isinstance(value, str) else list(value or [])
    matched = " or ".join(_c(label) for label in labels)
    gate = " and ".join(
        f"{_c(member)} is " + " or ".join(_c(label) for label in allowed)
        for member, allowed in (row.when or {}).items()
    )
    kind = "any" if row.any is not None else "every"
    return f"{kind} {among} is {matched}{f', when {gate}' if gate else ''} → {_c(row.result)}"


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


def _brier_interval(question: Mapping[str, Any]) -> str:
    interval = question.get("brier_interval")
    if not interval:
        return ""
    return f" [{num(interval['low'])}-{num(interval['high'])}, bootstrap]"


def _code_systems(m: PackManifest) -> list[str]:
    """The code systems the pack declares (`code_systems`), each licensed one with its terms."""
    if not m.code_systems:
        return []
    rows = ["", "Code systems:", ""]
    for use in m.code_systems:
        terms = f" Licence: {_t(' '.join(use.licence_note.split()))}" if use.licence_note else ""
        rows.append(f"- {_t(use.name)} ({_c(use.url)}).{terms}")
    return rows
