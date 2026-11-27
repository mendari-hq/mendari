# SPDX-License-Identifier: Apache-2.0
"""The System routes (`server/system_routes.py`): the deployment's readiness and its operator's
actions, on a server set up as the one-install path leaves it (memberships mode, an operator
organization, patient data mode, the scheduled jobs' secret, a signed checkpoint, an alert
webhook).

- The checks are read from what health and readiness say, and hold nothing secret: every secret
  a deployment has is seeded with a mark and searched for in every answer.
- Every action is refused to a role without the right, and to another organization's
  administrator.
- Verifying the chain records when it ran, and a chain that does not verify raises the alert.
- A test alert is recorded as delivered or failed, never left to be sent again.
- The EHR check answers only for an EHR registered to the caller's organization.
- The scheduled jobs and the backup report leave their last run for the checks.

SQLite and Postgres."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari import alerts as alerts_module
from mendari.server import recalls, smart_check
from mendari.server.app import create_app
from mendari.server.smart_check import Finding
from tests.conftest import (
    DASHBOARD,
    DEMO_AGENT_DIR,
    HASH_KEY_B64,
    PHI_KEY_B64,
    ROOT,
    encrypt_mode,
    encrypted_store,
    member,
    message,
    security_lines,
)
from tests.test_auth import CLIENT, IDP
from tests.test_chain_orgs import tamper
from tests.test_monitor_cron import Webhook
from tests.test_system_checks import KEYS

HTTPS = "https://mendari.test"
CHECKS = "/api/system/checks"
ACTIONS = ("/api/system/audit/verify", "/api/system/alerts/test", "/api/system/ehr-check")
EHR = "https://ehr.stmarys.example/fhir"
OTHER_EHR = "https://ehr.zqother.example/fhir"
# What someone with the database does to the chain's first event.
BROKEN = "UPDATE audit_events SET payload = '{}' WHERE seq = (SELECT MIN(seq) FROM audit_events)"
# Every secret a deployment holds, each with a mark to search the answers for.
CRON = "zq-cron-secret-0123456789abcdef"
SECRETS = {
    "MENDARI_OIDC_CLIENT_SECRET": "zq-oidc-client-secret",
    "CRON_SECRET": CRON,
    "MENDARI_ALERT_SECRET": "zq-alert-signing-secret-0123456789abcdef",
    "MENDARI_ALERT_WEBHOOK": "https://hooks.stmarys.example/services/T0/B1/zq-webhook-token",
    "MENDARI_CHECKPOINT_KEY": base64.b64encode(b"zq-checkpoint-key-32-bytes-long!").decode(),
    "MENDARI_REGISTRY_URL": "https://registry.stmarys.example/zq-registry-path",
    "MENDARI_REGISTRY_TOKEN": "zq-registry-token",
    "MENDARI_AGENT_TOKEN": "zq-agent-token",
    "DATABASE_URL": "postgresql://mendari:zq-database-password@db.internal:5432/mendari",
}


@dataclass
class Site:
    client: TestClient
    store: AuditStore
    operator: dict[str, str]  # an administrator of the operator organization, signed in

    def checks(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        answer = self.client.get(CHECKS, headers=headers or self.operator)
        assert answer.status_code == 200, answer.text
        body: dict[str, Any] = answer.json()
        return body

    def check(self, key: str) -> dict[str, Any]:
        return next(check for check in self.checks()["checks"] if check["key"] == key)

    def job(self, name: str) -> Any:
        return self.client.get(f"/api/internal/{name}", headers={"authorization": f"Bearer {CRON}"})


def configure(monkeypatch: pytest.MonkeyPatch, *, webhook: bool = True) -> None:
    """A deployment as the installer sets one up, with every secret marked."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_OPERATOR_ORG", "default")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    encrypt_mode(monkeypatch)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    if not webhook:
        monkeypatch.delenv("MENDARI_ALERT_WEBHOOK")
    # The registry is asked whether it answers: never over the network in a test.
    monkeypatch.setattr(recalls, "fetch_signs", lambda url: True)


