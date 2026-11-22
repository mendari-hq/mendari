# SPDX-License-Identifier: Apache-2.0
"""The one ranking (mendari.evals.ranking, LP-10): critical cases first, never accuracy alone;
only the pack as it is ranks; too few critical positives rank nothing; and every proportion a
person reads is k of n with its interval."""

from __future__ import annotations

import re
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
        # What the table shows besides (mendari.evals.leaderboard.markdown).
        "auto_rate": 0.0,
        "macro_f1": 0.9,
        "ece": 0.02,
        "latency_p50_ms": 1.0,
        "errors": 0,
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


REFERENCE_PACKS = (
    "ai-guard",
    "clinical-trial-prescreener",
    "patient-inbox-router",
    "prior-auth-preflight",
    "referral-router",
)


def printed_ranks(text: str) -> list[str]:
    """The lines of `text` that print a rank: a Rank column, a row whose first cell is a rank
    or why it has none ("none: ..."), or the words "Ranked", "rank", "ranks" or "unranked"."""
    return [
        line
        for line in text.splitlines()
        if re.match(r"\| (Rank|\d+|none: [^|]*) \|", line)
        or re.search(r"\b(Ranked|ranked|ranks?|unranked)\b", line)
    ]


def test_no_card_or_board_prints_a_rank_for_a_public_set() -> None:
    """A pack's own sets are public, and a board can also hold a site's own file, never a
    frozen hidden set, so each of its tables is a regression check, not a ranking (LP-17g):
    neither the five reference cards (as rendered, and as committed) nor the command line's
    tables print a rank. The rows keep theirs as data, and are listed in their order."""
    from mendari.core.card import render_card
    from mendari.evals import leaderboard

    for name in REFERENCE_PACKS:
        pack = resolve_pack(f"mendari/{name}")
        for card in (render_card(pack), (pack.root / "DECISION_CARD.md").read_text()):
            section = card[card.index("## Provider comparison") : card.index("## Known failure")]
            assert section.count("Public set: regression check, not a ranking.") == 1, name
            assert "| Configuration | Critical detected |" in section, name
            assert printed_ranks(section) == [], name

    # Rows that rank as data (on the pack's digest, with enough critical positives).
    rows = [
        row("b-second", cost_per_decision_usd=0.01),
        row("a-first"),
        row("c-third", cost_per_decision_usd=0.02),
    ]
    board = {"rows": ranking.rank(rows, DIGEST)}
    assert [(r["config"], r["rank"]) for r in board["rows"]] == [
        ("a-first", 1),
        ("b-second", 2),
        ("c-third", 3),
    ]
    table = "\n".join(leaderboard.markdown(board, DIGEST, "1.0.0"))
    assert printed_ranks(table) == []
    listed = [line.split(" | ")[0] for line in table.splitlines() if line.startswith("| ")][1:]
    assert listed == ["| a-first", "| b-second", "| c-third"]  # in the order, unnumbered


def test_the_card_says_the_order_its_rows_are_in() -> None:
    """The rows are listed as `ranking.grouped` lists them: those that rank as data first, in
    their order, then every other row by configuration name. The card says just that, and never
    that its rows come critical cases first: every row of the five reference boards is of an
    earlier version of its pack, so each of their tables is in order of configuration name."""
    from mendari.core.card import render_card
    from mendari.evals import leaderboard

    for name in REFERENCE_PACKS:
        pack = resolve_pack(f"mendari/{name}")
        board, problem = leaderboard.read(pack)
        assert board is not None, problem
        for _, rows in ranking.grouped(board["rows"], pack.digest):
            assert {r["standing"] for r in rows} == {ranking.EARLIER}, name
            configs = [r["config"] for r in rows]
            assert configs == sorted(configs), name
        card = render_card(pack)
        assert leaderboard.preface() in card, name
        assert "every other row follows, by configuration name" in card, name
        assert "rows are listed with the critical cases caught first" not in card, name

    # Ranked rows first in their order, then the rest by name, whatever the names.
    rows = [
        row("a-earlier", pack_digest="sha256:" + "b" * 64),
        row("z-second", cost_per_decision_usd=0.01),
        row("m-first"),
    ]
    assert order(rows) == [
        ("m-first", 1, ranking.RANKED),
        ("z-second", 2, ranking.RANKED),
        ("a-earlier", None, ranking.EARLIER),
    ]


