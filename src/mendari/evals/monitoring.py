# SPDX-License-Identifier: Apache-2.0
"""A deployed agent over time: is it still doing what its benchmark says?

It compares like with like (LP-21). For the last N whole weeks (N is the days asked for, 14 to
180, divided by 7 and rounded up, so the week before the last always exists), it reads the
decisions of the **served version** of the agent, for the organization the request acts for.
Decisions of other versions are only counted, as `other_versions`. The last 7 days and the week
before hold only decisions made since the agent's current configuration (this build and these
providers) started, by the deployment records (`configuration_since`); the weekly series shows
every decision of the version.

- **Weekly series:**
  - decisions;
  - refused inputs, counted apart and left out of every rate and the cost;
  - the automated share;
  - the human override rate, of reviewed decisions;
  - observed accuracy: the recommendation against what a reviewer or a downstream system said;
  - accuracy in each stratum the benchmark measured;
  - how many decisions still await review;
  - the cost per decision, unknown (null) when any decision's cost is unknown.
- **The reference.** The agent's benchmark counts only when it measured this build of the agent
  (its digest), with the providers that run here (each slot's variant, and its model where the
  server knows it), under the same governance (an approval in force here, or none, as when it
  was measured). An agent pulled from a registry has none, whatever it carries: its benchmark is
  its publisher's upload, never a reference (ADR-0012), so it is not read. Otherwise there is
  none: `benchmark.comparable` is false and `benchmark.reason` says why (`from_registry`,
  `no_benchmark`, `another_build`, `providers_unknown`, `other_providers`, `other_policy`), and
  only the checks that need no benchmark run: automated decisions rated
  wrong, an override rate that doubled, and operational health. Accuracy is compared stratum by
  stratum, as the benchmark measured it:
  - reviewed decisions (sent to a person, who kept or changed the recommendation) against the
    benchmark's reviewed accuracy, one minus its expected override rate;
  - automated decisions with an outcome recorded downstream against its automated accuracy.
- **Confidence drift:** the population stability index (PSI) between the last 7 days' primary
  confidences and the reference's (its calibration bins), in tenths from 0 to 1. It needs a
  comparable reference.
- **Operational health** (LP-21d), from stored columns: the share of decisions with a provider
  error recorded (`decisions.provider_errors`), the abstention share, and latency (p50, p95,
  mean of each decide).
- **Alerts**, each with a minimum amount of evidence:
  - a stratum's accuracy below the reference's by more than the agent's degradation tolerance,
    beyond doubt: even the top of its exact 95% interval is below (20 decisions or more);
  - drift at or above 0.25 (a common threshold for a real shift), over 50 confidences or more;
  - automated decisions that someone rated wrong (a thumbs-down);
  - an override rate that doubled against the week before (20 reviews or more in each);
  - week over week, over 50 decisions or more this week: a provider error share of 5% or more
    beyond doubt that doubled (on its level alone when the week before had fewer than 50
    counted); with 50 or more the week before too, an abstention share that doubled and rose 5
    points beyond doubt, and 5% or more of decisions beyond doubt taking twice the week before's
    p95 and 2 s or more. These need no benchmark.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from mendari.alerts import AlertSettings, deliver
from mendari.core.agent import Agent
from mendari.core.audit import AuditStore, truth_label
from mendari.core.card import benchmark
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals.render import shown

MIN_LABELLED = 20
MIN_FOR_DRIFT = 50
MIN_REVIEWED = 20
DRIFT_ALERT = 0.25
# Operational health (LP-21d), week over week, over 50 decisions or more this week: a provider
# error share at least ERROR_SHARE beyond doubt and doubled (on its level alone when the week
# before had fewer than 50); with 50 or more the week before too, an abstention share doubled
# and up ABSTAIN_RISE beyond doubt, and a p95 latency past twice the week before's and SLOW_MS
# beyond doubt (SLOW_SHARE of decisions or more past it).
MIN_FOR_OPERATIONS = 50
ERROR_SHARE = 0.05
ABSTAIN_RISE = 0.05
SLOW_MS = 2000.0
SLOW_SHARE = 0.05
# The days a report may cover: two whole weeks at least, so the week before always exists.
MIN_DAYS = 14
MAX_DAYS = 180
# A local provider's model is its type and a hash of the agent's files (`rules@661449489daf`):
# the agent's digest fixes it. Any other model (an LLM's, a composite's) is a setting.
_LOCAL_MODEL = re.compile(r"^[a-z_]+@[0-9a-f]+$")
# The deployment records read to find the current configuration and what runs.
_RECORDS = 500

Row = Mapping[str, Any]


def monitor(
    store: AuditStore,
    agent: Agent,
    *,
    days: int = 30,
    now: datetime | None = None,
    providers: Mapping[str, str] | None = None,
    models: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """`providers` is the variant each of the agent's slots runs here (None: not known, so no
    benchmark is comparable); `models` the model each runs, where known (a server knows its
    own; a command does not, so a benchmark of a model set by a setting is not comparable)."""
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise MendariError(f"monitoring covers {MIN_DAYS} to {MAX_DAYS} days, not {days}")
    now = now or datetime.now(UTC)
    weeks = math.ceil(days / 7)
    since = (now - timedelta(days=7 * weeks)).isoformat()
    rows = store.decision_rows(agent.id, since, version=agent.version)
    other_versions = {
        version: count
        for version, count in store.decision_versions(agent.id, since).items()
        if version != agent.version
    }
    started = _configuration_since(store, agent, providers)
    current = [r for r in rows if started is None or _when(r) >= started]
    week_ago, two_weeks_ago = now - timedelta(days=7), now - timedelta(days=14)
    last_week = [r for r in current if _when(r) >= week_ago]
    week_before = [r for r in current if two_weeks_ago <= _when(r) < week_ago]
    approved = store.active_approval(agent.id, agent.digest) is not None
    bench = _benchmark(agent, providers, models, approved=approved)
    bins = bench.pop("bins")  # what drift compares with, not part of the answer
    made = [r for r in last_week if not r["refused"]]
    drift = _psi(bins, made) if bench["comparable"] else None
    recent, before = _summary(last_week), _summary(week_before)
    # How many of this week's decisions took at least twice the week before's p95 (and SLOW_MS):
    # the latency alert's evidence, as k of n.
    if before["latency_ms"]["p95"] is not None:
        line = max(2 * before["latency_ms"]["p95"], SLOW_MS)
        took = [float(r["latency_ms"]) for r in made if r["latency_ms"] is not None]
        recent = {**recent, "slow": figures.of(sum(t >= line for t in took), len(took))}
        recent["slow_line_ms"] = line
    wrong_auto = sum(1 for r in last_week if r["outcome"] == "AUTO" and r["rated_wrong"])
    return {
        "agent": {"id": agent.id, "version": agent.version, "digest": agent.digest},
        "days": days,
        "benchmark": bench,
        "other_versions": other_versions,
        "configuration_since": None if started is None else started.isoformat(),
        "last_7_days": {**recent, "drift_psi": drift, "automated_rated_wrong": wrong_auto},
        "weeks": _weekly(rows, now, weeks),
        "alerts": _alerts(recent, before, drift, wrong_auto, bench),
    }


def organizations(
    store: AuditStore, *, days: int = 30, now: datetime | None = None, skip: str | None = None
) -> list[str]:
    """The organizations the monitor runs for, each on its own (LP-21f): every one with a
    decision in the window, but `skip` (the status organization, whose decisions are
    synthetic)."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=7 * math.ceil(days / 7))).isoformat()
    return [org for org in store.organizations_since(since) if org != skip]