@pytest.fixture
def site(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    configure(monkeypatch)
    app = create_app(db_path=db_target, agents_paths=[ROOT / "agents"])
    with (
        TestClient(app, base_url=HTTPS, headers=DASHBOARD) as client,
        encrypted_store(db_target) as store,
    ):
        operator = member(store, org="default", roles=["admin"], sub="operator")
        yield Site(client, store, operator)


def decide(store: AuditStore, count: int = 2) -> None:
    decision = Decision(load_agent(DEMO_AGENT_DIR), store=store)
    for n in range(count):
        decision(message(f"Please refill my prescription, number {n}."))


# -- the checks -----------------------------------------------------------------------------------


def test_the_checks_answer_the_operator_from_what_health_and_readiness_say(site: Site) -> None:
    body = site.checks()
    assert body["format"] == "mendari.system-checks/1"
    assert [check["key"] for check in body["checks"]] == KEYS
    assert sum(body["summary"].values()) == len(KEYS)
    health = site.client.get("/api/health", headers=site.operator).json()
    ready = site.client.get("/api/health/ready").json()
    assert body["version"] == health["version"] == ready["version"]
    states = {check["key"]: check["state"] for check in body["checks"]}
    assert states["sign_in"] == "ok" and states["patient_data"] == "ok"
    # The database answers; a deployment with sign-in on a single SQLite file is told so.
    assert ready["checks"]["database"] == "ok"
    assert states["database"] == ("ok" if health["storage"] == "postgres" else "attention")
    # What health shows by id, the checks name by the same id.
    assert health["phi_keys"]["current"] in site.check("sealing_key")["detail"]
    # One administrator so far: the first thing the checks ask for is a second.
    assert states["operators"] == "attention"
    assert "Invite a second administrator" in site.check("operators")["action"]
    # A fresh install: no decision yet, only the bundled demo agent.
    assert body["setup"] == {"fresh": True, "finished": False}
    assert states["agents"] == "not_set" and body["recalls"] == []
    decide(site.store, 1)
    assert site.checks()["setup"]["fresh"] is False


def test_nothing_secret_is_in_the_checks_or_in_any_action_s_answer(
    site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every secret this deployment holds carries a mark (`zq-`), and the keys are known: none
    of them, in any encoding a setting has, is in the checks, in an action's answer or in the
    security log's lines about them. Their public ids are."""
    monkeypatch.setattr(alerts_module.httpx, "post", Webhook(204, 500))
    monkeypatch.setattr(
        smart_check, "check", lambda base: [Finding("ok", "registration key", base)]
    )
    capsys.readouterr()
    client, operator = site.client, site.operator
    answers = [
        client.get(CHECKS, headers=operator),
        client.post("/api/system/audit/verify", headers=operator),
        client.post("/api/system/alerts/test", headers=operator),
        client.post("/api/system/alerts/test", headers=operator),  # the webhook answers 500
        client.post("/api/system/ehr-check", headers=operator, json={"base": EHR}),
        client.get(CHECKS, headers=operator),
    ]
    assert [answer.status_code for answer in answers] == [200, 200, 200, 200, 404, 200]
    written = "\n".join(answer.text for answer in answers)
    written += json.dumps(security_lines(capsys.readouterr().out))
    assert "zq-" not in written
    for secret in (*SECRETS.values(), PHI_KEY_B64, HASH_KEY_B64):
        assert secret not in written, secret
    for part in ("hooks.stmarys.example", "db.internal", "zq-registry-path"):
        assert part not in written, part
    # What is public is said: the registry by its origin, the keys by their ids.
    assert "https://registry.stmarys.example" in written
    assert site.check("alerts")["state"] == "attention"  # the last one was not delivered
    assert "the webhook answered HTTP 500" in site.check("alerts")["detail"]


def test_every_system_route_is_refused_to_a_role_without_the_right(site: Site) -> None:
    """The System routes answer the operator organization's administrators, signed in as
    people. Every other role, a machine, another organization's administrator and nobody at all
    are refused each of them, before anything runs."""
    client, store = site.client, site.store
    callers = {
        role: member(store, org="default", roles=[role], sub=f"p-{role}")
        for role in ("viewer", "reviewer", "service", "governance", "labeller", "agent_author")
    }
    callers["another organization's administrator"] = member(
        store, org="clinic-b", roles=["admin"], sub="lead-b"
    )
    for role in ("viewer", "service"):
        key = store.create_api_key(name=role, role=role, org="default", created_by="t")
        callers[f"a {role} key"] = {"authorization": f"Bearer {key['key']}"}
    before = len(store.runs())
    for who, headers in callers.items():
        assert client.get(CHECKS, headers=headers).status_code == 403, who
        for path in ACTIONS:
            refused = client.post(path, headers=headers, json={"base": EHR})
            assert refused.status_code == 403, (who, path)
            assert "zq-" not in refused.text
    assert client.get(CHECKS, headers={"cookie": ""}).status_code == 401
    for path in ACTIONS:
        assert client.post(path, headers={"cookie": ""}).status_code == 401, path
    assert len(store.runs()) == before  # nothing ran for any of them
    # The organization's own actions the System view offers keep their own permissions:
    # holding automation is governance's and an administrator's, erasing an administrator's.
    hold = {"reason": "a drill"}
    assert client.post("/api/holds", headers=callers["viewer"], json=hold).status_code == 403
    assert client.post("/api/holds", headers=callers["reviewer"], json=hold).status_code == 403
    held = client.post("/api/holds", headers=callers["governance"], json=hold)
    assert held.status_code == 201 and held.json()["agent_id"] is None  # every agent
    erase = {"reason": "request"}
    for role in ("viewer", "reviewer", "governance"):
        refused = client.post("/api/audit/d1/erase", headers=callers[role], json=erase)
        assert refused.status_code == 403, role


def test_what_the_checks_show_of_an_organization_is_the_caller_s_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two organizations' EHRs are registered; the operator's checks count its own, and the
    other's FHIR base is in no answer. The EHR check refuses the other's as an unknown one, and
    so a base that is not a URL, one that does not even split as one included."""
    configure(monkeypatch)
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv(
        "MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.stmarys.example,https://ehr.zqother.example"
    )
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR}=default:decide,{OTHER_EHR}=clinic-b:view")
    called: list[str] = []

    def check(base: str) -> list[Finding]:
        called.append(base)
        return [Finding("ok", "discovery", "issuer and endpoints on the allow-list")]

    monkeypatch.setattr(smart_check, "check", check)
    db = tmp_path / "a.db"
    with TestClient(create_app(db_path=db), base_url=HTTPS, headers=DASHBOARD) as client:
        with encrypted_store(db) as store:
            operator = member(store, org="default", roles=["admin"], sub="operator")
        shown = client.get(CHECKS, headers=operator)
        ehr = next(c for c in shown.json()["checks"] if c["key"] == "ehr")
        assert ehr["state"] == "attention" and "1 EHR registered to launch" in ehr["detail"]
        assert "zqother" not in shown.text
        for base in (OTHER_EHR, "https://ehr.unknown.example/fhir", "not a url", "http://["):
            refused = client.post("/api/system/ehr-check", headers=operator, json={"base": base})
            assert refused.status_code == 404, base
            assert refused.json()["detail"] == "no EHR with this FHIR base is registered to you"
        assert called == []  # nothing was asked of anyone's servers
        checked = client.post("/api/system/ehr-check", headers=operator, json={"base": EHR})
        assert checked.status_code == 200 and called == [EHR]
        assert checked.json() == {
            "base": EHR,
            "ok": True,
            "findings": [
                {
                    "status": "ok",
                    "check": "discovery",
                    "detail": "issuer and endpoints on the allow-list",
                }
            ],
        }
        ehr = next(
            c for c in client.get(CHECKS, headers=operator).json()["checks"] if c["key"] == "ehr"
        )
        assert ehr["state"] == "ok" and ehr["at"] is not None


def test_an_ehr_check_that_finds_a_failure_shows_in_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch)
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr.stmarys.example")
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR}=default:decide")
    long = "x" * 5000  # what an EHR's own configuration says is quoted, bounded
    findings = [Finding("ok", "allow-list", "listed"), Finding("fail", "PKCE S256", long)]
    monkeypatch.setattr(smart_check, "check", lambda base: findings)
    db = tmp_path / "a.db"
    with TestClient(create_app(db_path=db), base_url=HTTPS, headers=DASHBOARD) as client:
        with encrypted_store(db) as store:
            operator = member(store, org="default", roles=["admin"], sub="operator")
        checked = client.post("/api/system/ehr-check", headers=operator, json={"base": EHR})
        body = checked.json()
        assert body["ok"] is False and [f["status"] for f in body["findings"]] == ["ok", "fail"]
        assert len(body["findings"][1]["detail"]) == 500
        ehr = next(
            c for c in client.get(CHECKS, headers=operator).json()["checks"] if c["key"] == "ehr"
        )
        assert ehr["state"] == "attention" and "would refuse its launches" in ehr["detail"]


# -- verifying the chain --------------------------------------------------------------------------


def test_verifying_the_chain_records_when_and_a_broken_chain_raises_the_alert(
    site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    assert site.check("audit_chain")["state"] == "attention"  # never verified yet
    decide(site.store)
    before = site.store.verify()["events"]
    verified = site.client.post("/api/system/audit/verify", headers=site.operator)
    assert verified.status_code == 200
    body = verified.json()
    assert body["ok"] is True and body["events"] == before
    assert body["reason"] is None and body["broken_at"] is None
    assert site.store.verify()["events"] == before  # nothing was chained by verifying
    shown = site.check("audit_chain")
    assert shown["state"] == "ok" and shown["at"] == body["verified_at"]
    assert "Every event of the audit trail was checked" in shown["detail"]
    assert hook.sent == []

    tamper(site.store, BROKEN)  # someone with the database changes an event
    capsys.readouterr()
    broken = site.client.post("/api/system/audit/verify", headers=site.operator).json()
    assert broken["ok"] is False and broken["reason"] and broken["broken_at"] is not None
    (alert,) = hook.sent
    assert (alert["body"]["event"], alert["body"]["organization"]) == ("audit.broken", None)
    assert alert["body"]["alerts"][0]["severity"] == "critical"
    shown = site.check("audit_chain")
    assert shown["state"] == "attention" and "did not verify" in shown["detail"]
    assert "incident runbook" in shown["action"]
    events = [line["event"] for line in security_lines(capsys.readouterr().out)]
    assert "system.audit.verified" in events and "audit.chain.broken" in events


# -- the test alert -------------------------------------------------------------------------------


def test_a_test_alert_is_recorded_as_delivered_or_failed_and_never_sent_again(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = Webhook(204, httpx.ConnectError("down"), 500)
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    assert site.check("alerts")["state"] == "attention"  # set, and nothing delivered yet
    sent = site.client.post("/api/system/alerts/test", headers=site.operator).json()
    assert sent["delivered"] is True and sent["status"] == 204 and sent["error"] is None
    (post,) = hook.sent
    assert post["url"] == SECRETS["MENDARI_ALERT_WEBHOOK"]
    assert post["body"]["event"] == "alerts.test" and post["body"]["alerts"] == []
    assert post["headers"]["mendari-signature"].startswith("t=")  # signed, as every alert is
    assert site.check("alerts")["state"] == "ok"
    for expected in (
        {"status": None, "error": "ConnectError"},
        {"status": 500, "error": "http_500"},
    ):
        failed = site.client.post("/api/system/alerts/test", headers=site.operator).json()
        assert failed["delivered"] is False
        assert {key: failed[key] for key in expected} == expected
    recorded = site.store.alert_deliveries()
    assert sorted(row["status"] for row in recorded) == ["delivered", "failed", "failed"]
    assert {row["event"] for row in recorded} == {"alerts.test"}
    # Never pending: the scheduled monitor's retries find nothing to send again.
    with site.store._db.session(exclusive=True) as db:
        db.all("UPDATE alert_deliveries SET next_attempt_at = '2020-01-01T00:00:00+00:00'")
    assert site.job("monitor").json()["retried"] == {
        "delivered": 0,
        "pending": 0,
        "failed": 0,
        "deferred": 0,
    }
    assert len(hook.sent) == 3
    assert site.check("alerts")["state"] == "attention"


def test_a_test_alert_without_a_webhook_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, webhook=False)
    db = tmp_path / "a.db"
    with TestClient(create_app(db_path=db), base_url=HTTPS, headers=DASHBOARD) as client:
        with encrypted_store(db) as store:
            operator = member(store, org="default", roles=["admin"], sub="operator")
        refused = client.post("/api/system/alerts/test", headers=operator)
        assert refused.status_code == 409 and "no alert webhook" in refused.json()["detail"]
        alerts = next(
            c for c in client.get(CHECKS, headers=operator).json()["checks"] if c["key"] == "alerts"
        )
        assert alerts["state"] == "not_set"


# -- the scheduled jobs and the backup ------------------------------------------------------------


def test_the_scheduled_jobs_leave_their_last_run_for_the_checks(site: Site) -> None:
    assert site.check("scheduled_jobs")["state"] == "attention"  # the secret is set; none ran
    decide(site.store)
    assert site.job("retention").status_code == 200
    assert site.job("checkpoint").json() == {"ok": True, "recorded": True, "signed": True}
    assert site.job("monitor").status_code == 200
    runs = site.store.runs()
    assert {name: runs[name]["outcome"] for name in ("retention", "checkpoint", "monitor")} == {
        "retention": "ok",
        "checkpoint": "ok",
        "monitor": "ok",
    }
    assert runs["checkpoint"]["detail"] == "since_checkpoint"
    for key in ("scheduled_jobs", "checkpoint", "checkpoint_signing", "audit_chain"):
        assert site.check(key)["state"] == "ok", key
    assert "from its last checkpoint" in site.check("audit_chain")["detail"]
    # A second run of a job replaces its last run: one row a job, never a history.
    assert site.job("retention").status_code == 200
    assert len([name for name in site.store.runs() if name == "retention"]) == 1


def test_a_checkpoint_that_finds_the_chain_broken_shows_in_the_checks(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = Webhook()
    monkeypatch.setattr(alerts_module.httpx, "post", hook)
    decide(site.store)
    tamper(site.store, BROKEN)
    assert site.job("checkpoint").json() == {"ok": False, "recorded": False, "signed": False}
    assert [post["body"]["event"] for post in hook.sent] == ["audit.broken"]
    run = site.store.runs()["checkpoint"]
    assert run["outcome"] == "failed" and run["detail"]
    for key in ("checkpoint", "audit_chain", "scheduled_jobs"):
        assert site.check(key)["state"] == "attention", key


def test_a_backup_is_reported_with_the_jobs_secret_only(site: Site) -> None:
    assert site.check("backup")["state"] == "not_set"
    client = site.client
    assert client.post("/api/internal/backup?outcome=ok").status_code == 401
    wrong = {"authorization": "Bearer not-the-secret"}
    assert client.post("/api/internal/backup?outcome=ok", headers=wrong).status_code == 401
    # A person's session is no job's credential, an operator's included.
    assert client.post("/api/internal/backup?outcome=ok", headers=site.operator).status_code == 401
    assert "backup" not in site.store.runs()
    secret = {"authorization": f"Bearer {CRON}"}
    assert client.post("/api/internal/backup?outcome=sideways", headers=secret).status_code == 422
    reported = client.post("/api/internal/backup?outcome=ok", headers=secret)
    assert reported.status_code == 200 and reported.json() == {"recorded": True}
    assert site.check("backup")["state"] == "ok"
    assert client.post("/api/internal/backup?outcome=failed", headers=secret).status_code == 200
    shown = site.check("backup")
    assert shown["state"] == "attention" and "The last backup failed" in shown["detail"]


def test_without_the_jobs_secret_the_backup_report_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch)
    monkeypatch.delenv("CRON_SECRET")
    with TestClient(create_app(db_path=tmp_path / "a.db"), base_url=HTTPS) as client:
        answer = client.post(
            "/api/internal/backup?outcome=ok", headers={"authorization": f"Bearer {CRON}"}
        )
        assert answer.status_code == 404


def test_a_run_is_one_of_the_jobs_with_a_code_for_a_detail(store: AuditStore) -> None:
    assert store.runs() == {}
    first = store.record_run("backup", "ok")
    assert store.runs() == {"backup": first}
    second = store.record_run("backup", "failed", "disk_full")
    assert store.runs() == {"backup": second} and second["ran_at"] >= first["ran_at"]
    for job, outcome, detail in (
        ("nightly", "ok", None),
        ("backup", "maybe", None),
        ("backup", "failed", "the disk is full: /var/lib/postgres"),
        ("backup", "failed", "x" * 65),
    ):
        with pytest.raises(ValueError):
            store.record_run(job, outcome, detail)
    assert store.runs() == {"backup": second}
    assert store.any_decision() is False and store.last_checkpoint() is None
    assert store.last_alerts() == {"last": None, "delivered": None}


# -- the API description --------------------------------------------------------------------------


def test_the_api_description_says_who_may_call_each_system_route(site: Site) -> None:
    document = site.client.get("/openapi.json", headers=site.operator).json()
    described = {
        (method.upper(), path): operation
        for path, operations in document["paths"].items()
        if path.startswith("/api/system")
        for method, operation in operations.items()
    }
    assert set(described) == {("GET", CHECKS), *(("POST", path) for path in ACTIONS)}
    for (method, path), operation in described.items():
        assert operation["tags"] == ["System"], path
        assert operation["security"] == [{"session": []}], path  # people only
        assert "organization that operates this deployment" in operation["description"], path
        # The actions count against a limit; reading the checks does not.
        assert ("429" in operation["responses"]) == (method == "POST"), path
    assert "/api/internal/backup" not in document["paths"]  # the schedule's, as the purge is
    states = document["components"]["schemas"]["SystemCheck"]["properties"]["state"]
    assert states["enum"] == ["ok", "attention", "not_set", "not_applicable"]
