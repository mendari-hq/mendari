# SPDX-License-Identifier: Apache-2.0
"""Monitoring a deployed pack over time (evals/monitoring.py): series, drift and alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import tenancy
from mendari.errors import MendariError
from mendari.evals import figures
from mendari.evals.monitoring import _alerts, _psi, check_webhook, monitor
from mendari.server.app import create_app
from tests.conftest import SCREENED, message

# Every decision here is automated: the urgency screen runs with a fake Jev (`screened`).
pytestmark = pytest.mark.usefixtures("screened")

ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO
NOW = datetime.now(UTC)


def seed(
    store: AuditStore, pack: Pack, n: int, *, days_ago: int, final: str | None = None
) -> list[str]:
    decide = Decision(pack, providers=SCREENED, store=store)
    ids = []
    for i in range(n):
        result = decide(message(ROUTINE))
        when = (NOW - timedelta(days=days_ago, minutes=i)).isoformat(timespec="milliseconds")
        with store._db.session(exclusive=True) as db:
            db.all("UPDATE decisions SET created_at = ? WHERE id = ?", (when, result.id))
        if final is not None:
            label = result.recommendation if final == "same" else final
            store.record_outcome(
                result.id, final_label=str(label), source="staff-reroute", recorded_by="rn-9"
            )
        ids.append(result.id)
    return ids


def test_a_quiet_pack_has_no_alerts(store: AuditStore, inbox_pack: Pack) -> None:
    report = monitor(store, inbox_pack, now=NOW)
    assert report["alerts"] == [] and report["last_7_days"]["decisions"] == 0
    assert len(report["weeks"]) == 5 and report["last_7_days"]["drift_psi"] is None
    assert report["benchmark"]["accuracy"] is not None


def test_observed_accuracy_below_the_benchmark_raises_an_alert(
    store: AuditStore, inbox_pack: Pack
) -> None:
    seed(store, inbox_pack, 25, days_ago=2, final="billing")  # staff kept re-routing: wrong
    report = monitor(store, inbox_pack, now=NOW)
    recent = report["last_7_days"]
    assert (
        recent["decisions"] == 25
        and recent["labelled"] == 25
        and recent["observed_accuracy"] == 0.0
    )
    assert [a["kind"] for a in report["alerts"]] == ["accuracy"]
    assert report["weeks"][-1]["decisions"] == 25


def test_right_answers_raise_nothing(store: AuditStore, inbox_pack: Pack) -> None:
    seed(store, inbox_pack, 25, days_ago=2, final="same")
    report = monitor(store, inbox_pack, now=NOW)
    assert report["last_7_days"]["observed_accuracy"] == 1.0 and report["alerts"] == []


def test_an_automated_decision_rated_wrong_raises_an_alert(
    store: AuditStore, inbox_pack: Pack
) -> None:
    (decision_id,) = seed(store, inbox_pack, 1, days_ago=1)
    store.record_feedback(
        decision_id, rating="down", expected={"question": "destination", "label": "billing"}
    )
    alerts = monitor(store, inbox_pack, now=NOW)["alerts"]
    assert [a["kind"] for a in alerts] == ["automated_wrong"]


def test_old_decisions_fall_outside_the_last_week(store: AuditStore, inbox_pack: Pack) -> None:
    seed(store, inbox_pack, 25, days_ago=10, final="billing")
    report = monitor(store, inbox_pack, now=NOW)
    assert report["last_7_days"]["decisions"] == 0 and report["alerts"] == []
    assert sum(week["decisions"] for week in report["weeks"]) == 25


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
            "observed_accuracy": None,
            "labelled": 0,
            "decisions": reviewed,
            "reviewed": reviewed,
            "override_rate": rate,
            "figures": {"overrides": figures.of(round(rate * reviewed), reviewed)},
        }

    bench = {"accuracy": 0.9, "tolerance": 0.03}
    assert [a["kind"] for a in _alerts(summary(30, 0.4), summary(30, 0.2), None, 0, bench)] == [
        "overrides"
    ]
    assert _alerts(summary(30, 0.3), summary(30, 0.2), None, 0, bench) == []
    assert _alerts(summary(5, 0.8), summary(30, 0.2), None, 0, bench) == []  # too few to say


def test_the_api_serves_monitoring(tmp_path: Path, inbox_pack: Pack) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db", providers=SCREENED))
    decided = client.post(f"/api/packs/{inbox_pack.id}/decide", json=message(ROUTINE)).json()
    assert decided["outcome"] == "AUTO"
    report = client.get("/api/monitoring", params={"pack_id": inbox_pack.id}).json()
    assert report["last_7_days"]["decisions"] == 1 and report["pack"]["id"] == inbox_pack.id


def test_the_cli_exits_1_on_alerts_and_posts_them(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    seed(store, inbox_pack, 25, days_ago=1, final="billing")
    posted: list[Any] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        posted.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setattr("httpx.post", post)
    code = main(
        [
            "monitor",
            "--db",
            str(db),
            "--pack",
            inbox_pack.id,
            "--webhook",
            "https://alerts.example/hook",
        ]
    )
    assert code == 1
    assert "[high] accuracy: observed accuracy 0 of 25 [" in capsys.readouterr().out
    assert posted[0][0] == "https://alerts.example/hook"
    assert posted[0][1]["alerts"][0]["pack"]["id"] == inbox_pack.id


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
    with tenancy.acting_for("clinic-a"):
        seed(store, inbox_pack, 25, days_ago=1, final="billing")
    args = ["monitor", "--db", str(db), "--pack", inbox_pack.id]
    assert main([*args, "--org", "clinic-b"]) == 0
    assert main([*args, "--org", "clinic-a"]) == 1
    assert "observed accuracy 0 of 25 [" in capsys.readouterr().out