def post_alerts(
    settings: AlertSettings,
    org: str | None,
    reports: Sequence[Mapping[str, Any]],
    *,
    attempts: int,
    store: AuditStore | None = None,
) -> bool:
    """The alerts of one organization's reports (all organizations together when `org` is
    None), as one alert to the webhook (`mendari.alerts`): whether it was delivered."""
    alerting = [report for report in reports if report["alerts"]]
    lines = [
        f"{report['agent']['id']}: [{alert['severity']}] {alert['kind']}: {alert['message']}"
        for report in alerting
        for alert in report["alerts"]
    ]
    return deliver(
        settings,
        event="monitor.alerts",
        org=org,
        alerts=[{"agent": report["agent"], "alerts": report["alerts"]} for report in alerting],
        lines=lines,
        attempts=attempts,
        timeout=10,
        store=store,
    )


def recorded_providers(store: AuditStore, agent: Agent) -> dict[str, str] | None:
    """The variant each slot runs, from the deployment records of this build of the agent (a
    server records one when it first serves it, and again when its providers change), for a
    command that runs no server: the newest record of each environment, when they agree. None
    when no server recorded this build, or two environments run it on other providers."""
    newest: dict[str, dict[str, str]] = {}
    for record in store.deployments(agent.id, limit=_RECORDS, digest=agent.digest):
        ran = {str(slot): str(variant) for slot, variant in record["providers"].items()}
        newest.setdefault(str(record["environment"]), ran)
    running = list(newest.values())
    if not running or any(ran != running[0] for ran in running):
        return None
    return running[0]


