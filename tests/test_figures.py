# SPDX-License-Identifier: Apache-2.0
"""Figures as served (mendari.evals.figures, LP-10): each share of cases the dashboard and the
Hub show is the k of n the card shows, its interval worked out from k and n (never a stored
bound); what is not known is null, never a guessed n; a malformed board or registry version
serves no figures instead of failing a page."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mendari.core.card import benchmark, render_card
from mendari.evals import figures, leaderboard, ranking, stats
from mendari.evals.render import shown
from tests.conftest import CHART_DIR, CLINICAL_DIR, ROOT, TRIAGE_DIR
from tests.support.engine import published

# The test fixtures' agents, each published in a test with its own benchmark (`published`).
FIXTURES = {"triage": TRIAGE_DIR, "clinical-review": CLINICAL_DIR, "chart-summary": CHART_DIR}


def card_figures(f: dict[str, Any]) -> Iterator[dict[str, Any] | None]:
    """Every figure of `figures.report` that the card shows."""
    yield f["accuracy"]
    yield from f["outcomes"].values()
    yield f["automated_correct"]
    yield f["false_automation"]
    yield f["expected_override"]
    yield from (c["detected"] for c in f["critical"])
    for label in f["per_label"].values():
        yield label["precision"]
        yield label["recall"]
    for values in f["subgroups"].values():
        for group in values.values():
            yield group["accuracy"]
            yield group["automation"]
            yield group["abstention"]
    yield from f["calibration"]
    for row in f["sweep"]:
        yield row["automated"]
        yield row["correct"]


def test_a_figure_is_k_of_n_with_its_exact_interval_unrounded() -> None:
    figure = figures.of(596, 600)
    exact = stats.clopper_pearson(596, 600)
    assert (figure["low"], figure["high"]) == exact  # never the stored 6 places
    assert figures.of(0, 0) == {"k": 0, "n": 0, "low": None, "high": None}
    for k, n in ((True, 1), (1.0, 2), (3, 2), (-1, 2), (1, 10**9 + 1), ("1", 2)):
        with pytest.raises(ValueError):
            figures.of(k, n)
    assert figures.maybe(3, 2) is None


def test_a_rate_is_k_of_n_only_when_it_is_a_whole_number_of_cases() -> None:
    assert figures.counted(0.75, 28) == figures.of(21, 28)
    assert figures.counted(None, 0) == figures.of(0, 0)
    for rate, n in (
        (0.999, 7),  # not a share of 7 cases
        (1.5, 10),
        (-0.1, 10),
        (float("nan"), 10),
        (True, 10),
        (0.5, -1),
        (0.5, 10**400),
        (0.5, None),
        (None, 10),
        (0.5, 2.0),
    ):
        assert figures.counted(rate, n) is None, (rate, n)


def test_figures_never_format_a_stored_bound() -> None:
    """A bound stored to 6 places can print a different interval: 12 of 31 is [21.8-57.8%],
    while its stored lower bound, 0.219..., would print 21.9."""
    stored = stats.proportion(12, 31).to_dict()
    assert shown(stored) == shown(figures.of(12, 31)) == "12 of 31 [21.8-57.8%]"
    assert shown({**stored, "low": 0.5, "high": 0.6}) == "12 of 31 [21.8-57.8%]"


@pytest.mark.parametrize("name", FIXTURES)
def test_report_figures_are_the_ones_the_card_shows(name: str, tmp_path: Path) -> None:
    """Each served figure is the one on the card's own row for it: the same k of n over the
    same denominator, never merely text that appears somewhere on the card."""
    agent = published(FIXTURES[name], tmp_path)
    report = benchmark(agent)
    assert report is not None
    lines = render_card(agent).splitlines()
    worked = figures.report(report)
    assert all(figure is not None for figure in card_figures(worked))  # all counts are stored

    def row(*cells: str) -> None:
        line = "| " + " | ".join(cells) + " |"
        assert line in lines, line

    primary = report["primary"]
    row(f"`{primary}` accuracy", shown(worked["accuracy"]))
    for outcome in report["decisions"]["outcome_counts"]:
        row(outcome, shown(worked["outcomes"][outcome]))
    row("AUTO decisions correct", shown(worked["automated_correct"]))
    row("False automation (of labelled cases)", shown(worked["false_automation"]))
    row("Expected human override (of reviewed)", shown(worked["expected_override"]))
    for c in worked["critical"]:
        line = f"| Critical `{c['question']}={c['label']}` | {shown(c['detected'])} detected, "
        assert any(x.startswith(line) for x in lines), line
    for label, f in worked["per_label"].items():
        f1 = report["questions"][primary]["per_label"][label]["f1"]
        row(f"`{label}`", shown(f["precision"]), shown(f["recall"]), f"{f1:.3f}")
    for attribute, values in worked["subgroups"].items():
        for value, f in values.items():
            members = report["subgroups"][attribute][value]["n"]
            figures_shown = (shown(f["accuracy"]), shown(f["automation"]), shown(f["abstention"]))
            row(f"{attribute}={value}", str(members), *figures_shown)
    bins = report["questions"][primary]["calibration"]["bins"]
    for b, f in zip(bins, worked["calibration"], strict=True):
        row(f"{b['low']:.1f} to {b['high']:.1f}", f"{b['confidence']:.3f}", shown(f))
    for t, f in zip(report["threshold_sweep"], worked["sweep"], strict=True):
        row(f"{t['threshold']:.2f}", shown(f["automated"]), shown(f["correct"]))


def test_a_rate_beyond_any_share_is_never_multiplied() -> None:
    """A report can be someone else's file: 1e308 x 600 is infinite, and rounding it raised,
    which made every registry read of an agent, and search, a server error."""
    from mendari.evals.result import counted as result_counted

    for rate in (1e308, -1e308, 1.0000001):
        assert figures.counted(rate, 600) is None and result_counted(rate, 2) is None
    version = {"benchmark": {"n": 600, "accuracy": 1e308}, "leaderboard": {"rows": [
        {"config": "a", "dataset": "t", "n": 5, "critical": [], "auto_rate": 1e308,
         "questions": {"other": {"n": 5, "accuracy": 1e308, "ece": 0.1}}}]}}  # fmt: skip
    served = figures.registry(version)
    assert served["benchmark"] is None
    (row,) = served["leaderboard"]["rows"]
    assert row["figures"]["automation"] is None and row["figures"]["questions"] == {"other": None}


def test_a_registry_leaderboard_too_large_to_work_out_on_every_read_is_none() -> None:
    rows = [{"config": "a", "dataset": "d", "n": i + 1, "critical": []} for i in range(16500)]
    started = time.monotonic()
    served = figures.registry({"leaderboard": {"rows": rows}, "digest": None, "version": "1"})
    assert served["leaderboard"] is None and time.monotonic() - started < 0.2
    small = figures.board({"rows": rows[:2000]}, None, "1")  # the groups are counted once
    assert len(small["groups"]) == 2000


def test_comparison_figures_say_what_they_are_of() -> None:
    comparison = {
        "status": "degraded",
        "baseline": {"accuracy": 0.99, "n": 600},
        "local": {"accuracy": 0.8, "n": 300},
        "findings": [
            {"kind": "critical", "local": 2, "n": 20, "baseline": 0},
            {"kind": "calibration", "local": 0.2, "baseline": 0.02, "n": 300},
            {"kind": "subgroup", "baseline": 0.99, "local": 0.5, "n": 40, "local_n": 40,
             "baseline_n": 600, "baseline_scope": "overall"},
            # Stored before LP-10b: no counts. Recall's `n` is its local support, a subgroup's
            # `n` its members (not its labelled ones), so that local side stays n/a.
            {"kind": "label_recall", "baseline": 1.0, "local": 0.5, "n": 20},
            {"kind": "subgroup", "baseline": 1.0, "local": 0.5, "n": 20},
        ],
    }  # fmt: skip
    worked = figures.comparison(comparison)
    assert worked is not None
    assert shown(worked["baseline"]) == shown(figures.of(594, 600))
    critical, calibration, overall, recall, subgroup = worked["findings"]
    assert critical == {"automated": figures.of(2, 20), "insufficient_evidence": True}
    assert calibration is None
    assert overall["overall"] is True and overall["local"] == figures.of(20, 40)
    assert recall["baseline"] is None and recall["local"] == figures.of(10, 20)
    assert subgroup["local"] is None
    assert figures.comparison({"status": "unknown", "reason": "x", "findings": []}) is None


def rows_on(digest: str, sha: str = "d" * 64) -> list[dict[str, Any]]:
    base = {
        "dataset": "test", "dataset_sha256": sha, "n": 600, "agent_digest": digest,
        "agent_version": "1.0.0", "auto_rate": 0.0, "cost_per_decision_usd": 0.0, "errors": 0,
        "macro_f1": 0.9, "ece": 0.02, "latency_p50_ms": 1.0,
        "false_automation": stats.proportion(0, 600).to_dict(),
    }  # fmt: skip
    return [
        {**base, "config": config, "accuracy_proportion": stats.proportion(k, 600).to_dict(),
         "critical": [{"question": "q", "label": "yes", "n": 120, "detected": detected,
                       "auto_missed": 0}]}
        for config, k, detected in (("a", 590, 118), ("b", 599, 110), ("c", 560, 120))
    ]  # fmt: skip


def test_a_critical_figure_is_named_when_the_agent_has_several_labels() -> None:
    """As the card names it (leaderboard.critical_text): by the labels the agent has, not by
    those with positives on these records."""
    digest = "sha256:" + "a" * 64
    row = rows_on(digest)[0]
    two = [
        *row["critical"],
        {"question": "q", "label": "no", "n": 0, "detected": 0, "auto_missed": 0},
    ]
    served = figures.board({"rows": [{**row, "critical": two}]}, digest, "1.0.0")["rows"][0]
    assert served["figures"]["critical_named"] is True and len(served["figures"]["critical"]) == 1
    card = leaderboard.critical_text({**row, "critical": two})
    assert card.startswith("q=yes ")  # named on the card too
    alone = figures.board({"rows": [row]}, digest, "1.0.0")["rows"][0]["figures"]
    assert alone["critical_named"] is False


def test_a_current_board_with_enough_positives_ranks() -> None:
    digest = "sha256:" + "a" * 64
    rows = [*rows_on(digest), {**rows_on(digest, "e" * 64)[0], "config": "elsewhere"}]
    served = figures.board({"rows": rows}, digest, "1.0.0")
    assert [(r["config"], r["rank"], r["group"]) for r in served["rows"]] == [
        ("c", 1, 0),
        ("a", 2, 0),
        ("b", 3, 0),
        ("elsewhere", 1, 1),  # the same name on other records: its own set, its own ranking
    ]
    labels = [g["label"] for g in served["groups"]]
    assert labels == ["test n=600 (dddddddd)", "test n=600 (eeeeeeee)"]
    assert served["groups"][0]["about"] == ["n=600", "records sha256:dddddddddddd…"]


def test_a_stored_bound_is_never_trusted_for_the_ranking() -> None:
    """A board can come from a registry upload: a forged bound must not buy a rank."""
    digest = "sha256:" + "a" * 64
    honest, forged = rows_on(digest)[:2]
    forged = {**forged, "critical": honest["critical"], "config": "forged"}
    forged["accuracy_proportion"] = {"k": 500, "n": 600, "low": 0.999, "high": 1.0}
    ranked = ranking.rank([forged, honest], digest)
    assert [r["config"] for r in ranked] == ["a", "forged"]


@pytest.mark.parametrize(
    "rows",
    [
        [1],
        [{"config": "a"}],
        [{"config": "a", "dataset": "t", "n": "x", "critical": []}],
        [{"config": "a", "dataset": "t", "n": 10**12, "critical": []}],
        [{"config": "a", "dataset": "t", "n": 5, "critical": [
            {"question": "q", "label": "y", "n": 5, "detected": 6, "auto_missed": 0}]}],
        [{"config": "a", "dataset": "t", "n": 5, "critical": [
            {"question": "q", "label": "y", "n": 5, "detected": True, "auto_missed": 0}]}],
        [{"config": "a", "dataset": "t", "n": 5, "critical": [
            {"question": "q", "label": "y", "n": 5, "auto_missed": 0}]}],
        [{"config": "a", "dataset": "t", "n": 5, "critical": [],
          "accuracy_proportion": {"k": 9, "n": 5}}],
    ],
    ids=["number", "no-dataset", "n-text", "n-huge", "detected-over-n", "bool", "no-detected",
         "accuracy-over-n"],
)  # fmt: skip
def test_a_malformed_board_is_refused_and_the_registry_shows_none(rows: list[Any]) -> None:
    started = time.monotonic()
    with pytest.raises((TypeError, ValueError)):
        figures.board({"rows": rows}, None, None)
    served = figures.registry({"version": "1.0.0", "digest": None, "leaderboard": {"rows": rows}})
    assert served["leaderboard"] is None and time.monotonic() - started < 1.0


def test_an_unknown_cost_or_latency_is_not_malformed() -> None:
    digest = "sha256:" + "a" * 64
    row = {**rows_on(digest)[0], "cost_per_decision_usd": None, "latency_p50_ms": None}
    served = figures.board({"rows": [row]}, digest, "1.0.0")
    assert served["rows"][0]["rank"] == 1
    assert "| unknown |" in "\n".join(leaderboard.markdown({"rows": [row]}, digest, "1.0.0"))


def test_a_registry_version_has_its_benchmark_figure() -> None:
    version = {"version": "1.0.0", "digest": None, "benchmark": {"n": 600, "accuracy": 0.995}}
    assert figures.registry(version)["benchmark"] == figures.of(597, 600)
    version["benchmark"] = {"n": 7, "accuracy": 0.999}  # not a share of 7 cases
    assert figures.registry(version) == {
        "format": figures.FORMAT,
        "benchmark": None,
        "leaderboard": None,
    }


def test_periods_are_the_days_the_overview_shows() -> None:
    def day(when: str, auto: int, review: int, labelled: int = 0, agreed: int = 0) -> Any:
        outcomes = {"AUTO": auto, "HUMAN_REVIEW": review, "ABSTAIN": 0, "BLOCK": 0}
        reviews = {"reviewed": review, "overridden": 0, "labelled": labelled, "agreed": agreed}
        return {"date": when, **outcomes, **reviews}

    daily = [day("2026-09-01", 1, 1), day("2026-09-28", 2, 2, 2, 1), day("2026-10-07", 3, 1, 1, 1)]
    out = figures.periods(daily, date(2026, 10, 7))
    week, month, ever = out["7"], out["30"], out["all"]
    assert week["from"] == "2026-10-01" and week["now"]["decisions"] == 4
    assert week["now"]["figures"]["automated"] == figures.of(3, 4)
    assert week["before"]["decisions"] == 4  # 2026-09-24 to 2026-09-30
    assert week["before"]["figures"]["accuracy"] == figures.of(1, 2)
    assert month["from"] == "2026-09-08" and month["now"]["decisions"] == 8
    assert month["before"]["decisions"] == 2  # 2026-08-09 to 2026-09-07
    assert ever["before"] is None and ever["now"]["decisions"] == 10
    assert ever["now"]["figures"]["outcomes"]["HUMAN_REVIEW"] == figures.of(4, 10)
    empty = figures.periods([], date(2026, 10, 7))["7"]["now"]["figures"]
    assert empty["automated"] == figures.of(0, 0)


def test_the_shown_fixture_is_what_render_shows() -> None:
    """tests/fixtures/shown.json, which the ports of `render.shown` are checked against, holds
    the cases scripts/shown_fixture.py generates and what render prints for them on this
    platform; its bounds are within 1e-12 of this platform's (their last bits are the maths
    library's), and print the same."""
    module = _fixture_script()
    assert module.problems(Path(module.FIXTURE).read_text()) == []


def test_the_fixture_check_finds_a_wrong_text_or_bound() -> None:
    module = _fixture_script()
    data = json.loads(Path(module.FIXTURE).read_text())
    data["shown"][100][4] = "1 of 2 [0.0-100.0%]"
    data["shown"][200][2] = data["shown"][200][2] * 1.001 if data["shown"][200][2] else 0.5
    data["percent"][5][1] = "99.9"
    found = module.problems(json.dumps(data))
    assert len(found) == 3, found


def _fixture_script() -> Any:
    import importlib.util

    path = ROOT / "scripts" / "shown_fixture.py"
    spec = importlib.util.spec_from_file_location("shown_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
