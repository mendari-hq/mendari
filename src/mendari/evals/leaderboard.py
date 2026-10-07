# SPDX-License-Identifier: Apache-2.0
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
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from mendari.core.pack import Pack
from mendari.evals import figures, ranking, stats
from mendari.evals.render import INTERVALS, evidence, num, shown
from mendari.evals.result import MIN_POSITIVES, counted, false_automation, normalize

FORMAT = "mendari.leaderboard/2"
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
    report = normalize(report)
    run = report["run"]
    primary = report["primary"]
    question = report["questions"].get(primary, {})
    labelled = question.get("n", 0) > 0
    decisions = report["decisions"]
    return {
        "config": config_label(report["providers"]),
        "providers": report["providers"],
        "dataset": report["dataset"]["name"],
        "n": report["dataset"]["n"],
        "report_id": run.get("id"),
        "created_at": run.get("created_at"),
        "pack_version": report["pack"]["version"],
        # What a row is of: the pack as it was, the records, and the run's ledger (LP-10).
        "pack_digest": report["pack"].get("digest"),
        "dataset_sha256": report["dataset"].get("sha256"),
        "limit": report["dataset"].get("limit"),
        "ledger": (report.get("ledger") or {}).get("id"),
        "accuracy_proportion": _stored(counted(question.get("accuracy"), question.get("n")))
        if labelled
        else None,
        "false_automation": _stored(false_automation(report)),
        "accuracy": question.get("accuracy") if labelled else None,
        "macro_f1": question.get("macro_f1") if labelled else None,
        "ece": question.get("calibration", {}).get("ece") if labelled else None,
        "auto_rate": decisions["outcome_rates"]["AUTO"],
        "false_automation_rate": decisions["automation"]["false_automation_rate"],
        "critical": [
            {k: item[k] for k in ("question", "label", "n", "detected", "auto_missed")}
            for item in decisions.get("critical", [])
        ],
        "latency_p50_ms": run.get("latency_ms", {}).get("p50"),
        "latency_p95_ms": run.get("latency_ms", {}).get("p95"),
        "cost_per_decision_usd": report["cost"]["per_decision_usd"],
        "errors": sum(report.get("errors", {}).values()),
        "error_codes": dict(report.get("errors", {})),
        # The other questions, e.g. a safety screen or the criteria behind a derived primary.
        "questions": {
            key: {"n": q["n"], "accuracy": q["accuracy"], "ece": q["calibration"]["ece"]}
            for key, q in report["questions"].items()
            if key != primary and q.get("n")
        },
    }