def _configuration_since(
    store: AuditStore, agent: Agent, providers: Mapping[str, str] | None
) -> datetime | None:
    """When this build of the agent started running on these providers (the deployment records,
    `AuditStore.configuration_started`); None when that is not known (no providers, no record)."""
    if providers is None:
        return None
    started = store.configuration_started(agent.id, agent.digest, providers, limit=_RECORDS)
    return None if started is None else datetime.fromisoformat(started)


def _number(value: Any) -> float | None:
    """A share stated in a benchmark, or None when it is not one (an agent's file is checked for
    its shape, not each value: what cannot be read is absent, never an error)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


def _counted(rate: float | None, n: Any) -> figures.Figure | None:
    """A benchmark's share as k of n, or None when it measured none (`counted` gives 0 of 0)."""
    return None if rate is None else figures.counted(rate, n)


def _whole(value: Any) -> int:
    """A count stated in a benchmark, or 0 when it is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _benchmark(
    agent: Agent,
    providers: Mapping[str, str] | None,
    models: Mapping[str, str | None] | None,
    *,
    approved: bool,
) -> dict[str, Any]:
    """The benchmark as measured (`accuracy`: the primary question over every case), whether it
    is comparable here and why not, and the strata's figures the alerts compare with. An agent
    from a registry has none: what it carries is its publisher's upload (ADR-0012)."""
    from_registry = agent.origin == "hub"
    report = _mapping(None if from_registry else benchmark(agent))
    reference = _mapping(_mapping(report.get("questions")).get(agent.manifest.policy.primary))
    measured = _mapping(report.get("decisions"))
    review = _mapping(measured.get("review"))
    automation = _mapping(measured.get("automation"))
    expected = _number(review.get("expected_override_rate"))
    reviewed_accuracy = None if expected is None else 1 - expected
    automated_accuracy = _number(automation.get("accuracy"))
    ran = {str(slot): _mapping(info) for slot, info in _mapping(report.get("providers")).items()}
    governed = _mapping(report.get("policy")).get("governance_override") is not None
    if from_registry:
        reason: str | None = "from_registry"
    elif not report:
        reason = "no_benchmark"
    elif _mapping(report.get("agent")).get("digest") != agent.digest:
        reason = "another_build"
    elif providers is None:
        reason = "providers_unknown"
    elif {slot: info.get("variant") for slot, info in ran.items()} != dict(providers):
        reason = "other_providers"
    elif models is None and any(
        not _LOCAL_MODEL.match(str(info.get("model"))) for info in ran.values()
    ):
        reason = "providers_unknown"  # a model a setting chose, which a command cannot see
    elif models is not None and any(
        info.get("model") != models.get(slot) for slot, info in ran.items()
    ):
        reason = "other_providers"
    elif governed != approved:
        reason = "other_policy"  # automated under an approval, or measured with one
    else:
        reason = None
    accuracy = _number(reference.get("accuracy"))
    return {
        "accuracy": accuracy,
        "n": reference.get("n"),
        # The strata the alerts compare with: decisions sent to a person (one minus the expected
        # override rate) and automated decisions.
        "reviewed_accuracy": reviewed_accuracy,
        "automated_accuracy": automated_accuracy,
        "tolerance": agent.manifest.evaluation.degradation_tolerance,
        "comparable": reason is None,
        "reason": reason,
        "figures": {
            "accuracy": _counted(accuracy, reference.get("n")),
            "reviewed_accuracy": _counted(reviewed_accuracy, review.get("n")),
            "automated_accuracy": _counted(automated_accuracy, automation.get("n")),
        },
        "bins": _mapping(reference.get("calibration")).get("bins") or [],
    }


