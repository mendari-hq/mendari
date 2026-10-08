# SPDX-License-Identifier: Apache-2.0
"""Monitoring a deployed pack over time (evals/monitoring.py): series, drift and alerts, like
with like (LP-21c): the served version only, the benchmark only for this build and the
providers that run here, accuracy stratum by stratum, refusals apart, whole weeks, an unknown
cost as unknown."""

from __future__ import annotations

import copy
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import tenancy
from mendari.core.card import benchmark
from mendari.core.pack import load_pack
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals import monitoring as monitoring_module
from mendari.evals.monitoring import _alerts, _psi, check_webhook, monitor, recorded_providers
from mendari.server.app import create_app
from tests.conftest import SCREENED, message

# The automated decisions here run the urgency screen with a fake Jev (`screened`).
pytestmark = pytest.mark.usefixtures("screened")

ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
NOW = datetime.now(UTC)
# What the inbox's benchmark ran (its defaults), and what `screened` runs.
MEASURED = {"safety_screen": "rules", "router": "naive_bayes"}
JEV_SCREEN = {"safety_screen": "rules_and_jev", "router": "naive_bayes"}


def backdate(store: AuditStore, decision_id: str, days_ago: float, minute: int = 0) -> None:
    when = (NOW - timedelta(days=days_ago, minutes=minute)).isoformat(timespec="milliseconds")
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET created_at = ? WHERE id = ?", (when, decision_id))


def seed(
    store: AuditStore, pack: Pack, n: int, *, days_ago: float, final: str | None = None
) -> list[str]:
    """Automated decisions (the screen on a fake Jev), each with an outcome recorded downstream
    when `final` is given (`same`: the recommendation)."""
    decide = Decision(pack, providers=SCREENED, store=store)
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
    store: AuditStore, pack: Pack, n: int, *, days_ago: float, final: str | None
) -> list[str]:
    """Decisions on the benchmark's own providers, sent to a person, who keeps the
    recommendation (`same`), changes it to `final`, or has not reviewed it yet (None)."""
    decide = Decision(pack, store=store)
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


def ran_here(store: AuditStore, pack: Pack, providers: dict[str, str]) -> None:
    """What a server records when it first serves the pack (`deployments`)."""
    store.record_deployment(
        environment="test",
        release="t",
        pack_id=pack.id,
        pack_version=pack.version,
        pack_digest=pack.digest,
        providers=providers,
    )


# -- the reference: only like with like --------------------------------------------------------


def test_a_quiet_pack_has_no_alerts(store: AuditStore, inbox_pack: Pack) -> None:
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["alerts"] == [] and report["last_7_days"]["decisions"] == 0
    assert len(report["weeks"]) == 5 and report["last_7_days"]["drift_psi"] is None
    bench = report["benchmark"]
    assert bench["accuracy"] is not None and bench["comparable"] is True
    assert bench["reason"] is None and "bins" not in bench
    assert bench["figures"]["reviewed_accuracy"]["n"] > 0


def test_reviewed_accuracy_below_the_benchmark_raises_an_alert(
    store: AuditStore, inbox_pack: Pack
) -> None:
    reviewed(store, inbox_pack, 25, days_ago=2, final="billing")  # every recommendation changed
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    recent = report["last_7_days"]
    assert recent["decisions"] == 25 and recent["reviewed"] == 25
    assert recent["figures"]["reviewed_accuracy"] == figures.of(0, 25)
    assert [a["kind"] for a in report["alerts"]] == ["accuracy"]
    assert report["alerts"][0]["message"].startswith("reviewed decisions: accuracy 0 of 25 [")
    assert report["weeks"][-1]["decisions"] == 25


def test_reviews_that_keep_the_recommendation_raise_nothing(
    store: AuditStore, inbox_pack: Pack
) -> None:
    reviewed(store, inbox_pack, 25, days_ago=2, final="same")
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(25, 25)
    assert report["alerts"] == []


