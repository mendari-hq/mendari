# SPDX-License-Identifier: Apache-2.0
"""The scheduled monitor (LP-21f): `GET /api/internal/monitor` with CRON_SECRET only, each
organization on its own (the status checks' left out), one recorded alert per organization
that alerts, the pending ones sent again first, a time limit that says what it left; and
`mendari monitor --each-org`, the same from the command line."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import Decision, Pack
from mendari import alerts as alerts_module
from mendari.alerts import AlertSettings, deliver
from mendari.cli import main
from mendari.core import db as db_module
from mendari.core import tenancy
from mendari.core.audit import AuditStore
from mendari.errors import MendariError
from mendari.server import app as app_module
from mendari.server.app import create_app
from tests.conftest import SCREENED, encrypt_mode, encrypted_store, message, signed_in
from tests.test_hosted_data import sign_in_env

pytestmark = pytest.mark.usefixtures("screened")  # automated decisions: the screen on fake Jev

CRON = "a-cron-secret-of-some-length-0123456789"
HOOK = "https://hooks.example.org/services/T000/B000/WebhookSecretToken"
ROUTE = "/api/internal/monitor"
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, inbox_pack, "clinic-a")
    healthy(store, inbox_pack, "clinic-b")
    assert run(client) == {
        "organizations": 2,
        "alerting": 1,
        "delivered": 1,
        "not_delivered": 0,
        "not_reached": 0,
        "retried": {"delivered": 0, "pending": 0, "failed": 0},
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, inbox_pack, "clinic-a")
    degraded(store, inbox_pack, "clinic-b")
    degraded(store, inbox_pack, "clinic-b")
    assert run(client)["delivered"] == 2
    by_org = {sent["body"]["organization"]: sent["body"] for sent in hook.sent}
    assert set(by_org) == {"clinic-a", "clinic-b"}
    for org, body in by_org.items():
        other = "clinic-b" if org == "clinic-a" else "clinic-a"
        assert other not in json.dumps(body)
    counted = [a["message"] for a in by_org["clinic-b"]["alerts"][0]["alerts"]]
    assert counted == ["2 automated decision(s) rated wrong in the last 7 days"]  # its own two


def test_the_status_organization_is_not_monitored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    monkeypatch.setenv("MENDARI_STATUS_ORG", "status")
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, inbox_pack, "status")
    healthy(store, inbox_pack, "clinic-a")
    assert run(client)["organizations"] == 1 and hook.sent == []


def test_an_overrun_reports_the_organizations_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_pack: Pack, no_waiting: None
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    monkeypatch.setattr(app_module, "MONITOR_SECONDS", -1.0)  # out of time from the start
    client = server(tmp_path, monkeypatch)
    store = encrypted_store(tmp_path / "a.db")
    degraded(store, inbox_pack, "clinic-a")
    healthy(store, inbox_pack, "clinic-b")
    result = run(client)
    assert (result["organizations"], result["not_reached"]) == (0, 2)
    (sent,) = hook.sent
    assert sent["body"]["event"] == "monitor.incomplete" and sent["body"]["organization"] is None
    assert sent["body"]["alerts"][0]["kind"] == "monitor_incomplete"


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
    assert run(client)["retried"] == {"delivered": 1, "pending": 0, "failed": 0}
    assert up.sent[0]["body"]["event"] == "quota.reached"


def test_a_webhook_it_cannot_use_posts_nothing_and_the_route_still_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inbox_pack: Pack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    client = server(tmp_path, monkeypatch)
    monkeypatch.setenv("MENDARI_ALERT_WEBHOOK", "http://hooks.example.org/plain")
    client = TestClient(create_app(db_path=tmp_path / "b.db"), base_url="https://mendari.test")
    degraded(encrypted_store(tmp_path / "b.db"), inbox_pack, "clinic-a")
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
    inbox_pack: Pack,
    no_waiting: None,
) -> None:
    db = tmp_path / "a.db"
    store = AuditStore(db)
    degraded(store, inbox_pack, "clinic-a")
    degraded(store, inbox_pack, "status")
    healthy(store, inbox_pack, "clinic-b")
    monkeypatch.setenv("MENDARI_STATUS_ORG", "status")
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    args = ["monitor", "--db", str(db), "--pack", inbox_pack.id, "--each-org", "--webhook", HOOK]
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "clinic-a: mendari/patient-inbox-router: 1 alert(s)" in out
    assert "clinic-b: mendari/patient-inbox-router: ok" in out and "status:" not in out
    assert [sent["body"]["organization"] for sent in hook.sent] == ["clinic-a"]
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(*[httpx.ConnectError("down")] * 3))
    assert main(args) == 3
    assert main(["monitor", "--db", str(db), "--org", "x", "--each-org"]) == 1  # one or the other
    assert "not allowed with argument" in capsys.readouterr().err
