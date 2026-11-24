# SPDX-License-Identifier: Apache-2.0
"""The deployment's readiness checks (`server/system.py`): every check in every state it can
take, from the facts alone. `healthy()` is a one-machine install as the installer leaves it,
where every check is `ok` or `not_applicable`; each case changes one fact and says which check
moves, to which state, and what its sentence says. The routes that serve them, and who may read
them, are tests/test_system_routes.py."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mendari.server import system
from mendari.server.system import Alerts, Deployment, Registry, RegistryProbe

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).isoformat(timespec="milliseconds")


def run(outcome: str = "ok", detail: str | None = None, **delta: float) -> dict[str, Any]:
    return {"ran_at": ago(**(delta or {"hours": 2})), "outcome": outcome, "detail": detail}


def health(**changes: Any) -> dict[str, Any]:
    """Signed-in `/api/health` on a one-machine install: sign-in, patient data mode, Postgres."""
    shown: dict[str, Any] = {
        "status": "ok",
        "version": "9.9.9",
        "sign_in": True,
        "packs": 2,
        "packs_by_origin": {"maintained": 1, "hub": 1},
        "invalid": {},
        "storage": "postgres",
        "timezone": "Europe/Paris",
        "registry_recalls": {},
        "smart_launch": False,
        "smart_ehrs": [],
        "cds_clients": None,
        "phi_mode": "encrypt",
        "phi_keys": {"current": "a1b2c3d4", "read": []},
        "retention_days": 30,
        "checkpoint_key": "ed25519:0123456789abcdef",
        "last_purge": {"at": ago(hours=2), "retention_days": 30, "source": "schedule"},
        "input_hash": "hmac-sha256:e5f6a7b8",
        "rate_limits": {
            "mode": "on",
            "client_ip": "203.0.113.7",
            "x_forwarded_for_used": True,
            "limits": {"decide": "600/min:100", "signin": "300/min:120"},
        },
        "org_quotas": {"daily_limits": {}, "used_today": {}},
    }
    shown.update(changes)
    return shown


def healthy(**changes: Any) -> Deployment:
    """Everything set up: each check `ok` or `not_applicable`."""
    deployment = Deployment(
        health=health(),
        ready={"status": "ready", "version": "9.9.9", "checks": {"database": "ok", "packs": "ok"}},
        now=NOW,
        accounts="memberships",
        operator_org="default",
        operator_org_name="St Mary's Hospital",
        administrators=2,
        first_admin="used",
        public_url="https://mendari.stmarys.example",
        forwarded=True,
        scheduled=True,
        runs={
            "retention": run(),
            "checkpoint": run(detail="since_checkpoint"),
            "monitor": run(),
            "backup": run(),
            "audit.verify": run(detail="full", hours=1),
        },
        checkpoint={"seq": 41, "hash": "f" * 64, "created_at": ago(hours=2)},
        statement_day="2026-11-23",
        alerts=Alerts(
            webhook=True,
            signed=True,
            last={"status": "delivered", "updated_at": ago(hours=3), "last_status": 200},
            delivered={"status": "delivered", "updated_at": ago(hours=3), "last_status": 200},
        ),
        registry=Registry("https://registry.example.org", True, ago(minutes=1)),
        any_decision=True,
        demo_only=False,
    )
    return replace(deployment, **changes)


def found(deployment: Deployment) -> dict[str, system.SystemCheck]:
    return {check.key: check for check in system.checks(deployment)}


KEYS = [
    "sign_in",
    "operators",
    "public_address",
    "client_address",
    "patient_data",
    "sealing_key",
    "database",
    "audit_chain",
    "checkpoint",
    "checkpoint_signing",
    "scheduled_jobs",
    "backup",
    "alerts",
    "limits",
    "ehr",
    "registry",
    "agents",
    "version",
]


def test_a_finished_install_passes_every_check_in_the_order_a_person_sets_it_up() -> None:
    checks = system.checks(healthy())
    assert [check.key for check in checks] == KEYS
    states = {check.key: check.state for check in checks}
    # No EHR is connected yet: an optional part, not something to fix.
    assert states.pop("ehr") == "not_set"
    assert set(states.values()) == {"ok"}, states
    for check in checks:
        assert check.label and check.detail.endswith((".", ")")), check.key
        assert check.area in (
            system.ACCESS,
            system.DATA,
            system.AUDIT,
            system.SCHEDULE,
            system.ALERTS,
            system.CONNECTIONS,
            system.ENGINE,
        )


# (the check, the facts that change, the state it takes, words its sentence or its action says)
Case = tuple[str, dict[str, Any], str, str]
CASES: list[Case] = [
    # Sign-in.
    (
        "sign_in",
        {"health": health(sign_in=False), "accounts": "open"},
        "attention",
        "Sign-in is off",
    ),
    ("sign_in", {"accounts": "claims"}, "ok", "groups your identity provider sends"),
    ("sign_in", {}, "ok", "managed in the Access view"),
    # Who runs it.
    ("operators", {"accounts": "open"}, "not_applicable", "nobody is told apart"),
    ("operators", {"accounts": "claims", "operator_org": None}, "ok", "administrator role runs"),
    ("operators", {"administrators": 1}, "attention", "Invite a second administrator"),
    ("operators", {}, "ok", "The 2 administrators of St Mary's Hospital"),
    ("operators", {"first_admin": "used"}, "ok", "has done its work and is ignored"),
    ("operators", {"first_admin": "waiting"}, "ok", "it can be removed"),
    # The public address.
    ("public_address", {"public_url": None}, "attention", "Set MENDARI_PUBLIC_URL"),
    (
        "public_address",
        {"public_url": None, "health": health(sign_in=False), "accounts": "open"},
        "not_applicable",
        "while sign-in is off",
    ),
    ("public_address", {"public_url": "http://mendari.example"}, "attention", "is not https"),
    ("public_address", {}, "ok", "https://mendari.stmarys.example"),
    # Each person's own address.
    (
        "client_address",
        {"health": health(sign_in=False), "accounts": "open"},
        "not_applicable",
        "once sign-in is on",
    ),
    ("client_address", {}, "ok", "203.0.113.7, as the proxy in front reports it"),
    (
        "client_address",
        {
            "health": health(
                rate_limits={"mode": "on", "client_ip": "172.31.87.10", "limits": {}},
            )
        },
        "attention",
        "Set FORWARDED_ALLOW_IPS to the address the proxy connects from (172.31.87.10)",
    ),
    (
        "client_address",
        {
            "forwarded": False,
            "health": health(rate_limits={"mode": "on", "client_ip": "10.0.0.5", "limits": {}}),
        },
        "ok",
        "directly, with no proxy in front",
    ),
    # Patient data mode and what it needs.
    ("patient_data", {}, "ok", "kept encrypted"),
    ("patient_data", {"health": health(phi_mode="hash")}, "ok", "No case's input is ever kept"),
    (
        "patient_data",
        {"health": health(phi_mode="off", sign_in=False), "accounts": "open"},
        "attention",
        "synthetic data only",
    ),
    (
        "patient_data",
        {"health": health(sign_in=False), "accounts": "open"},
        "attention",
        "sign-in is off, so anyone who can reach this address reads",
    ),
    ("patient_data", {"health": health(input_hash="sha256")}, "attention", "not keyed"),
    # The sealing key.
    ("sealing_key", {}, "ok", "key id a1b2c3d4"),
    (
        "sealing_key",
        {"health": health(phi_keys={"current": "a1b2c3d4", "read": ["0f0f0f0f"]})},
        "ok",
        "1 earlier key is still read",
    ),
    ("sealing_key", {"health": health(phi_mode="hash", phi_keys=None)}, "not_applicable", "seal"),
    ("sealing_key", {"health": health(phi_mode="off", phi_keys=None)}, "not_set", "No sealing key"),
    # The database.
    ("database", {}, "ok", "The database (Postgres) answers"),
    (
        "database",
        {"ready": {"status": "unready", "checks": {"database": "unreachable"}}},
        "attention",
        "did not answer",
    ),
    ("database", {"health": health(storage="sqlite")}, "attention", "single SQLite file"),
    (
        "database",
        {"health": health(storage="sqlite", sign_in=False), "accounts": "open"},
        "ok",
        "The database (SQLite) answers",
    ),
    # The audit chain: verified, and when.
    ("audit_chain", {}, "ok", "Every event of the audit trail was checked"),
    (
        "audit_chain",
        {"runs": {"checkpoint": run(detail="since_checkpoint")}},
        "ok",
        "from its last checkpoint",
    ),
    ("audit_chain", {"runs": {}}, "attention", "has not been verified"),
    (
        "audit_chain",
        {"runs": {"audit.verify": run(detail="full", days=3)}},
        "attention",
        "more than a day and a half ago",
    ),
    (
        "audit_chain",
        {"runs": {"audit.verify": run("failed", "chain"), "checkpoint": run(days=1)}},
        "attention",
        "did not verify",
    ),
    (
        # A later good check supersedes an earlier failure.
        "audit_chain",
        {"runs": {"audit.verify": run("failed", "chain", days=1), "checkpoint": run()}},
        "ok",
        "from its last checkpoint",
    ),
    # The last checkpoint.
    ("checkpoint", {}, "ok", "position was recorded"),
    (
        "checkpoint",
        {"checkpoint": {"seq": 3, "hash": "a" * 64, "created_at": ago(days=9)}},
        "ok",
        "nothing new to record",
    ),
    ("checkpoint", {"checkpoint": None, "runs": {}}, "attention", "has not recorded a checkpoint"),
    (
        "checkpoint",
        {"checkpoint": None, "runs": {}, "scheduled": False},
        "not_set",
        "No checkpoint is recorded",
    ),
    (
        "checkpoint",
        {"checkpoint": {"seq": 3, "hash": "a" * 64, "created_at": ago(days=4)}, "runs": {}},
        "attention",
        "more than a day and a half old",
    ),
    ("checkpoint", {"runs": {"checkpoint": run("failed", "chain")}}, "attention", "did not verify"),
    # Its signature.
    ("checkpoint_signing", {}, "ok", "checkpoint of 2026-11-23 is signed"),
    ("checkpoint_signing", {"health": health(checkpoint_key=None)}, "not_set", "not signed"),
    ("checkpoint_signing", {"statement_day": None}, "attention", "no checkpoint has been signed"),
    # The scheduled jobs.
    ("scheduled_jobs", {}, "ok", "every agent was checked for drift"),
    (
        "scheduled_jobs",
        {"runs": {}, "checkpoint": None, "health": health(last_purge=None), "scheduled": False},
        "not_set",
        "Nothing runs on a schedule",
    ),
    (
        # Just installed: scheduled, and the first run has not happened yet.
        "scheduled_jobs",
        {"runs": {}, "checkpoint": None, "health": health(last_purge=None)},
        "attention",
        "none has run yet",
    ),
    (
        "scheduled_jobs",
        {"runs": {"retention": run(days=3), "checkpoint": run()}},
        "attention",
        "Not run in the last day and a half: deleting kept inputs past their retention. "
        "No run on record yet: checking each agent for drift.",
    ),
    (
        "scheduled_jobs",
        {"runs": {"retention": run(), "checkpoint": run(), "monitor": run("failed", "error")}},
        "attention",
        "The last run failed for: checking each agent for drift",
    ),
    (
        "scheduled_jobs",
        {"runs": {"retention": run(days=3), "checkpoint": run(), "monitor": run()}},
        "attention",
        "deleting kept inputs past their retention",
    ),
    (
        # A schedule that runs the commands leaves a purge's summary and a checkpoint, and no
        # record of the drift check: nothing is called missing.
        "scheduled_jobs",
        {"runs": {}, "scheduled": False},
        "ok",
        "leaves no record when it is run from the command line",
    ),
    # Backups.
    ("backup", {}, "ok", "backed up and the backup file was read back"),
    ("backup", {"runs": {}}, "not_set", "No backup has been reported"),
    ("backup", {"runs": {"backup": run("failed")}}, "attention", "The last backup failed"),
    ("backup", {"runs": {"backup": run(days=2)}}, "attention", "more than a day and a half old"),
    # Alerts.
    ("alerts", {}, "ok", "the last one was delivered"),
    ("alerts", {"alerts": Alerts()}, "not_set", "No alert destination"),
    ("alerts", {"alerts": Alerts(webhook=True)}, "attention", "unsigned"),
    ("alerts", {"alerts": Alerts(webhook=True, signed=True)}, "attention", "Send a test alert"),
    (
        "alerts",
        {
            "alerts": Alerts(
                webhook=True,
                signed=True,
                last={"status": "failed", "updated_at": ago(hours=1), "last_status": 500},
                delivered={"status": "delivered", "updated_at": ago(days=5), "last_status": 200},
            )
        },
        "attention",
        "the webhook answered HTTP 500",
    ),
    (
        "alerts",
        {
            "alerts": Alerts(
                webhook=True,
                signed=True,
                last={"status": "pending", "updated_at": ago(hours=1), "last_status": None},
            )
        },
        "attention",
        "the webhook did not answer",
    ),
    # Limits.
    ("limits", {}, "ok", "decisions 600/min:100 per caller"),
    (
        "limits",
        {"health": health(org_quotas={"daily_limits": {"decisions": 5000}, "used_today": {}})},
        "ok",
        "5000 decisions a day",
    ),
    (
        "limits",
        {"health": health(rate_limits={"mode": "log", "limits": {}})},
        "attention",
        "only counted, not enforced",
    ),
    ("limits", {"health": health(rate_limits={"mode": "off"})}, "attention", "Rate limits are off"),
    ("limits", {"health": health(sign_in=False), "accounts": "open"}, "not_applicable", "sign-in"),
    # The EHR connection.
    ("ehr", {}, "not_set", "No EHR is connected"),
    ("ehr", {"health": health(smart_launch=True)}, "attention", "every launch is refused"),
    (
        "ehr",
        {"health": health(smart_launch=True, smart_ehrs=[{"base": "https://ehr.example/fhir"}])},
        "attention",
        "has not been checked from here yet",
    ),
    (
        "ehr",
        {
            "health": health(smart_launch=True, smart_ehrs=[{"base": "https://ehr.example/fhir"}]),
            "ehr_checked": run(),
        },
        "ok",
        "1 EHR registered to launch",
    ),
    (
        "ehr",
        {
            "health": health(smart_launch=True, smart_ehrs=[{"base": "https://ehr.example/fhir"}]),
            "ehr_checked": run("failed"),
        },
        "attention",
        "would refuse its launches",
    ),
    ("ehr", {"health": health(cds_clients=["epic", "cerner"])}, "ok", "2 calling with their own"),
    # The registry.
    ("registry", {}, "ok", "https://registry.example.org answers"),
    ("registry", {"registry": Registry()}, "not_set", "No agent registry is set"),
    (
        "registry",
        {"registry": Registry("https://registry.example.org", False, ago(minutes=1))},
        "attention",
        "did not answer",
    ),
    (
        "registry",
        {"registry": Registry("https://registry.example.org", None)},
        "attention",
        "did not answer",
    ),
    (
        "registry",
        {"health": health(registry_recalls={"https://r.example": {"failing": "ConnectError"}})},
        "attention",
        "could not be read for recalls",
    ),
    (
        "registry",
        {
            "registry": Registry(),
            "health": health(registry_recalls={"https://r.example": {"failing": None}}),
        },
        "ok",
        "is read for recalls",
    ),
    # The agents.
    ("agents", {}, "ok", "2 agents are served, 1 of them from a registry"),
    ("agents", {"demo_only": True}, "not_set", "Only the bundled demo agent"),
    (
        "agents",
        {"health": health(invalid={"clinic/triage": "its manifest does not load"})},
        "attention",
        "1 agent did not load: clinic/triage",
    ),
    (
        "agents",
        {"recalls": [{"agent": "acme/triage", "version": "1.2.0", "reason": "recalled (high)"}]},
        "attention",
        "1 served version was recalled by its registry and no longer automates",
    ),
    # The engine.
    ("version", {}, "ok", "Mendari 9.9.9"),
]


@pytest.mark.parametrize(
    ("key", "changes", "state", "says"),
    CASES,
    ids=[f"{key}-{state}-{index}" for index, (key, _, state, _) in enumerate(CASES)],
)
def test_each_check_in_each_state(key: str, changes: dict[str, Any], state: str, says: str) -> None:
    check = found(healthy(**changes))[key]
    assert check.state == state, check
    assert says in f"{check.detail} {check.action or ''}", check
    # What to do is said exactly when there is something to do.
    assert (check.action is not None) == (state in ("attention", "not_set")), check
    assert (check.action or "x").strip() and not check.detail.startswith(" ")


def test_every_state_of_every_check_is_covered() -> None:
    """The cases above reach each state a check can take: a check that gains a state fails here
    until a case names it."""
    covered: dict[str, set[str]] = {}
    for key, _, state, _ in CASES:
        covered.setdefault(key, set()).add(state)
    assert covered == {
        "sign_in": {"ok", "attention"},
        "operators": {"ok", "attention", "not_applicable"},
        "public_address": {"ok", "attention", "not_applicable"},
        "client_address": {"ok", "attention", "not_applicable"},
        "patient_data": {"ok", "attention"},
        "sealing_key": {"ok", "not_set", "not_applicable"},
        "database": {"ok", "attention"},
        "audit_chain": {"ok", "attention"},
        "checkpoint": {"ok", "attention", "not_set"},
        "checkpoint_signing": {"ok", "attention", "not_set"},
        "scheduled_jobs": {"ok", "attention", "not_set"},
        "backup": {"ok", "attention", "not_set"},
        "alerts": {"ok", "attention", "not_set"},
        "limits": {"ok", "attention", "not_applicable"},
        "ehr": {"ok", "attention", "not_set"},
        "registry": {"ok", "attention", "not_set"},
        "agents": {"ok", "attention", "not_set"},
        "version": {"ok"},
    }
    assert set(covered) == set(KEYS)


def test_a_check_says_when_what_it_reports_happened() -> None:
    checks = found(healthy())
    assert checks["audit_chain"].at == ago(hours=1)
    assert checks["backup"].at == ago(hours=2)
    assert checks["alerts"].at == ago(hours=3)
    assert checks["registry"].at == ago(minutes=1)
    # A setting has no time.
    assert checks["sign_in"].at is None and checks["version"].at is None
    # The daily jobs: the oldest of their last runs, the weakest link.
    runs = {"retention": run(hours=20), "checkpoint": run(hours=1), "monitor": run(hours=5)}
    assert found(healthy(runs=runs))["scheduled_jobs"].at == ago(hours=20)


def test_a_time_nobody_can_read_counts_as_old() -> None:
    stale = {"audit.verify": {"ran_at": "yesterday", "outcome": "ok", "detail": "full"}}
    assert found(healthy(runs=stale))["audit_chain"].state == "attention"


def test_the_answer_counts_the_states_and_says_whether_setting_up_is_finished() -> None:
    answered = system.answer(healthy(), version="9.9.9")
    assert answered.format == "mendari.system-checks/1" and answered.version == "9.9.9"
    assert answered.checked_at == "2026-11-23T12:00:00+00:00"
    assert answered.summary == {"ok": 17, "attention": 0, "not_set": 1, "not_applicable": 0}
    assert sum(answered.summary.values()) == len(KEYS)
    # In use (a decision made, an agent adopted): not a fresh install, whatever is unset.
    assert answered.setup.fresh is False and answered.setup.finished is False
    # A fresh install: no decision yet, only the demo agent. Not finished while a check is open.
    fresh = system.answer(healthy(any_decision=False, demo_only=True), version="9.9.9")
    assert fresh.setup.fresh is True and fresh.setup.finished is False
    # Only one of the two: in use.
    for changes in ({"any_decision": False}, {"demo_only": True}):
        assert system.answer(healthy(**changes), version="9.9.9").setup.fresh is False
    # Finished: every check ok or not applicable (an EHR connected and checked, here).
    connected = health(smart_launch=True, smart_ehrs=[{"base": "https://ehr.example/fhir"}])
    done = system.answer(healthy(health=connected, ehr_checked=run()), version="9.9.9")
    assert done.setup.finished is True and done.summary["ok"] == len(KEYS)


def test_recalled_versions_are_listed_for_the_banner() -> None:
    recalls = [{"agent": "acme/triage", "version": "1.2.0", "reason": "recalled (critical)"}]
    answered = system.answer(healthy(recalls=recalls), version="9.9.9")
    assert [recall.model_dump() for recall in answered.recalls] == recalls
    assert system.answer(healthy(), version="9.9.9").recalls == []


def test_an_origin_never_shows_a_path_a_query_or_credentials() -> None:
    assert system.origin("https://hooks.example.org/services/T0/B1/secret") == (
        "https://hooks.example.org"
    )
    assert system.origin("https://user:secret@registry.example.org:8443/v1?token=x") == (
        "https://registry.example.org:8443"
    )
    assert system.origin("http://[::1]:8100/registry") == "http://[::1]:8100"
    for not_one in ("", "registry.example.org", "ftp://x.example", "https://", "https://x:port"):
        assert system.origin(not_one) is None, not_one


# -- the registry's probe -------------------------------------------------------------------------


def test_the_registry_is_asked_once_and_its_answer_reused() -> None:
    asked: list[str] = []
    now = [1000.0]
    probe = RegistryProbe("https://registry.example.org/", ask=asked.append, clock=lambda: now[0])
    first = probe.state()
    assert (first.origin, first.reachable) == ("https://registry.example.org", True)
    assert first.checked_at is not None and asked == ["https://registry.example.org"]
    now[0] += system.REGISTRY_SECONDS - 1
    assert probe.state().reachable is True and len(asked) == 1  # reused
    now[0] += 2  # due again: asked in the background, the last answer given meanwhile
    assert probe.state().reachable is True
    for _ in range(200):
        if len(asked) == 2:
            break
        threading.Event().wait(0.01)
    assert len(asked) == 2


def test_a_registry_that_fails_is_unreachable_and_its_words_are_never_repeated() -> None:
    def refuses(url: str) -> None:
        raise RuntimeError("secret-token-in-an-error at https://registry.example.org/v1")

    state = RegistryProbe("https://registry.example.org", ask=refuses).state()
    assert state == Registry("https://registry.example.org", False, state.checked_at)
    assert "secret" not in str(state)


def test_a_registry_is_asked_over_https_only_and_none_is_no_registry() -> None:
    asked: list[str] = []
    plain = RegistryProbe("http://registry.example.org", ask=asked.append).state()
    assert (plain.origin, plain.reachable) == ("http://registry.example.org", False)
    assert asked == []  # never sent
    local = RegistryProbe("http://127.0.0.1:8100", ask=asked.append).state()
    assert local.reachable is True and asked == ["http://127.0.0.1:8100"]
    for unset in (None, "", "   "):
        assert RegistryProbe(unset, ask=asked.append).state() == Registry()
    assert RegistryProbe("not a url", ask=asked.append).state() == Registry()


def test_a_slow_registry_never_holds_the_checks_for_long() -> None:
    release = threading.Event()

    def slow(url: str) -> None:
        release.wait(30)

    probe = RegistryProbe("https://registry.example.org", ask=slow)
    try:
        waited = datetime.now(UTC)
        state = probe.state()
        assert state.reachable is None  # not known yet: the check says it did not answer
        assert (datetime.now(UTC) - waited).total_seconds() < system.REGISTRY_WAIT_S + 2
        waited = datetime.now(UTC)
        assert probe.state().reachable is None  # a second caller does not wait at all
        assert (datetime.now(UTC) - waited).total_seconds() < 1
    finally:
        release.set()
