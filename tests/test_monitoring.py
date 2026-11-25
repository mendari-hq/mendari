# SPDX-License-Identifier: Apache-2.0
"""Monitoring a deployed agent over time (evals/monitoring.py): series, drift and alerts, like
with like (LP-21c): the served version only, the benchmark only for this build and the
providers that run here, accuracy stratum by stratum, refusals apart, whole weeks, an unknown
cost as unknown. The agent is the triage fixture, published with its own benchmark."""

from __future__ import annotations

import copy
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Agent, AuditStore, Decision
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.agent import load_agent
from mendari.core.card import benchmark
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals import monitoring as monitoring_module
from mendari.evals.monitoring import (
    MIN_REVIEWED,
    _alerts,
    _psi,
    check_webhook,
    monitor,
    recorded_providers,
)
from mendari.providers.agent import AgentProvider
from mendari.server.app import create_app
from tests.conftest import CHART_DIR, CLINICAL_DIR, TRIAGE_DIR, message
from tests.support.engine import published

ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # the pharmacy's
NOW = datetime.now(UTC)
# What the triage fixture's benchmark ran (its defaults: the keyword rules in both slots, so it
# automated nothing), and what its automated decisions here run: the screen on its agent (canned
# answers that find nothing urgent), as its policy requires to automate.
MEASURED = {"router": "rules", "screen": "rules"}
AGENT_SCREEN = {"router": "rules", "screen": "agent"}
SCREENED = {"screen": "agent"}


# A week's summary with nothing in it, for the unit tests of `_alerts` to fill in.
QUIET: dict[str, Any] = {
    "decisions": 0,
    "confidences": 0,
    "reviewed": 0,
    "override_rate": None,
    "latency_ms": {"n": 0, "p50": None, "p95": None, "mean": None},
    "figures": {"provider_errors": figures.of(0, 0), "abstained": figures.of(0, 0)},
}


@pytest.fixture(scope="module")
def published_triage(tmp_path_factory: pytest.TempPathFactory) -> Agent:
    """The triage fixture as its publisher ships it, with its own benchmark (`published`: a
    fixture's agent commits none), which the monitor compares with. One copy for the module: no
    test changes it."""
    return published(TRIAGE_DIR, tmp_path_factory.mktemp("published"))


def backdate(store: AuditStore, decision_id: str, days_ago: float, minute: int = 0) -> None:
    when = (NOW - timedelta(days=days_ago, minutes=minute)).isoformat(timespec="milliseconds")
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET created_at = ? WHERE id = ?", (when, decision_id))


def seed(
    store: AuditStore, agent: Agent, n: int, *, days_ago: float, final: str | None = None
) -> list[str]:
    """Automated decisions (the screen on its agent), each with an outcome recorded downstream
    when `final` is given (`same`: the recommendation)."""
    decide = Decision(agent, providers=SCREENED, store=store)
    ids = []
    for i in range(n):
        result = decide(message(ROUTINE))
        assert result.outcome == "AUTO"
        backdate(store, result.id, days_ago, i)
        if final is not None:
            label = result.recommendation if final == "same" else final
            store.record_outcome(
                result.id, final_label=str(label), source="staff-reroute", recorded_by="rn-9"
            )
        ids.append(result.id)
    return ids


def reviewed(
    store: AuditStore, agent: Agent, n: int, *, days_ago: float, final: str | None
) -> list[str]:
    """Decisions on the benchmark's own providers, sent to a person, who keeps the
    recommendation (`same`), changes it to `final`, or has not reviewed it yet (None)."""
    decide = Decision(agent, store=store)
    ids = []
    for i in range(n):
        result = decide(message(ROUTINE))
        assert result.outcome == "HUMAN_REVIEW"
        backdate(store, result.id, days_ago, i)
        if final is not None:
            label = result.recommendation if final == "same" else final
            store.resolve_review(result.id, final_label=str(label), reviewer="rn-9")
        ids.append(result.id)
    return ids


def measured_by(agent: Agent) -> dict[str, Any]:
    """A copy of the agent's benchmark, to change."""
    report = benchmark(agent)
    assert report is not None
    return copy.deepcopy(report)


def ran_here(
    store: AuditStore, agent: Agent, providers: dict[str, str], *, days_ago: float = 30
) -> None:
    """What a server records when it first serves the agent (`deployments`), before it decides
    anything: here `days_ago`, before the decisions a test seeds."""
    record = store.record_deployment(
        environment="test",
        release="t",
        agent_id=agent.id,
        agent_version=agent.version,
        agent_digest=agent.digest,
        providers=providers,
    )
    assert record is not None
    when = (NOW - timedelta(days=days_ago)).isoformat()
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE deployments SET started_at = ? WHERE id = ?", (when, record["id"]))


# -- the reference: only like with like --------------------------------------------------------