def test_a_benchmark_for_other_providers_or_another_build_is_not_a_reference(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewed(store, inbox_pack, 25, days_ago=2, final="billing")
    for providers, reason in ((JEV_SCREEN, "other_providers"), (None, "providers_unknown")):
        report = monitor(store, inbox_pack, now=NOW, providers=providers)
        assert report["benchmark"]["comparable"] is False
        assert report["benchmark"]["reason"] == reason
        assert report["alerts"] == [] and report["last_7_days"]["drift_psi"] is None
    other = copy.deepcopy(benchmark(inbox_pack))
    other["pack"]["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(monitoring_module, "benchmark", lambda pack: other)
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["benchmark"]["reason"] == "another_build" and report["alerts"] == []
    monkeypatch.setattr(monitoring_module, "benchmark", lambda pack: None)
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["benchmark"]["reason"] == "no_benchmark" and report["alerts"] == []


def test_automated_decisions_are_compared_with_the_benchmarks_automated_accuracy(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outcomes recorded downstream for automated decisions are their own stratum: against a
    benchmark that measured these providers automating, at 95 of 100."""
    seed(store, inbox_pack, 25, days_ago=2, final="billing")
    automating = copy.deepcopy(benchmark(inbox_pack))
    automating["providers"]["safety_screen"]["variant"] = "rules_and_jev"
    automating["decisions"]["automation"].update(n=100, accuracy=0.95)
    monkeypatch.setattr(monitoring_module, "benchmark", lambda pack: automating)
    report = monitor(store, inbox_pack, now=NOW, providers=JEV_SCREEN)
    assert report["benchmark"]["comparable"] is True
    assert report["last_7_days"]["figures"]["automated_accuracy"] == figures.of(0, 25)
    assert report["last_7_days"]["figures"]["reviewed_accuracy"] == figures.of(0, 0)
    (alert,) = report["alerts"]
    assert alert["kind"] == "accuracy"
    assert alert["message"].startswith("automated decisions with an outcome recorded: accuracy")


def test_a_benchmark_that_measured_no_automation_compares_none(
    store: AuditStore, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inbox's benchmark automated nothing: its automated accuracy is unknown, so outcomes of
    automated decisions are shown and never compared (they were, with its whole-set accuracy)."""
    seed(store, inbox_pack, 25, days_ago=2, final="billing")
    measured = copy.deepcopy(benchmark(inbox_pack))
    measured["providers"]["safety_screen"]["variant"] = "rules_and_jev"
    monkeypatch.setattr(monitoring_module, "benchmark", lambda pack: measured)
    report = monitor(store, inbox_pack, now=NOW, providers=JEV_SCREEN)
    assert report["benchmark"]["automated_accuracy"] is None
    assert report["last_7_days"]["observed_accuracy"] == 0.0 and report["alerts"] == []


# -- what is counted ---------------------------------------------------------------------------


def test_rows_from_another_version_are_not_compared(store: AuditStore, inbox_pack: Pack) -> None:
    reviewed(store, inbox_pack, 25, days_ago=2, final="billing")
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET pack_version = '0.0.1'")
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert report["other_versions"] == {"0.0.1": 25}


def test_refusals_count_apart_and_leave_every_rate_and_drift(
    store: AuditStore, inbox_pack: Pack
) -> None:
    ids = seed(store, inbox_pack, 4, days_ago=1)
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET refused = 1, outcome = 'BLOCK' WHERE id IN (?, ?)", ids[:2])
    recent = monitor(store, inbox_pack, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["decisions"] == 4 and recent["refused"] == 2
    assert recent["figures"]["automated"] == figures.of(2, 2)  # of the decisions made
    assert recent["confidences"] == 2


def test_drift_needs_fifty_confidences_not_fifty_decisions() -> None:
    def summary(decisions: int, confidences: int) -> dict[str, Any]:
        return {
            "decisions": decisions,
            "confidences": confidences,
            "reviewed": 0,
            "override_rate": None,
            "figures": {},
        }

    bench = {"comparable": False, "tolerance": 0.03}  # drift is null unless comparable
    assert _alerts(summary(100, 10), summary(0, 0), 0.9, 0, bench) == []
    drifted = _alerts(summary(100, 60), summary(0, 0), 0.9, 0, bench)
    assert [a["kind"] for a in drifted] == ["drift"]


def test_every_weekly_bucket_is_whole(store: AuditStore, inbox_pack: Pack) -> None:
    """30 days are five whole weeks, 35 days back: the oldest week holds its first days too (it
    held two of its seven)."""
    seed(store, inbox_pack, 1, days_ago=33)
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["weeks"][0]["decisions"] == 1
    assert report["weeks"][0]["from"] == (NOW - timedelta(days=35)).date().isoformat()


def test_an_unknown_cost_is_shown_as_unknown(store: AuditStore, inbox_pack: Pack) -> None:
    ids = seed(store, inbox_pack, 3, days_ago=1)
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET cost_usd = 0.002")
        db.all("UPDATE decisions SET cost_usd = NULL WHERE id = ?", (ids[0],))
    recent = monitor(store, inbox_pack, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["cost_per_decision"] is None and recent["cost_unknown"] == 1
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET cost_usd = 0.002")
    recent = monitor(store, inbox_pack, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["cost_per_decision"] == pytest.approx(0.002) and recent["cost_unknown"] == 0


def test_decisions_awaiting_review_are_counted(store: AuditStore, inbox_pack: Pack) -> None:
    reviewed(store, inbox_pack, 3, days_ago=1, final=None)
    reviewed(store, inbox_pack, 2, days_ago=1, final="same")
    recent = monitor(store, inbox_pack, now=NOW, providers=MEASURED)["last_7_days"]
    assert recent["awaiting_review"] == 3 and recent["reviewed"] == 2


def test_old_decisions_fall_outside_the_last_week(store: AuditStore, inbox_pack: Pack) -> None:
    reviewed(store, inbox_pack, 25, days_ago=10, final="billing")
    report = monitor(store, inbox_pack, now=NOW, providers=MEASURED)
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert sum(week["decisions"] for week in report["weeks"]) == 25


def test_a_report_covers_two_weeks_or_more(
    store: AuditStore, inbox_pack: Pack, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for days in (7, 13, 181):
        with pytest.raises(MendariError, match="14 to 180 days"):
            monitor(store, inbox_pack, days=days)
    assert len(monitor(store, inbox_pack, days=14, now=NOW)["weeks"]) == 2
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    held = client.get("/api/monitoring", params={"pack_id": inbox_pack.id, "days": 7}).json()
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
        return {
            "decisions": reviewed,
            "confidences": 0,
            "reviewed": reviewed,
            "override_rate": rate,
            "figures": {"overrides": figures.of(round(rate * reviewed), reviewed)},
        }

    bench = {"comparable": False, "tolerance": 0.03}
    assert [a["kind"] for a in _alerts(summary(30, 0.4), summary(30, 0.2), None, 0, bench)] == [
        "overrides"
    ]
    assert _alerts(summary(30, 0.3), summary(30, 0.2), None, 0, bench) == []
    assert _alerts(summary(5, 0.8), summary(30, 0.2), None, 0, bench) == []  # too few to say


def test_an_automated_decision_rated_wrong_raises_an_alert(
    store: AuditStore, inbox_pack: Pack
) -> None:
    (decision_id,) = seed(store, inbox_pack, 1, days_ago=1)
    store.record_feedback(
        decision_id, rating="down", expected={"question": "destination", "label": "billing"}
    )
    alerts = monitor(store, inbox_pack, now=NOW)["alerts"]
    assert [a["kind"] for a in alerts] == ["automated_wrong"]  # comparable or not


# -- the server and the command line ---------------------------------------------------------


def test_the_api_compares_with_the_providers_the_server_runs(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db", providers=SCREENED))
    decided = client.post(f"/api/packs/{inbox_pack.id}/decide", json=message(ROUTINE)).json()
    assert decided["outcome"] == "AUTO"
    report = client.get("/api/monitoring", params={"pack_id": inbox_pack.id}).json()
    assert report["last_7_days"]["decisions"] == 1 and report["pack"]["id"] == inbox_pack.id
    assert report["benchmark"]["reason"] == "other_providers"  # Jev screens; it measured rules
    measured = TestClient(create_app(db_path=tmp_path / "b.db"))
    report = measured.get("/api/monitoring", params={"pack_id": inbox_pack.id}).json()
    assert report["benchmark"]["comparable"] is True


def test_recorded_providers_are_this_builds_newest(store: AuditStore, inbox_pack: Pack) -> None:
    assert recorded_providers(store, inbox_pack) is None
    ran_here(store, inbox_pack, MEASURED)
    assert recorded_providers(store, inbox_pack) == MEASURED
    store.record_deployment(
        environment="test",
        release="t2",
        pack_id=inbox_pack.id,
        pack_version="0.0.1",
        pack_digest="sha256:" + "1" * 64,
        providers=JEV_SCREEN,
    )
    assert recorded_providers(store, inbox_pack) == MEASURED  # another build's record


def test_the_cli_exits_1_on_alerts_and_posts_them(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    reviewed(store, inbox_pack, 25, days_ago=1, final="billing")
    posted: list[Any] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        posted.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr("httpx.post", post)
    args = ["monitor", "--db", str(db), "--pack", inbox_pack.id]
    hook = ["--webhook", "https://alerts.example/hook"]
    assert main([*args, *hook]) == 0  # no server recorded what runs: nothing to compare
    assert "(no comparable benchmark: providers_unknown)" in capsys.readouterr().out
    ran_here(store, inbox_pack, MEASURED)
    assert main([*args, *hook]) == 1
    assert "[high] accuracy: reviewed decisions: accuracy 0 of 25 [" in capsys.readouterr().out
    assert posted[0][0] == "https://alerts.example/hook"
    assert posted[0][1]["alerts"][0]["pack"]["id"] == inbox_pack.id


def test_the_cli_checks_the_newest_version_of_each_pack(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copied = tmp_path / "older"
    shutil.copytree(inbox_pack.root, copied)
    manifest = copied / "pack.yaml"
    manifest.write_text(
        manifest.read_text().replace(f"version: {inbox_pack.version}", "version: 0.0.1")
    )
    older = load_pack(copied, origin="maintained")
    assert older.id == inbox_pack.id and older.version == "0.0.1"

    class Found:
        packs = [inbox_pack, older]

    monkeypatch.setattr("mendari.cli.discover", lambda *a, **k: Found())
    AuditStore(tmp_path / "a.db").close()
    assert main(["monitor", "--db", str(tmp_path / "a.db")]) == 0
    assert capsys.readouterr().out.count(f"{inbox_pack.id}:") == 1


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
    tmp_path: Path, inbox_pack: Pack, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    ran_here(store, inbox_pack, MEASURED)
    with tenancy.acting_for("clinic-a"):
        reviewed(store, inbox_pack, 25, days_ago=1, final="billing")
    args = ["monitor", "--db", str(db), "--pack", inbox_pack.id]
    assert main([*args, "--org", "clinic-b"]) == 0
    assert main([*args, "--org", "clinic-a"]) == 1
    assert "reviewed decisions: accuracy 0 of 25 [" in capsys.readouterr().out
