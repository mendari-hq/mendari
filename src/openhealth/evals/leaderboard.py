"""Provider leaderboard: provider configurations compared on the same datasets.

Each row summarises one evaluation report, i.e. one configuration on one dataset. Reports come
from local runs or from a deployment's evaluation endpoint (for providers whose credentials
exist only there), so every configuration is measured on identical records with identical
metrics. The leaderboard lives at `evals/leaderboard.json` in the pack; like the benchmark it
is an attestation about the pack and is not part of its digest. It contains no case text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from openhealth.core.pack import Pack
from openhealth.evals.render import num, pct

FORMAT = "openhealth.leaderboard/1"
PATH = "evals/leaderboard.json"
_COMPOSITES = ("any_of", "cascade", "vote")


def config_label(providers: Mapping[str, Mapping[str, Any]]) -> str:
    """`slot=variant` pairs, naming the model of remote providers (it is a deployment choice,
    so the same variant on two models is two configurations). Local artifacts are
    fingerprinted as `type@hash` and are fixed by the pack version, so they are left out."""
    parts = []
    for slot, info in providers.items():
        model = str(info.get("model") or "")
        if info.get("type") in _COMPOSITES:  # "name:model + name:model" (or " → ")
            models = [m.split(":", 1)[1] for m in re.split(r" \+ | → ", model) if ":" in m]
        else:
            models = [model] if model else []
        remote = [m for m in models if "@" not in m and m not in ("rules", "naive_bayes")]
        parts.append(f"{slot}={info['variant']}" + (f" ({', '.join(remote)})" if remote else ""))
    return " · ".join(parts)


def row(report: Mapping[str, Any]) -> dict[str, Any]:
    primary = report["primary"]
    question = report["questions"].get(primary, {})
    labelled = question.get("n", 0) > 0
    decisions = report["decisions"]
    return {
        "config": config_label(report["providers"]),
        "providers": report["providers"],
        "dataset": report["dataset"]["name"],
        "n": report["dataset"]["n"],
        "report_id": report["id"],
        "created_at": report["created_at"],
        "pack_version": report["pack"]["version"],
        "accuracy": question.get("accuracy") if labelled else None,
        "macro_f1": question.get("macro_f1") if labelled else None,
        "ece": question.get("calibration", {}).get("ece") if labelled else None,
        "auto_rate": decisions["outcome_rates"]["AUTO"],
        "false_automation_rate": decisions["automation"]["false_automation_rate"],
        "critical": [
            {k: item[k] for k in ("question", "label", "n", "detected", "auto_missed")}
            for item in decisions.get("critical", [])
        ],
        "latency_p50_ms": report["latency_ms"]["p50"],
        "latency_p95_ms": report["latency_ms"]["p95"],
        "cost_per_decision_usd": report["cost"]["per_decision_usd"],
        "errors": sum(report.get("errors", {}).values()),
        "error_codes": dict(report.get("errors", {})),
    }


def build(
    pack: Pack,
    reports: Iterable[Mapping[str, Any]],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A leaderboard from reports, merged into `existing`: a new report replaces the row for
    the same configuration and dataset. Rows from another pack are refused."""
    rows = {
        (r["config"], r["dataset"]): r for r in (existing or {}).get("rows", []) if "config" in r
    }
    for report in reports:
        if report["pack"]["id"] != pack.id:
            raise ValueError(f"report {report['id']} is for {report['pack']['id']}, not {pack.id}")
        new = row(report)
        rows[(new["config"], new["dataset"])] = new
    return {
        "format": FORMAT,
        "pack": {"id": pack.id, "version": pack.version},
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": sorted(rows.values(), key=lambda r: (r["dataset"], r["config"])),
    }


def by_dataset(board: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in board.get("rows", []):
        grouped.setdefault(item["dataset"], []).append(item)
    return grouped


def critical_text(item: Mapping[str, Any]) -> str:
    """e.g. `28/28 (0 automated)`; empty when the dataset has no critical cases."""
    return "; ".join(
        f"{c['detected']}/{c['n']} ({c['auto_missed']} automated)"
        for c in item["critical"]
        if c["n"]
    )


def load(pack: Pack) -> dict[str, Any] | None:
    path = pack.path(PATH)
    if not path.is_file():
        return None
    board: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return board


def markdown(board: Mapping[str, Any]) -> list[str]:
    """One table per dataset, best primary accuracy first."""
    out: list[str] = []
    for dataset, rows in by_dataset(board).items():
        n = max(item["n"] for item in rows)
        out += [
            f"**`{dataset}`** (n={n})",
            "",
            "| Configuration | Accuracy | Macro-F1 | ECE | AUTO | False automation "
            "| Critical detected | p50 latency | Cost / decision | Errors |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        ranked = sorted(rows, key=lambda item: -(item["accuracy"] or 0.0))
        for item in ranked:
            out.append(
                f"| {item['config']} | {pct(item['accuracy'])} | {num(item['macro_f1'])} "
                f"| {num(item['ece'])} | {pct(item['auto_rate'])} "
                f"| {pct(item['false_automation_rate'])} | {critical_text(item) or 'n/a'} "
                f"| {_ms(item['latency_p50_ms'])} | ${item['cost_per_decision_usd']:.5f} "
                f"| {_errors(item)} |"
            )
        out.append("")
    return out


def _ms(value: float) -> str:
    return f"{value:.2f} ms" if value < 10 else f"{value:,.0f} ms"


def _errors(item: Mapping[str, Any]) -> str:
    codes = item.get("error_codes") or {}
    detail = ", ".join(f"{code} {count}" for code, count in sorted(codes.items()))
    return f"{item['errors']} ({detail})" if detail else str(item["errors"])
