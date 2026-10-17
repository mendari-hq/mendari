# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import gc
import json
import os
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack, load_pack
from mendari.core.evalset_keys import EvalsetKeys, evalset_keys
from mendari.core.phi import PhiPolicy
from mendari.core.registry import resolve_pack
from mendari.errors import ProviderError
from mendari.hub import archive
from mendari.hub.registry import create_registry_app
from mendari.providers import credentials, llm
from mendari.providers.jev import JevProvider
from mendari.server import sessions

ROOT = Path(__file__).resolve().parents[1]
INBOX_DIR = ROOT / "packs" / "patient-inbox-router"
LOCAL_DATASET = ROOT / "datasets" / "local-site-example.jsonl"
# Postgres tests run when this points at a disposable database, e.g.
# postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test (see CLAUDE.md).
POSTGRES_URL = os.environ.get("MENDARI_TEST_DATABASE_URL", "").strip()
# A developer's shell may hold a deployment's own systems: its database (Neon's integration
# exports these), the AI Gateway's key or a Vercel OIDC token (`vercel env pull`), an LLM or Jev
# endpoint and key (and the origins they may be at), the alert webhook, the registry, the payer.
# No test may reach them, whatever a module opens at import (the Vercel entrypoint builds its
# app on DATABASE_URL) or a subprocess inherits: they are gone before anything is collected, and
# so are the old OPENHEALTH_* names, which `mendari.legacy` would copy back in a subprocess. A
# test that needs one sets its own.
LIVE_SETTINGS = (
    "DATABASE_URL", "DATABASE_URL_UNPOOLED", "AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "JEV_API_URL", "JEV_API_KEY", "JEV_MODEL",
    "JEV_CONNECTOR", "MENDARI_PROVIDERS", "MENDARI_PROVIDER_ALLOWED_ORIGINS",
    "MENDARI_ALERT_WEBHOOK", "MENDARI_ALERT_SECRET", "MENDARI_ALERT_FORMAT",
    "MENDARI_REGISTRY_URL", "MENDARI_ACCOUNTS",
    "MENDARI_REGISTRY_TOKEN", "MENDARI_REGISTRY_ADMIN_TOKEN", "MENDARI_PAS_PAYER_URL",
    "CRON_SECRET", "MENDARI_EVALSET_KEY", "MENDARI_EVALSET_READ_KEYS", "MENDARI_EVALSET_INDEX_KEY",
)  # fmt: skip
for _name in [*LIVE_SETTINGS, *(name for name in os.environ if name.startswith("OPENHEALTH_"))]:
    os.environ.pop(_name, None)
# An input hash key (MENDARI_INPUT_HASH_KEY), which PHI modes hash and encrypt require.
INPUT_HASH_KEY = secrets.token_bytes(32)
HASH_KEY_B64 = base64.b64encode(INPUT_HASH_KEY).decode()
PHI_KEY = secrets.token_bytes(32)  # test keys for PHI mode `encrypt` (`encrypt_mode`)
PHI_KEY_B64 = base64.b64encode(PHI_KEY).decode()
# The edit loop, `uv run pytest -m core` (about 20 s; the full suite takes about 6 min): pack
# loading, decisions, policy and the audit. Only files in tests/ itself: `-m core` replaces the
# default marker filter, so a browser or slow test here would run.
CORE = {
    "test_spec.py", "test_spec_02.py", "test_pack_status.py", "test_input_schema.py",
    "test_patterns.py", "test_templates.py", "test_community_caps.py", "test_new_pack.py",
    "test_decision.py", "test_criteria.py", "test_any_of.py", "test_composites.py",
    "test_dosing.py", "test_providers.py", "test_guard.py", "test_inbox_pack.py",
    "test_referral_pack.py", "test_trial_pack.py", "test_prior_auth_pack.py",
    "test_ai_guard_pack.py", "test_policy.py", "test_safety_invariant.py", "test_governance.py",
    "test_hold.py", "test_gate.py", "test_audit.py", "test_evals.py", "test_registry_engine.py",
    "test_rater_agreement.py",
}  # fmt: skip


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.parent == ROOT / "tests" and item.path.name in CORE:
            item.add_marker(pytest.mark.core)