def _alerts(
    recent: Row, before: Row, drift: float | None, wrong_auto: int, bench: Row
) -> list[dict[str, str]]:
    alerts = []
    strata = (
        ("reviewed_accuracy", "reviewed decisions", MIN_REVIEWED),
        ("automated_accuracy", "automated decisions with an outcome recorded", MIN_LABELLED),
    )
    for key, which, least in strata if bench["comparable"] else ():
        observed, target = recent["figures"][key], bench[key]
        if target is None or observed["n"] < least:
            continue
        # Beyond doubt: even the top of the observed share's interval is below the line.
        if observed["high"] < target - bench["tolerance"]:
            alert = _alert(
                "accuracy",
                "high",
                f"{which}: accuracy {shown(observed)} over the last 7 days, below the "
                f"benchmark's {shown(bench['figures'][key])} by more than "
                f"{bench['tolerance'] * 100:.1f} pts",
            )
            alerts.append({**alert, "stratum": key})
    if drift is not None and recent["confidences"] >= MIN_FOR_DRIFT and drift >= DRIFT_ALERT:
        message = f"PSI {drift:.2f} against the benchmark's confidences (0.25 or more is a shift)"
        alerts.append(_alert("drift", "medium", message))
    if wrong_auto:
        message = f"{wrong_auto} automated decision(s) rated wrong in the last 7 days"
        alerts.append(_alert("automated_wrong", "high", message))
    alerts.extend(_operations(recent, before))
    now_rate, then_rate = recent["override_rate"], before["override_rate"]
    compared = recent["reviewed"] >= MIN_REVIEWED and before["reviewed"] >= MIN_REVIEWED
    override_spike = (
        compared
        and now_rate is not None
        and then_rate is not None
        and (
            (then_rate > 0 and now_rate >= 2 * then_rate)
            or (then_rate == 0 and recent["figures"]["overrides"]["low"] >= ABSTAIN_RISE)
        )
    )
    if override_spike:
        message = (
            f"overridden {shown(recent['figures']['overrides'])} this week, "
            f"{shown(before['figures']['overrides'])} the week before"
        )
        alerts.append(_alert("overrides", "medium", message))
    return alerts


def _operations(recent: Row, before: Row) -> list[dict[str, str]]:
    """Operational health, week over week: provider errors, abstentions, latency."""
    alerts = []
    now, then = recent["figures"]["provider_errors"], before["figures"]["provider_errors"]
    if now["n"] >= MIN_FOR_OPERATIONS and now["low"] >= ERROR_SHARE:
        compared = then["n"] >= MIN_FOR_OPERATIONS
        if not compared or now["k"] / now["n"] >= 2 * then["k"] / then["n"]:
            since = f", {shown(then)} the week before" if compared else ""
            message = f"decisions with a provider error: {shown(now)} this week{since}"
            alerts.append(_alert("provider_errors", "medium", message))
    now, then = recent["figures"]["abstained"], before["figures"]["abstained"]
    if min(now["n"], then["n"]) >= MIN_FOR_OPERATIONS:
        share = then["k"] / then["n"]
        if now["low"] >= max(2 * share, share + ABSTAIN_RISE):
            message = f"abstained {shown(now)} this week, {shown(then)} the week before"
            alerts.append(_alert("abstention", "medium", message))
    slow, then_ms = recent.get("slow"), before["latency_ms"]
    if slow is None or min(slow["n"], then_ms["n"]) < MIN_FOR_OPERATIONS:
        return alerts
    if slow["low"] >= SLOW_SHARE:  # the p95 is past the line, beyond doubt
        message = (
            f"{shown(slow)} of decisions took {recent['slow_line_ms'] / 1000:.1f} s or more "
            f"this week (twice the week before's p95 of {then_ms['p95'] / 1000:.1f} s, or 2 s)"
        )
        alerts.append(_alert("latency", "medium", message))
    return alerts


def _alert(kind: str, severity: str, message: str) -> dict[str, str]:
    return {"kind": kind, "severity": severity, "message": message}


def _when(row: Row) -> datetime:
    return datetime.fromisoformat(str(row["created_at"]))


def _rate(part: int, whole: Sequence[Row]) -> float | None:
    return part / len(whole) if whole else None


