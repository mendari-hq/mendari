"""Render a pack's Decision Card (Markdown) from its manifest, policy and published benchmark."""

from __future__ import annotations

import json
from typing import Any

from openhealth.core.pack import Pack
from openhealth.core.spec import Condition, DeriveRow, Question
from openhealth.evals import leaderboard
from openhealth.evals.render import num, pct

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


def benchmark(pack: Pack) -> dict[str, Any] | None:
    relative = pack.manifest.evaluation.benchmark
    if relative is None or not pack.path(relative).is_file():
        return None
    with pack.path(relative).open(encoding="utf-8") as handle:
        report: dict[str, Any] = json.load(handle)
    return report


def render_card(pack: Pack) -> str:
    m = pack.manifest
    level = int(m.risk.level)
    report = benchmark(pack)
    category = f" ({m.risk.category})" if m.risk.category != RISK_NAMES[level] else ""
    out: list[str] = [
        f"# Decision Card: {m.name}",
        "",
        f"`{m.id}` · version **{m.version}** · {m.license} · "
        f"published by {m.publisher.name} ({m.publisher.type})",
        "",
        f"> {m.summary}",
        "",
        "| | |",
        "|---|---|",
        f"| Risk level | **{level}**, {RISK_NAMES[level]}{category} |",
        f"| Automation | {RISK_AUTOMATION[level]} |",
        f"| Pack digest | `{pack.digest}` |",
    ]
    if report:
        out.append(
            f"| Last validation | {report['created_at'][:10]} on `{report['dataset']['name']}` "
            f"(n={report['dataset']['n']}, sha256 `{report['dataset']['sha256'][:12]}…`) |"
        )
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
        f"- AUTO when `{p.primary}` confidence ≥ **{p.thresholds.auto:.2f}**; human review "
        f"≥ **{p.thresholds.review:.2f}**; abstain below.",
    ]
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
        out.append(
            f"- Rule **{rule.name}**: {_condition(rule.when)} ⇒ "
            f"`{rule.outcome.value}`{target}{priority}. {rule.reason}"
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
    board = leaderboard.load(pack)
    if board and board.get("rows"):
        out += [
            "",
            "## Provider comparison",
            "",
            "Every configuration below ran on the same records. Remote models are named because "
            "they are a deployment choice. Synthetic and hand-written data: compare "
            "configurations with each other, not with performance at a real site.",
            "",
            *leaderboard.markdown(board),
        ]
    out += ["", "## Known failure modes", "", *[f"- {item}" for item in m.card.known_failure_modes]]
    out += ["", "## Evidence and provenance", ""]
    for evidence in m.card.evidence:
        title = f"[{evidence.title}]({evidence.url})" if evidence.url else evidence.title
        out.append(f"- **{title}**" + (f": {evidence.note}" if evidence.note else ""))
    changelog = pack.root / "CHANGELOG.md"
    if changelog.is_file():
        history = changelog.read_text(encoding="utf-8").split("\n", 1)[-1].strip()
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
        "*Generated by `openhealth card`. A Decision Card describes validation on the data named "
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


def _validation(report: dict[str, Any]) -> list[str]:
    primary = report["primary"]
    q = report["questions"][primary]
    d = report["decisions"]
    providers = ", ".join(f"{slot}=`{p['variant']}`" for slot, p in report["providers"].items())
    rows = [
        f"Benchmark `{report['dataset']['name']}` (n={report['dataset']['n']}) with {providers}:",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| `{primary}` accuracy | {pct(q['accuracy'])} |",
        f"| Macro-F1 | {num(q['macro_f1'])} |",
        f"| Expected calibration error | {num(q['calibration']['ece'])} |",
        f"| Brier score | {num(q['brier'])} |",
        *[f"| {k} rate | {pct(v)} |" for k, v in d["outcome_rates"].items()],
        f"| Accuracy of AUTO decisions | {pct(d['automation']['accuracy'])} |",
        f"| False automation rate | {pct(d['automation']['false_automation_rate'])} |",
        f"| Expected human override rate | {pct(d['review']['expected_override_rate'])} |",
        f"| Latency p50 / p95 | {num(report['latency_ms']['p50'], 2)} / "
        f"{num(report['latency_ms']['p95'], 2)} ms |",
    ]
    cost = report["cost"]["per_decision_usd"]
    rows.append(f"| Cost per decision | {'unknown' if cost is None else f'${cost:.5f}'} |")
    for item in d["critical"]:
        rows.append(
            f"| Critical `{item['question']}={item['label']}` | {item['detected']}/{item['n']} "
            f"detected, {item['auto_missed']} automated |"
        )
    rows += [
        "",
        "Per-label performance:",
        "",
        "| Label | Precision | Recall | F1 | n |",
        "|---|---|---|---|---|",
    ]
    for label, m in q["per_label"].items():
        rows.append(
            f"| `{label}` | {pct(m['precision'])} | {pct(m['recall'])} | {num(m['f1'])} "
            f"| {m['support']} |"
        )
    rows += [
        "",
        "Subgroups:",
        "",
        "| Subgroup | n | Accuracy | AUTO rate | Abstention |",
        "|---|---|---|---|---|",
    ]
    for attribute, values in report["subgroups"].items():
        for value, m in values.items():
            rows.append(
                f"| {attribute}={value} | {m['n']} | {pct(m['accuracy'])} "
                f"| {pct(m['auto_rate'])} | {pct(m['abstention_rate'])} |"
            )
    rows += [
        "",
        "Calibration (reliability):",
        "",
        "| Confidence bin | n | Mean confidence | Accuracy |",
        "|---|---|---|---|",
    ]
    for b in q["calibration"]["bins"]:
        rows.append(
            f"| {b['low']:.1f} to {b['high']:.1f} | {b['n']} | {pct(b['confidence'])} "
            f"| {pct(b['accuracy'])} |"
        )
    rows += [
        "",
        "Threshold sweep (if everything ≥ threshold were automated):",
        "",
        "| Threshold | Coverage | Accuracy | Errors |",
        "|---|---|---|---|",
    ]
    for row in report["threshold_sweep"]:
        rows.append(
            f"| {row['threshold']:.2f} | {pct(row['coverage'])} | {pct(row['accuracy'])} "
            f"| {row['errors']} |"
        )
    return rows