@pytest.fixture(autouse=True)
def _no_vercel_oidc_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test mints a Vercel OIDC token: without one in the environment, the vercel library
    would mint one through the developer's Vercel CLI login for a linked project (a `.vercel`
    folder above the working directory) and the AI Gateway would bill it. A test of that path
    gives its own (tests/test_llm.py, tests/test_credentials.py)."""

    def refused() -> str:
        raise ProviderError("credential_unavailable", "no Vercel OIDC token in the test suite")

    monkeypatch.setattr(credentials, "vercel_oidc_token", refused)
    monkeypatch.setattr(llm, "vercel_oidc_token", refused)


@pytest.fixture(autouse=True)
def _own_hub_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test has its own Hub config and pack cache, never the developer's (the cache is on
    the pack search path)."""
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(tmp_path / "hub-config"))
    monkeypatch.setenv("MENDARI_CACHE_DIR", str(tmp_path / "hub-cache"))
    monkeypatch.delenv("MENDARI_REGISTRY_URL", raising=False)
    monkeypatch.delenv("MENDARI_REGISTRY_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_community_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Community packs are off unless a test turns them on (tests/test_community_packs.py), as
    in a deployment that never set MENDARI_COMMUNITY_PACKS."""
    monkeypatch.delenv("MENDARI_COMMUNITY_PACKS", raising=False)


@pytest.fixture(autouse=True)
def _no_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests make many requests as one principal: rate limits are off unless a test turns them
    on (tests/test_limits.py), and no organization has a quota, nor any principal a daily cap,
    unless a test gives it one."""
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "off")
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "off")  # the registry's too (LP-18f)
    # The regression check runs a child process per publish: on only where a test turns it on.
    monkeypatch.setenv("MENDARI_REGISTRY_REGRESSION", "off")
    monkeypatch.delenv("MENDARI_ORG_DAILY_LIMITS", raising=False)
    monkeypatch.delenv("MENDARI_PRINCIPAL_DAILY_LIMIT", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)


@pytest.fixture(autouse=True)
def _no_notice_acknowledgement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests decide as signed-in people without first acknowledging the notice: the
    acknowledgement is off unless a test turns it on (tests/test_notice.py), as rate limits are."""
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "off")


@pytest.fixture(autouse=True)
def _phi_mode_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open-mode tests on synthetic data run PHI mode `off` unless they set another
    (tests/test_phi.py); a test that turns sign-in on runs `encrypt_mode`, since sign-in never
    runs `off` (core/phi.py `require_protection`). None runs as the hosted site, whatever the
    developer's shell says."""
    monkeypatch.setenv("MENDARI_PHI_MODE", "off")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.delenv("MENDARI_RETENTION_DAYS", raising=False)


@pytest.fixture(autouse=True)
def _no_evalset_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden evaluation sets are off unless a test turns them on (`evalset_on`): no key from
    the developer's shell reaches a test (LP-11)."""
    for name in ("MENDARI_EVALSET_KEY", "MENDARI_EVALSET_READ_KEYS", "MENDARI_EVALSET_INDEX_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jev is never set up, and no provider is chosen, unless a test does it (with a fake): a
    developer's own Jev settings would turn on the servers' Jev defaults and send test cases to
    Jev, and their MENDARI_PROVIDERS would change what every pack runs."""
    for name in ("JEV_API_URL", "JEV_MODEL", "JEV_API_KEY", "JEV_CONNECTOR", "MENDARI_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)


# The inbox's policy automates only while its urgency screen includes a model
# (automation_requires). Tests that need an automated inbox decision run the screen with Jev,
# against `calm_jev`, which finds nothing urgent.
SCREENED = {"safety_screen": "rules_and_jev"}


# What `calm_jev` answers for the inbox's questions: confidently nothing urgent, and a team it is
# unsure of (below the automation threshold, so a routing it answers is never automated).
CALM = {
    "urgency": {"urgent": 0.02, "routine": 0.98},
    "destination": {
        "front_desk": 0.1,
        "medical_assistant": 0.6,
        "nurse": 0.1,
        "physician": 0.1,
        "billing": 0.1,
    },
}


def calm_jev(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set up a fake Jev for this test that answers the inbox's questions (`CALM`: a confident
    `routine` urgency), and return the inbox providers that use it."""
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        questions = json.loads(request.content)["questions"]
        if not set(questions) <= set(CALM):
            return httpx.Response(400)  # only the inbox's questions are faked
        answers = {}
        for key in questions:
            choice = max(CALM[key], key=lambda label: CALM[key][label])
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": CALM[key][choice],
                "probabilities": CALM[key],
            }
        usage = {"input_tokens": 100, "output_tokens": 10}
        return httpx.Response(200, json={"model": "jev-9.9.9", "answers": answers, "usage": usage})

    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    return dict(SCREENED)