def test_a_quiet_agent_has_no_alerts(store: AuditStore, published_triage: Agent) -> None:
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["alerts"] == [] and report["last_7_days"]["decisions"] == 0
    assert len(report["weeks"]) == 5 and report["last_7_days"]["drift_psi"] is None
    bench = report["benchmark"]
    assert bench["accuracy"] is not None and bench["comparable"] is True
    assert bench["reason"] is None and "bins" not in bench
    assert bench["figures"]["reviewed_accuracy"]["n"] > 0


def test_reviewed_accuracy_below_the_benchmark_raises_an_alert(
    store: AuditStore, published_triage: Agent
) -> None:
    # Every recommendation changed.
    reviewed(store, published_triage, 25, days_ago=2, final="billing")
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    recent = report["last_7_days"]
    assert recent["decisions"] == 25 and recent["reviewed"] == 25
    assert recent["figures"]["reviewed_accuracy"] == figures.of(0, 25)
    assert [a["kind"] for a in report["alerts"]] == ["accuracy"]
    assert report["alerts"][0]["message"].startswith("reviewed decisions: accuracy 0 of 25 [")
    assert report["weeks"][-1]["decisions"] == 25


def test_reviews_that_keep_the_recommendation_raise_nothing(
    store: AuditStore, published_triage: Agent
) -> None:
    reviewed(store, published_triage, 25, days_ago=2, final="same")
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(25, 25)
    assert report["alerts"] == []


