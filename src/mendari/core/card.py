# SPDX-License-Identifier: Apache-2.0
"""Render a pack's Decision Card (Markdown) from its manifest, policy and published benchmark."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mendari import notices
from mendari.core import clock
from mendari.core.origin import COMMUNITY_AUTOMATION
from mendari.core.pack import Pack
from mendari.core.spec import Condition, DeriveRow, Outcome, PackManifest, Question
from mendari.evals import leaderboard, ranking, stats
from mendari.evals.render import INTERVALS, equal_mass, evidence, num, shown
from mendari.evals.result import (
    MIN_POSITIVES,
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
_SHAPE_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError)


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


def render_card(pack: Pack) -> str:
    m = pack.manifest
    level = int(m.risk.level)
    report, report_problem = read_benchmark(pack)
    board, board_problem = leaderboard.read(pack)
    category = f" ({m.risk.category})" if m.risk.category != RISK_NAMES[level] else ""
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
        f"# Decision Card: {m.name}",
        "",
        f"**{pack.status_label}**",
        "",
        f"*{notices.NOTICE}*",
        "",
        f"`{m.id}` · version **{m.version}** · {m.license} · "
        f"published by {m.publisher.name} ({m.publisher.type}, self-declared)",
        "",
        f"> {m.summary}",
        "",
        "| | |",
        "|---|---|",
        *source,
        f"| Risk level | **{level}**, {RISK_NAMES[level]}{category} |",
        f"| Automation | {automation} |",
        f"| Pack digest | `{pack.digest}` |",
    ]
    if report:
        out.append(_last_validation(report))
    out += [
        "",
        "## Intended use",
        "",
        m.card.intended_use,
        "",
        f"**Why risk level {level}:** {m.risk.rationale}",
        "",
        "## Intended users",
        "",
        *[f"- {user}" for user in m.card.intended_users],
        "",
        "## Patient population",
        "",
        m.card.patient_population,
        "",
        "## Out of scope",
        "",
        *[f"- {item}" for item in m.card.out_of_scope],
        "",
        "## Inputs",
        "",
        "| Field | Type | Required |",
        "|---|---|---|",
        *_schema_rows(pack.input_schema),
        "",
        f"FHIR R4 mapping: `{m.inputs.fhir}`." if m.inputs.fhir else "No FHIR mapping.",
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
            f"- Never automated: a person reviews from `{p.primary}` confidence "
            f"≥ **{p.thresholds.review:.2f}**; abstain below."
        )
    else:
        out.append(
            f"- AUTO when `{p.primary}` confidence ≥ **{p.thresholds.auto:.2f}**; human review "
            f"≥ **{p.thresholds.review:.2f}**; abstain below."
        )
        for label, label_policy in p.labels.items():
            if not label_policy.allow_auto:
                out.append(f"- `{label}` is never automated.")
            elif label_policy.auto_threshold is not None:
                out.append(f"- `{label}` needs ≥ **{label_policy.auto_threshold:.2f}** for AUTO.")
        for slot, variants in p.automation_requires.items():
            running = " or ".join(f"`{variant}`" for variant in variants)
            out.append(f"- AUTO only while `{slot}` is {running}; otherwise a person reviews.")
    for rule in p.rules:
        target = f" → `{rule.recommendation}`" if rule.recommendation else ""
        priority = f", priority `{rule.priority.value}`" if rule.priority else ""
        outcome = rule.outcome
        if pack.community and outcome in (Outcome.AUTO, Outcome.BLOCK):
            outcome = Outcome.HUMAN_REVIEW  # what the policy makes of it for a community pack
        out.append(
            f"- Rule **{rule.name}**: {_condition(rule.when)} ⇒ "
            f"`{outcome.value}`{target}{priority}. {rule.reason}"
        )
    out += [f"- Fallback (ABSTAIN / BLOCK): {p.fallback}", "", "## Providers", ""]
    out += ["| Slot | Answers | Default | Alternatives |", "|---|---|---|---|"]
    for slot, spec in m.providers.items():
        alternatives = ", ".join(
            f"`{name}` ({v.type})" for name, v in spec.variants.items() if name != spec.use
        )
        default = spec.variants[spec.use]
        out.append(
            f"| `{slot}` | {', '.join(spec.answers)} | `{spec.use}` ({default.type}) | "
            f"{alternatives or 'none'} |"
        )
    out += ["", "## Validation", ""]
    out += _validation(report) if report else ["No published benchmark."]
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
            "Each table below is one dataset at one size, and the configurations in a table that "
            "records the dataset's hash ran on exactly the same records. Remote models are named "
            "because they are a deployment choice. Synthetic and hand-written data: compare "
            "configurations with each other, not with performance at a real site. Ranked "
            f"({ranking.METHOD}) by the critical cases caught first, never accuracy alone: only "
            "configurations measured on this version of the pack rank, and only on records with "
            f"at least {MIN_POSITIVES} positives of a critical label.",
            "",
            *leaderboard.markdown(board, pack.digest, pack.version),
        ]
    out += ["", "## Known failure modes", "", *[f"- {item}" for item in m.card.known_failure_modes]]
    out += ["", "## Evidence and provenance", ""]
    for cited in m.card.evidence:
        title = f"[{cited.title}]({cited.url})" if cited.url else cited.title
        out.append(f"- **{title}**" + (f": {cited.note}" if cited.note else ""))
    if pack.path("CHANGELOG.md").is_file():
        history = pack.read_text("CHANGELOG.md").split("\n", 1)[-1].strip()
        out += [
            "",
            "## Version history",
            "",
            history.replace("\n## ", "\n### ").replace("## ", "### ", 1),
        ]
    out += [
        "",
        "---",
        "",
        "*Generated by `mendari card`. A Decision Card describes validation on the data named "
        "above. It is not evidence of performance at your site; run local validation first.*",
        "",
    ]
    return "\n".join(out)


def _question(key: str, question: Question, *, primary: bool) -> list[str]:
    marker = " (primary)" if primary else ""
    kind = f" *({question.type})*" if question.type != "choice" else ""
    out = [f"**`{key}`**{marker}{kind}: {question.instructions}", ""]
    if question.derive is not None:
        members = ", ".join(f"`{member}`" for member in question.derive.members)
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
            f"| `{label}` | {levels[label].score:g} | {question.criteria[label]} |"
            for label in question.labels
        ]
    else:
        out += ["| Label | Criteria |", "|---|---|"]
        out += [f"| `{label}` | {text} |" for label, text in question.criteria.items()]
    return [*out, ""]


def _row(row: DeriveRow) -> str:
    if row.otherwise is not None:
        return f"otherwise → `{row.result}`"
    among = f"of {', '.join(f'`{m}`' for m in row.scope)}" if row.scope else "member"
    value = row.any if row.any is not None else row.all
    labels = [value] if isinstance(value, str) else list(value or [])
    matched = " or ".join(f"`{label}`" for label in labels)
    gate = " and ".join(
        f"`{member}` is " + " or ".join(f"`{label}`" for label in allowed)
        for member, allowed in (row.when or {}).items()
    )
    kind = "any" if row.any is not None else "every"
    return f"{kind} {among} is {matched}{f', when {gate}' if gate else ''} → `{row.result}`"


def _condition(when: Condition) -> str:
    parts = []
    if when.min_probability is not None:
        labels = " or ".join(f"`{x}`" for x in when.labels)
        parts.append(f"P(`{when.question}` is {labels}) ≥ {when.min_probability:.2f}")
    elif when.labels:
        parts.append(f"`{when.question}` is " + " or ".join(f"`{x}`" for x in when.labels))
    if when.min_score is not None:
        parts.append(f"`{when.question}` expected score ≥ {when.min_score:g}")
    if when.max_score is not None:
        parts.append(f"`{when.question}` expected score ≤ {when.max_score:g}")
    confidence = f" (p ≥ {when.min_confidence:.2f})" if when.min_confidence else ""
    return " and ".join(parts) + confidence


def _schema_rows(schema: dict[str, Any], prefix: str = "") -> list[str]:
    rows = []
    required = set(schema.get("required", []))
    for name, prop in schema.get("properties", {}).items():
        path = f"{prefix}{name}"
        if prop.get("type") == "object":
            rows.append(f"| `{path}` | object | {'yes' if name in required else 'no'} |")
            rows += _schema_rows(prop, f"{path}.")
            continue
        kind = (
            " \\| ".join(f"`{v}`" for v in prop["enum"])
            if "enum" in prop
            else prop.get("type", "any")
        )
        rows.append(f"| `{path}` | {kind} | {'yes' if name in required else 'no'} |")
    return rows


def _required(requires: Mapping[str, list[str]]) -> str:
    """The providers the policy needs before anything is automated (`automation_requires`)."""
    needs = [
        f"`{slot}` is " + " or ".join(f"`{variant}`" for variant in allowed)
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
        terms = f" Licence: {' '.join(use.licence_note.split())}" if use.licence_note else ""
        rows.append(f"- {use.name} (`{use.url}`).{terms}")
    return rows