def build(
    pack: Pack,
    reports: Iterable[Mapping[str, Any]],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A leaderboard from reports, merged into `existing`: a new report replaces the row of the
    same configuration on the same pack digest and records; a row of an earlier version stays,
    marked and unranked. Rows from another pack are refused. Ranked as `ranking.rank` does."""
    rows = {_row_key(r): r for r in (existing or {}).get("rows", []) if "config" in r}
    for report in reports:
        if report["pack"]["id"] != pack.id:
            named = normalize(report)["run"].get("id")
            raise ValueError(f"report {named} is for {report['pack']['id']}, not {pack.id}")
        new = row(report)
        rows[_row_key(new)] = new
    return {
        "format": FORMAT,
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "ranking": {"method": ranking.METHOD, "min_positives": MIN_POSITIVES},
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": ranking.rank(list(rows.values()), pack.digest),
    }


def _row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (row.get("pack_digest") or row.get("pack_version"), *ranking.records(row), row["config"])


def _stored(p: stats.Proportion | None) -> dict[str, Any] | None:
    return None if p is None else p.to_dict()


def critical_text(item: Mapping[str, Any]) -> str:
    """e.g. `28 of 28 [87.7-100.0%] (0 automated; insufficient evidence ...)`, each named
    (`note_pregnancy=yes 5 of 5 …`) when a pack has several critical labels; empty when the
    dataset has no critical cases."""
    named = len(item["critical"]) > 1
    return "; ".join(
        (f"{c['question']}={c['label']} " if named else "")
        + f"{shown(stats.proportion(c['detected'], c['n']))} ({c['auto_missed']} automated"
        + evidence(c["n"], short=True)
        + ")"
        for c in item["critical"]
        if c["n"]
    )


def load(pack: Pack) -> dict[str, Any] | None:
    """The pack's leaderboard as served (`figures.board`): ranked against the pack as it is
    (its digest), its rows with their figures, by set of records. None when there is none, or
    not one the card can show (`read`)."""
    board = read(pack)[0]
    if board is None:
        return None
    return figures.board(board, pack.digest, pack.version)


def read(pack: Pack) -> tuple[dict[str, Any] | None, str | None]:
    """The leaderboard, or None and why it is not shown. As the registry does, one that is not
    JSON, holds what a JSON response cannot or whose rows are not the rows `markdown` shows is
    treated as absent, never an error."""
    board, problem = pack.attestation_json(PATH)
    if board is None:
        return None, problem
    try:
        if not isinstance(board.get("rows", []), list):
            raise TypeError("rows")
        markdown(board, pack.digest, pack.version)
        figures.board(board, pack.digest, pack.version)  # what the server serves of it
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, ArithmeticError) as error:
        return None, f"not a leaderboard ({type(error).__name__})"
    return board, None


def markdown(
    board: Mapping[str, Any], digest: str | None = None, version: str | None = None
) -> list[str]:
    """One table per set of records (`ranking.grouped`), ranked against `digest` (the pack's
    own; `version` is its version): ranked rows first, then the unranked, each saying why (an
    earlier version of the pack, whose AUTO and false automation followed that version's policy,
    or too few critical positives)."""
    out: list[str] = []
    recorded = board.get("pack")
    recorded = recorded if isinstance(recorded, Mapping) else {}
    if digest is None:  # the pack it was last built for (mendari.leaderboard/2 records it)
        digest = recorded.get("digest")
        version = version if version is not None else recorded.get("version")
    groups = ranking.grouped(board.get("rows", []), digest)
    sizes = Counter((name, n) for (name, _, n), _ in groups)

    def config(item: Mapping[str, Any]) -> str:
        if item["standing"] == ranking.EARLIER:
            why = ranking.measured_on(item.get("pack_version"), item.get("pack_digest"), version)
            return f"{item['config']} ({why})"
        return str(item["config"])

    def place(item: Mapping[str, Any]) -> str:
        if item["rank"] is not None:
            return str(item["rank"])
        if item["standing"] == ranking.EARLIER:
            why = ranking.measured_on(item.get("pack_version"), item.get("pack_digest"), version)
            return f"none: {why}"
        return f"none: {item['standing']}"

    for group, rows in groups:
        name, _, n = group
        about = ranking.about(
            group,
            limited=any(item.get("limit") for item in rows),
            twin=sizes[(name, n)] > 1,
        )
        out += [
            f"**`{name}`** ({'; '.join(about)}; {INTERVALS})",
            "",
            "| Rank | Configuration | Critical detected | False automation | Accuracy | AUTO "
            "| Macro-F1 | ECE | p50 latency | Cost / decision | Errors |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for item in rows:
            cost = item["cost_per_decision_usd"]
            out.append(
                f"| {place(item)} | {item['config']} | {critical_text(item) or 'n/a'} "
                f"| {shown(_false_automation(item))} | {shown(_accuracy(item))} "
                f"| {shown(counted(item['auto_rate'], item['n']))} | {num(item['macro_f1'])} "
                f"| {num(item['ece'])} | {_ms(item['latency_p50_ms'])} "
                f"| {'unknown' if cost is None else f'${cost:.5f}'} | {_errors(item)} |"
            )
        if any(item.get("accuracy_proportion") is None for item in rows):
            out += [
                "",
                "Accuracy and false automation are n/a on a row recorded before leaderboards kept "
                "the counts they are of (mendari.leaderboard/1).",
            ]
        out.append("")
        out += _question_table(rows, config)
    return out


def _accuracy(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """A row's primary accuracy over the cases labelled for it. A row from before stored only
    the rate, and not how many cases were labelled (fewer than the dataset's when some have no
    label), so it shows n/a."""
    stored: Mapping[str, Any] | None = item.get("accuracy_proportion")
    return stored


def _false_automation(item: Mapping[str, Any]) -> stats.Proportion | Mapping[str, Any] | None:
    """A row's false automation over its labelled cases. A row from before stored only the rate
    over its automated cases, a different measure, so it shows n/a."""
    stored: Mapping[str, Any] | None = item.get("false_automation")
    return stored


def _question_table(
    rows: list[dict[str, Any]], config: Callable[[Mapping[str, Any]], str]
) -> list[str]:
    """Accuracy and ECE of the other questions, for those whose results differ between the
    configurations (the ones the providers being compared actually answer). Rows measured
    before reports carried them have none, and are left out."""
    rows = [item for item in rows if "questions" in item]
    keys = sorted({key for item in rows for key in item.get("questions", {})})
    varying = [
        key
        for key in keys
        if len({json.dumps(item.get("questions", {}).get(key), sort_keys=True) for item in rows})
        > 1
    ]
    if not varying:
        return []

    def cell(item: Mapping[str, Any], key: str) -> str:
        metrics = item.get("questions", {}).get(key)
        if metrics is None:
            return "n/a"
        return f"{shown(counted(metrics['accuracy'], metrics.get('n')))} / {num(metrics['ece'])}"

    return [
        "Other questions (accuracy / ECE):",
        "",
        "| Configuration | " + " | ".join(f"`{key}`" for key in varying) + " |",
        "|---|" + "---|" * len(varying),
        *(
            f"| {config(item)} | " + " | ".join(cell(item, k) for k in varying) + " |"
            for item in rows
        ),
        "",
    ]


def _ms(value: float | None) -> str:
    if value is None:  # a row whose run did not say
        return "n/a"
    return f"{value:.2f} ms" if value < 10 else f"{value:,.0f} ms"


def _errors(item: Mapping[str, Any]) -> str:
    codes = item.get("error_codes") or {}
    detail = ", ".join(f"{code} {count}" for code, count in sorted(codes.items()))
    return f"{item['errors']} ({detail})" if detail else str(item["errors"])