def test_a_benchmark_for_other_providers_or_another_build_is_not_a_reference(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewed(store, published_triage, 25, days_ago=2, final="billing")
    for providers, reason in ((AGENT_SCREEN, "other_providers"), (None, "providers_unknown")):
        report = monitor(store, published_triage, now=NOW, providers=providers)
        assert report["benchmark"]["comparable"] is False
        assert report["benchmark"]["reason"] == reason
        assert report["alerts"] == [] and report["last_7_days"]["drift_psi"] is None
    other = measured_by(published_triage)
    other["agent"]["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: other)
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["benchmark"]["reason"] == "another_build" and report["alerts"] == []
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: None)
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["benchmark"]["reason"] == "no_benchmark" and report["alerts"] == []


def test_an_agent_from_a_registry_is_never_compared_with_the_benchmark_it_carries(
    store: AuditStore, tmp_path: Path
) -> None:
    """LP-18h: a pulled agent's benchmark is its publisher's upload (ADR-0012), so the monitor
    takes none as a reference and shows none of its figures, even byte for byte an agent of
    one's own (the rule follows the origin, as the cap does); only the checks that need no
    benchmark run. The same agent of one's own is compared as before. (Published at a version
    this release bundles none of: a registry's copy never takes a bundled agent's place.)"""
    agent = published(TRIAGE_DIR, tmp_path, manifest=lambda m: m.update(version="1.0.1"))
    reviewed(store, agent, 25, days_ago=2, final="billing")
    own = monitor(store, agent, now=NOW, providers=MEASURED)
    assert own["benchmark"]["comparable"] is True
    assert [a["kind"] for a in own["alerts"]] == ["accuracy"]
    pulled = load_agent(agent.root, origin="hub")
    assert pulled.digest == agent.digest and benchmark(pulled) is not None
    report = monitor(store, pulled, now=NOW, providers=MEASURED)
    bench = report["benchmark"]
    assert (bench["comparable"], bench["reason"]) == (False, "from_registry")
    assert (bench["accuracy"], bench["n"]) == (None, None)
    assert (bench["reviewed_accuracy"], bench["automated_accuracy"]) == (None, None)
    assert set(bench["figures"].values()) == {None}
    assert report["alerts"] == [] and report["last_7_days"]["drift_psi"] is None
    # The live figures are the same decisions' either way.
    assert report["last_7_days"]["figures"] == own["last_7_days"]["figures"]


def test_automated_decisions_are_compared_with_the_benchmarks_automated_accuracy(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outcomes recorded downstream for automated decisions are their own stratum: against a
    benchmark that measured these providers automating, at 95 of 100."""
    seed(store, published_triage, 25, days_ago=2, final="billing")
    automating = measured_by(published_triage)
    automating["providers"]["screen"]["variant"] = "agent"
    automating["decisions"]["automation"].update(n=100, accuracy=0.95)
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: automating)
    report = monitor(store, published_triage, now=NOW, providers=AGENT_SCREEN)
    assert report["benchmark"]["comparable"] is True
    assert report["last_7_days"]["figures"]["automated_accuracy"] == figures.of(0, 25)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(0, 0)
    (alert,) = report["alerts"]
    assert (alert["kind"], alert["stratum"]) == ("accuracy", "automated_accuracy")
    assert alert["message"].startswith("automated decisions with an outcome recorded: accuracy")


def test_each_stratum_holds_only_its_own_decisions(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Automated decisions with wrong outcomes and reviewed ones a person kept, together: each
    stratum counts its own, and only the automated one alerts. (Fifty decisions on one message
    are also a drift from the benchmark's confidences, an alert of another kind.)"""
    seed(store, published_triage, 25, days_ago=2, final="billing")
    reviewed(store, published_triage, 25, days_ago=2, final="same")
    automating = measured_by(published_triage)
    automating["providers"]["screen"]["variant"] = "agent"
    automating["decisions"]["automation"].update(n=100, accuracy=0.95)
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: automating)
    report = monitor(store, published_triage, now=NOW, providers=AGENT_SCREEN)
    shares = report["last_7_days"]["figures"]
    assert shares["automated_accuracy"] == figures.of(0, 25)
    assert shares["reviewed_accuracy"] == figures.of(25, 25)
    accuracy = [a["stratum"] for a in report["alerts"] if a["kind"] == "accuracy"]
    assert accuracy == ["automated_accuracy"]


def test_an_accuracy_alert_needs_twenty_decisions_and_evidence_beyond_doubt(
    store: AuditStore, published_triage: Agent
) -> None:
    ids = reviewed(store, published_triage, MIN_REVIEWED - 1, days_ago=2, final="billing")
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(0, 19)
    assert report["alerts"] == []  # too few to say
    reviewed(store, published_triage, 1, days_ago=2, final="billing")
    assert [
        a["kind"] for a in monitor(store, published_triage, now=NOW, providers=MEASURED)["alerts"]
    ] == ["accuracy"]
    with store._db.session(exclusive=True) as db:  # 19 of 20 kept: 95%, below 96.8%, in doubt
        marks = ", ".join("?" for _ in ids)
        db.all(f"UPDATE decisions SET human_override = 0 WHERE id IN ({marks})", ids)
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(19, 20)
    assert report["alerts"] == []


def test_an_agent_doing_as_its_benchmark_says_rarely_alerts(tmp_path: Path) -> None:
    """At each fixture agent's own benchmark accuracy (published with it), the chance that a
    week's reviews alert stays under 1% for any count from 20 to 200: the alert needs the
    interval, not a point."""
    for folder in (TRIAGE_DIR, CLINICAL_DIR, CHART_DIR):
        agent = published(folder, tmp_path)
        bench = monitoring_module._benchmark(agent, None, None, approved=False)
        target, tolerance = bench["reviewed_accuracy"], bench["tolerance"]
        assert target is not None, agent.id
        for n in range(MIN_REVIEWED, 201):
            chance = sum(
                math.comb(n, k) * target**k * (1 - target) ** (n - k)
                for k in range(n + 1)
                if figures.of(k, n)["high"] < target - tolerance
            )
            assert chance < 0.01, (agent.id, n, chance)


def test_a_benchmark_measured_under_other_governance_is_not_a_reference(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An approval lets a risk level 2 or 3 agent automate: a benchmark measured without one
    says nothing of the decisions it automates (and the reverse). The clinical review fixture,
    at risk level 2, published with its own benchmark."""
    clinical = published(CLINICAL_DIR, tmp_path)
    assert clinical.manifest.risk.level == 2
    ran = {slot: info["variant"] for slot, info in measured_by(clinical)["providers"].items()}
    assert monitor(store, clinical, now=NOW, providers=ran)["benchmark"]["comparable"] is True
    store.approve_automation(
        agent_id=clinical.id,
        agent_digest=clinical.digest,
        risk_level=2,
        approver="committee",
        reference="minutes 12",
        expires_at=(NOW + timedelta(days=30)).isoformat(),
    )
    report = monitor(store, clinical, now=NOW, providers=ran)
    assert (report["benchmark"]["comparable"], report["benchmark"]["reason"]) == (
        False,
        "other_policy",
    )
    with tenancy.acting_for("clinic-b"):  # an approval is its organization's
        assert monitor(store, clinical, now=NOW, providers=ran)["benchmark"]["comparable"]
    governed = measured_by(published_triage)
    governed["policy"]["governance_override"] = {"approved_by": "committee"}
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: governed)
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["benchmark"]["reason"] == "other_policy"


def test_a_model_other_than_the_benchmarks_is_not_a_reference(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    measured = measured_by(published_triage)
    models = {slot: info["model"] for slot, info in measured["providers"].items()}
    report = monitor(store, published_triage, now=NOW, providers=MEASURED, models=models)
    assert report["benchmark"]["comparable"]
    other = {**models, "router": "rules@000000000000"}  # other rules
    report = monitor(store, published_triage, now=NOW, providers=MEASURED, models=other)
    assert report["benchmark"]["reason"] == "other_providers"
    # A benchmark of the router's LLM variant names its model: only the same model compares.
    llm = copy.deepcopy(measured)
    llm["providers"]["router"] = {"variant": "llm", "type": "llm", "model": "vendor/a"}
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: llm)
    on_llm = {**MEASURED, "router": "llm"}
    report = monitor(store, published_triage, now=NOW, providers=on_llm)  # a command: no models
    assert report["benchmark"]["reason"] == "providers_unknown"
    running = {**models, "router": "vendor/b"}
    report = monitor(store, published_triage, now=NOW, providers=on_llm, models=running)
    assert report["benchmark"]["reason"] == "other_providers"
    running = {**models, "router": "vendor/a"}
    report = monitor(store, published_triage, now=NOW, providers=on_llm, models=running)
    assert report["benchmark"]["comparable"] is True


@pytest.mark.parametrize(
    "broken",
    [
        {"decisions": {"review": {"n": 598, "expected_override_rate": "0.005"}}},
        {"decisions": {"automation": {"n": 100, "accuracy": "0.95"}}},
        {"agent": "mendari-test/triage"},
        {"providers": ["rules", "agent"]},
        {"policy": "none"},
        {
            "questions": {
                "destination": {"accuracy": "high", "calibration": {"bins": ["x", {"low": "a"}]}}
            }
        },
    ],
)
def test_a_benchmark_with_values_it_cannot_read_never_breaks_monitoring(
    store: AuditStore,
    published_triage: Agent,
    monkeypatch: pytest.MonkeyPatch,
    broken: dict[str, Any],
) -> None:
    seed(store, published_triage, 25, days_ago=2, final="billing")
    report = {**measured_by(published_triage), **broken}
    if isinstance(report["providers"], dict):  # these providers run: only the values are broken
        report["providers"]["screen"]["variant"] = "agent"
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: report)
    answer = monitor(store, published_triage, now=NOW, providers=AGENT_SCREEN)
    assert answer["last_7_days"]["decisions"] == 25
    assert [a["kind"] for a in answer["alerts"] if a["kind"] == "accuracy"] == []


def test_a_benchmark_that_measured_no_automation_compares_none(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The triage fixture's benchmark automated nothing (its screen was the keyword rules alone):
    its automated accuracy is unknown, so outcomes of automated decisions are shown and never
    compared (they were, with its whole-set accuracy)."""
    seed(store, published_triage, 25, days_ago=2, final="billing")
    measured = measured_by(published_triage)
    assert measured["decisions"]["automation"]["n"] == 0
    measured["providers"]["screen"]["variant"] = "agent"
    monkeypatch.setattr(monitoring_module, "benchmark", lambda agent: measured)
    report = monitor(store, published_triage, now=NOW, providers=AGENT_SCREEN)
    assert report["benchmark"]["automated_accuracy"] is None
    assert report["last_7_days"]["observed_accuracy"] == 0.0 and report["alerts"] == []


# -- what is counted ---------------------------------------------------------------------------


def test_rows_from_another_version_are_not_compared(
    store: AuditStore, published_triage: Agent
) -> None:
    ids = reviewed(store, published_triage, 25, days_ago=2, final="billing")
    with store._db.session(exclusive=True) as db:
        marks = ", ".join("?" for _ in ids[:10])
        db.all(f"UPDATE decisions SET agent_version = '0.0.1' WHERE id IN ({marks})", ids[:10])
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["other_versions"] == {"0.0.1": 10}  # never the served version's own
    assert report["last_7_days"]["decisions"] == 15
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET agent_version = '0.0.1'")
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert report["other_versions"] == {"0.0.1": 25}


def test_only_decisions_since_the_current_configuration_are_compared(
    store: AuditStore, published_triage: Agent
) -> None:
    """The agent ran its screen on its agent service until three days ago, then on the
    benchmark's own providers: the decisions made before are shown week by week, never
    compared."""
    ids = reviewed(store, published_triage, 25, days_ago=5, final="billing")
    with store._db.session(exclusive=True) as db:
        db.all("DELETE FROM deployments")
    ran_here(store, published_triage, AGENT_SCREEN)
    when = (NOW - timedelta(days=6)).isoformat()
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE deployments SET started_at = ?", (when,))
    ran_here(store, published_triage, MEASURED)
    since = NOW - timedelta(days=3)
    with store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE deployments SET started_at = ? WHERE started_at <> ?",
            (since.isoformat(), when),
        )
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["configuration_since"] == since.isoformat()
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert sum(week["decisions"] for week in report["weeks"]) == len(ids)


def test_refusals_count_apart_and_leave_every_rate_and_the_cost(
    store: AuditStore, published_triage: Agent
) -> None:
    ids = seed(store, published_triage, 4, days_ago=1)
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET refused = 1, outcome = 'BLOCK' WHERE id IN (?, ?)", ids[:2])
        # As a refusal is stored: no confidence, no provider ran, nothing spent.
        db.all("UPDATE decisions SET confidence = NULL, cost_usd = 0.0 WHERE refused = 1")
        db.all("UPDATE decisions SET cost_usd = 0.002 WHERE refused IS NULL OR refused = 0")
    recent = monitor(store, published_triage, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["decisions"] == 4 and recent["refused"] == 2
    assert recent["figures"]["automated"] == figures.of(2, 2)  # of the decisions made
    assert recent["confidences"] == 2
    assert recent["cost_per_decision"] == pytest.approx(0.002) and recent["cost_unknown"] == 0


def test_drift_needs_fifty_confidences_not_fifty_decisions() -> None:
    def summary(decisions: int, confidences: int) -> dict[str, Any]:
        return {**QUIET, "decisions": decisions, "confidences": confidences}

    bench = {"comparable": False, "tolerance": 0.03}  # drift is null unless comparable
    assert _alerts(summary(100, 10), summary(0, 0), 0.9, 0, bench) == []
    drifted = _alerts(summary(100, 60), summary(0, 0), 0.9, 0, bench)
    assert [a["kind"] for a in drifted] == ["drift"]


def test_every_weekly_bucket_is_whole(store: AuditStore, published_triage: Agent) -> None:
    """30 days are five whole weeks, 35 days back: the oldest week holds its first days too (it
    held two of its seven)."""
    seed(store, published_triage, 1, days_ago=33)
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["weeks"][0]["decisions"] == 1
    assert report["weeks"][0]["from"] == (NOW - timedelta(days=35)).date().isoformat()


def test_an_unknown_cost_is_shown_as_unknown(store: AuditStore, published_triage: Agent) -> None:
    ids = seed(store, published_triage, 3, days_ago=1)
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET cost_usd = 0.002")
        db.all("UPDATE decisions SET cost_usd = NULL WHERE id = ?", (ids[0],))
    recent = monitor(store, published_triage, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["cost_per_decision"] is None and recent["cost_unknown"] == 1
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET cost_usd = 0.002")
    recent = monitor(store, published_triage, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["cost_per_decision"] == pytest.approx(0.002) and recent["cost_unknown"] == 0


def test_decisions_awaiting_review_are_counted(store: AuditStore, published_triage: Agent) -> None:
    reviewed(store, published_triage, 3, days_ago=1, final=None)
    reviewed(store, published_triage, 2, days_ago=1, final="same")
    recent = monitor(store, published_triage, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["awaiting_review"] == 3 and recent["reviewed"] == 2


def test_old_decisions_fall_outside_the_last_week(
    store: AuditStore, published_triage: Agent
) -> None:
    reviewed(store, published_triage, 25, days_ago=10, final="billing")
    report = monitor(store, published_triage, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert sum(week["decisions"] for week in report["weeks"]) == 25


def test_a_report_covers_two_weeks_or_more(
    store: AuditStore, published_triage: Agent, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for days in (7, 13, 181):
        with pytest.raises(MendariError, match="14 to 180 days"):
            monitor(store, published_triage, days=days)
    assert len(monitor(store, published_triage, days=14, now=NOW)["weeks"]) == 2
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    held = client.get("/api/monitoring", params={"agent_id": published_triage.id, "days": 7}).json()
    assert held["days"] == 14
    assert main(["monitor", "--db", str(tmp_path / "a.db"), "--days", "7"]) == 1
    assert "7 days: give 14 to 180" in capsys.readouterr().err


# -- drift and overrides -----------------------------------------------------------------------


def test_drift_is_near_zero_for_the_same_distribution_and_large_for_a_shift() -> None:
    bins = [{"low": 0.9, "high": 1.0, "n": 90}, {"low": 0.5, "high": 0.6, "n": 10}]
    same = [{"confidence": 0.95}] * 90 + [{"confidence": 0.55}] * 10
    shifted = [{"confidence": 0.55}] * 80 + [{"confidence": 0.95}] * 20
    assert _psi(bins, same) == pytest.approx(0.0, abs=1e-6)
    assert (_psi(bins, shifted) or 0) > 0.25
    assert _psi([], same) is None and _psi(bins, []) is None


def test_overrides_that_double_raise_an_alert() -> None:
    def summary(reviewed: int, rate: float) -> dict[str, Any]:
        overrides = figures.of(round(rate * reviewed), reviewed)
        return {
            **QUIET,
            "decisions": reviewed,
            "reviewed": reviewed,
            "override_rate": rate,
            "figures": {**QUIET["figures"], "overrides": overrides},
        }

    bench = {"comparable": False, "tolerance": 0.03}
    assert [a["kind"] for a in _alerts(summary(30, 0.4), summary(30, 0.2), None, 0, bench)] == [
        "overrides"
    ]
    assert _alerts(summary(30, 0.3), summary(30, 0.2), None, 0, bench) == []
    assert _alerts(summary(5, 0.8), summary(30, 0.2), None, 0, bench) == []  # too few to say


def test_an_automated_decision_rated_wrong_raises_an_alert(
    store: AuditStore, published_triage: Agent
) -> None:
    (decision_id,) = seed(store, published_triage, 1, days_ago=1)
    store.record_feedback(
        decision_id, rating="down", expected={"question": "destination", "label": "billing"}
    )
    alerts = monitor(store, published_triage, now=NOW)["alerts"]
    assert [a["kind"] for a in alerts] == ["automated_wrong"]  # comparable or not


# -- the server and the command line ---------------------------------------------------------


def test_the_api_compares_with_the_providers_the_server_runs(
    tmp_path: Path, published_triage: Agent
) -> None:
    """A server serving the published agent alone, on its screen's agent, then on its defaults."""
    served = [published_triage.root.parent]
    client = TestClient(
        create_app(db_path=tmp_path / "a.db", providers=SCREENED, agents_paths=served)
    )
    decided = client.post(f"/api/agents/{published_triage.id}/decide", json=message(ROUTINE)).json()
    assert decided["outcome"] == "AUTO"
    report = client.get("/api/monitoring", params={"agent_id": published_triage.id}).json()
    assert report["last_7_days"]["decisions"] == 1 and report["agent"]["id"] == published_triage.id
    assert report["benchmark"]["reason"] == "other_providers"  # an agent screens; it measured rules
    measured = TestClient(create_app(db_path=tmp_path / "b.db", agents_paths=served))
    report = measured.get("/api/monitoring", params={"agent_id": published_triage.id}).json()
    assert report["benchmark"]["comparable"] is True


def test_recorded_providers_are_this_builds_newest(
    store: AuditStore, published_triage: Agent
) -> None:
    assert recorded_providers(store, published_triage) is None
    ran_here(store, published_triage, MEASURED)
    assert recorded_providers(store, published_triage) == MEASURED
    store.record_deployment(
        environment="test",
        release="t2",
        agent_id=published_triage.id,
        agent_version="0.0.1",
        agent_digest="sha256:" + "1" * 64,
        providers=AGENT_SCREEN,
    )
    assert recorded_providers(store, published_triage) == MEASURED  # another build's record
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE deployments SET started_at = '2020-01-01T00:00:00.000+00:00'")
    # The same build, restarted on the screen's agent.
    ran_here(store, published_triage, AGENT_SCREEN)
    assert recorded_providers(store, published_triage) == AGENT_SCREEN
    for n in range(60):  # many newer records of another build never hide this one's
        store.record_deployment(
            environment="test",
            release=f"r{n}",
            agent_id=published_triage.id,
            agent_version="0.0.2",
            agent_digest="sha256:" + "2" * 64,
            providers=MEASURED,
        )
    assert recorded_providers(store, published_triage) == AGENT_SCREEN
    store.record_deployment(
        environment="preview",
        release="p1",
        agent_id=published_triage.id,
        agent_version=published_triage.version,
        agent_digest=published_triage.digest,
        providers=MEASURED,
    )
    assert recorded_providers(store, published_triage) is None  # two environments disagree


def test_the_cli_exits_1_on_alerts_and_posts_them(
    tmp_path: Path,
    published_triage: Agent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    reviewed(store, published_triage, 25, days_ago=1, final="billing")
    posted: list[Any] = []

    class Response:
        status_code = 204

    def post(url: str, **kwargs: Any) -> Response:
        posted.append((url, json.loads(kwargs["content"])))
        return Response()

    monkeypatch.setattr("httpx.post", post)
    args = ["monitor", "--db", str(db), "--agent", str(published_triage.root)]  # the published copy
    hook = ["--webhook", "https://alerts.example/hook"]
    assert main([*args, *hook]) == 0  # no server recorded what runs: nothing to compare
    assert "(no comparable benchmark: providers_unknown)" in capsys.readouterr().out
    ran_here(store, published_triage, MEASURED)
    assert main([*args, *hook]) == 1
    assert "[high] accuracy: reviewed decisions: accuracy 0 of 25 [" in capsys.readouterr().out
    assert posted[0][0] == "https://alerts.example/hook"
    assert posted[0][1]["alerts"][0]["agent"]["id"] == published_triage.id


@pytest.mark.parametrize("newest_first", [True, False])
def test_the_cli_checks_the_newest_version_of_each_agent(
    tmp_path: Path,
    published_triage: Agent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    newest_first: bool,
) -> None:
    copied = tmp_path / "older"
    shutil.copytree(published_triage.root, copied)
    manifest = copied / "agent.yaml"
    manifest.write_text(
        manifest.read_text().replace(f"version: {published_triage.version}", "version: 0.0.1")
    )
    older = load_agent(copied, origin="maintained")
    assert older.id == published_triage.id and older.version == "0.0.1"

    class Found:
        agents = [published_triage, older] if newest_first else [older, published_triage]

    checked: list[str] = []
    real = monitoring_module.monitor

    def watched(store: AuditStore, agent: Agent, **kwargs: Any) -> dict[str, Any]:
        checked.append(agent.version)
        return real(store, agent, **kwargs)

    monkeypatch.setattr("mendari.cli.discover", lambda *a, **k: Found())
    monkeypatch.setattr(monitoring_module, "monitor", watched)
    AuditStore(tmp_path / "a.db").close()
    assert main(["monitor", "--db", str(tmp_path / "a.db")]) == 0
    assert checked == [published_triage.version]
    out = capsys.readouterr().out
    assert out.count(f"{published_triage.id}:") == 1 and "providers_unknown" in out


@pytest.mark.parametrize(
    "url",
    [
        "http://alerts.example.org/hook",
        "ftp://alerts.example.org",
        "https://u:p@alerts.example.org",
    ],
)
def test_alerts_leave_only_over_https_without_credentials(url: str) -> None:
    with pytest.raises(MendariError):
        check_webhook(url)


def test_local_webhooks_may_use_http() -> None:
    for url in ("http://localhost:9000/hook", "http://alertmanager:9093/api", "https://x.org/h"):
        check_webhook(url)


def test_the_cli_watches_one_organization(
    tmp_path: Path, published_triage: Agent, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    ran_here(store, published_triage, MEASURED)
    with tenancy.acting_for("clinic-a"):
        reviewed(store, published_triage, 25, days_ago=1, final="billing")
    args = ["monitor", "--db", str(db), "--agent", str(published_triage.root)]
    assert main([*args, "--org", "clinic-b"]) == 0
    assert main([*args, "--org", "clinic-a"]) == 1
    assert "reviewed decisions: accuracy 0 of 25 [" in capsys.readouterr().out


# -- operational health (LP-21d) -----------------------------------------------------------------


def set_rows(store: AuditStore, ids: list[str], **columns: Any) -> None:
    """Stored columns of decisions, as a decide would have left them."""
    marks = ", ".join("?" for _ in ids)
    sets = ", ".join(f"{column} = ?" for column in columns)
    with store._db.session(exclusive=True) as db:
        db.all(f"UPDATE decisions SET {sets} WHERE id IN ({marks})", (*columns.values(), *ids))


def test_a_decision_stores_how_many_questions_had_a_provider_error(
    store: AuditStore, published_triage: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count beside the record, never in it: the chained record is what it was."""
    from mendari.errors import ProviderError

    def down(*args: Any, **kwargs: Any) -> Any:
        raise ProviderError("provider_unavailable", "down")

    decided = Decision(published_triage, providers=SCREENED, store=store)(message(ROUTINE))
    assert decided.errors == {}
    monkeypatch.setattr(AgentProvider, "answer", down)  # the screen's agent
    failed = Decision(published_triage, providers=SCREENED, store=store)(message(ROUTINE))
    assert failed.errors and failed.outcome.value != "AUTO"  # a person decides
    with store._db.session(exclusive=False) as db:
        rows = {
            row["id"]: row for row in db.all("SELECT id, provider_errors, record FROM decisions")
        }
    assert rows[decided.id]["provider_errors"] == 0
    assert rows[failed.id]["provider_errors"] == len(failed.errors)
    assert "provider_errors" not in rows[failed.id]["record"]


def test_operational_health_comes_from_stored_columns(
    store: AuditStore, published_triage: Agent
) -> None:
    ids = reviewed(store, published_triage, 21, days_ago=1, final=None)
    set_rows(store, ids[:3], provider_errors=1)
    set_rows(store, ids[3:5], outcome="ABSTAIN")
    for n, decision_id in enumerate(ids[:20]):
        set_rows(store, [decision_id], latency_ms=100.0 * (n + 1))
    # A refused input: no provider ran, it decided nothing; never in these figures.
    set_rows(store, ids[20:], refused=1, outcome="BLOCK", provider_errors=1, latency_ms=99999.0)
    recent = monitor(store, published_triage, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["figures"]["provider_errors"] == figures.of(3, 20)
    assert recent["figures"]["abstained"] == figures.of(2, 20)
    # Nearest rank: the 19th of 20 is the p95 (never simply the largest), the 10th the p50.
    assert recent["latency_ms"] == {"n": 20, "p50": 1000.0, "p95": 1900.0, "mean": 1050.0}


def health(
    failed: int, abstained: int, p95: float, n: int = 60, slow: int | None = None
) -> dict[str, Any]:
    """A week's summary; `slow`: this week's decisions past the latency line, as k of n."""
    week: dict[str, Any] = {
        "decisions": n,
        "confidences": 0,
        "reviewed": 0,
        "override_rate": None,
        "latency_ms": {"n": n, "p50": p95 / 2, "p95": p95, "mean": p95 / 2},
        "figures": {
            "provider_errors": figures.of(failed, n),
            "abstained": figures.of(abstained, n),
        },
    }
    if slow is not None:
        week |= {"slow": figures.of(slow, n), "slow_line_ms": 2400.0}
    return week


def test_provider_errors_alert_once_they_are_frequent_beyond_doubt_and_doubled() -> None:
    bench = {"comparable": False, "tolerance": 0.03}
    kinds = lambda now, then: [a["kind"] for a in _alerts(now, then, None, 0, bench)]  # noqa: E731
    assert kinds(health(12, 0, 500), health(0, 0, 500)) == ["provider_errors"]
    assert kinds(health(4, 0, 500), health(0, 0, 500)) == []  # 4 of 60: not beyond doubt
    assert kinds(health(12, 0, 500), health(9, 0, 500)) == []  # high, but not doubled
    assert kinds(health(12, 0, 500, n=40), health(0, 0, 500)) == []  # too few to say
    alone = kinds(health(12, 0, 500), health(0, 0, 500, n=10))  # no week before to compare
    assert alone == ["provider_errors"]


def test_abstentions_and_latency_alert_against_the_week_before() -> None:
    bench = {"comparable": False, "tolerance": 0.03}
    kinds = lambda now, then: [a["kind"] for a in _alerts(now, then, None, 0, bench)]  # noqa: E731
    # Abstentions: past both twice the week before and 5 points up, beyond doubt.
    assert kinds(health(0, 24, 500), health(0, 6, 500)) == ["abstention"]  # 40% against 10%
    assert kinds(health(0, 15, 500), health(0, 6, 500)) == []  # 25%: doubled, within doubt
    assert kinds(health(0, 12, 500), health(0, 1, 500)) == ["abstention"]  # 20% against 1.7%
    assert kinds(health(0, 6, 500), health(0, 1, 500)) == []  # 10%: the 5 points not certain
    assert kinds(health(0, 24, 500), health(0, 6, 500, n=40)) == []  # too few the week before
    # Latency: 5% or more of this week's decisions past the line, beyond doubt.
    assert kinds(health(0, 0, 3000, slow=10), health(0, 0, 1200)) == ["latency"]
    assert kinds(health(0, 0, 3000, slow=4), health(0, 0, 1200)) == []  # within doubt
    assert kinds(health(0, 0, 3000, slow=10), health(0, 0, 1200, n=40)) == []
    assert kinds(health(0, 0, 3000, slow=10, n=40), health(0, 0, 1200)) == []


def test_a_rise_in_provider_errors_alerts_from_what_decisions_stored(
    store: AuditStore, published_triage: Agent
) -> None:
    """No benchmark is needed (and none is compared: these decisions, all on one message, are
    also a drift from its confidences)."""
    reviewed(store, published_triage, 60, days_ago=9, final=None)
    now = reviewed(store, published_triage, 60, days_ago=2, final=None)
    assert monitor(store, published_triage, now=NOW, providers=None)["alerts"] == []
    set_rows(store, now[:15], provider_errors=1)
    alerts = monitor(store, published_triage, now=NOW, providers=None)["alerts"]
    assert [a["kind"] for a in alerts] == ["provider_errors"]
    assert alerts[0]["message"].startswith("decisions with a provider error: 15 of 60 [")


def test_a_steady_timeout_tail_does_not_alert_but_a_real_slowdown_does(
    store: AuditStore, published_triage: Agent
) -> None:
    """5% of decisions at a 30 s timeout both weeks is the same service; three times slower is
    not. (A p95 against a p95 flapped on such a tail: review of LP-21d.)"""
    before = reviewed(store, published_triage, 60, days_ago=9, final=None)
    now = reviewed(store, published_triage, 60, days_ago=2, final=None)
    for ids in (before, now):
        set_rows(store, ids, latency_ms=800.0)
        set_rows(store, ids[:3], latency_ms=30000.0)
    report = monitor(store, published_triage, now=NOW, providers=None)
    assert [a["kind"] for a in report["alerts"]] == []
    set_rows(store, now, latency_ms=2400.0)
    report = monitor(store, published_triage, now=NOW, providers=None)
    (alert,) = report["alerts"]
    assert alert["kind"] == "latency" and alert["message"].startswith("60 of 60 [")


def test_drift_reads_only_confidences_that_are_shares() -> None:
    """Before, a stored confidence of -0.25 was counted in the ninth tenth (index -2 of the
    list), so a few bad rows read as a drift of 17, and a NaN raised a ValueError."""
    from mendari.evals.monitoring import _psi

    bins = [{"low": 0.9, "n": 10}]
    sure = [{"confidence": 0.95}] * 5
    assert _psi(bins, sure) == _psi(bins, [*sure, *[{"confidence": -0.25}] * 5])
    assert _psi(bins, [*sure, {"confidence": math.nan}]) == _psi(bins, sure)
    assert _psi(bins, [{"confidence": -0.25}]) is None  # nothing to compare
