# SPDX-License-Identifier: Apache-2.0
"""Scoreboard entries (`mendari.scoreboard/1`, LP-17d, `evals/scoreboard.py`) and run records
(`mendari.run/1`, `evals/runs.py`): an entry is a pure function of its ledger, dataset, run
record, price table and method; evidence is decided on distinct positives, every critical label
needing enough; the grade's bound is simultaneous and over distinct groups; every figure is k of
n with its interval, or a mean with its bootstrap interval, and ECE a description; cost comes
from tokens and a dated table, latency from the run record; a public set is a regression check;
and freshness is worked out when a board is built, never stored."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from scipy.stats import beta
from statsmodels.stats.proportion import proportion_confint

from mendari import Decision, Pack, __version__
from mendari.core.decision import DecisionResult
from mendari.core.registry import resolve_pack
from mendari.core.status import SYNTHETIC_EXAMPLE
from mendari.errors import MendariError
from mendari.evals import arms, method, runs, scoreboard, stats
from mendari.evals import ledger as ledgers
from mendari.evals.coverage import coverage
from mendari.evals.harness import Dataset, load_dataset, resolve_dataset
from mendari.evals.ledger import Ledger, canonical
from mendari.evals.render import scoreboard_markdown
from mendari.evals.result import ERROR, PREDICTED
from tests.conftest import ROOT, calm_jev

DATE = datetime.date(2026, 10, 18)
INBOX = "mendari/patient-inbox-router"
GUARD = "mendari/ai-guard"
REFERRAL = "mendari/referral-router"
PRESCREENER = "mendari/clinical-trial-prescreener"
PROPORTION = {"k", "n", "low", "high"}
MEAN = {"estimate", "low", "high"}


@dataclass(frozen=True)
class Built:
    """One run of one configuration over one set, and what an entry is made from."""

    pack: Pack
    dataset: Dataset
    decision: Decision
    results: list[DecisionResult]
    ledger: Ledger
    run: dict[str, Any]
    configuration: arms.Configuration

    def entry(self, **changes: Any) -> dict[str, Any]:
        """The entry of this run, with any input changed."""
        inputs: dict[str, Any] = {
            "ledger": self.ledger,
            "dataset": self.dataset,
            "run": self.run,
            "pack": scoreboard.PackScope.of(self.pack),
            "arm": self.configuration.arm,
            "configuration": self.configuration,
            "prices": None,
            "disclosures": None,
            **changes,
        }
        return scoreboard.entry(
            inputs.pop("ledger"), inputs.pop("dataset"), inputs.pop("run"), **inputs
        )


def build(ref: str, dataset: str | Dataset, providers: dict[str, str] | None = None) -> Built:
    pack = resolve_pack(ref)
    found = resolve_dataset(pack, dataset) if isinstance(dataset, str) else dataset
    decision = Decision(pack, providers=providers)
    results = [decision.decide(record["input"]) for record in found.records]
    ledger = ledgers.build(decision, found, results)
    run = runs.record(decision, ledger, results, date=DATE)
    variants = {slot: info.variant for slot, info in decision.provider_info.items()}
    (configuration,) = [c for c in arms.configurations(pack) if c.providers == variants]
    return Built(pack, found, decision, results, ledger, run, configuration)


def run_for(ledger: Ledger, like: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """`like`, made the run record of `ledger` (another ledger's id), with `changes`."""
    return runs.check_run({**like, "ledger": ledger.id, **changes})


def edited(ledger: Ledger, edit: Callable[[list[dict[str, Any]]], None]) -> Ledger:
    """A copy of `ledger` whose case lines `edit` changes (a test's own ledger)."""
    cases = json.loads(json.dumps(ledger.cases))
    edit(cases)
    return Ledger(json.loads(json.dumps(ledger.header)), cases)


def written(tmp_path: Path, name: str, records: list[dict[str, Any]]) -> Dataset:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return load_dataset(path)


def records_of(ref: str, name: str) -> list[dict[str, Any]]:
    return [dict(record) for record in resolve_dataset(resolve_pack(ref), name).records]


def price_table(tmp_path: Path, prices: dict[str, Any], date: str = "2026-10-18") -> runs.Prices:
    path = tmp_path / "prices" / f"{date}.json"
    path.parent.mkdir(exist_ok=True)
    table = {"format": runs.PRICES_FORMAT, "date": date, "source": "a test", "prices": prices}
    path.write_text(json.dumps(table, indent=2))
    return runs.load_prices(path)


def stated(**statements: str) -> runs.Disclosures:
    data = {"format": runs.DISCLOSURES_FORMAT, "date": "2026-10-18", "statements": statements}
    return runs.disclosures(json.dumps(data).encode())


def bare_percentages(text: str) -> list[str]:
    """What in `text` is a percentage outside an interval: bracketed intervals ("[5.7-43.7%]",
    "[exact 95% interval, ...]") and the grade's bound ("lower bound X% at Y%") are not."""
    kept = re.sub(r"\[[^\[\]]*\]", "", text)
    kept = re.sub(r"lower bound \S+% at \S+%", "", kept)
    return [line for line in kept.splitlines() if "%" in line]


@pytest.fixture(scope="module")
def holdout() -> Built:
    """The inbox's red-flag holdout: 20 urgent positives, run locally."""
    return build(INBOX, "red_flags_holdout")


@pytest.fixture(scope="module")
def guard_test() -> tuple[Built, dict[str, Any]]:
    """AI Guard's 600-case test set (81 distinct inputs), and its entry."""
    built = build(GUARD, "test")
    return built, built.entry()


def test_a_rebuild_from_the_same_inputs_is_byte_identical(holdout: Built) -> None:
    first = holdout.entry()
    again = build(INBOX, "red_flags_holdout")
    assert again.ledger.dumps() == holdout.ledger.dumps()  # a local configuration re-runs alike
    second = again.entry(run=holdout.run)  # latency is an input, never measured again
    assert canonical(second).encode() == canonical(first).encode()
    assert first["body_sha256"] == scoreboard.body_sha256(first)
    assert (
        scoreboard.body_sha256({**first, "signature": {"key_id": "k", "value": "v"}})
        == (first["body_sha256"])
    )
    assert first["inputs"] == {
        "ledger": holdout.ledger.id,
        "dataset": holdout.dataset.sha256,
        "run": {"sha256": runs.run_sha256(holdout.run), "replicate": 1, "engine": __version__},
        "prices": None,
        "method": method.METHOD_SHA256,
    }
    assert first["seeds"] == {"bootstrap": 0, "resamples": 2000, "level": 0.95}


def test_twenty_emergency_positives_show_insufficient_evidence(holdout: Built) -> None:
    found = holdout.entry()
    (urgent,) = found["figures"]["critical"]
    assert (urgent["question"], urgent["label"], urgent["detected"]["n"]) == (
        "urgency",
        "urgent",
        20,
    )
    assert urgent["distinct"]["positives"] == 20
    assert urgent["evidence"] == {"status": "insufficient evidence", "needed": 100}
    shown_board = scoreboard.board([found], {INBOX: scoreboard.Current.of(holdout.pack)})
    (table,) = shown_board["tables"]
    assert table["banner"] == {
        "short": [
            {
                "question": "urgency",
                "label": "urgent",
                "positives": 20,
                "needed": 100,
                "configuration": "safety_screen=rules,router=naive_bayes",
            }
        ]
    }
    text = scoreboard_markdown(shown_board)
    assert "**Insufficient evidence.**" in text
    assert "urgency=urgent, 20 distinct positives (needs 100)" in text
    assert re.search(
        r"20 distinct positives, \d+ detected; insufficient evidence \(needs 100\)", text
    )
    assert bare_percentages(text) == []


def test_evidence_counts_distinct_positives_never_raw(guard_test: tuple[Built, dict]) -> None:
    built, found = guard_test
    critical = {c["question"]: c for c in found["figures"]["critical"]}
    block = critical["verdict"]
    assert block["label"] == "block"
    assert (block["detected"]["n"], block["distinct"]["positives"]) == (164, 30)
    assert block["evidence"]["status"] == "insufficient evidence"
    # 114 raw positives are over 100, and still too few: they are 39 inputs.
    emergency = critical["question_emergency"]
    assert (emergency["detected"]["n"], emergency["distinct"]["positives"]) == (114, 39)
    assert emergency["evidence"]["status"] == "insufficient evidence"
    # The same distinct counts as the coverage report's (norm/v1).
    report = coverage(built.pack.manifest, built.dataset, pack=built.pack.ref)
    assert [c["distinct"] for c in report["critical"]] == [
        c["distinct"]["positives"] for c in found["figures"]["critical"]
    ]
    assert found["scope"]["set"]["distinct_inputs"] == report["distinct"]["normalised"] == 81


def test_every_critical_label_needs_enough_not_just_one(tmp_path: Path) -> None:
    """A referral set with 99 distinct `urgency=urgent` positives and 110 `next_step=
    urgent_review` ones: the second has enough, the first does not, and the banner says so."""
    records = records_of(REFERRAL, "test")

    def positive(record: dict[str, Any], question: str, label: str) -> bool:
        return record.get("labels", {}).get(question) == label

    urgent = [r for r in records if positive(r, "urgency", "urgent")]
    both = [r for r in urgent if positive(r, "next_step", "urgent_review")]
    dropped = {r["id"] for r in both[: len(urgent) - 99]}
    kept = [
        r
        for r in records
        if r["id"] not in dropped
        and (positive(r, "urgency", "urgent") or positive(r, "next_step", "urgent_review"))
    ]
    built = build(REFERRAL, written(tmp_path, "urgent_subset", kept))
    found = built.entry()
    evidence = {
        c["label"]: (c["distinct"]["positives"], c["evidence"]["status"])
        for c in found["figures"]["critical"]
    }
    assert evidence == {
        "urgent": (99, "insufficient evidence"),
        "urgent_review": (110, "sufficient"),
    }
    (table,) = scoreboard.board([found], {})["tables"]
    assert [(s["label"], s["positives"]) for s in table["banner"]["short"]] == [("urgent", 99)]
    # The minimum is the method's n_min at the pack's L, not 100 alone: the prescreener has six
    # critical labels, and needs 107.
    prescreener = build(PRESCREENER, "exclusions_holdout").entry()
    assert {c["evidence"]["needed"] for c in prescreener["figures"]["critical"]} == {107}
    assert method.min_positives(0.95, 6) == 107


@pytest.mark.parametrize(
    ("ref", "name", "labels"),
    [
        (INBOX, "red_flags_holdout", 1),
        (REFERRAL, "urgent_referrals_holdout", 2),
        (GUARD, "unsafe_drafts_holdout", 4),
        (PRESCREENER, "exclusions_holdout", 6),
    ],
)
def test_the_grades_bound_is_simultaneous_and_over_distinct_groups(
    ref: str, name: str, labels: int
) -> None:
    found = build(ref, name).entry()
    alpha = 0.05 / labels
    assert len(found["figures"]["critical"]) == labels
    checked = 0
    for critical in found["figures"]["critical"]:
        bound = critical["bound"]
        assert (bound["level"], bound["labels"]) == (1 - alpha, labels)
        k, n = bound["k"], bound["n"]
        assert (k, n) == (critical["distinct"]["detected"], critical["distinct"]["positives"])
        if not n:
            assert bound["low"] is None and bound["high"] is None
            continue
        low = 0.0 if k == 0 else beta.ppf(alpha / 2, k, n - k + 1)
        high = 1.0 if k == n else beta.ppf(1 - alpha / 2, k + 1, n - k)
        sm_low, sm_high = proportion_confint(k, n, alpha=alpha, method="beta")
        for ours, scipy_value, statsmodels_value in (
            (bound["low"], low, sm_low),
            (bound["high"], high, sm_high),
        ):
            assert math.isclose(ours, scipy_value, abs_tol=5e-7)
            assert math.isclose(ours, statsmodels_value, abs_tol=5e-7)
        checked += 1
    assert checked


def test_a_group_is_detected_only_when_every_positive_case_in_it_is(tmp_path: Path) -> None:
    """Three cases of one blocked input (one written in capitals, which `norm/v1` folds) are one
    distinct group: missing any one of them misses the group, and missing two misses it once."""
    plain = build(GUARD, "unsafe_drafts_holdout")
    records = [dict(record) for record in plain.dataset.records]
    detected = [
        record
        for record, case in zip(records, plain.ledger.cases, strict=True)
        if case["q"]["verdict"][0] == "block" and case["q"]["verdict"][PREDICTED] == "block"
    ]
    blocked = detected[0]
    upper = {**blocked["input"], "draft": blocked["input"]["draft"].upper()}
    records += [{**blocked, "id": "copy-1"}, {**blocked, "id": "copy-2", "input": upper}]
    built = build(GUARD, written(tmp_path, "duplicated", records))
    block = built.entry()["figures"]["critical"][0]
    assert (block["detected"]["n"], block["distinct"]["positives"]) == (11, 9)
    group = [i for i, r in enumerate(records) if r["id"] in (blocked["id"], "copy-1", "copy-2")]
    assert all(built.ledger.cases[i]["q"]["verdict"][PREDICTED] == "block" for i in group)

    def missed(count: int) -> dict[str, Any]:
        def edit(cases: list[dict[str, Any]]) -> None:
            for i in group[:count]:
                cases[i]["q"]["verdict"][PREDICTED] = "pass"

        ledger = edited(built.ledger, edit)
        return built.entry(ledger=ledger, run=run_for(ledger, built.run))["figures"]["critical"][0]

    one, two = missed(1), missed(2)
    assert one["detected"]["k"] == block["detected"]["k"] - 1
    assert one["distinct"]["detected"] == block["distinct"]["detected"] - 1
    assert two["detected"]["k"] == block["detected"]["k"] - 2
    assert two["distinct"]["detected"] == one["distinct"]["detected"]  # one group, missed once
    assert one["bound"]["k"] == one["distinct"]["detected"]


def _walk_figures(found: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every proportion and every mean in an entry's figures."""
    proportions: list[dict[str, Any]] = []
    means: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) >= PROPORTION:
                proportions.append(value)
            elif set(value) >= MEAN:
                means.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(found["figures"])
    return proportions, means


def test_every_figure_is_k_of_n_with_its_interval_or_a_bootstrap_interval(
    holdout: Built, guard_test: tuple[Built, dict]
) -> None:
    _, guard = guard_test
    for found in (holdout.entry(), guard):
        figures = found["figures"]
        proportions, means = _walk_figures(found)
        for p in proportions:
            assert (p["low"] is None) == (p["n"] == 0), p
            if p["n"]:
                assert p["low"] <= p["k"] / p["n"] <= p["high"], p
        for m in means:
            assert m["low"] is not None and m["low"] <= m["estimate"] <= m["high"], m
        for name in ("false_automation", "auto_error_rate", "automation", "accuracy"):
            assert set(figures[name]) >= PROPORTION
        for critical in figures["critical"]:
            for name in ("detected", "automated", "bound"):
                assert set(critical[name]) >= PROPORTION
        for name in ("macro_f1", "brier"):
            assert set(figures[name]) == MEAN
        for name in ("p50", "p95"):
            assert set(figures["latency_ms"][name]) == MEAN
    reliability = guard["figures"]["brier_reliability"]
    assert reliability["status"] == "ok" and set(reliability) >= MEAN
    # The estimate is the statistic on every case, as `result_from_ledger` has it.
    built, _ = guard_test
    from mendari.evals.result import result_from_ledger

    primary = result_from_ledger(built.ledger)["questions"]["verdict"]
    assert guard["figures"]["macro_f1"]["estimate"] == round(primary["macro_f1"], 6)
    assert guard["figures"]["brier"]["estimate"] == round(primary["brier"], 6)
    text = scoreboard_markdown(scoreboard.board([holdout.entry(), guard], {}))
    assert bare_percentages(text) == []
    f1 = guard["figures"]["macro_f1"]
    assert f"| Macro-F1 | {f1['estimate']:.3f} [{f1['low']:.3f}-{f1['high']:.3f}] |" in text


def test_ece_is_described_never_given_an_interval(
    holdout: Built, guard_test: tuple[Built, dict]
) -> None:
    _, guard = guard_test
    ece = guard["figures"]["ece"]
    assert set(ece) == {"status", "value"} and ece["status"] == "ok"
    small = holdout.entry()["figures"]["ece"]
    assert small["status"] == "not_estimable" and "value" not in small and "low" not in small
    assert "need 300" in small["reason"]
    text = scoreboard_markdown(scoreboard.board([guard], {}))
    (line,) = [line for line in text.splitlines() if line.startswith("| ECE")]
    assert line.endswith(f"| {ece['value']:.3f}, a description: no interval |")


@pytest.fixture
def jev_run(monkeypatch: pytest.MonkeyPatch) -> Built:
    """The inbox's red-flag holdout with Jev as its safety screen: a fake Jev that reports
    `jev-9.9.9` and 100 input and 10 output tokens a call."""
    calm_jev(monkeypatch)
    return build(INBOX, "red_flags_holdout", {"safety_screen": "jev"})


def test_cost_per_1000_is_from_tokens_and_the_dated_table_or_unknown(
    jev_run: Built, holdout: Built, tmp_path: Path
) -> None:
    assert all(case["tokens"] == {"jev": [100, 10]} for case in jev_run.run["cases"])
    table = price_table(
        tmp_path, {"jev": {"jev-9.9.9": {"input_per_1000": 0.5, "output_per_1000": 1.5}}}
    )
    found = jev_run.entry(prices=table)
    cost = found["figures"]["cost_per_1000"]
    each = 1000 * (100 / 1000 * 0.5 + 10 / 1000 * 1.5)  # USD per 1,000 decisions
    assert cost == {
        "status": "priced",
        "currency": "USD",
        "estimate": each,
        "low": each,
        "high": each,
    }
    assert found["inputs"]["prices"] == {"sha256": table.sha256, "date": "2026-10-18"}
    other = price_table(tmp_path, {"jev": {"jev-1.0": {"input_per_1000": 1, "output_per_1000": 0}}})
    assert jev_run.entry(prices=other)["figures"]["cost_per_1000"] == {
        "status": "unknown",
        "reason": "jev: its model is not in the price table",
    }

    def failed(cases: list[dict[str, Any]]) -> None:
        cases[0]["q"]["urgency"][ERROR] = "timeout"

    ledger = edited(jev_run.ledger, failed)
    with_error = jev_run.entry(ledger=ledger, run=run_for(ledger, jev_run.run), prices=table)
    assert with_error["figures"]["cost_per_1000"] == {
        "status": "unknown",
        "reason": "a provider call failed",
    }
    local = holdout.entry(prices=table)
    assert local["figures"]["cost_per_1000"] == {"status": "none", "text": "0 (no metered calls)"}
    assert local["inputs"]["prices"] is None  # a table that priced nothing is not cited
    disclosed = jev_run.entry(prices=table, disclosures=stated(jev="No relationship."))
    text = scoreboard_markdown(scoreboard.board([disclosed], {}))
    assert f"| USD {each:.4f} [{each:.4f}-{each:.4f}] |" in text


def test_a_pack_set_or_built_in_price_is_never_used(jev_run: Built, tmp_path: Path) -> None:
    """Jev's built-in price is in the ledger (`u`), and never in the entry: without a table the
    cost is unknown, and with one it is the table's."""
    assert all(case["u"][0] and case["u"][0] > 0 for case in jev_run.ledger.cases)
    assert jev_run.entry()["figures"]["cost_per_1000"] == {
        "status": "unknown",
        "reason": "no price table",
    }
    table = price_table(
        tmp_path, {"jev": {"jev-9.9.9": {"input_per_1000": 2, "output_per_1000": 0}}}
    )
    assert jev_run.entry(prices=table)["figures"]["cost_per_1000"]["estimate"] == 200.0
    # A table is never read from Mendari's code or from a pack's directory.
    for inside in (
        ROOT / "src" / "mendari" / "evals" / "method.py",
        ROOT / "packs" / "ai-guard" / "pack.yaml",
    ):
        with pytest.raises(MendariError, match="never read from Mendari's code or a pack"):
            runs.load_prices(inside)
    pack_dir = tmp_path / "a-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text("id: someone/pack\n")
    (pack_dir / "prices.json").write_text((tmp_path / "prices" / "2026-10-18.json").read_text())
    with pytest.raises(MendariError, match="never read from Mendari's code or a pack"):
        runs.load_prices(pack_dir / "prices.json")


def test_latency_comes_from_the_run_record_and_is_never_ranked(holdout: Built) -> None:
    slow = run_for(
        holdout.ledger,
        holdout.run,
        cases=[
            {**case, "latency_ms": case["latency_ms"] * 10 + 5} for case in holdout.run["cases"]
        ],
    )
    fast, slower = holdout.entry(), holdout.entry(run=slow)
    values = sorted(case["latency_ms"] for case in slow["cases"])
    latency = slower["figures"]["latency_ms"]
    assert latency["note"] == "measured on the runner, not reproducible"
    assert latency["p50"]["estimate"] == round(stats.percentile(values, 0.5), 6)
    assert latency["p95"]["estimate"] == round(stats.percentile(values, 0.95), 6)
    assert {k: v for k, v in fast["figures"].items() if k != "latency_ms"} == {
        k: v for k, v in slower["figures"].items() if k != "latency_ms"
    }
    # Two configurations listed in their order, whichever is faster.
    rules = build(INBOX, "red_flags_holdout", {"router": "rules"})
    for first_run, second_run in ((holdout.run, rules.run), (slow, rules.run)):
        entries = [rules.entry(run=second_run), holdout.entry(run=first_run)]
        (table,) = scoreboard.board(entries, {})["tables"]
        assert [item["configuration"] for item in table["entries"]] == [
            "safety_screen=rules,router=naive_bayes",
            "safety_screen=rules,router=rules",
        ]
    assert fast["ranking"] == {
        "status": "off",
        "reason": "public set: regression check, not a ranking",
    }


def test_a_run_record_must_agree_with_its_ledger(holdout: Built) -> None:
    other = build(INBOX, "red_flags_holdout", {"router": "rules"})
    assert runs.agrees(holdout.run, holdout.ledger)
    assert not runs.agrees(other.run, holdout.ledger)
    with pytest.raises(MendariError, match="the run record of another ledger"):
        holdout.entry(run=other.run)
    short = run_for(holdout.ledger, holdout.run, cases=holdout.run["cases"][:-1])
    with pytest.raises(MendariError, match="it holds 29 cases and the ledger 30"):
        holdout.entry(run=short)
    spent = [dict(case) for case in holdout.run["cases"]]
    spent[3] = {**spent[3], "tokens": {"jev": [5, 1]}}
    with pytest.raises(MendariError, match="case 4's tokens are not the ledger's"):
        holdout.entry(run=run_for(holdout.ledger, holdout.run, cases=spent))
    assert runs.disagreement(holdout.run, holdout.ledger) is None
    for replicate in (0, method.MAX_REPLICATES + 1):
        with pytest.raises(MendariError, match="replicate"):
            run_for(holdout.ledger, holdout.run, replicate=replicate)


def test_a_run_record_holds_no_key_endpoint_or_case_text(jev_run: Built) -> None:
    data = runs.dumps_run(jev_run.run)
    assert runs.loads_run(data) == jev_run.run
    assert runs.run_sha256(jev_run.run) == "sha256:" + hashlib.sha256(data).hexdigest()
    text = data.decode()
    for secret in ("test-key", "jev.test", "https://", "JEV_"):
        assert secret not in text
    for record in jev_run.dataset.records:
        assert record["input"]["message"]["body"] not in text
        assert str(record["id"]) not in text
    assert set(jev_run.run) == {
        "format",
        "replicate",
        "engine",
        "date",
        "ledger",
        "models",
        "cases",
    }
    assert jev_run.run["models"] == {"jev": ["jev-9.9.9"]}
    entry_text = canonical(jev_run.entry())
    assert "test-key" not in entry_text and "jev.test" not in entry_text
    # The schema is closed, and a model id is never a URL; a refusal names the field, not the value.
    for bad in (
        {**jev_run.run, "endpoint": "https://jev.test/v1/decide"},
        {**jev_run.run, "models": {"jev": ["https://jev.test/v1/decide"]}},
        {**jev_run.run, "cases": [{**jev_run.run["cases"][0], "text": "chest pain"}]},
    ):
        with pytest.raises(MendariError) as refused:
            runs.check_run(bad)
        assert "jev.test" not in str(refused.value) and "chest" not in str(refused.value)
    with pytest.raises(MendariError, match="not in canonical form"):
        runs.loads_run(json.dumps(jev_run.run, indent=1).encode())


def test_a_run_record_names_the_models_its_answers_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through a composite too: a cascade's answer names its member (`jev:jev-9.9.9`), and the
    member's type is the model type its tokens are counted under."""
    calm_jev(monkeypatch)
    cascade = build(INBOX, "red_flags_holdout", {"router": "cascade"})
    assert cascade.run["models"] == {"jev": ["jev-9.9.9"]}
    assert "jev:jev-9.9.9" in cascade.ledger.header["models"]
    assert all(set(case["tokens"]) <= {"jev"} for case in cascade.run["cases"])
    assert cascade.run["cases"][0]["latency_ms"] == round(cascade.results[0].latency_ms, 3)
    with pytest.raises(MendariError, match="another digest"):
        runs.record(Decision(resolve_pack(GUARD)), cascade.ledger, cascade.results, date=DATE)


def test_the_scope_names_digest_set_models_method_date_disclaimer_and_disclosure(
    jev_run: Built, holdout: Built
) -> None:
    found = jev_run.entry(disclosures=stated(jev="Mendari has no commercial relationship."))
    scope = found["scope"]
    pack = jev_run.pack
    assert scope["pack"] == {"id": pack.id, "version": pack.version, "digest": pack.digest}
    assert scope["status"] == SYNTHETIC_EXAMPLE
    assert scope["set"] == {
        "kind": "public",
        "name": "red_flags_holdout",
        "sha256": jev_run.dataset.sha256,
        "n": 30,
        "distinct_inputs": 30,
        "norm": "norm/v1",
    }
    assert scope["models"] == {
        "answered": jev_run.ledger.header["models"],
        "by_type": {"jev": ["jev-9.9.9"]},
    }
    assert scope["method"] == {
        "version": "mendari.grading/1.0",
        "sha256": method.METHOD_SHA256,
        "in_effect": False,
    }
    assert scope["measured_on"] == "2026-10-18"
    assert scope["evaluation"] == "engineering evaluation, not clinical validation"
    assert scope["disclosure"] == {
        "status": "stated",
        "statements": {"jev": "Mendari has no commercial relationship."},
    }
    assert holdout.entry()["scope"]["disclosure"] == {
        "status": "no model provider",
        "statements": {},
    }
    # Without a statement, the scope is shown and the figures are not.
    pending = jev_run.entry()
    assert pending["scope"]["disclosure"] == {
        "status": "not published: disclosure pending",
        "statements": {"jev": None},
    }
    shown_board = scoreboard.board([pending], {})
    assert shown_board["tables"][0]["entries"][0]["published"] is False
    text = scoreboard_markdown(shown_board)
    assert "| Disclosure | not published: disclosure pending |" in text
    assert "| Accuracy | not published: disclosure pending |" in text
    assert "| Measured on | 2026-10-18 |" in text and "Subgroups of" not in text
    stated_text = scoreboard_markdown(scoreboard.board([found], {}))
    assert "| Disclosure | jev: Mendari has no commercial relationship. |" in stated_text
    assert "| Models | jev: jev-9.9.9 |" in stated_text


def test_a_public_set_is_a_regression_check_with_no_rank_and_no_grade(holdout: Built) -> None:
    found = holdout.entry()
    assert found["ranking"] == {
        "status": "off",
        "reason": "public set: regression check, not a ranking",
    }
    assert found["grade"] is None
    assert found["scope"]["set"]["kind"] == "public"
    shown_board = scoreboard.board([found], {INBOX: scoreboard.Current.of(holdout.pack)})
    assert shown_board["tables"][0]["ranking"] == found["ranking"]
    text = scoreboard_markdown(shown_board)
    assert "Public set: regression check, not a ranking." in text
    assert re.search(r"\bRank\b|\branked\b|unranked|\brank\b", text) is None
    assert "rank" not in canonical(found).replace('"ranking"', "").replace("not a ranking", "")


def test_weighted_figures_match_a_hand_computation_and_recall_is_never_weighted(
    holdout: Built, tmp_path: Path
) -> None:
    records = records_of(INBOX, "red_flags_holdout")
    weights = [3.0 if i % 3 == 0 else 1.5 for i in range(len(records))]
    for record, weight in zip(records, weights, strict=True):
        record["attributes"] = {**record.get("attributes", {}), "weight": weight}
    built = build(INBOX, written(tmp_path, "weighted", records))
    found = built.entry()
    weighted = found["figures"]["weighted"]
    assert set(weighted) == {
        "status",
        "accuracy",
        "false_automation",
        "automation",
        "auto_error_rate",
    }
    assert all("weighted" not in critical for critical in found["figures"]["critical"])
    cases, primary = built.ledger.cases, built.ledger.header["primary"]
    labelled = [
        (c, w)
        for c, w in zip(cases, weights, strict=True)
        if primary in c["q"] and c["q"][primary][0] is not None
    ]
    hits = [c["q"][primary][PREDICTED] == c["q"][primary][0] for c, _ in labelled]
    total = math.fsum(w for _, w in labelled)
    estimate = math.fsum(w for (_, w), hit in zip(labelled, hits, strict=True) if hit) / total
    kish = total**2 / math.fsum(w * w for _, w in labelled)
    accuracy = weighted["accuracy"]
    assert accuracy["n"] == len(labelled) == found["figures"]["accuracy"]["n"]
    assert math.isclose(accuracy["estimate"], estimate, abs_tol=5e-7)
    assert math.isclose(accuracy["effective_n"], kish, abs_tol=5e-7)
    x = estimate * kish
    low = 0.0 if x < 1 else beta.ppf(0.025, x, kish - x + 1)
    high = 1.0 if kish - x < 1 else beta.ppf(0.975, x + 1, kish - x)
    assert math.isclose(accuracy["low"], low, abs_tol=5e-6)
    assert math.isclose(accuracy["high"], high, abs_tol=5e-6)
    automation = weighted["automation"]
    assert automation["n"] == 30 and automation["estimate"] == 0.0  # nothing automated here
    # Unit weights give the unweighted k of n, and its exact interval.
    for record in records:
        record["attributes"]["weight"] = 1
    unit = build(INBOX, written(tmp_path, "unit", records)).entry()["figures"]
    assert unit["weighted"]["accuracy"]["low"] == unit["accuracy"]["low"]
    assert unit["weighted"]["accuracy"]["high"] == unit["accuracy"]["high"]
    assert holdout.entry()["figures"]["weighted"] == {"status": "as sampled"}
    del records[0]["attributes"]["weight"]
    with pytest.raises(MendariError, match="some records carry a sampling weight and some do not"):
        build(INBOX, written(tmp_path, "partly", records)).entry()


def test_arms_are_measured_not_measured_or_not_applicable(holdout: Built) -> None:
    guard = build(GUARD, "unsafe_drafts_holdout")
    current = {
        GUARD: scoreboard.Current.of(guard.pack),
        INBOX: scoreboard.Current.of(holdout.pack),
    }
    shown_board = scoreboard.board([guard.entry(), holdout.entry()], current)
    statuses = {
        table["pack"]: [arm["status"] for arm in table["arms"]] for table in shown_board["tables"]
    }
    assert statuses == {
        GUARD: ["measured", "not measured", "not measured", "not applicable"],
        INBOX: ["measured", "not measured", "not measured", "not measured"],
    }
    text = scoreboard_markdown(shown_board)
    assert (
        "Arms: Mendari only: measured; + specialised classifier: not measured; + general LLM: "
        "not measured; + both: not applicable." in text
    )


def test_freshness_is_worked_out_when_shown_and_never_stored(
    holdout: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = holdout.entry()
    assert "stale" not in canonical(found) and "marks" not in found
    current = scoreboard.Current.of(holdout.pack)

    def marks(entries: list[dict[str, Any]], now: scoreboard.Current = current) -> list[list[str]]:
        (table,) = scoreboard.board(entries, {INBOX: now})["tables"]
        return [item["marks"] for item in table["entries"]]

    fresh = ["unsigned", "method draft, not in effect", "stale after 2026-12-02"]
    assert marks([found]) == [fresh]
    assert marks([{**found, "signature": {"key_id": "k", "value": "v"}}]) == [fresh[1:]]
    assert marks([found], replace(current, digest="sha256:" + "0" * 64)) == [
        [*fresh[:2], "stale: pack changed", fresh[2]]
    ]
    assert marks(
        [found], replace(current, sets={**current.sets, "red_flags_holdout": "0" * 64})
    ) == [[*fresh[:2], "stale: set changed", fresh[2]]]
    old_method = json.loads(json.dumps(found))
    old_method["scope"]["method"]["sha256"] = "0" * 64
    assert marks([old_method]) == [[*fresh[:2], "stale: method changed", fresh[2]]]
    # A later run of the same configuration that reports another model makes the earlier stale.
    calm_jev(monkeypatch)
    jev = build(INBOX, "red_flags_holdout", {"safety_screen": "jev"})
    later = run_for(jev.ledger, jev.run, date="2026-10-20", models={"jev": ["jev-10.0.0"]})
    earlier_entry, later_entry = jev.entry(), jev.entry(run=later)
    assert [m[2:] for m in marks([later_entry, earlier_entry])] == [
        ["stale: another model reported", "stale after 2026-12-02"],
        ["stale after 2026-12-04"],
    ]
    # The same model on a later run changes nothing.
    same = jev.entry(run=run_for(jev.ledger, jev.run, date="2026-10-20"))
    assert [m[2:] for m in marks([same, earlier_entry])] == [
        ["stale after 2026-12-02"],
        ["stale after 2026-12-04"],
    ]
    assert scoreboard.stale_after(found) == "2026-12-02"


def test_a_ledger_of_another_digest_or_dataset_is_refused(holdout: Built, tmp_path: Path) -> None:
    scope = scoreboard.PackScope.of(holdout.pack)
    with pytest.raises(MendariError, match="is of mendari/patient-inbox-router"):
        holdout.entry(pack=replace(scope, digest="sha256:" + "0" * 64))
    with pytest.raises(MendariError, match="is of mendari/patient-inbox-router"):
        holdout.entry(pack=replace(scope, version="9.9.9"))
    with pytest.raises(MendariError, match="status line"):
        holdout.entry(pack=replace(scope, status="Clinically validated."))
    renamed = replace(holdout.dataset, name="red_flags")
    with pytest.raises(MendariError, match="another dataset \\(its name differs\\)"):
        holdout.entry(dataset=renamed)
    records = records_of(INBOX, "red_flags_holdout")
    longer = written(tmp_path, "red_flags_holdout", [*records, {**records[0], "id": "one-more"}])
    with pytest.raises(MendariError, match="another dataset \\(its sha256 differs\\)"):
        holdout.entry(dataset=longer)
    reordered = replace(holdout.dataset, records=list(reversed(holdout.dataset.records)))
    with pytest.raises(MendariError, match="case 1 is not red_flags_holdout's case 1"):
        holdout.entry(dataset=reordered)
    rules = next(c for c in arms.configurations(holdout.pack) if c.providers["router"] == "rules")
    with pytest.raises(MendariError, match="another configuration"):
        holdout.entry(configuration=rules)
    with pytest.raises(MendariError, match="not in the arm llm"):
        holdout.entry(arm="llm")
    jev_tokens = [{**case, "tokens": {"jev": [0, 0]}} for case in holdout.run["cases"]]
    with pytest.raises(MendariError, match="names a type the configuration"):
        holdout.entry(run=run_for(holdout.ledger, holdout.run, cases=jev_tokens))
