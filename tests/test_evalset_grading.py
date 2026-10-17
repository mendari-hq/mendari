# SPDX-License-Identifier: Apache-2.0
"""The grading route over hidden evaluation sets (LP-11l, `server/app.py` `add_grading_routes`,
ADR-0011): `POST /api/evalsets/{set_id}/runs` runs the pack a frozen or active set was made for
over its cases, in the server's process, and answers aggregates only
(`mendari.hidden_result/1`, `evals/hidden.py`).

- Only the organization's grading key passes: anonymous `401`; a member of every role, the
  administrator and the labeller included, a key of each machine role, a legacy key of each
  other role, scoped keys and Backend Services clients the `grading_only` `403`; an EHR launch's
  cookie `401`; a person of no organization `NoOrganization`; another organization's grading
  key `404` on this one's set, as on an unknown one. Open mode cannot start with the keys.
- The answer is a closed model pinned here field by field: aggregates by question and label,
  calibration bins and Brier intervals over `MIN_GROUP` cases or more only, never a case's id
  or hash, a planted mark, a missed or misclassified case, a subgroup, the dataset's or the
  ledger's identity.
- Each run is chained (`evalset.graded`, and `evalset.activated` on a frozen set's first) and
  counted against the rotation rule, which refuses a run past its runs or its age, as it does a
  draft or retired set; a refusal reads and chains nothing.
- Every provider slot runs a local variant: a remote one, asked for or the deployment's own, is
  refused before any case is read, as is a pack whose own names the closed result cannot hold.
- Nothing is stored: no decision, report, ledger or usage, and `/api/evals` answers the same.
- A set changed behind the store's back (a case, or its header) is a `409` `set_changed`
  naming no case, and the operator's log names the set only; a run that fails counts and is
  told by its code only.
- Mounted with the keys in both account modes, documented under its own tag, its rate-limit
  class `evaluate`, and logged by set id only; what it opens is what threat model I13 says, and
  the runbook's steps run as written.
On SQLite and Postgres (`db_target`) where the store is written; keys are made per test, cases
are hand-written synthetic text."""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, ValidationError

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.evalsets import NO_SET, EvalsetStore, GradingRead
from mendari.core.registry import SearchPath
from mendari.errors import MendariError
from mendari.evals import ledger as ledgers
from mendari.evals.harness import Dataset, resolve_dataset
from mendari.evals.hidden import (
    HIDDEN_RESULT_FORMAT,
    BrierInterval,
    HiddenCritical,
    HiddenDecisions,
    HiddenQuestion,
    HiddenResult,
    HiddenSeeds,
    Proportion,
    hidden_result,
    holds,
)
from mendari.evals.result import result_from_ledger
from mendari.evals.share import (
    MIN_GROUP,
    Automation,
    Calibration,
    CalibrationBin,
    LabelMetrics,
    Review,
    SharedPack,
    SharedPolicy,
    SharedProvider,
    SharedThresholds,
    SweepRow,
)
from mendari.server import auth as auth_module
from mendari.server.app import GRADING_REFUSALS, create_app, mounted_routes
from mendari.server.auth import ROLES, permission_for
from mendari.server.backend_services import check_jwks, default_scope
from mendari.server.scopes import DECIDE, parse
from tests.conftest import (
    INBOX_DIR,
    ROOT,
    calm_jev,
    encrypt_mode,
    encrypted_store,
    evalset_on,
    launched,
    member,
    signed_in,
    without_request_id,
)
from tests.test_auth import CLIENT, IDP, legacy_key
from tests.test_evalset_labelling import cases, events, label_all, labellers
from tests.test_evalset_store import CLINIC, MARKS, a_set, tamper
from tests.test_labelling_routes import SEALED_WORDS
from tests.test_security_log import logged
from tests.test_threat_model import rows

