# SPDX-License-Identifier: Apache-2.0
"""The scheduled monitor (LP-21f): `GET /api/internal/monitor` with CRON_SECRET only, each
organization on its own (the status checks' left out), one recorded alert per organization
that alerts, the pending ones sent again first, a time limit that says what it left; and
`mendari monitor --each-org`, the same from the command line. The decisions are the triage
fixture's."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, Pack
from mendari import alerts as alerts_module
from mendari.alerts import AlertSettings, deliver
from mendari.cli import main
from mendari.core import clock as clock_module
from mendari.core import db as db_module
from mendari.core import tenancy
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import app as app_module
from mendari.server.app import create_app
from tests.conftest import ROOT, encrypt_mode, encrypted_store, message, signed_in
from tests.test_hosted_data import sign_in_env

CRON = "a-cron-secret-of-some-length-0123456789"
HOOK = "https://hooks.example.org/services/T000/B000/WebhookSecretToken"
ROUTE = "/api/internal/monitor"
# Automated: the pharmacy's, with the screen running its agent (canned answers that find nothing
# urgent), as the fixture's policy requires.
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"
SCREENED = {"screen": "agent"}


class Webhook:
    """A webhook that answers each post with the next status (or raises the next error)."""

    def __init__(self, *answers: int | Exception):
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.sent.append({"url": url, **kwargs, "body": json.loads(kwargs["content"])})
        answer = self.answers.pop(0) if self.answers else 204
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, request=httpx.Request("POST", url))


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts_module, "_sleep", lambda seconds: None)


class Clock:
    """The route's and the alerts' monotonic clock, moved only by what the test says takes time
    (a post's timeout, a wait between tries); everything else of `time` as it is."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(app_module, "time", clock)
    monkeypatch.setattr(alerts_module, "time", clock)

    def wait(seconds: float) -> None:
        clock.now += seconds

    monkeypatch.setattr(alerts_module, "_sleep", wait)
    return clock


class HungWebhook:
    """A receiver that never answers: each post takes its whole timeout, then fails."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.sent.append(json.loads(kwargs["content"]))
        self.clock.now += kwargs["timeout"]
        raise httpx.ConnectTimeout("no answer")


def pending(store: AuditStore, count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """`count` quota alerts that failed their one try, due again now."""
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(*[503] * count))
    alert = {"severity": "warning", "kind": "quota_reached", "message": "x"}
    for _ in range(count):
        deliver(AlertSettings(HOOK), event="quota.reached", org="clinic-a", alerts=[alert],
                lines=["x"], attempts=1, timeout=5, store=store)  # fmt: skip
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE alert_deliveries SET next_attempt_at = '2020-01-01T00:00:00+00:00'")


def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, secret: str | None = CRON
) -> TestClient:
    encrypt_mode(monkeypatch)
    sign_in_env(monkeypatch)
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", HOOK)
    if secret is not None:
        monkeypatch.setenv("CRON_SECRET", secret)
    return TestClient(create_app(db_path=tmp_path / "a.db"), base_url="https://mendari.test")


def degraded(store: AuditStore, pack: Pack, org: str) -> None:
    """An automated decision of `org` someone rated wrong: an alert, comparable or not."""
    with tenancy.acting_for(org):
        decided = Decision(pack, providers=SCREENED, store=store)(message(ROUTINE))
        expected = {"question": "destination", "label": "billing"}
        store.record_feedback(decided.id, rating="down", expected=expected)


def healthy(store: AuditStore, pack: Pack, org: str) -> None:
    with tenancy.acting_for(org):
        Decision(pack, providers=SCREENED, store=store)(message(ROUTINE))


def run(client: TestClient) -> dict[str, Any]:
    answer = client.get(ROUTE, headers={"authorization": f"Bearer {CRON}"})
    assert answer.status_code == 200, answer.text
    result: dict[str, Any] = answer.json()
    return result


# -- the acceptance tests ----------------------------------------------------------------------


def test_the_cron_route_alerts_on_a_seeded_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "clinic-a")
    healthy(store, triage_agent, "clinic-b")
    assert run(client) == {
        "organizations": 2,
        "alerting": 1,
        "delivered": 1,
        "not_delivered": 0,
        "not_reached": 0,
        "retried": {"delivered": 0, "pending": 0, "failed": 0, "deferred": 0},
    }
    (sent,) = hook.sent
    assert sent["body"]["organization"] == "clinic-a" and sent["body"]["event"] == "monitor.alerts"
    kinds = [a["kind"] for report in sent["body"]["alerts"] for a in report["alerts"]]
    assert kinds == ["automated_wrong"] and ROUTINE not in json.dumps(sent["body"])
    (recorded,) = store.alert_deliveries()
    assert (recorded["org"], recorded["status"]) == ("clinic-a", "delivered")


def test_the_cron_route_refuses_a_request_without_its_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[bool] = []
    for backend in (db_module.SqliteBackend, db_module.PostgresBackend):
        real = backend.session

        def spy(self: Any, *, exclusive: bool, _real: Any = real) -> Any:
            opened.append(exclusive)
            return _real(self, exclusive=exclusive)

        monkeypatch.setattr(backend, "session", spy)
    off = server(tmp_path / "off", monkeypatch, secret=None)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    opened.clear()
    assert off.get(ROUTE, headers={"authorization": f"Bearer {CRON}"}).status_code == 404
    assert opened == []  # off: not found before any database work
    client = server(tmp_path, monkeypatch)
    admin = signed_in(encrypted_store(tmp_path / "a.db"), role="admin")
    opened.clear()
    assert client.get(ROUTE).status_code == 401
    wrong = {"authorization": "Bearer " + CRON[:-1] + "x"}
    assert client.get(ROUTE, headers=wrong).status_code == 401
    assert client.get(ROUTE, headers=admin).status_code == 401  # a session is not the secret
    assert opened == []  # refused before any database work


# -- one organization at a time ---------------------------------------------------------------


def test_alerts_never_mix_organizations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "clinic-a")
    degraded(store, triage_agent, "clinic-b")
    degraded(store, triage_agent, "clinic-b")
    assert run(client)["delivered"] == 2
    by_org = {sent["body"]["organization"]: sent["body"] for sent in hook.sent}
    assert set(by_org) == {"clinic-a", "clinic-b"}
    for org, body in by_org.items():
        other = "clinic-b" if org == "clinic-a" else "clinic-a"
        assert other not in json.dumps(body)
    counted = [a["message"] for a in by_org["clinic-b"]["alerts"][0]["alerts"]]
    assert counted == ["2 automated decision(s) rated wrong in the last 7 days"]  # its own two


def test_the_status_organization_is_not_monitored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    monkeypatch.setenv("MENDARI_STATUS_ORG", "status")
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "status")
    healthy(store, triage_agent, "clinic-a")
    assert run(client)["organizations"] == 1 and hook.sent == []


def test_an_overrun_reports_the_organizations_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    monkeypatch.setattr(app_module, "MONITOR_SECONDS", -1.0)  # out of time from the start
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "clinic-a")
    healthy(store, triage_agent, "clinic-b")
    result = run(client)
    assert (result["organizations"], result["not_reached"]) == (0, 2)
    (sent,) = hook.sent
    assert sent["body"]["event"] == "monitor.incomplete" and sent["body"]["organization"] is None
    (incomplete,) = sent["body"]["alerts"]
    assert incomplete["kind"] == "monitor_incomplete"
    named = incomplete["message"].removesuffix(")").split("after 0 (")[1]
    assert sorted(named.split(", ")) == ["clinic-a", "clinic-b"]  # ids: the operator's alert


def test_alerts_still_pending_are_sent_again_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_waiting: None
) -> None:
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(503))
    alert = {"severity": "warning", "kind": "quota_reached", "message": "x"}
    assert not deliver(
        AlertSettings(HOOK),
        event="quota.reached",
        org="clinic-a",
        alerts=[alert],
        lines=["x"],
        attempts=1,
        timeout=5,
        store=store,
    )
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE alert_deliveries SET next_attempt_at = '2020-01-01T00:00:00+00:00'")
    up = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", up)
    assert run(client)["retried"] == {"delivered": 1, "pending": 0, "failed": 0, "deferred": 0}
    assert up.sent[0]["body"]["event"] == "quota.reached"


def test_an_organization_s_alert_that_is_not_delivered_is_tried_three_times_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, no_waiting: None
) -> None:
    hook = Webhook(503, 503, 503)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "clinic-a")
    result = run(client)
    assert (result["alerting"], result["delivered"], result["not_delivered"]) == (1, 0, 1)
    assert [sent["body"]["organization"] for sent in hook.sent] == ["clinic-a"] * 3
    (recorded,) = store.alert_deliveries()
    assert (recorded["org"], recorded["status"], recorded["attempts"]) == ("clinic-a", "pending", 3)


def test_a_hung_webhook_and_a_backlog_keep_the_run_inside_the_function_s_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, fake_clock: Clock
) -> None:
    """20 pending alerts and five organizations alerting, to a receiver that never answers:
    the retries stop at 60 s (the rest stay due), no organization starts after 180 s, and the
    alert naming those left is still sent, all well inside the 300 s a function has."""
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    pending(store, 20, monkeypatch)
    orgs = [f"clinic-{letter}" for letter in "abcde"]
    for org in orgs:
        degraded(store, triage_agent, org)
    monkeypatch.setattr(clock_module, "today", lambda: date.fromordinal(5 * 1000))  # a first
    hung = HungWebhook(fake_clock)
    monkeypatch.setattr(alerts_module.httpx, "post", hung)
    result = run(client)
    assert result["retried"] == {"delivered": 0, "pending": 6, "failed": 0, "deferred": 14}
    # 60 s of retries; four organizations of 40 s each (three tries of 10 s, 2 + 8 s of waits),
    # the fourth started at 180 s; the fifth left; then the incomplete alert's one try.
    assert (result["organizations"], result["not_reached"]) == (4, 1)
    assert fake_clock.now == 60 + 4 * 40 + 10 <= 300 - 60
    assert hung.sent[-1]["event"] == "monitor.incomplete"
    assert hung.sent[-1]["alerts"][0]["message"].endswith("(clinic-e)")
    due = [row for row in store.alert_deliveries() if row["event"] == "quota.reached"]
    assert sum(row["attempts"] == 2 for row in due) == 6 and len(due) == 20  # 14 still due


def test_each_day_starts_at_another_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, triage_agent: Pack, fake_clock: Clock
) -> None:
    """With time for one organization a run, two days in a row reach both."""
    monkeypatch.setattr(app_module, "MONITOR_SECONDS", 0.0)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, triage_agent, "clinic-a")
    degraded(store, triage_agent, "clinic-b")
    reached: list[str] = []
    for day in (800_000, 800_001):
        monkeypatch.setattr(clock_module, "today", lambda day=day: date.fromordinal(day))
        hung = HungWebhook(fake_clock)
        monkeypatch.setattr(alerts_module.httpx, "post", hung)
        fake_clock.now = 0.0
        assert run(client)["not_reached"] == 1
        reached.append(hung.sent[0]["organization"])
    assert sorted(reached) == ["clinic-a", "clinic-b"]


def test_a_webhook_it_cannot_use_posts_nothing_and_the_route_still_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    triage_agent: Pack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", "http://hooks.example.org/plain")
    client = TestClient(create_app(db_path=tmp_path / "b.db"), base_url="https://mendari.test")
    degraded(encrypted_store(tmp_path / "b.db"), triage_agent, "clinic-a")
    with caplog.at_level(logging.WARNING, logger="mendari.monitor"):
        result = run(client)
    assert (result["alerting"], result["delivered"]) == (1, 0) and hook.sent == []
    assert "not usable" in caplog.text and "hooks.example.org" not in caplog.text


def test_a_status_organization_no_organization_could_have_fails_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_STATUS_ORG", "Not An Id")
    with pytest.raises(MendariError, match="MENDARI_STATUS_ORG"):
        create_app(db_path=tmp_path / "a.db")


# -- the command line ----------------------------------------------------------------------------


def test_mendari_monitor_each_org_posts_one_alert_per_organization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    triage_agent: Pack,
    no_waiting: None,
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    degraded(store, triage_agent, "clinic-a")
    degraded(store, triage_agent, "status")
    healthy(store, triage_agent, "clinic-b")
    monkeypatch.setenv("MENDARI_STATUS_ORG", "status")
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    args = ["monitor", "--db", str(db), "--agent", triage_agent.id, "--each-org", "--webhook", HOOK]
    assert main(args) == 0  # a scheduled job: every alert posted is the job done
    out = capsys.readouterr().out
    assert f"clinic-a: {triage_agent.id}: 1 alert(s)" in out
    assert f"clinic-b: {triage_agent.id}: ok" in out and "status:" not in out
    assert [sent["body"]["organization"] for sent in hook.sent] == ["clinic-a"]
    assert main(args[:-2]) == 1  # alerts with nowhere to post them
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(*[httpx.ConnectError("down")] * 3))
    assert main(args) == 3
    assert main(["monitor", "--db", str(db), "--org", "x", "--each-org"]) == 1  # one or the other
    assert "not allowed with argument" in capsys.readouterr().err


def test_a_lost_alert_or_a_failed_organization_never_hides_behind_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    triage_agent: Pack,
    no_waiting: None,
) -> None:
    """clinic-a's alert is lost; clinic-b's is posted after it: still exit 3. clinic-a's
    monitoring fails; clinic-b is still monitored and posted: exit 1, the line names clinic-a."""
    db = tmp_path / "a.db"
    store = AuditStore(db)
    degraded(store, triage_agent, "clinic-a")
    degraded(store, triage_agent, "clinic-b")
    args = ["monitor", "--db", str(db), "--agent", triage_agent.id, "--each-org", "--webhook", HOOK]
    hook = Webhook(*[httpx.ConnectError("down")] * 3)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    assert main(args) == 3
    assert [sent["body"]["organization"] for sent in hook.sent] == ["clinic-a"] * 3 + ["clinic-b"]
    from mendari.evals import monitoring

    real = monitoring.monitor

    def failing(store: AuditStore, pack: Pack, **kwargs: Any) -> dict[str, Any]:
        if tenancy.current() == "clinic-a":
            raise MendariError("cannot read clinic-a's records")
        return real(store, pack, **kwargs)

    monkeypatch.setattr(monitoring, "monitor", failing)
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    capsys.readouterr()
    assert main(args) == 1
    assert "error: clinic-a: cannot read clinic-a's records" in capsys.readouterr().err
    assert [sent["body"]["organization"] for sent in hook.sent] == ["clinic-b"]
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(*[httpx.ConnectError("down")] * 3))
    assert main(args) == 3  # clinic-a failed and clinic-b's alert was lost: the lost alert shows


# -- the templates ---------------------------------------------------------------------------------


def test_the_kubernetes_job_fails_on_any_failure_and_is_never_run_again(tmp_path: Path) -> None:
    """Every command runs whatever the others did (the purge, the checkpoint, the monitor), and
    the job fails if any failed; it is never re-run (that would post every organization's alert
    again)."""
    documents = list(yaml.safe_load_all((ROOT / "deploy/kubernetes/mendari.yaml").read_text()))
    (job,) = [d for d in documents if d["kind"] == "CronJob"]
    spec = job["spec"]["jobTemplate"]["spec"]
    assert spec["backoffLimit"] == 0 and spec["template"]["spec"]["restartPolicy"] == "Never"
    (container,) = spec["template"]["spec"]["containers"]
    shell, flag, line = container["command"]
    fake = tmp_path / "mendari"
    fake.write_text(
        '#!/bin/sh\necho "$1" >> "$RAN"\n'
        'case "$1" in phi) exit "$PURGE";; audit) exit "$CHECKPOINT";; esac\nexit "$MONITOR"\n'
    )
    fake.chmod(0o755)

    def job_exit(purge: int, checkpoint: int, monitor: int) -> tuple[int, list[str]]:
        ran = tmp_path / f"ran-{purge}-{checkpoint}-{monitor}"
        env = {
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RAN": str(ran),
            "PURGE": str(purge),
            "CHECKPOINT": str(checkpoint),
            "MONITOR": str(monitor),
            "DATABASE_URL": "postgresql://x",
        }
        done = subprocess.run([shell, flag, line], env=env, check=False)
        return done.returncode, ran.read_text().split()

    assert job_exit(0, 0, 0) == (0, ["phi", "audit", "monitor"])
    for exits in ((0, 0, 1), (0, 0, 3), (1, 0, 0), (0, 1, 0)):
        code, ran = job_exit(*exits)
        assert code != 0 and ran == ["phi", "audit", "monitor"], exits