@pytest.fixture
def screened(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The inbox providers whose decisions may be automated, with `calm_jev` in the screen."""
    return calm_jev(monkeypatch)


@pytest.fixture(params=["sqlite", "postgres"])
def db_target(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """An empty audit database: a SQLite file, or a fresh Postgres schema dropped afterwards."""
    if request.param == "sqlite":
        yield str(tmp_path / "audit.db")
        return
    if not POSTGRES_URL:
        pytest.skip("set MENDARI_TEST_DATABASE_URL to run the Postgres backend tests")
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in POSTGRES_URL else "?"
    try:
        yield f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    finally:
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def store(db_target: str) -> Iterator[AuditStore]:
    with AuditStore(db_target) as audit_store:
        yield audit_store


REGISTRY_ADMIN = "admin-token-for-tests"


@dataclass
class Registry:
    client: TestClient
    token: str  # the mendari namespace's publisher token
    key: archive.Ed25519PrivateKey

    def publish(
        self,
        pack: Pack | None = None,  # the referral router
        *,
        key: Any = None,
        token: str | None = None,
        version: str | None = None,
    ) -> Any:
        pack = pack or resolve_pack("mendari/referral-router")
        data = archive.build(pack, key or self.key)
        namespace, name = pack.id.split("/")
        return self.client.put(
            f"/v1/packs/{namespace}/{name}/{version or pack.version}",
            content=data,
            headers={"authorization": f"Bearer {token or self.token}"},
        )


@pytest.fixture
def registry(db_target: str) -> Iterator[Registry]:
    """A registry (hub/registry.py) with the `mendari` namespace, its token and a key."""
    client = TestClient(create_registry_app(db_target, admin_token=REGISTRY_ADMIN))
    client.__enter__()  # runs the app's lifespan, so its store is closed at the end
    admin = {"authorization": f"Bearer {REGISTRY_ADMIN}"}
    assert (
        client.post(
            "/v1/namespaces",
            json={"name": "mendari", "display_name": "Mendari"},
            headers=admin,
        ).status_code
        == 201
    )
    token = client.post("/v1/namespaces/mendari/tokens", headers=admin).json()["token"]
    private, public = archive.generate_key()
    added = client.post(
        "/v1/namespaces/mendari/keys",
        json={"public_pem": public.decode()},
        headers={"authorization": f"Bearer {token}"},
    )
    assert added.status_code == 201
    yield Registry(client, token, archive.load_private_key(private))
    client.__exit__(None, None, None)


@pytest.fixture(scope="session")
def inbox_pack() -> Pack:
    return load_pack(INBOX_DIR)


@pytest.fixture(scope="session")
def router(inbox_pack: Pack) -> Decision:
    return Decision(inbox_pack)


@pytest.fixture
def pack_copy(tmp_path: Path) -> Callable[..., Path]:
    """Copy the reference pack and apply edits to pack.yaml / policy.yaml before loading."""

    def make(
        manifest: Callable[[dict[str, Any]], None] | None = None,
        policy: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        target = tmp_path / "pack"
        shutil.copytree(INBOX_DIR, target, ignore=shutil.ignore_patterns("data"))
        (target / "data").mkdir()
        for split in ("train", "calibration", "test"):
            # Tiny stand-ins: most tests do not need the real splits.
            source = (INBOX_DIR / "data" / f"{split}.jsonl").read_text().splitlines()[:20]
            (target / "data" / f"{split}.jsonl").write_text("\n".join(source) + "\n")
        for name, edit in (("pack.yaml", manifest), ("policy.yaml", policy)):
            if edit is None:
                continue
            path = target / name
            data = yaml.safe_load(path.read_text())
            edit(data)
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        return target

    return make


T = TypeVar("T")


def run_async(coroutine: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run` in a thread of its own. Once the browser tests have run in this process,
    Playwright leaves the main thread's event loop marked as running, and `asyncio.run` refuses
    to start there."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@contextmanager
def cpu_time() -> Iterator[Callable[[], float]]:
    """The processor time this thread spends in the block, as `spent()`, for a budget on
    single-threaded work (loading a pack, as core/patterns.py budgets a pattern: by
    `time.thread_time`). Not the process's: other threads' time (a server or pool an earlier
    test left running) is not this work's, nor is collecting the garbage of earlier tests, so
    the collector runs before and is off during it."""
    gc.collect()
    collecting = gc.isenabled()
    gc.disable()
    started = time.thread_time()
    ended: float | None = None
    try:
        yield lambda: (time.thread_time() if ended is None else ended) - started
    finally:
        ended = time.thread_time()
        if collecting:
            gc.enable()


def security_lines(stdout: str) -> list[dict[str, Any]]:
    """The security log's lines in captured stdout (server/security_log.py), parsed: each one
    must be a JSON object on its own line."""
    return [json.loads(line) for line in stdout.splitlines() if line.startswith('{"ts":')]


def without_request_id(body: dict[str, Any], response: Any) -> dict[str, Any]:
    """A JSON error body without its `request_id`, which must be the response's
    `X-Request-Id`."""
    assert body.get("request_id") == response.headers["x-request-id"]
    return {key: value for key, value in body.items() if key != "request_id"}


def encrypt_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHI mode `encrypt` with the test keys (`PHI_KEY`, `INPUT_HASH_KEY`): how a signed-in
    deployment runs (sign-in never runs `off`, core/phi.py `require_protection`), synthetic
    cases included."""
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", PHI_KEY_B64)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)


def evalset_on(monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, bytes]:
    """Hidden evaluation sets on, with throwaway keys made for this test: (the key, the index
    key)."""
    key, index = secrets.token_bytes(32), secrets.token_bytes(32)
    monkeypatch.setenv("MENDARI_EVALSET_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("MENDARI_EVALSET_INDEX_KEY", base64.b64encode(index).decode())
    monkeypatch.delenv("MENDARI_EVALSET_READ_KEYS", raising=False)
    return key, index


# The routes hidden evaluation sets mount in both account modes (LP-11l: the grading route, an
# organization's grading key's), and those they mount in memberships mode only (LP-11j: a
# labeller's; LP-11k: an organization's curators').
EVALSET_CLAIMS_ROUTES = frozenset({("POST", "/api/evalsets/{set_id}/runs")})
EVALSET_MEMBERSHIPS_ROUTES = frozenset(
    {
        ("GET", "/api/labelling"),
        ("GET", "/api/labelling/next"),
        ("POST", "/api/labelling/{assignment_id}"),
        ("GET", "/api/evalsets"),
        ("GET", "/api/evalsets/{set_id}"),
        ("POST", "/api/evalsets/{set_id}/sample"),
        ("POST", "/api/evalsets/{set_id}/assignments"),
        ("POST", "/api/evalsets/{set_id}/adjudication"),
        *EVALSET_CLAIMS_ROUTES,
    }
)


def evalsets_are_on(client: TestClient, headers: Mapping[str, str]) -> None:
    """Hidden evaluation sets are on in the server `client` talks to, as a route inventory needs
    (LP-11g, conflict 37: with them off, their routes are not mounted and the inventory passes on
    nothing): health names, to a reader (`headers`), the ids of this environment's
    evaluation-set keys; and the routes they mount are mounted: the grading route in either
    account mode (LP-11l), and in memberships mode a labeller's (LP-11j) and its curators'
    (LP-11k) too."""
    keys = evalset_keys()
    assert keys is not None, "turn them on first: evalset_on(monkeypatch)"
    shown = client.get("/api/health", headers=dict(headers)).json()
    assert shown.get("evalset_keys") == keys.key_ids, shown.get("evalset_keys")
    app = cast(FastAPI, client.app)
    mounted = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert mounted >= EVALSET_CLAIMS_ROUTES, "the keys mount the grading route"
    if client.get("/api/auth/me").json().get("accounts") == "memberships":
        assert mounted >= EVALSET_MEMBERSHIPS_ROUTES, "memberships mode mounts the sets' routes"


def evalset_keys_for_test() -> EvalsetKeys:
    """Throwaway evaluation-set keys for a store made in a test (no environment)."""
    return EvalsetKeys(secrets.token_bytes(32), secrets.token_bytes(32))


def hand_written(body: str, **attributes: Any) -> dict[str, Any]:
    """A hidden case as its author writes it: the inbox pack's input, hand-written, in English,
    of the routine stratum (each overridable, or removed with None)."""
    given: dict[str, Any] = {"author": "author-1", "language": "en", "stratum": "routine"}
    given.update({"source": "hand_written", "template": None, **attributes})
    return {
        "input": {"message": {"body": body, "channel": "portal"}},
        "attributes": {name: value for name, value in given.items() if value is not None},
    }


def encrypted_store(target: str | Path) -> AuditStore:
    """A store under `encrypt_mode`'s policy, as a signed-in server opens its database: what a
    test records beside such a server, it records through this (a store under `off` may not)."""
    return AuditStore(target, phi=PhiPolicy(mode="encrypt", key=PHI_KEY, hash_key=INPUT_HASH_KEY))


def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as the hosted site does: `VERCEL` set and the test keys, no mode chosen, so it runs
    its default, `encrypt`."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("MENDARI_PHI_MODE", raising=False)
    monkeypatch.setenv("MENDARI_PHI_KEY", PHI_KEY_B64)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)


def message(body: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"body": body, **extra}}


