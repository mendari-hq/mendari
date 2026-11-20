# SPDX-License-Identifier: Apache-2.0
"""Executive Validation & Go-Live Readiness Brief (mendari.validation-brief/1).

Turns live or shadow-mode operational evidence from an organization's own audit trail into an
exact, board-ready validation summary: safety gates, exact Clopper-Pearson confidence intervals,
reclaimable clinician hours, and clustered human-override patterns for closed-loop improvement.
Never reads or emits PHI or free-text case inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mendari import notices
from mendari.core.audit import AuditStore
from mendari.core.pack import Pack
from mendari.evals import figures

FORMAT = "mendari.validation-brief/1"
MIN_PILOT_CASES = 30
DEFAULT_MINUTES_PER_CASE = 3.5


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


def build(
    store: AuditStore,
    pack: Pack,
    *,
    days: int = 30,
    minutes_per_case: float = DEFAULT_MINUTES_PER_CASE,
    holds: Sequence[Mapping[str, Any]] = (),
    governance_active: Mapping[str, Any] | None = None,
    registry_approval: Mapping[str, Any] | None = None,
    monitoring_alerts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the structured `mendari.validation-brief/1` report for `pack`."""
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

    high_alerts = [a for a in monitoring_alerts if a.get("severity") == "high"]
    drift_alerts = [a for a in monitoring_alerts if a.get("kind") == "drift"]

    # Five transparent go-live readiness gates.
    gates: list[dict[str, Any]] = []
    if total >= MIN_PILOT_CASES:
        gates.append(
            {
                "key": "volume",
                "label": "Local pilot volume",
                "status": "pass",
                "detail": (
                    f"{total} local decisions evaluated in the last {days} days "
                    f"(target >= {MIN_PILOT_CASES})."
                ),
            }
        )
    elif total > 0:
        gates.append(
            {
                "key": "volume",
                "label": "Local pilot volume",
                "status": "warn",
                "detail": (
                    f"{total} of {MIN_PILOT_CASES} recommended local decisions recorded "
                    f"in the last {days} days."
                ),
            }
        )
    else:
        gates.append(
            {
                "key": "volume",
                "label": "Local pilot volume",
                "status": "fail",
                "detail": f"No local decisions recorded in the last {days} days.",
            }
        )

    if down_on_auto == 0 and not high_alerts:
        gates.append(
            {
                "key": "safety",
                "label": "Safety & zero automated errors",
                "status": "pass",
                "detail": (
                    "0 automated decisions rated wrong and no high-severity monitoring alerts."
                ),
            }
        )
    else:
        problems = []
        if down_on_auto:
            problems.append(f"{down_on_auto} automated decision(s) rated wrong")
        if high_alerts:
            problems.append(f"{len(high_alerts)} high-severity alert(s) active")
        gates.append(
            {
                "key": "safety",
                "label": "Safety & zero automated errors",
                "status": "fail",
                "detail": "; ".join(problems) + ".",
            }
        )

    if labelled == 0:
        gates.append(
            {
                "key": "agreement",
                "label": "Clinician agreement",
                "status": "warn",
                "detail": (
                    "No clinician-resolved or downstream-labelled decisions in this window yet."
                ),
            }
        )
    else:
        rate = agreed / labelled
        status = "pass" if rate >= 0.90 else ("warn" if rate >= 0.80 else "fail")
        gates.append(
            {
                "key": "agreement",
                "label": "Clinician agreement",
                "status": status,
                "detail": (
                    f"{agreed} of {labelled} recommendations matched clinician or "
                    f"downstream outcome ({rate * 100:.1f}%)."
                ),
            }
        )

    risk_level = pack.manifest.risk.level
    needs_risk_approval = risk_level in (2, 3) and governance_active is None
    needs_reg_approval = pack.origin == "hub" and not (
        registry_approval and registry_approval.get("active")
    )
    if pack.community or risk_level >= 4:
        gates.append(
            {
                "key": "governance",
                "label": "Governance & risk gates",
                "status": "pass",
                "detail": "Human-in-the-loop permanently enforced by pack origin or risk level.",
            }
        )
    elif hard_holds or needs_risk_approval or needs_reg_approval:
        pending_items = []
        if hard_holds:
            pending_items.append(f"{len(hard_holds)} automation hold(s) active")
        if needs_risk_approval:
            pending_items.append(f"risk level {risk_level} committee approval required")
        if needs_reg_approval:
            pending_items.append("registry digest approval required")
        gates.append(
            {
                "key": "governance",
                "label": "Governance & risk gates",
                "status": "warn",
                "detail": "; ".join(pending_items).capitalize() + ".",
            }
        )
    else:
        gates.append(
            {
                "key": "governance",
                "label": "Governance & risk gates",
                "status": "pass",
                "detail": "Required governance approvals are in force with no operational holds.",
            }
        )

    if not drift_alerts and not monitoring_alerts:
        gates.append(
            {
                "key": "drift",
                "label": "Calibration & operational drift",
                "status": "pass",
                "detail": (
                    "No confidence drift or operational degradation alerts in the last 7 days."
                ),
            }
        )
    else:
        gates.append(
            {
                "key": "drift",
                "label": "Calibration & operational drift",
                "status": "fail" if high_alerts else "warn",
                "detail": (
                    f"{len(monitoring_alerts)} monitoring alert(s) active in the last 7 days."
                ),
            }
        )

    ready = all(g["status"] == "pass" for g in gates)
    return {
        "format": FORMAT,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "notice": notices.NOTICE,
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


def _fmt_figure(fig: Mapping[str, Any] | None) -> str:
    if not fig or not fig.get("n"):
        return "no cases"
    k, n = int(fig["k"]), int(fig["n"])
    low, high = fig.get("low"), fig.get("high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{k} of {n} [{low * 100:.1f}-{high * 100:.1f}%]"
    return f"{k} of {n}"


def render_markdown(brief: Mapping[str, Any]) -> str:
    """Render a committee-ready Markdown validation & readiness brief (zero PHI)."""
    pack = brief["pack"]
    vol = brief["volume"]
    figs = brief["figures"]
    roi = brief["roi"]
    readiness = "ALL GATES PASSED" if brief["ready_for_live"] else "REVIEW REQUIRED"
    hours_line = (
        f"| Projected clinician time reclaimable | {roi['weekly_hours_reclaimable']} hrs/week "
        f"(at {roi['minutes_per_case']} min/case) |"
    )
    lines = [
        f"# Clinical AI Validation & Readiness Brief: {pack['name']}",
        "",
        f"*{brief['notice']}*",
        "",
        f"- **Pack**: `{pack['id']}` v{pack['version']} (`{pack['digest'][:19]}...`)",
        f"- **Risk level**: {pack['risk_level']} ({pack['risk_category']})",
        f"- **Validation window**: Last {brief['window_days']} days ({brief['generated_at']})",
        f"- **Shadow pilot**: {'Active' if brief['shadow_pilot']['active'] else 'Off'}",
        f"- **Go-live readiness**: {readiness}",
        "",
        "## Executive Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Decisions evaluated | {vol['total']} ({vol['stat_cases']} stat) |",
        f"| Observed clinician agreement | {_fmt_figure(figs['agreement'])} |",
        f"| Human override rate | {_fmt_figure(figs['override_rate'])} |",
        f"| Automation-ready decisions | {_fmt_figure(figs['automation_readiness'])} |",
        f"| Automated decisions rated wrong | {vol['down_on_auto']} |",
        hours_line,
        "",
        "## Readiness Gates",
        "",
    ]
    for gate in brief["gates"]:
        mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(gate["status"], gate["status"])
        lines.append(f"- **[{mark}] {gate['label']}**: {gate['detail']}")
    clusters = brief.get("override_clusters") or []
    if clusters:
        lines.extend(["", "## Top Human Override Patterns (Closed-Loop Improvement)", ""])
        lines.append("| Recommended | Clinician Chose | Count | Share of Overrides |")
        lines.append("| --- | --- | ---: | ---: |")
        for cluster in clusters:
            lines.append(
                f"| `{cluster['recommended']}` | `{cluster['chosen']}` | "
                f"{cluster['count']} | {cluster['share'] * 100:.1f}% |"
            )
    lines.append("")
    return "\n".join(lines)
