# SPDX-License-Identifier: Apache-2.0
"""The validation and readiness brief (mendari.validation-brief/2).

Counts from an organization's own audit trail, live or in a shadow pilot, as a summary a
committee can read: five readiness gates, each share of cases as k of n with its exact
Clopper-Pearson interval, clinician hours that could be reclaimed, and the patterns in reviewers'
overrides. Never reads or emits PHI or free-text case inputs.

**What it is.** The deployment computes it when asked, from the rows the caller's organization
may read. Nothing signs it: it is not an attestation, and a figure in it is checked against the
audit trail (its chain and signed checkpoint), not against the file.

**A gate says what was measured.** `pass`, `warn` and `fail` are findings; a gate with nothing
to measure is `no_data` ("not enough data"), never `pass`: no decision was automated (every
shadow pilot), nobody looked at an automated decision, nothing was labelled, or the monitor had
too few decisions to compare. `ready_for_live` is true only when every gate is `pass`.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mendari import notices
from mendari.core.audit import AuditStore
from mendari.core.pack import Pack
from mendari.evals import figures
from mendari.evals.monitoring import MIN_FOR_DRIFT, MIN_FOR_OPERATIONS

FORMAT = "mendari.validation-brief/2"
MIN_PILOT_CASES = 30
DEFAULT_MINUTES_PER_CASE = 3.5
# A gate's states, and what a person reads for each. `no_data`: nothing was measured.
PASS, WARN, FAIL, NO_DATA = "pass", "warn", "fail", "no_data"
GATE_WORDS = {PASS: "Pass", WARN: "Review", FAIL: "Action needed", NO_DATA: "Not enough data"}
ABOUT = (
    "Computed by this deployment from its organization's own audit trail when it is asked for: "
    "counts and label codes, never case text. It is not signed and is not an attestation; check "
    "a figure against the audit trail, whose chain and daily checkpoint are."
)
# Why the monitor compared no confidences with a benchmark (`monitoring`'s `benchmark.reason`).
NOT_COMPARED = {
    "from_registry": "an agent from a registry has no benchmark of its own here",
    "no_benchmark": "the agent carries no benchmark",
    "another_build": "its benchmark measured another build of the agent",
    "providers_unknown": "the providers running here are not known",
    "other_providers": "its benchmark ran on other providers or models than run here",
    "other_policy": "its benchmark ran under other governance than is in force here",
}


def _shadow_hold(holds: Sequence[Mapping[str, Any]], pack: Pack) -> Mapping[str, Any] | None:
    """Return the active shadow-pilot hold covering `pack`, if any."""
    for hold in holds:
        pack_id = hold.get("pack_id")
        if pack_id is not None and pack_id != pack.id:
            continue
        reason = str(hold.get("reason") or "").lower()
        if reason.startswith("shadow pilot:") or reason.startswith("[shadow]"):
            return hold
    return None


def _non_shadow_holds(holds: Sequence[Mapping[str, Any]], pack: Pack) -> list[Mapping[str, Any]]:
    """Active operational holds covering `pack` other than a shadow-mode pilot hold."""
    found: list[Mapping[str, Any]] = []
    for hold in holds:
        pack_id = hold.get("pack_id")
        if pack_id is not None and pack_id != pack.id:
            continue
        reason = str(hold.get("reason") or "").lower()
        if reason.startswith("shadow pilot:") or reason.startswith("[shadow]"):
            continue
        found.append(hold)
    return found


def _gate(key: str, label: str, status: str, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _volume_gate(total: int, days: int) -> dict[str, Any]:
    label = "Local pilot volume"
    if total >= MIN_PILOT_CASES:
        detail = (
            f"{total} local decisions evaluated in the last {days} days "
            f"(target >= {MIN_PILOT_CASES})."
        )
        return _gate("volume", label, PASS, detail)
    if total > 0:
        detail = (
            f"{total} of {MIN_PILOT_CASES} recommended local decisions recorded "
            f"in the last {days} days."
        )
        return _gate("volume", label, WARN, detail)
    return _gate("volume", label, NO_DATA, f"No local decisions recorded in the last {days} days.")


def _safety_gate(
    summary: Mapping[str, Any], high_alerts: Sequence[Mapping[str, Any]], days: int
) -> dict[str, Any]:
    """Automated decisions that someone rated wrong, and high-severity alerts. With nothing
    automated (every shadow pilot), or no automated decision that anyone rated or recorded an
    outcome for, there is nothing to pass on."""
    label = "Safety & zero automated errors"
    automated = int(summary["outcomes"].get("AUTO", 0))
    wrong = int(summary["down_on_auto"])
    rated, outcomes = int(summary["auto_rated"]), int(summary["auto_labelled"])
    held = int(summary["would_automate"]) - automated
    if wrong or high_alerts:
        problems = []
        if wrong:
            problems.append(
                f"{_plural(wrong, 'rating')} of wrong on {_plural(automated, 'automated decision')}"
            )
        if high_alerts:
            problems.append(f"{_plural(len(high_alerts), 'high-severity alert')} active")
        return _gate("safety", label, FAIL, "; ".join(problems) + ".")
    if not automated:
        waiting = (
            f" Automation was held for {_plural(held, 'decision')} that would have been automated."
            if held > 0
            else ""
        )
        detail = (
            f"0 of 0 decisions were automated in the last {days} days, so nothing shows how the "
            f"agent does when it acts alone.{waiting}"
        )
        return _gate("safety", label, NO_DATA, detail)
    if not rated and not outcomes:
        detail = (
            f"{_plural(automated, 'decision')} automated in the last {days} days, and nobody has "
            "rated one or recorded its outcome: nothing shows whether they were right."
        )
        return _gate("safety", label, NO_DATA, detail)
    detail = (
        f"0 rated wrong of {_plural(automated, 'automated decision')} "
        f"({_plural(rated, 'rating')}, {_plural(outcomes, 'recorded outcome')}), and no "
        "high-severity monitoring alerts."
    )
    return _gate("safety", label, PASS, detail)


def _agreement_gate(agreement: Mapping[str, Any] | None, days: int) -> dict[str, Any]:
    label = "Clinician agreement"
    if agreement is None or not agreement["n"]:
        detail = f"No decision of the last {days} days has a reviewer's label or recorded outcome."
        return _gate("agreement", label, NO_DATA, detail)
    rate = agreement["k"] / agreement["n"]
    status = PASS if rate >= 0.90 else (WARN if rate >= 0.80 else FAIL)
    detail = f"{_fmt_figure(agreement)} recommendations matched clinician or downstream outcome."
    return _gate("agreement", label, status, detail)


def _governance_gate(
    pack: Pack,
    hard_holds: Sequence[Mapping[str, Any]],
    governance_active: Mapping[str, Any] | None,
    registry_approval: Mapping[str, Any] | None,
) -> dict[str, Any]:
    label = "Governance & risk gates"
    risk_level = pack.manifest.risk.level
    needs_risk_approval = risk_level in (2, 3) and governance_active is None
    needs_reg_approval = pack.origin == "hub" and not (
        registry_approval and registry_approval.get("active")
    )
    if hard_holds:
        return _gate("governance", label, WARN, f"{len(hard_holds)} automation hold(s) active.")
    if pack.community or risk_level >= 4:
        detail = "Human-in-the-loop permanently enforced by agent origin or risk level."
        return _gate("governance", label, PASS, detail)
    if needs_risk_approval or needs_reg_approval:
        pending_items = []
        if needs_risk_approval:
            pending_items.append(f"risk level {risk_level} committee approval required")
        if needs_reg_approval:
            pending_items.append("registry digest approval required")
        return _gate("governance", label, WARN, "; ".join(pending_items).capitalize() + ".")
    detail = "Required governance approvals are in force with no operational holds."
    return _gate("governance", label, PASS, detail)


def _drift_gate(monitoring: Mapping[str, Any] | None) -> dict[str, Any]:
    """The monitor's alerts over the last 7 days. Without alerts it passes only when the monitor
    had the decisions its operational checks compare (`MIN_FOR_OPERATIONS`); it then says
    whether confidences were compared with a benchmark too, and why not."""
    label = "Calibration & operational drift"
    if monitoring is None:
        return _gate("drift", label, NO_DATA, "The monitor was not read for this brief.")
    alerts = list(monitoring.get("alerts") or ())
    if alerts:
        status = FAIL if any(a.get("severity") == "high" for a in alerts) else WARN
        detail = f"{len(alerts)} monitoring alert(s) active in the last 7 days."
        return _gate("drift", label, status, detail)
    recent = monitoring["last_7_days"]
    made = int(recent["decisions"]) - int(recent["refused"])
    if made < MIN_FOR_OPERATIONS:
        detail = (
            f"{_plural(made, 'decision')} in the last 7 days: the monitor compares errors, "
            f"abstentions and latency from {MIN_FOR_OPERATIONS}, and confidences from "
            f"{MIN_FOR_DRIFT} with a comparable benchmark. Nothing was compared."
        )
        return _gate("drift", label, NO_DATA, detail)
    benchmark = monitoring["benchmark"]
    if not benchmark["comparable"]:
        why = NOT_COMPARED.get(str(benchmark["reason"]), "no benchmark is comparable here")
        compared = f"Confidences were not compared with a benchmark: {why}."
    elif int(recent["confidences"]) < MIN_FOR_DRIFT:
        compared = (
            f"Confidences were not compared with the benchmark: {recent['confidences']} of the "
            f"{MIN_FOR_DRIFT} it takes."
        )
    else:
        compared = "Confidences were compared with the benchmark's."
    detail = f"No operational alert over {made} decisions in the last 7 days. {compared}"
    return _gate("drift", label, PASS, detail)


def build(
    store: AuditStore,
    pack: Pack,
    *,
    days: int = 30,
    minutes_per_case: float = DEFAULT_MINUTES_PER_CASE,
    holds: Sequence[Mapping[str, Any]] = (),
    governance_active: Mapping[str, Any] | None = None,
    registry_approval: Mapping[str, Any] | None = None,
    monitoring: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured `mendari.validation-brief/2` report for `pack`. `monitoring` is the
    monitor's report for the same organization (`evals.monitoring.monitor`); without it the
    drift gate has no data."""
    if days < 1:
        raise ValueError("days must be >= 1")
    if not math.isfinite(minutes_per_case) or minutes_per_case < 0:
        raise ValueError("minutes_per_case must be a finite non-negative number")
    summary = store.validation_summary(pack.id, days=days)
    total = int(summary["total"])
    automated = int(summary["outcomes"].get("AUTO", 0))
    would_auto = int(summary["would_automate"])
    reviewed = int(summary["reviewed"])
    overrides = int(summary["overrides"])
    labelled = int(summary["labelled"])
    agreed = int(summary["agreed"])
    down_on_auto = int(summary["down_on_auto"])

    shadow_pilot = _shadow_hold(holds, pack)
    hard_holds = _non_shadow_holds(holds, pack)

    agreement_fig = figures.of(agreed, labelled) if labelled else None
    override_fig = figures.of(overrides, reviewed) if reviewed else None
    readiness_fig = figures.of(would_auto, total) if total else None

    weeks = max(days / 7.0, 1.0 / 7.0)
    weekly_cases = round(total / weeks, 1)
    weekly_auto = round(would_auto / weeks, 1)
    weekly_hours = round((weekly_auto * minutes_per_case) / 60.0, 1)

    alerts = list((monitoring or {}).get("alerts") or ())
    high_alerts = [a for a in alerts if a.get("severity") == "high"]
    risk_level = pack.manifest.risk.level
    gates = [
        _volume_gate(total, days),
        _safety_gate(summary, high_alerts, days),
        _agreement_gate(agreement_fig, days),
        _governance_gate(pack, hard_holds, governance_active, registry_approval),
        _drift_gate(monitoring),
    ]
    ready = all(g["status"] == PASS for g in gates)
    return {
        "format": FORMAT,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "notice": notices.NOTICE,
        # What this document is: computed on request, and signed by nothing.
        "about": ABOUT,
        "signed": False,
        "pack": {
            "id": pack.id,
            "name": pack.manifest.name,
            "version": pack.version,
            "digest": pack.digest,
            "origin": pack.origin,
            "risk_level": risk_level,
            "risk_category": pack.manifest.risk.category,
            "auto_threshold": pack.manifest.policy.thresholds.auto,
            "review_threshold": pack.manifest.policy.thresholds.review,
        },
        "window_days": days,
        "shadow_pilot": (
            {
                "active": True,
                "hold_id": shadow_pilot["id"],
                "set_at": shadow_pilot["set_at"],
                "set_by": shadow_pilot.get("set_by"),
                "reason": shadow_pilot.get("reason"),
            }
            if shadow_pilot is not None
            else {"active": False, "hold_id": None}
        ),
        "ready_for_live": ready,
        "volume": {
            "total": total,
            "automated": automated,
            "human_review": int(summary["outcomes"].get("HUMAN_REVIEW", 0)),
            "abstain": int(summary["outcomes"].get("ABSTAIN", 0)),
            "block": int(summary["outcomes"].get("BLOCK", 0)),
            "shadow_decisions": int(summary["shadow_decisions"]),
            "would_automate": would_auto,
            "stat_cases": int(summary["stat_cases"]),
            "pending_reviews": int(summary["pending_reviews"]),
            "reviewed": reviewed,
            "overrides": overrides,
            "labelled": labelled,
            "agreed": agreed,
            "down_on_auto": down_on_auto,
            # Automated decisions anyone looked at: their current ratings, and those with an
            # outcome recorded. With neither, the safety gate has nothing to pass on.
            "auto_rated": int(summary["auto_rated"]),
            "auto_labelled": int(summary["auto_labelled"]),
        },
        "figures": {
            "agreement": agreement_fig,
            "override_rate": override_fig,
            "automation_readiness": readiness_fig,
        },
        "roi": {
            "minutes_per_case": minutes_per_case,
            "weekly_cases_projected": weekly_cases,
            "weekly_auto_cases_projected": weekly_auto,
            "weekly_hours_reclaimable": weekly_hours,
            "p95_latency_ms": summary["p95_latency_ms"],
            "cost_per_decision_usd": summary["cost_per_decision_usd"],
        },
        "gates": gates,
        "override_clusters": summary["override_clusters"],
    }