def signed_in(
    store: AuditStore, *, role: str, org: str = "default", host: str = "mendari.test"
) -> dict[str, str]:
    """Request headers for a person signed in with `role` in `org` on `host`: a session row as
    `/auth/callback` makes one (the chain names them `<role>@<org>.example.org`, a verified
    email), under the cookie name that host uses. Review is a person's (keys are viewer or
    service)."""
    sid = secrets.token_urlsafe(32)
    email = f"{role}@{org}.example.org"
    claims = {"roles": [role], "email": email, "email_verified": True}
    row = {"sub": f"{org}-{role}", "name": f"{org} {role}", "actor": email, "claims": claims}
    session, expires_at = sessions.begin(
        {**row, "roles": [role], "org": org}, int(time.time()), sessions.Lifetime()
    )
    store.smart_put(sid, "user", session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SESSION, host)}={sid}"}


def member(
    store: AuditStore,
    *,
    org: str | None,
    roles: list[str] | None = None,
    sub: str = "member-1",
    email: str | None = None,
    host: str = "mendari.test",
) -> dict[str, str]:
    """Request headers for a person signed in in memberships mode (`MENDARI_ACCOUNTS`): their
    record, `org`'s record if it has none, and, with `roles`, their membership of it, chosen in
    a session row as `/auth/callback` makes one (issuer https://idp.example.org). `org=None`: a
    person of no organization."""
    from mendari.server.auth import MEMBER, person_id

    issuer = "https://idp.example.org"
    person = person_id(issuer, sub)
    store.record_user(issuer=issuer, subject=sub, email=email, name=sub)
    chosen = None
    if org is not None:
        if store.organization(org) is None:  # `default`'s too, as the bootstrap makes it
            with store._db.session(exclusive=True) as db:
                store._insert_organization(db, org=org, name=org.title(), created_by="test")
        found = store.membership_in_force(person, org)
        if found is None and roles:
            found = store.add_membership(org=org, roles=roles, created_by="test", user_id=person)
        chosen = found
    row = {
        "iss": issuer,
        "sub": sub,
        "name": sub,
        "actor": email or f"{issuer}|{sub}",
        "email": email,
        "org": None if chosen is None else org,
        "membership": None if chosen is None else chosen["id"],
    }
    session, expires_at = sessions.begin(row, int(time.time()), sessions.Lifetime())
    sid = secrets.token_urlsafe(32)
    store.smart_put(sid, MEMBER, session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SESSION, host)}={sid}"}


def launched(
    store: AuditStore,
    *,
    iss: str = "https://ehr.example.org/fhir",
    sub: str = "clinician-1",
    host: str = "mendari.test",
) -> dict[str, str]:
    """Request headers for a browser an EHR launched (from the FHIR base `iss`, by the user
    `sub` at that EHR): a session row as `/smart/callback` makes one, with who launched, under
    the launch cookie's name on `host`. No request is ever made with its token."""
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    row = {
        "iss": iss,
        "access_token": f"access-{sid[:8]}",
        "scope": "launch openid fhirUser patient/*.rs",
        "patient": "ref-p1",
        "encounter": None,
        "fhir_user": "Practitioner/pr-1",
        "expires_at": now + 3600,
        "pack": "mendari/referral-router",
        "identity": {"iss": iss, "sub": sub},
        "actor": f"{iss}|{sub}",
        "identified": True,  # made with sign-in on: the callback's checks ran
    }
    session, expires_at = sessions.begin(row, now, sessions.Lifetime(), ends_by=now + 3600)
    store.smart_put(sid, "session", session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SMART, host)}={sid}"}