HTTPS = "https://mendari.test"
OTHER = "clinic-b"
RUNS = "/api/evalsets/{set_id}/runs"
GRADED = {"set", "org", "commitment", "grader", "pack_digest", "n", "run"}
LOCAL = {"safety_screen": "rules", "router": "naive_bayes"}
# Every closed model of the answer, with the fields it holds: a field added fails here until it
# is listed, and reviewed for what it would show (never a case, its id or hash, a missed or
# misclassified case, a subgroup, the dataset's or the ledger's identity).
PINNED: dict[type[BaseModel], set[str]] = {
    HiddenResult: {
        "format",
        "set",
        "commitment",
        "pack",
        "providers",
        "policy",
        "n",
        "run",
        "runs_left",
        "seeds",
        "primary",
        "questions",
        "decisions",
        "threshold_sweep",
        "errors",
    },
    SharedPack: {"id", "version", "digest"},
    SharedProvider: {"variant", "type", "model"},
    SharedPolicy: {"risk_level", "thresholds", "governance_override"},
    SharedThresholds: {"auto", "review"},
    HiddenSeeds: {"bootstrap", "resamples", "level"},
    HiddenQuestion: {
        "n",
        "accuracy",
        "macro_f1",
        "per_label",
        "confusion",
        "calibration",
        "brier",
        "proportion",
        "brier_interval",
    },
    LabelMetrics: {"precision", "recall", "f1", "support"},
    Calibration: {"ece", "bins"},
    CalibrationBin: {"low", "high", "n", "confidence", "accuracy"},
    Proportion: {"k", "n", "low", "high"},
    BrierInterval: {"low", "high"},
    HiddenDecisions: {
        "outcome_counts",
        "outcome_rates",
        "abstention_rate",
        "automation",
        "review",
        "recommendation_accuracy",
        "critical",
    },
    Automation: {"n", "accuracy", "false_automation_rate", "false_automation_share"},
    Review: {"n", "expected_override_rate"},
    HiddenCritical: {
        "question",
        "label",
        "n",
        "detected",
        "detection_rate",
        "auto_missed",
        "proportion",
    },
    SweepRow: {"threshold", "coverage", "accuracy", "errors"},
}
# What a `mendari.eval/2` result holds of its cases or its dataset, and no answer may.
PER_CASE = {
    "missed_cases",
    "misclassified",
    "subgroups",
    "dataset",
    "ledger",
    "equal_mass",
    "case",
    "cases",
    "models",
    "latency_ms",
}
# Tables a run must leave as they were (the set's row changes state on its first run, and the
# chain gains its reads; a rate-limit bucket counts).
UNTOUCHED = (
    "decisions",
    "decision_inputs",
    "eval_reports",
    "provider_usage",
    "usage_ledger",
    "org_usage",
    "principal_usage",
    "deployments",
    "notes",
    "feedback",
    "evalset_cases",
    "evalset_index",
    "evalset_assignments",
    "evalset_labels",
)


@dataclass
class World:
    client: TestClient
    store: AuditStore
    hidden: EvalsetStore  # the key holder's, its clock `now` (the server's is the real one)
    keys: EvalsetKeys
    pack: Pack
    db: str
    now: list[datetime]
    users: dict[str, list[str]] = field(default_factory=dict)  # each organization's labellers

    def frozen(
        self, count: int = 3, *, org: str = CLINIC, max_runs: int = 3, max_age_days: int = 30
    ) -> str:
        """A set of `org`'s frozen by the key holder: `count` hand-written cases (every planted
        mark in them), drawn, labelled blind by two labellers who agree, frozen under the rule
        given. Its id."""
        set_id = a_set(self.hidden, self.pack, org)
        self.hidden.add_cases(set_id, cases(count))
        self.hidden.sample(set_id, per_stratum=count)
        if org not in self.users:
            self.users[org] = labellers(self.store, 2, org=org)
        users = self.users[org]
        self.hidden.assign(set_id, users)
        label_all(self.hidden, users, org=org)
        self.hidden.freeze(set_id, max_runs=max_runs, max_age_days=max_age_days, resamples=20)
        return set_id

    def grader(self, org: str = CLINIC) -> dict[str, str]:
        """A new grading key of `org` (the operator's), as its request headers."""
        made = self.store.create_grading_key(org=org, created_by="ops", reason="the tests")
        return {"authorization": f"Bearer {made['key']}"}

    def run(self, set_id: str, headers: dict[str, str], **body: Any) -> Any:
        sent = {"pack": self.pack.id, **body}
        return self.client.post(url(set_id), json=sent, headers=headers)

    def state(self, set_id: str) -> str:
        with self.store._db.session(exclusive=False) as db:
            row = db.one("SELECT state FROM evalset_sets WHERE id = ?", (set_id,))
        assert row is not None
        return str(row["state"])


def deployment(monkeypatch: pytest.MonkeyPatch, accounts: str) -> EvalsetKeys:
    """A deployment in `accounts` mode with sign-in, Backend Services, PHI mode `encrypt` and
    the evaluation-set keys (throwaway ones, made here), its rate limits off (one test puts
    them on): those keys."""
    monkeypatch.setenv("MENDARI_ACCOUNTS", accounts)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", HTTPS)
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "off")
    encrypt_mode(monkeypatch)
    return EvalsetKeys(*evalset_on(monkeypatch))


@contextmanager
def started(
    db: str,
    pack: Pack,
    keys: EvalsetKeys,
    providers: dict[str, str] | None = None,
    packs_paths: Sequence[SearchPath] | None = None,
) -> Iterator[World]:
    """The deployment started on `db` (serving the packs found on `packs_paths`, if given), and
    the key holder's store beside it (its clock a test may move back)."""
    now = [datetime.now(UTC)]
    with (
        encrypted_store(db) as store,
        TestClient(
            create_app(db_path=db, providers=providers, packs_paths=packs_paths), base_url=HTTPS
        ) as client,
    ):
        hidden = EvalsetStore(store, keys, clock=lambda: now[0])
        yield World(client, store, hidden, keys, pack, db, now)


