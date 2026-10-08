# SPDX-License-Identifier: Apache-2.0
"""A deployed pack over time: is it still doing what its benchmark says?

It compares like with like (LP-21). For the last N whole weeks (N is the days asked for, 14 to
180, divided by 7 and rounded up, so the week before the last always exists), it reads the
decisions of the **served version** of the pack, for the organization the request acts for.
Decisions of other versions are only counted, as `other_versions`.

- **Weekly series:**
  - decisions;
  - refused inputs, counted apart and left out of every rate;
  - the automated share;
  - the human override rate, of reviewed decisions;
  - observed accuracy: the recommendation against what a person or a downstream system said;
  - accuracy in each stratum the benchmark measured;
  - how many decisions still await review;
  - the cost per decision, unknown (null) when any decision's cost is unknown.
- **The reference.** The pack's benchmark counts only when it measured this build of the pack
  (its digest) with the providers that run here (each slot's variant). Otherwise there is none:
  `benchmark.comparable` is false, `benchmark.reason` says why, and only the week-over-week
  checks run. Accuracy is compared stratum by stratum, as the benchmark measured it:
  - reviewed decisions (a reviewer kept or changed the recommendation) against the benchmark's
    reviewed accuracy, one minus its expected override rate;
  - automated decisions with an outcome recorded downstream against its automated accuracy.
- **Confidence drift:** the population stability index (PSI) between the last 7 days' primary
  confidences and the reference's (its calibration bins), in tenths from 0 to 1. It needs a
  comparable reference.
- **Alerts**, each with a minimum amount of evidence:
  - accuracy in a stratum below the reference's by more than the pack's degradation tolerance;
  - drift at or above 0.25 (a common threshold for a real shift), over 50 confidences or more;
  - automated decisions that someone rated wrong;
  - an override rate that doubled against the week before.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from mendari.core.audit import AuditStore
from mendari.core.card import benchmark
from mendari.core.pack import Pack
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals.render import shown

MIN_LABELLED = 20
MIN_FOR_DRIFT = 50
MIN_REVIEWED = 20
DRIFT_ALERT = 0.25
# The days a report may cover: two whole weeks at least, so the week before always exists.
MIN_DAYS = 14
MAX_DAYS = 180

Row = Mapping[str, Any]


def monitor(
    store: AuditStore,
    pack: Pack,
    *,
    days: int = 30,
    now: datetime | None = None,
    providers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """`providers` is the variant each of the pack's slots runs here (None: not known, so no
    benchmark is comparable)."""
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise MendariError(f"monitoring covers {MIN_DAYS} to {MAX_DAYS} days, not {days}")
    now = now or datetime.now(UTC)
    weeks = math.ceil(days / 7)
    since = (now - timedelta(days=7 * weeks)).isoformat()
    rows = store.decision_rows(pack.id, since, version=pack.version)
    other_versions = {
        version: count
        for version, count in store.decision_versions(pack.id, since).items()
        if version != pack.version
    }
    week_ago, two_weeks_ago = now - timedelta(days=7), now - timedelta(days=14)
    last_week = [r for r in rows if _when(r) >= week_ago]
    week_before = [r for r in rows if two_weeks_ago <= _when(r) < week_ago]
    bench = _benchmark(pack, providers)
    bins = bench.pop("bins")  # what drift compares with, not part of the answer
    made = [r for r in last_week if not r["refused"]]
    drift = _psi(bins, made) if bench["comparable"] else None
    recent, before = _summary(last_week), _summary(week_before)
    wrong_auto = sum(1 for r in last_week if r["outcome"] == "AUTO" and r["rated_wrong"])
    return {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "days": days,
        "benchmark": bench,
        "other_versions": other_versions,
        "last_7_days": {**recent, "drift_psi": drift, "automated_rated_wrong": wrong_auto},
        "weeks": _weekly(rows, now, weeks),
        "alerts": _alerts(recent, before, drift, wrong_auto, bench),
    }


def recorded_providers(store: AuditStore, pack: Pack) -> dict[str, str] | None:
    """The variant each slot ran, from the newest deployment record of this build of the pack
    (a server records one when it first serves it), for a command that runs no server: None
    when no server has recorded this build."""
    for record in store.deployments(pack.id):
        if record["pack_digest"] == pack.digest:
            return {str(slot): str(variant) for slot, variant in record["providers"].items()}
    return None


def check_webhook(url: str) -> None:
    """Alerts may leave only over https (or http to this machine or a compose service), and
    never with credentials in the address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    local = host in ("localhost", "127.0.0.1") or (bool(host) and "." not in host)
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MendariError("the alert webhook must use https")
    if parts.username or parts.password:
        raise MendariError("the alert webhook must not carry credentials in its address")