def test_a_bound_too_close_to_print_never_reads_0_or_100() -> None:
    assert shown(stats.proportion(1, 60000)) == "1 of 60000 [<0.0001-0.01%]"
    assert shown(stats.proportion(59999, 60000)) == "59999 of 60000 [99.99->99.9999%]"
    assert shown(stats.proportion(0, 60000)).startswith("0 of 60000 [0.0-")  # truly 0
    assert shown(stats.proportion(60000, 60000)).endswith("-100.0%]")  # truly 100


def test_each_table_is_one_set_of_records() -> None:
    """A dataset at two sizes, or without its hash recorded, is two sets of records: each its
    own table and its own ranking, never two rank 1s under one heading."""
    from mendari.evals.leaderboard import markdown

    full = [row("a-full"), row("b-full", cost_per_decision_usd=0.01)]
    first = [row(c, n=100, limit=100) for c in ("a-first", "b-first")]
    old = row("old", pack_digest=None, dataset_sha256=None)
    board = {"rows": [old, *first, *full]}
    groups = ranking.grouped(board["rows"], DIGEST)
    assert [(name, n, bool(sha)) for (name, sha, n), _ in groups] == [
        ("test", 600, True),
        ("test", 600, False),
        ("test", 100, True),
    ]
    assert [[r["rank"] for r in rows] for _, rows in groups] == [[1, 2], [None], [1, 2]]
    tables = "\n".join(markdown(board, DIGEST, "1.0.0")).split("**`test`**")[1:]
    assert len(tables) == 3
    assert tables[1].startswith(" (n=600; dataset hash not recorded;")
    assert tables[2].startswith(" (n=100; the first 100 cases of the file;")
    assert "| old (version 1.0.0, its pack digest not recorded) |" in tables[1]


def test_unranked_rows_come_after_the_ranked_whatever_their_names() -> None:
    late = row("zz-current")
    earlier = row("aa-earlier", pack_digest="sha256:" + "0" * 64, pack_version="0.9.0")
    assert order([earlier, late]) == [
        ("zz-current", 1, "ranked"),
        ("aa-earlier", None, "earlier version"),
    ]


def test_why_a_row_is_not_of_the_pack_as_it_is() -> None:
    assert ranking.measured_on("0.9.0", DIGEST, "1.0.0") == "earlier version 0.9.0"
    assert ranking.measured_on("1.0.0", DIGEST, "1.0.0") == "version 1.0.0 before its last change"
    assert ranking.measured_on("1.0.0", None, "1.0.0") == (
        "version 1.0.0, its pack digest not recorded"
    )


def test_a_row_from_leaderboard_1_has_no_accuracy_as_k_of_n() -> None:
    """A v1 row stored its accuracy over the cases labelled for the primary question, and not
    how many: on red_flags (38 cases, 28 labelled) 0.75 is 21 of 28, never "28 of 38"."""
    from mendari.evals.leaderboard import markdown

    v2_only = ("accuracy_proportion", "false_automation")
    v1 = {k: v for k, v in row("v1").items() if k not in v2_only}
    v1.update(dataset="red_flags", n=38, accuracy=0.75, pack_digest=None, dataset_sha256=None)
    v1.update(critical=[])
    table = "\n".join(markdown({"rows": [v1]}, DIGEST, "1.0.0"))
    (line,) = [x for x in table.splitlines() if x.startswith("| v1 ")]
    assert "28 of 38" not in line and "| n/a | n/a | 0 of 38" in line
    assert "n/a on a row recorded before leaderboards kept the counts" in table


def test_an_unknown_cost_is_said_not_a_crash() -> None:
    from mendari.evals.leaderboard import markdown

    unknown = row("remote", cost_per_decision_usd=None)
    table = "\n".join(markdown({"rows": [unknown]}, DIGEST, "1.0.0"))
    assert "| unknown |" in table