@pytest.fixture
def world(db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    with started(db_target, inbox_pack, deployment(monkeypatch, "memberships")) as world:
        yield world


@pytest.fixture(params=["memberships", "claims"])
def either(
    request: pytest.FixtureRequest,
    db_target: str,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[World]:
    """The deployment in each account mode: the route is mounted in both."""
    with started(db_target, inbox_pack, deployment(monkeypatch, request.param)) as world:
        yield world


def url(set_id: str) -> str:
    return RUNS.replace("{set_id}", set_id)


def dump(store: AuditStore, tables: tuple[str, ...] = UNTOUCHED) -> str:
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in tables}, default=str
        )


def private_of(world: World, set_id: str) -> list[str]:
    """What no answer may hold of a set: its cases' ids and keyed hashes, and every planted
    mark (an input, an author, a stratum, a language, an author's id, a site, the set's name)."""
    with world.store._db.session(exclusive=False) as db:
        found = db.all("SELECT id, case_hash FROM evalset_cases WHERE set_id = ?", (set_id,))
    return [*MARKS.values(), *(str(row[c]) for row in found for c in ("id", "case_hash"))]


def keys_of(value: Any) -> set[str]:
    """Every key of a JSON value, at any depth."""
    if isinstance(value, dict):
        return set(value) | {k for item in value.values() for k in keys_of(item)}
    if isinstance(value, list):
        return {k for item in value for k in keys_of(item)}
    return set()


def graded_events(world: World, set_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in events(world.store, "evalset.graded")
        if event["payload"]["set"] == set_id
    ]


# -- what a grading key is answered ----------------------------------------------------------------


def test_a_grading_key_grades_its_organizations_frozen_set_with_aggregates_only(
    either: World,
) -> None:
    """In each account mode: the organization's grading key runs the set's pack over its frozen
    set, which becomes active, and is answered aggregates only, as the closed result pins them:
    the set's id and commitment, its size, this run's number and the runs left, the pack and its
    local providers, each question's metrics, the outcome counts and the critical label's
    counts; no case, mark, per-case field or dataset identity. Each run is chained before the
    cases are read, the first after the set's activation; the set verifies."""
    world = either
    set_id = world.frozen(4)
    grader = world.grader()
    answered = world.run(set_id, grader)
    assert answered.status_code == 200, answered.text
    shown = answered.json()
    assert world.state(set_id) == "active"
    (frozen,) = events(world.store, "evalset.frozen")
    assert (shown["format"], shown["set"], shown["commitment"]) == (
        HIDDEN_RESULT_FORMAT,
        set_id,
        frozen["payload"]["commitment"],
    )
    assert (shown["n"], shown["run"], shown["runs_left"]) == (4, 1, 2)
    assert shown["pack"] == {
        "id": world.pack.id,
        "version": world.pack.version,
        "digest": world.pack.digest,
    }
    assert {slot: p["variant"] for slot, p in shown["providers"].items()} == LOCAL
    assert set(shown["questions"]) == {"destination", "urgency"}
    assert shown["questions"]["urgency"]["n"] == 4  # each case's final label, in memory
    assert sum(shown["decisions"]["outcome_counts"].values()) == 4
    (critical,) = shown["decisions"]["critical"]
    assert (critical["question"], critical["label"]) == ("urgency", "urgent")
    assert shown["seeds"] == {"bootstrap": 0, "resamples": 2000, "level": 0.95}
    assert not keys_of(shown) & PER_CASE
    assert [m for m in private_of(world, set_id) if m in answered.text] == []
    HiddenResult.model_validate(shown)
    kinds = [
        e["event"] for e in events(world.store, "evalset.activated") + graded_events(world, set_id)
    ]
    assert kinds == ["evalset.activated", "evalset.graded"]
    (read,) = graded_events(world, set_id)
    assert set(read["payload"]) == GRADED
    assert (read["payload"]["n"], read["payload"]["run"]) == (4, 1)
    assert read["payload"]["pack_digest"] == world.pack.digest
    assert read["payload"]["grader"].startswith("key:")
    again = world.run(set_id, grader).json()
    assert (again["run"], again["runs_left"]) == (2, 1)
    assert len(events(world.store, "evalset.activated")) == 1
    assert {k: v for k, v in again.items() if k not in ("run", "runs_left")} == {
        k: v for k, v in shown.items() if k not in ("run", "runs_left")
    }  # the same cases, the same pack: the same aggregates
    assert world.store.verify(evalsets=world.keys)["ok"]


def test_a_frozen_set_becomes_active_on_its_first_run(world: World) -> None:
    """A frozen set is `active` from its first run on: one `evalset.activated`, chained in the
    run's own transaction before its `evalset.graded`; an active set is graded without
    another."""
    set_id = world.frozen(2)
    assert world.state(set_id) == "frozen"
    grader = world.grader()
    assert world.run(set_id, grader).status_code == 200
    assert world.state(set_id) == "active"
    activated = events(world.store, "evalset.activated")
    (read,) = graded_events(world, set_id)
    assert [e["payload"]["set"] for e in activated] == [set_id]
    assert activated[0]["seq"] < read["seq"]
    assert world.run(set_id, grader).status_code == 200
    assert len(events(world.store, "evalset.activated")) == 1