def _benchmark(pack: Pack, providers: Mapping[str, str] | None) -> dict[str, Any]:
    """The benchmark as measured (`accuracy`: the primary question over every case), whether it
    is comparable here and why not, and the strata's figures the alerts compare with."""
    report = benchmark(pack) or {}
    reference = report.get("questions", {}).get(pack.manifest.policy.primary, {})
    measured = report.get("decisions") or {}
    review = measured.get("review") or {}
    automation = measured.get("automation") or {}
    expected = review.get("expected_override_rate")
    reviewed_accuracy = None if expected is None else 1 - expected
    ran = {
        str(slot): str(info.get("variant"))
        for slot, info in (report.get("providers") or {}).items()
        if isinstance(info, Mapping)
    }
    if not report:
        reason: str | None = "no_benchmark"
    elif (report.get("pack") or {}).get("digest") != pack.digest:
        reason = "another_build"
    elif providers is None:
        reason = "providers_unknown"
    elif ran != dict(providers):
        reason = "other_providers"
    else:
        reason = None
    return {
        "accuracy": reference.get("accuracy"),
        "n": reference.get("n"),
        # The strata the alerts compare with: decisions sent to a person (one minus the expected
        # override rate) and automated decisions.
        "reviewed_accuracy": reviewed_accuracy,
        "automated_accuracy": automation.get("accuracy"),
        "tolerance": pack.manifest.evaluation.degradation_tolerance,
        "comparable": reason is None,
        "reason": reason,
        "figures": {
            "accuracy": figures.counted(reference.get("accuracy"), reference.get("n")),
            "reviewed_accuracy": figures.counted(reviewed_accuracy, review.get("n")),
            "automated_accuracy": figures.counted(automation.get("accuracy"), automation.get("n")),
        },
        "bins": reference.get("calibration", {}).get("bins", []),
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
        if observed["k"] / observed["n"] < target - bench["tolerance"]:
            alerts.append(
                _alert(
                    "accuracy",
                    "high",
                    f"{which}: accuracy {shown(observed)} over the last 7 days, below the "
                    f"benchmark's {shown(bench['figures'][key])} by more than "
                    f"{bench['tolerance'] * 100:.1f} pts",
                )
            )
    if drift is not None and recent["confidences"] >= MIN_FOR_DRIFT and drift >= DRIFT_ALERT:
        message = f"PSI {drift:.2f} against the benchmark's confidences (0.25 or more is a shift)"
        alerts.append(_alert("drift", "medium", message))
    if wrong_auto:
        message = f"{wrong_auto} automated decision(s) rated wrong in the last 7 days"
        alerts.append(_alert("automated_wrong", "high", message))
    now_rate, then_rate = recent["override_rate"], before["override_rate"]
    compared = recent["reviewed"] >= MIN_REVIEWED and before["reviewed"] >= MIN_REVIEWED
    if compared and then_rate and now_rate is not None and now_rate >= 2 * then_rate:
        message = (
            f"overridden {shown(recent['figures']['overrides'])} this week, "
            f"{shown(before['figures']['overrides'])} the week before"
        )
        alerts.append(_alert("overrides", "medium", message))
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
    labelled = [r for r in made if r["final_label"] and r["recommendation"]]
    agreed = sum(r["final_label"] == r["recommendation"] for r in labelled)
    automated = sum(r["outcome"] == "AUTO" for r in made)
    overridden = sum(bool(r["human_override"]) for r in reviewed)
    # The strata the benchmark measures: decisions sent to a person, kept or changed there, and
    # automated decisions whose outcome a downstream system recorded.
    sent = [r for r in reviewed if r["outcome"] == "HUMAN_REVIEW"]
    kept = sum(not r["human_override"] for r in sent)
    auto_labelled = [r for r in labelled if r["outcome"] == "AUTO"]
    auto_right = sum(r["final_label"] == r["recommendation"] for r in auto_labelled)
    costs = [r["cost_usd"] for r in made]
    unknown = sum(cost is None for cost in costs)
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
        # The same shares as k of n with their intervals (mendari.evals.figures), as shown.
        "figures": {
            "automated": figures.of(automated, len(made)),
            "overrides": figures.of(overridden, len(reviewed)),
            "accuracy": figures.of(agreed, len(labelled)),
            "reviewed_accuracy": figures.of(kept, len(sent)),
            "automated_accuracy": figures.of(auto_right, len(auto_labelled)),
        },
    }


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
    values = [float(r["confidence"]) for r in rows if r["confidence"] is not None]
    total = sum(int(b.get("n") or 0) for b in bins)
    if not values or not total:
        return None
    expected = [0.0] * 10
    for b in bins:
        expected[min(round(float(b["low"]) * 10), 9)] += int(b.get("n") or 0) / total
    actual = [0.0] * 10
    for value in values:
        actual[min(int(value * 10), 9)] += 1 / len(values)
    smooth = 1e-4
    pairs = zip(actual, expected, strict=True)
    return sum((a - e) * math.log((a + smooth) / (e + smooth)) for a, e in pairs)
