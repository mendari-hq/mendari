# SPDX-License-Identifier: Apache-2.0
"""A deployed pack over time: is it still doing what its benchmark says?

For the last N days of a pack's decisions (for the organization the request acts for):

- **weekly series:** decisions, the automated share, the human override rate (of reviewed
  decisions), observed accuracy (the recommendation against what a person or a downstream system
  said), and the cost per decision;
- **confidence drift:** the population stability index (PSI) between the last 7 days' primary
  confidences and the benchmark's (its calibration bins), in tenths from 0 to 1;
- **alerts**, each with a minimum amount of evidence:
  - observed accuracy below the benchmark by more than the pack's degradation tolerance;
  - drift at or above 0.25 (a common threshold for a real shift);
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

Row = Mapping[str, Any]


def monitor(
    store: AuditStore, pack: Pack, *, days: int = 30, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    rows = store.decision_rows(pack.id, (now - timedelta(days=days)).isoformat())
    week_ago, two_weeks_ago = now - timedelta(days=7), now - timedelta(days=14)
    last_week = [r for r in rows if _when(r) >= week_ago]
    week_before = [r for r in rows if two_weeks_ago <= _when(r) < week_ago]
    primary = pack.manifest.policy.primary
    reference = (benchmark(pack) or {}).get("questions", {}).get(primary, {})
    bench = {
        "accuracy": reference.get("accuracy"),
        "n": reference.get("n"),
        "tolerance": pack.manifest.evaluation.degradation_tolerance,
        "figures": {"accuracy": figures.counted(reference.get("accuracy"), reference.get("n"))},
    }
    drift = _psi(reference.get("calibration", {}).get("bins", []), last_week)
    recent, before = _summary(last_week), _summary(week_before)
    wrong_auto = sum(1 for r in last_week if r["outcome"] == "AUTO" and r["rated_wrong"])
    return {
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "days": days,
        "benchmark": bench,
        "last_7_days": {**recent, "drift_psi": drift, "automated_rated_wrong": wrong_auto},
        "weeks": _weekly(rows, now, days),
        "alerts": _alerts(recent, before, drift, wrong_auto, bench),
    }


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


def _alerts(
    recent: Row, before: Row, drift: float | None, wrong_auto: int, bench: Row
) -> list[dict[str, str]]:
    alerts = []
    observed, target = recent["observed_accuracy"], bench["accuracy"]
    evidence = recent["labelled"] >= MIN_LABELLED and observed is not None
    if evidence and target is not None and observed < target - bench["tolerance"]:
        alerts.append(
            _alert(
                "accuracy",
                "high",
                f"observed accuracy {shown(recent['figures']['accuracy'])} over the last 7 "
                f"days, below the benchmark's {shown(bench['figures']['accuracy'])} by more than "
                f"{bench['tolerance'] * 100:.1f} pts",
            )
        )
    if drift is not None and recent["decisions"] >= MIN_FOR_DRIFT and drift >= DRIFT_ALERT:
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
    reviewed = [r for r in rows if r["human_override"] is not None]
    labelled = [r for r in rows if r["final_label"] and r["recommendation"]]
    agreed = sum(r["final_label"] == r["recommendation"] for r in labelled)
    automated = sum(r["outcome"] == "AUTO" for r in rows)
    overridden = sum(bool(r["human_override"]) for r in reviewed)
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return {
        "decisions": len(rows),
        "automated_rate": _rate(automated, rows),
        "reviewed": len(reviewed),
        "override_rate": _rate(overridden, reviewed),
        "labelled": len(labelled),
        "observed_accuracy": _rate(agreed, labelled),
        "cost_per_decision": sum(costs) / len(costs) if costs else None,
        # The same shares as k of n with their intervals (mendari.evals.figures), as shown.
        "figures": {
            "automated": figures.of(automated, len(rows)),
            "overrides": figures.of(overridden, len(reviewed)),
            "accuracy": figures.of(agreed, len(labelled)),
        },
    }


def _weekly(rows: Sequence[Row], now: datetime, days: int) -> list[dict[str, Any]]:
    """Whole weeks counted back from now (the last is the last 7 days), oldest first."""
    weeks = []
    for back in range(math.ceil(days / 7)):
        end = now - timedelta(days=7 * back)
        begin = end - timedelta(days=7)
        chunk = [r for r in rows if begin <= _when(r) < end or (back == 0 and _when(r) >= end)]
        span = {"from": begin.date().isoformat(), "to": end.date().isoformat()}
        weeks.append({**span, **_summary(chunk)})
    return weeks[::-1]


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