def _summary(rows: Sequence[Row]) -> dict[str, Any]:
    made = [r for r in rows if not r["refused"]]  # a refused input decided nothing
    reviewed = [r for r in made if r["human_override"] is not None]
    labelled = [r for r in made if truth_label(r) and r["recommendation"]]
    agreed = sum(truth_label(r) == r["recommendation"] for r in labelled)
    automated = sum(r["outcome"] == "AUTO" for r in made)
    overridden = sum(bool(r["human_override"]) for r in reviewed)
    # The strata the benchmark measures: decisions sent to a person, kept or changed there, and
    # automated decisions whose outcome a downstream system recorded.
    sent = [r for r in reviewed if r["outcome"] == "HUMAN_REVIEW"]
    kept = sum(not r["human_override"] for r in sent)
    auto_labelled = [r for r in labelled if r["outcome"] == "AUTO"]
    auto_right = sum(truth_label(r) == r["recommendation"] for r in auto_labelled)
    costs = [r["cost_usd"] for r in made]
    unknown = sum(cost is None for cost in costs)
    # Operational health: decisions whose providers failed, abstentions, and how long a decision
    # took (its whole decide).
    failed = sum(r["provider_errors"] > 0 for r in made)
    abstained = sum(r["outcome"] == "ABSTAIN" for r in made)
    took = sorted(float(r["latency_ms"]) for r in made if r["latency_ms"] is not None)
    return {
        "decisions": len(rows),
        "refused": len(rows) - len(made),
        "automated_rate": _rate(automated, made),
        "reviewed": len(reviewed),
        "awaiting_review": sum(r["review_status"] == "pending" for r in made),
        "override_rate": _rate(overridden, reviewed),
        "labelled": len(labelled),
        "observed_accuracy": _rate(agreed, labelled),
        "confidences": sum(r["confidence"] is not None for r in made),
        # Unknown, never an average of the known ones, when any decision's cost is unknown.
        "cost_per_decision": sum(costs) / len(costs) if costs and not unknown else None,
        "cost_unknown": unknown,
        "latency_ms": {
            "n": len(took),
            "p50": _percentile(took, 0.5),
            "p95": _percentile(took, 0.95),
            "mean": sum(took) / len(took) if took else None,
        },
        # The same shares as k of n with their intervals (mendari.evals.figures), as shown.
        "figures": {
            "automated": figures.of(automated, len(made)),
            "overrides": figures.of(overridden, len(reviewed)),
            "accuracy": figures.of(agreed, len(labelled)),
            "reviewed_accuracy": figures.of(kept, len(sent)),
            "automated_accuracy": figures.of(auto_right, len(auto_labelled)),
            "provider_errors": figures.of(failed, len(made)),
            "abstained": figures.of(abstained, len(made)),
        },
    }


def _percentile(ordered: Sequence[float], share: float) -> float | None:
    """The nearest-rank percentile of values in order (None without any)."""
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]


def _weekly(rows: Sequence[Row], now: datetime, weeks: int) -> list[dict[str, Any]]:
    """Whole weeks counted back from now (the last is the last 7 days), oldest first."""
    series = []
    for back in range(weeks):
        end = now - timedelta(days=7 * back)
        begin = end - timedelta(days=7)
        chunk = [r for r in rows if begin <= _when(r) < end or (back == 0 and _when(r) >= end)]
        span = {"from": begin.date().isoformat(), "to": end.date().isoformat()}
        series.append({**span, **_summary(chunk)})
    return series[::-1]


def _psi(bins: Sequence[Row], rows: Sequence[Row]) -> float | None:
    """Population stability index of recent confidences against the benchmark's, over tenths."""
    # A stored confidence that is not a share is no evidence of drift: a NaN has no tenth, and a
    # negative one would index a tenth from the end of the list.
    values = [
        float(r["confidence"])
        for r in rows
        if r["confidence"] is not None and 0.0 <= float(r["confidence"]) <= 1.0
    ]
    usable = [b for b in bins if isinstance(b, Mapping) and _number(b.get("low")) is not None]
    total = sum(_whole(b.get("n")) for b in usable)
    if not values or not total:
        return None
    expected = [0.0] * 10
    for b in usable:
        expected[min(round(float(b["low"]) * 10), 9)] += _whole(b.get("n")) / total
    actual = [0.0] * 10
    for value in values:
        actual[min(int(value * 10), 9)] += 1 / len(values)
    smooth = 1e-4
    denom = 1.0 + len(actual) * smooth
    pairs = zip(actual, expected, strict=True)
    return sum(
        ((a + smooth) / denom - (e + smooth) / denom) * math.log((a + smooth) / (e + smooth))
        for a, e in pairs
    )
