# SPDX-License-Identifier: Apache-2.0
"""The one ranking (mendari.evals.ranking, LP-10): critical cases first, never accuracy alone;
only the pack as it is ranks; too few critical positives rank nothing; and every proportion a
person reads is k of n with its interval."""

from __future__ import annotations

from typing import Any

from mendari import Decision, Pack
from mendari.core.registry import resolve_pack
from mendari.evals import ranking, stats
from mendari.evals.harness import evaluate, resolve_dataset
from mendari.evals.render import report_text, shown

DIGEST = "sha256:" + "a" * 64


def row(config: str, **changes: Any) -> dict[str, Any]:
    base = {
        "config": config,
        "dataset": "test",
        "dataset_sha256": "d" * 64,
        "n": 600,
        "pack_digest": DIGEST,
        "pack_version": "1.0.0",
        "critical": [
            {"question": "q", "label": "yes", "n": 120, "detected": 118, "auto_missed": 0}
        ],
        "false_automation": stats.proportion(1, 600).to_dict(),
        "accuracy_proportion": stats.proportion(570, 600).to_dict(),
        "cost_per_decision_usd": 0.0,
    }
    return {**base, **changes}


def critical(detected: int, auto_missed: int = 0, n: int = 120) -> list[dict[str, Any]]:
    return [
        {"question": "q", "label": "yes", "n": n, "detected": detected, "auto_missed": auto_missed}
    ]


def order(rows: list[dict[str, Any]]) -> list[tuple[str, int | None, str]]:
    return [(r["config"], r["rank"], r["standing"]) for r in ranking.rank(rows, DIGEST)]


def test_critical_cases_come_before_accuracy() -> None:
    accurate = row(
        "accurate", critical=critical(110), accuracy_proportion=stats.proportion(599, 600).to_dict()
    )
    careful = row(
        "careful", critical=critical(120), accuracy_proportion=stats.proportion(500, 600).to_dict()
    )
    automated = row("automated", critical=critical(120, auto_missed=1))
    assert order([accurate, automated, careful]) == [
        ("careful", 1, "ranked"),
        ("accurate", 2, "ranked"),
        ("automated", 3, "ranked"),  # a critical case automated: last, whatever else
    ]


def test_then_false_automation_accuracy_cost_and_name() -> None:
    risky = row("risky", false_automation=stats.proportion(20, 600).to_dict())
    cheap = row("cheap", cost_per_decision_usd=0.0)
    dear = row("dear", cost_per_decision_usd=0.01)
    twin = row("twin", cost_per_decision_usd=0.0)
    assert [c for c, _, _ in order([risky, dear, twin, cheap])] == [
        "cheap",
        "twin",
        "dear",
        "risky",
    ]


def test_only_the_pack_as_it_is_ranks() -> None:
    old = row("old", pack_digest="sha256:" + "b" * 64, pack_version="0.9.0", critical=critical(120))
    unknown = row("v1-row", pack_digest=None)
    current = row("current", critical=critical(100))
    assert order([old, unknown, current]) == [
        ("current", 1, "ranked"),
        ("old", None, "earlier version"),
        ("v1-row", None, "earlier version"),
    ]


def test_too_few_critical_positives_rank_nothing() -> None:
    small = [row(name, critical=critical(20, n=20)) for name in ("b", "a")]
    assert order(small) == [
        ("a", None, "insufficient evidence"),
        ("b", None, "insufficient evidence"),
    ]


def test_rows_on_other_records_rank_apart() -> None:
    other = row("other", dataset_sha256="e" * 64)
    assert {(c, r) for c, r, _ in order([row("here"), other])} == {("here", 1), ("other", 1)}


def test_twenty_emergency_positives_say_insufficient_evidence(inbox_pack: Pack) -> None:
    report = evaluate(Decision(inbox_pack), resolve_dataset(inbox_pack, "red_flags_holdout"))
    text = report_text(report)
    (item,) = report["decisions"]["critical"]
    assert item["n"] == 20
    assert "(insufficient evidence: under 100 positives)" in text
    assert "%" not in text.replace("%]", "]").replace("% ", " ")  # no bare percentage


def test_a_proportion_is_shown_with_its_interval() -> None:
    assert shown(stats.proportion(596, 600)) == "596 of 600 [98.3-99.8%]"
    assert shown(stats.proportion(598, 600)) == "598 of 600 [98.8-99.96%]"  # never "100.0"
    assert shown(stats.proportion(1, 5000)) == "1 of 5000 [0.001-0.1%]"  # never "0.0"
    assert shown(stats.proportion(0, 0)) == "no cases" and shown(None) == "n/a"
    assert shown({"k": 3, "n": 10, "low": 0.06674, "high": 0.652453}) == "3 of 10 [6.7-65.2%]"


def test_the_reference_cards_hold_no_bare_percentage() -> None:
    """Every proportion on a reference pack's card is k of n with its interval; what else is a
    percentage there is a threshold or a tolerance, never a share of cases."""
    from mendari.core.card import render_card

    for name in ("patient-inbox-router", "referral-router"):
        card = render_card(resolve_pack(f"mendari/{name}"))
        validation = card[card.index("Benchmark `") : card.index("Calibration (reliability)")]
        for line in validation.splitlines():
            if line.startswith("|") and "%" in line:
                assert " of " in line or "no cases" in line, line