def test_the_result_holds_no_per_case_field(inbox_pack: Pack) -> None:
    """Every answer model forbids a field it does not list and lists exactly those pinned; a
    `mendari.eval/2` result projected onto it keeps none of its per-case fields (the critical
    labels' missed cases, the misclassified cases, the subgroups, the dataset's and the
    ledger's identity, the equal-mass bins); calibration bins and the Brier interval over fewer
    than `MIN_GROUP` cases are left out; a result that does not fit names nothing of it."""
    for model, fields in PINNED.items():
        assert model.model_config.get("extra") == "forbid", model.__name__
        assert set(model.model_fields) == fields, model.__name__
    pack = inbox_pack
    named = resolve_dataset(pack, "test")
    for size in (60, 4):
        dataset = Dataset(
            name="hidden", path=named.path, records=named.records[:size], sha256="", case_key=b"k"
        )
        decision = Decision(pack, origin_cap=False)
        results = [decision.decide(record["input"]) for record in dataset.records]
        full = result_from_ledger(ledgers.build(decision, dataset, results))
        assert {"misclassified", "subgroups", "dataset", "ledger"} <= set(full)
        assert "missed_cases" in full["decisions"]["critical"][0]
        read = GradingRead(str(uuid.uuid4()), "c" * 64, size, 1, 0)
        shown = hidden_result(full, read).model_dump(mode="json")
        assert not keys_of(shown) & PER_CASE
        assert all(
            found["n"] >= MIN_GROUP
            for question in shown["questions"].values()
            for found in question["calibration"]["bins"]
        )
        intervals = [q["brier_interval"] for q in shown["questions"].values()]
        assert all(intervals) == (size == 60) and any(intervals) == (size == 60), size
        kept = sum(len(q["calibration"]["bins"]) for q in shown["questions"].values())
        assert (kept > 0) == (size == 60)
        texts = json.dumps(shown)
        assert not [record["id"] for record in dataset.records if str(record["id"]) in texts]
    with pytest.raises(ValidationError, match="Extra inputs"):
        HiddenResult.model_validate({**shown, "missed_cases": []})
    with pytest.raises(ValidationError, match="Extra inputs"):
        HiddenCritical.model_validate(
            {**shown["decisions"]["critical"][0], "missed_cases": ["a case"]}
        )
    with pytest.raises(MendariError, match=HIDDEN_RESULT_FORMAT) as refused:
        hidden_result({**full, "errors": {MARKS["input"]: 1}}, read)
    assert MARKS["input"] not in str(refused.value)


# -- who reaches it --------------------------------------------------------------------------------