def readiness_line(gates: Sequence[Mapping[str, Any]]) -> str:
    """The gates in one line, by state: "2 of 5 gates pass; 3 without enough data". Never "all
    gates passed" of a gate that measured nothing."""
    counts = Counter(str(gate["status"]) for gate in gates)
    parts = [f"{counts[PASS]} of {len(gates)} gates pass"]
    for status, words in ((NO_DATA, "without enough data"), (WARN, "to review"), (FAIL, "failing")):
        if counts[status]:
            parts.append(f"{counts[status]} {words}")
    return "; ".join(parts)


def _fmt_figure(fig: Mapping[str, Any] | None) -> str:
    if not fig or not fig.get("n"):
        return "no cases"
    k, n = int(fig["k"]), int(fig["n"])
    low, high = fig.get("low"), fig.get("high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{k} of {n} [{low * 100:.1f}-{high * 100:.1f}%]"
    return f"{k} of {n}"


def _md_cell(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def render_markdown(brief: Mapping[str, Any]) -> str:
    """The brief as Markdown, for a committee's file (zero PHI). It says what it is: computed
    here, and not signed."""
    pack = brief["pack"]
    vol = brief["volume"]
    figs = brief["figures"]
    roi = brief["roi"]
    hours_line = (
        f"| Projected clinician time reclaimable | {roi['weekly_hours_reclaimable']} hrs/week "
        f"(at {roi['minutes_per_case']} min/case) |"
    )
    lines = [
        f"# Clinical AI Validation & Readiness Brief: {_md_cell(pack['name'])}",
        "",
        f"*{brief['notice']}*",
        "",
        f"*{brief['about']}*",
        "",
        f"- **Agent**: `{_md_cell(pack['id'])}` v{_md_cell(pack['version'])} "
        f"(`{pack['digest'][:19]}...`)",
        f"- **Risk level**: {pack['risk_level']} ({_md_cell(pack['risk_category'])})",
        f"- **Validation window**: Last {brief['window_days']} days ({brief['generated_at']})",
        f"- **Shadow pilot**: {'Active' if brief['shadow_pilot']['active'] else 'Off'}",
        f"- **Go-live readiness**: {readiness_line(brief['gates'])}",
        "",
        "## Executive Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Decisions evaluated | {vol['total']} ({vol['stat_cases']} stat) |",
        f"| Observed clinician agreement | {_fmt_figure(figs['agreement'])} |",
        f"| Human override rate | {_fmt_figure(figs['override_rate'])} |",
        f"| Automation-ready decisions | {_fmt_figure(figs['automation_readiness'])} |",
        f"| Automated decisions | {vol['automated']} of {vol['total']} |",
        f"| Ratings of wrong on automated decisions | {vol['down_on_auto']} "
        f"({vol['auto_rated']} rated, {vol['auto_labelled']} with an outcome recorded) |",
        hours_line,
        "",
        "## Readiness Gates",
        "",
    ]
    for gate in brief["gates"]:
        mark = GATE_WORDS.get(gate["status"], str(gate["status"])).upper()
        lines.append(f"- **[{mark}] {gate['label']}**: {gate['detail']}")
    clusters = brief.get("override_clusters") or []
    if clusters:
        lines.extend(["", "## Top Human Override Patterns (Closed-Loop Improvement)", ""])
        lines.append("| Recommended | Clinician Chose | Count | Share of Overrides |")
        lines.append("| --- | --- | ---: | ---: |")
        for cluster in clusters:
            lines.append(
                f"| `{_md_cell(cluster['recommended'])}` | `{_md_cell(cluster['chosen'])}` | "
                f"{cluster['count']} | {cluster['count']} of {vol['overrides']} |"
            )
    lines.append("")
    return "\n".join(lines)