def test_only_the_grading_key_passes(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """Anonymous: `401`. A member of each role (the administrator's among them) and a labeller,
    a key of each machine role, a legacy key of each other role (the administrator's among
    them), scoped keys and Backend Services clients of each machine role: the `grading_only`
    `403`. An EHR launch's cookie is no credential here: `401`. A person of no organization:
    `NoOrganization`. Nothing is written, read or logged as a hidden set's; the grading key is
    answered. Open mode cannot start with the keys, and holds no `grade` anyway. The runbook
    says so of an EHR launch."""
    set_id = world.frozen(2)
    secrets = {
        role: world.store.create_api_key(name=role, role=role, org=CLINIC, created_by="t")["key"]
        for role in ("viewer", "service")
    }
    for role in sorted({*ROLES, "labeller"} - set(secrets)):
        secrets[f"legacy {role}"] = legacy_key(world.db, role, CLINIC)
    for role, scope in (("service", DECIDE), ("viewer", "system/*.rs")):
        granted = parse(scope, permissions=auth_module.PERMISSIONS[role]).scope
        made = world.store.create_api_key(
            name=f"{role} scoped", role=role, org=CLINIC, created_by="t", scopes=granted
        )
        secrets[f"scoped {role}"] = made["key"]
    public = json.loads(RSAAlgorithm.to_jwk(rsa.generate_private_key(65537, 2048).public_key()))
    jwks = check_jwks({"keys": [{**public, "kid": "k"}]})
    for role in ("viewer", "service"):
        granted = parse(default_scope(role), permissions=auth_module.PERMISSIONS[role]).scope
        client = world.store.register_client(
            name=role, role=role, org=CLINIC, created_by="t", scopes=granted, jwks=jwks
        )
        secrets[f"client {role}"] = world.store.issue_token(client["client_id"], granted, 300)[0]
    people = {
        role: member(world.store, org=CLINIC, roles=[role], sub=f"member-{role}")
        for role in (*ROLES, "labeller")
    }
    launch = launched(world.store)
    nobody = member(world.store, org=None, sub="nobody")
    before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
    capsys.readouterr()
    assert world.run(set_id, {}).status_code == 401
    assert world.run(set_id, launch).status_code == 401
    for name, headers in [
        *people.items(),
        *((name, {"authorization": f"Bearer {s}"}) for name, s in secrets.items()),
    ]:
        refused = world.run(set_id, headers)
        assert (refused.status_code, refused.json()["detail"]) == (
            403,
            "only the deployment's grading key may do this",
        ), name
    refused = world.run(set_id, nobody)
    assert (refused.status_code, refused.json()["error"]) == (403, "NoOrganization")
    assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
    lines = logged(capsys)
    refusals = {line["reason"] for line in lines if line["event"] == "auth.permission.refused"}
    assert refusals == {"grading_only", "no_organization"}
    assert not [line for line in lines if line["event"].startswith("evalset.")]
    assert world.run(set_id, world.grader()).status_code == 200
    assert not auth_module.OPEN.may("grade")
    runbook = " ".join((ROOT / "docs/operations/evalsets.md").read_text().split())
    assert "an EHR launch's cookie is no credential there (`401`)" in runbook


def test_open_mode_cannot_start_with_the_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evaluation-set keys refuse a server without sign-in (LP-11c), so no grading route
    is ever mounted in open mode; without the keys, none in either account mode."""
    evalset_on(monkeypatch)
    with pytest.raises(MendariError, match="sign-in"):
        create_app(db_path=tmp_path / "open.db")
    for accounts in ("claims", "memberships"):
        deployment(monkeypatch, accounts)  # the keys set again
        for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_INDEX_KEY"):
            monkeypatch.delenv(name)
        keyless = create_app(db_path=tmp_path / f"{accounts}-keyless.db")
        assert ("POST", RUNS) not in mounted_routes(keyless), accounts


def test_another_organizations_set_answers_as_an_unknown_one_does(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another organization's grading key, on this organization's frozen set, an unknown id and
    a malformed one: one `404` (`no_set`), the same body, naming no id; nothing is read,
    written or chained, and the log names no set. This organization's key is then answered."""
    set_id = world.frozen(2)
    world.frozen(2, org=OTHER)  # the other organization, recorded, with a set of its own
    theirs = world.grader(OTHER)
    before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
    capsys.readouterr()
    told = []
    for asked in (set_id, str(uuid.uuid4()), "not-a-set"):
        refused = world.run(asked, theirs)
        assert refused.status_code == 404, asked
        body = without_request_id(refused.json(), refused)
        assert body == {"error": "GradingRefused", "detail": NO_SET, "reason": "no_set"}
        assert asked not in refused.text
        told.append(body)
    assert told[0] == told[1] == told[2]
    assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
    lines = [line for line in logged(capsys) if line["event"] == "evalset.graded"]
    assert [(line["outcome"], line["reason"], line.get("ids")) for line in lines] == [
        ("refused", "no_set", None)
    ] * 3
    assert world.run(set_id, world.grader()).status_code == 200


# -- the rotation rule -----------------------------------------------------------------------------


def test_runs_are_counted_and_the_rotation_rule_refuses(world: World) -> None:
    """Two runs allowed: each answered with its number and the runs left, chained; the third is
    a `409` `no_runs_left` that reads and chains nothing. A set past its age (frozen 31 days ago,
    for 30) is a `409` `expired`; a draft set and a retired one are `409` `not_gradable`; each
    refusal reads and chains nothing, and says nothing of the set but its code."""
    set_id = world.frozen(2, max_runs=2)
    grader = world.grader()
    answered = [world.run(set_id, grader).json() for _ in range(2)]
    assert [(a["run"], a["runs_left"]) for a in answered] == [(1, 1), (2, 0)]
    world.now[0] = datetime.now(UTC) - timedelta(days=31)
    old = world.frozen(2, max_age_days=30)
    world.now[0] = datetime.now(UTC)
    draft = a_set(world.hidden, world.pack)
    world.hidden.add_cases(draft, cases(1))
    retired = world.frozen(2)
    world.hidden.retire(retired)
    for asked, reason in (
        (set_id, "no_runs_left"),
        (old, "expired"),
        (draft, "not_gradable"),
        (retired, "not_gradable"),
    ):
        before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
        refused = world.run(asked, grader)
        assert (refused.status_code, refused.json()["reason"]) == (409, reason), asked
        assert refused.json()["detail"] == GRADING_REFUSALS[reason][1]
        assert asked not in refused.text
        assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
    assert len(graded_events(world, set_id)) == 2
    assert world.state(old) == "frozen" and world.state(draft) == "draft"


# -- the providers ---------------------------------------------------------------------------------


def test_a_remote_variant_is_refused_before_any_case_is_read(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every slot runs a local variant on a hidden set (conflict 52): one asked for that calls a
    remote provider (Jev, alone or in a composite), or the deployment's own choice of one, is a
    `422` `remote_provider`; a slot or a variant the pack does not have is `unknown_variant`,
    and another pack than the set's `wrong_pack`. Each is refused having opened the set's
    header only: no case is read, no run counted, nothing chained. Naming a local variant for
    the slot the deployment runs remotely is answered."""
    keys = deployment(monkeypatch, "memberships")
    with started(db_target, inbox_pack, keys, providers=calm_jev(monkeypatch)) as world:
        set_id = world.frozen(2)
        grader = world.grader()
        opened: list[str] = []
        real = EvalsetKeys.open

        def spied(keys: EvalsetKeys, kind: Any, *bound: Any) -> tuple[Any, str]:
            opened.append(kind)
            return real(keys, kind, *bound)

        monkeypatch.setattr(EvalsetKeys, "open", spied)
        before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
        for body, reason, status in (
            ({}, "remote_provider", 422),  # the deployment's own: rules_and_jev
            ({"variants": {"safety_screen": "jev"}}, "remote_provider", 422),
            ({"variants": {"safety_screen": "rules", "router": "cascade"}}, "remote_provider", 422),
            ({"variants": {"safety_screen": "rules", "screen": "rules"}}, "unknown_variant", 422),
            ({"variants": {"safety_screen": "nothing"}}, "unknown_variant", 422),
            ({"pack": "mendari/ai-guard", "variants": LOCAL}, "wrong_pack", 422),
        ):
            opened.clear()
            refused = world.run(set_id, grader, **body)
            assert (refused.status_code, refused.json()["reason"]) == (status, reason), body
            assert opened == ["header"], body
        assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
        assert world.state(set_id) == "frozen"
        opened.clear()
        answered = world.run(set_id, grader, variants={"safety_screen": "rules"})
        assert answered.status_code == 200, answered.text
        assert answered.json()["providers"]["safety_screen"]["variant"] == "rules"
        assert {"input", "attributes", "label", "freeze"} <= set(opened)


def test_a_pack_whose_result_cannot_fit_is_refused_before_any_run_counts(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pack whose own names `mendari.hidden_result/1` cannot hold, whatever the cases (here
    the set's pack, served with its router's variant named in 66 characters, past the 64 a name
    takes): a `409` `pack_unavailable`, refused having opened the set's header only, so no case
    is read, no run counted (a run would fail its answer after its read) and nothing chained.
    The reference packs' local variants hold."""
    served = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, served)
    manifest = yaml.safe_load((served / "pack.yaml").read_text())
    router = manifest["providers"]["router"]
    long = "naive_bayes_" + "retrained" * 6
    router["variants"][long] = router["variants"].pop("naive_bayes")
    router["use"] = long
    (served / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    keys = deployment(monkeypatch, "memberships")
    with started(
        db_target, inbox_pack, keys, packs_paths=[SearchPath(served, "maintained")]
    ) as world:
        set_id = world.frozen(2)
        grader = world.grader()
        opened: list[str] = []
        real = EvalsetKeys.open

        def spied(keys: EvalsetKeys, kind: Any, *bound: Any) -> tuple[Any, str]:
            opened.append(kind)
            return real(keys, kind, *bound)

        monkeypatch.setattr(EvalsetKeys, "open", spied)
        before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
        refused = world.run(set_id, grader)
        assert (refused.status_code, refused.json()["reason"]) == (409, "pack_unavailable")
        assert refused.json()["detail"] == GRADING_REFUSALS["pack_unavailable"][1]
        assert opened == ["header"]
        assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
        assert graded_events(world, set_id) == [] and world.state(set_id) == "frozen"
    assert holds(Decision(inbox_pack, providers=LOCAL, origin_cap=False))


# -- nothing stored --------------------------------------------------------------------------------


def test_nothing_is_stored_and_api_evals_is_unchanged(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run records no decision, saves no report or ledger and spends no usage (the store's
    `save_report` and `record` fail the test if called): `/api/evals` answers the same before
    and after, and every table but the set's row (its state) and the chain (its reads) is as it
    was."""
    set_id = world.frozen(3)
    admin = member(world.store, org=CLINIC, roles=["admin"], sub="admin")
    ran = world.client.post(
        f"/api/packs/{world.pack.id}/evals", json={"dataset": "test", "limit": 2}, headers=admin
    )
    assert ran.status_code == 200
    listed = world.client.get("/api/evals", headers=admin).json()
    before = dump(world.store)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a grading run stored something")

    with monkeypatch.context() as storing:
        storing.setattr(AuditStore, "save_report", never)
        storing.setattr(AuditStore, "record", never)
        assert world.run(set_id, world.grader()).status_code == 200
    assert dump(world.store) == before
    assert world.client.get("/api/evals", headers=admin).json() == listed


# -- refusals and failures -------------------------------------------------------------------------


@pytest.mark.parametrize("tampered", ["a case's status", "the header"])
def test_a_tampered_set_is_refused_without_naming_the_case(
    world: World,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tampered: str,
) -> None:
    """A case's status changed behind the store's back, or another set's header (of the same
    organization, sealed under the same key) copied into the set's row, which then opens as no
    set's: `409` `set_changed`, its fixed sentence only, naming no case; no read is chained, no
    run counted; the security log names the set and the reason, and the operator's log the set
    only, sending the operator to `audit verify` (never to a key rotation: nothing is
    missing)."""
    caplog.set_level(logging.WARNING, logger="mendari.evalsets")
    set_id = world.frozen(2)
    if tampered == "the header":
        other = world.frozen(2)
        with world.store._db.session(exclusive=False) as db:
            theirs = db.one("SELECT header FROM evalset_sets WHERE id = ?", (other,))
        assert theirs is not None
        tamper(
            world.store,
            "UPDATE evalset_sets SET header = ? WHERE id = ?",
            (theirs["header"], set_id),
        )
    else:
        with world.store._db.session(exclusive=False) as db:
            case = db.one("SELECT id, case_hash FROM evalset_cases WHERE set_id = ?", (set_id,))
        assert case is not None
        tamper(
            world.store, "UPDATE evalset_cases SET status = 'resolved' WHERE id = ?", (case["id"],)
        )
    capsys.readouterr()
    refused = world.run(set_id, world.grader())
    assert refused.status_code == 409
    assert without_request_id(refused.json(), refused) == {
        "error": "GradingRefused",
        "detail": GRADING_REFUSALS["set_changed"][1],
        "reason": "set_changed",
    }
    assert [m for m in private_of(world, set_id) if m in refused.text] == []
    assert graded_events(world, set_id) == []
    lines = [line for line in logged(capsys) if line["event"] == "evalset.graded"]
    assert [(line["outcome"], line["reason"], line["ids"]) for line in lines] == [
        ("refused", "set_changed", [set_id])
    ]
    assert world.state(set_id) == "frozen"
    (warned,) = [r for r in caplog.records if r.name == "mendari.evalsets"]
    said = warned.getMessage()
    assert set_id in said and "does not hold what it was frozen with" in said
    assert "`mendari audit verify --evalsets`" in said and "rotate" not in said
    assert [m for m in private_of(world, set_id) if m in said] == []


def test_a_failed_run_counts_and_is_told_by_its_code_only(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that fails after its read (here a decision raising with a case's text in its
    message): `500` `run_failed`, its fixed sentence only; the read stays chained, so it counts
    against the rotation rule; the operator's log names the set and the error's type only."""
    caplog.set_level(logging.WARNING, logger="mendari.evalsets")
    set_id = world.frozen(2)

    def failing(self: Decision, inputs: Any, **kwargs: Any) -> Any:
        raise MendariError(f"{MARKS['input']}: {json.dumps(inputs)}")

    monkeypatch.setattr(Decision, "decide", failing)
    refused = world.run(set_id, world.grader())
    assert (refused.status_code, refused.json()["reason"]) == (500, "run_failed")
    assert refused.json()["detail"] == GRADING_REFUSALS["run_failed"][1]
    assert [m for m in private_of(world, set_id) if m in refused.text] == []
    assert [e["payload"]["run"] for e in graded_events(world, set_id)] == [1]
    (warned,) = [r for r in caplog.records if r.name == "mendari.evalsets"]
    assert warned.getMessage() == (
        f"a grading run of evaluation set {set_id} failed (MendariError): it counts as a run"
    )


def test_each_run_and_refusal_is_logged_by_set_id_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run is logged `evalset.graded` with the set's id and how many cases were graded, a
    refusal with its reason (and the set's id, but for `no_set`); the access line names the
    route's template; no line holds a case, a mark or a label word. The operator's guide lists
    the event among the security events (docs/deploying-with-phi.md, §7)."""
    set_id = world.frozen(3)
    grader = world.grader()
    capsys.readouterr()
    assert world.run(set_id, grader).status_code == 200
    assert world.run(set_id, grader, pack="mendari/ai-guard").status_code == 422
    assert world.run(str(uuid.uuid4()), grader).status_code == 404
    lines = logged(capsys)
    shown = [
        (e["event"], e.get("ids"), e.get("count"), e["outcome"], e.get("reason"))
        for e in lines
        if e["event"] != "request"
    ]
    assert shown == [
        ("evalset.graded", [set_id], 3, "ok", None),
        ("evalset.graded", [set_id], None, "refused", "wrong_pack"),
        ("evalset.graded", None, None, "refused", "no_set"),
    ]
    assert {e["route"] for e in lines if e["event"] == "request"} == {RUNS}
    written = json.dumps(lines)
    assert [m for m in private_of(world, set_id) if m in written] == []
    assert not re.search(r"\b(routine|urgent|nurse)\b", written)
    guide = (ROOT / "docs" / "deploying-with-phi.md").read_text()
    assert "`evalset.graded`" in guide


def test_the_rate_limit_class_is_evaluate(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grading run counts against the grading key's `evaluate` bucket (6 an hour by default):
    the seventh call in an hour is a `429` before the route, refused or not, chaining nothing."""
    keys = deployment(monkeypatch, "memberships")
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    with started(db_target, inbox_pack, keys) as world:
        set_id = world.frozen(2, max_runs=10)
        grader = world.grader()
        assert world.run(set_id, grader).status_code == 200
        statuses = [world.run(set_id, grader, pack="x/y").status_code for _ in range(5)]
        assert statuses == [422] * 5
        before = dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events"))
        limited = world.run(set_id, grader)
        assert limited.status_code == 429 and "retry-after" in limited.headers
        assert dump(world.store, (*UNTOUCHED, "evalset_sets", "audit_events")) == before
        assert permission_for("POST", RUNS) == "grade"


# -- where it is mounted, what it opens, and how it is documented ---------------------------------


def test_the_route_is_mounted_with_the_keys_in_both_modes_and_documented(either: World) -> None:
    """With both keys, in either account mode: the route, needing `grade` (never `read`,
    `discover` or `curate`), under its own tag in the reference, for an API key only, its
    refusals' reasons named; and the `/docs` table lists it under `grade`, with no scope."""
    world = either
    assert ("POST", RUNS) in mounted_routes(world.client.app)  # type: ignore[arg-type]
    assert permission_for("POST", RUNS) == "grade"
    for method in ("GET", "PUT", "DELETE"):
        assert permission_for(method, url("s1")) == "grade"
    reader = signed_in(world.store, role="viewer")
    if world.client.get("/api/auth/me").json().get("accounts") == "memberships":
        reader = member(world.store, org=CLINIC, roles=["viewer"], sub="reader")
    schema = world.client.get("/openapi.json", headers=reader).json()
    operation = schema["paths"][RUNS]["post"]
    assert operation["tags"] == ["Grading"]
    assert operation["security"] == [{"apiKey": []}]
    assert "grading key" in operation["description"]
    assert all(f"`{reason}`" in operation["description"] for reason in GRADING_REFUSALS)
    assert "Grading" in {tag["name"] for tag in schema["tags"]}
    landing = world.client.get("/docs").text
    row = re.search(r'<tr data-permission="grade">(.*?)</tr>', landing, re.S)
    assert row and f"<code>POST {RUNS}</code></li>" in row[1]
    assert "docs-scope" not in row[1] and "grading key" in row[1]


def test_what_the_route_opens_is_what_the_threat_model_says(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sealed value a run opens in the server (`EvalsetKeys.open`), as threat model row
    I13 says of it: the set's header (to check the pack asked for), then its freeze record and
    each case's input, attributes and labels (to recompute its commitment and hand the cases to
    the run). The row's list of the routes that open a sealed column names the grading key's,
    in either account mode, and it never says that no route computes statistics."""
    set_id = world.frozen(2)
    grader = world.grader()
    opened: list[str] = []
    real = EvalsetKeys.open

    def spied(keys: EvalsetKeys, kind: Any, *bound: Any) -> tuple[Any, str]:
        opened.append(kind)
        return real(keys, kind, *bound)

    with monkeypatch.context() as spying:
        spying.setattr(EvalsetKeys, "open", spied)
        assert world.run(set_id, grader).status_code == 200
    assert set(opened) == {"header", "freeze", "input", "attributes", "label"}
    (row,) = [line for line in rows() if line.startswith("| I13 |")]
    said = re.search(rf"`POST {re.escape(RUNS)}` opens ([^;.]*)", row)
    assert said is not None
    named = {kind for word, kind in SEALED_WORDS.items() if re.search(rf"\b{word}\b", said[1])}
    assert named == set(opened)
    lead = row.split(" in the server:", 1)[0]
    assert "and its grading key's (LP-11l), in either account mode with the keys" in lead
    assert not re.search(r"\bno route (freezes|retires|computes)", row)


def test_the_runbooks_grading_runs_as_written(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runbook's grading key (`mendari access grader-key`) and its run (the `curl` body), as
    docs/operations/evalsets.md writes them, on a frozen set: answered, the key's secret printed
    once and the run chained with its id."""
    set_id = world.frozen(2)
    text = (ROOT / "docs/operations/evalsets.md").read_text().replace("\\\n", " ")
    (minting,) = [line for line in text.splitlines() if line.startswith("mendari access grader-")]
    argv = shlex.split(minting.replace("<org>", CLINIC).replace('"$DATABASE_URL"', world.db))
    capsys.readouterr()
    assert main(argv[1:]) == 0
    key = json.loads(capsys.readouterr().out)
    (posted,) = re.findall(r"curl -sS -X POST \"\$MENDARI/api/evalsets/<set id>/runs\".*", text)
    body = json.loads(shlex.split(posted)[-1])
    answered = world.client.post(
        url(set_id), json=body, headers={"authorization": f"Bearer {key['key']}"}
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["providers"]["router"]["variant"] == body["variants"]["router"]
    (read,) = graded_events(world, set_id)
    assert read["payload"]["grader"] == f"key:{key['id']}"
