# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import copy
import functools
import gc
import json
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Agent, AuditStore, Decision, load_agent
from mendari.core import reference as _reference
from mendari.core import registry as _registry
from mendari.core.agent import agent_digest
from mendari.core.phi import PhiPolicy
from mendari.core.registry import resolve_agent
from mendari.errors import ProviderError
from mendari.hub import archive
from mendari.hub.registry import RegistryStore, create_registry_app
from mendari.providers import credentials, llm
from mendari.providers.agent import AgentProvider
from mendari.server import app as server_app
from mendari.server import sessions
from mendari.server.origin import OWN_REQUEST

ROOT = Path(__file__).resolve().parents[1]
# The synthetic agents the tests decide with (`tests/support/fixture_agents.py` writes their data)
# are fixtures, not part of the product: here they stand where a release's bundled agents do, so
# they load with the maintained origin. Both names are read when called, never copied, so setting
# them here reaches every module. Tests reach the fixtures by these names, never by a literal path.
FIXTURE_AGENTS = ROOT / "tests" / "fixtures" / "agents"
_reference.BUNDLED_DIRS = (FIXTURE_AGENTS,)
_registry._BUNDLED_DIRS = _reference.BUNDLED_DIRS
TRIAGE_DIR = FIXTURE_AGENTS / "triage"  # mendari-test/triage: two slots, risk level 1
CLINICAL_DIR = FIXTURE_AGENTS / "clinical-review"  # mendari-test/clinical-review: risk level 2
CHART_DIR = FIXTURE_AGENTS / "chart-summary"  # mendari-test/chart-summary: a FHIR record in
FIXTURE_NAMESPACE = "mendari-test"  # the fixtures' ids' namespace
# Postgres tests run when this points at a disposable database, e.g.
# postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test (see CLAUDE.md).
_BASE_POSTGRES_URL = os.environ.get("MENDARI_TEST_DATABASE_URL", "").strip()
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "").strip()
_WORKER_DB_LOCK = 72_610_491


def fixture_agents() -> list[Path]:
    """The folder of every agent under tests/fixtures."""
    return sorted(path.parent for path in (ROOT / "tests" / "fixtures").glob("**/agent.yaml"))


def _worker_postgres(base_url: str, worker: str) -> tuple[str, str | None]:
    """Under pytest-xdist, give each worker (and any nested pytest subprocess) its own Postgres
    database so database-scoped advisory locks, schema catalog operations, and teardown drops
    never contend across workers or parent/child processes."""
    if not base_url or not worker:
        return base_url, None
    db_name = urlsplit(base_url).path.lstrip("/")
    candidate = f"{db_name}_{worker}_{os.getpid()}"
    if not db_name or not re.fullmatch(r"[A-Za-z0-9_]+", candidate):
        return base_url, None
    worker_url = re.sub(r"/[^/?]+(\?|$)", f"/{candidate}\\1", base_url, count=1)
    return worker_url, candidate


POSTGRES_URL, _WORKER_POSTGRES_DB = _worker_postgres(_BASE_POSTGRES_URL, _XDIST_WORKER)
# A developer's shell may hold a deployment's own systems: its database (Neon's integration
# exports these), the AI Gateway's key or a Vercel OIDC token (`vercel env pull`), an LLM
# endpoint and key (and the origins they may be at), the alert webhook, the registry,
# the CDS clients whose tokens a server would take (LP-23b) and a site's code map (LP-23i).
# No test may reach them, whatever a module opens at import (the Vercel entrypoint builds its
# app on DATABASE_URL) or a subprocess inherits: they are gone before anything is collected. A
# test that needs one sets its own.
LIVE_SETTINGS = (
    "DATABASE_URL", "DATABASE_URL_UNPOOLED", "AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "MENDARI_PROVIDERS", "MENDARI_PROVIDER_ALLOWED_ORIGINS",
    "MENDARI_ALERT_WEBHOOK", "MENDARI_ALERT_SECRET", "MENDARI_ALERT_FORMAT",
    "MENDARI_REGISTRY_URL", "MENDARI_ACCOUNTS", "MENDARI_REGISTRY_HUB_URL",
    "MENDARI_REGISTRY_TOKEN", "MENDARI_REGISTRY_ADMIN_TOKEN",
    "CRON_SECRET",
    "MENDARI_CDS_CLIENTS", "MENDARI_SITE_CODES", "MENDARI_EHR_DECLARATIONS",
    "MENDARI_AGENT_URL", "MENDARI_AGENT_TOKEN",
)  # fmt: skip
# And every setting under the project's former name, which stops a server at startup.
for _name in [*LIVE_SETTINGS, *(name for name in os.environ if name.startswith("OPENHEALTH_"))]:
    os.environ.pop(_name, None)
# An input hash key (MENDARI_INPUT_HASH_KEY), which PHI modes hash and encrypt require.
INPUT_HASH_KEY = secrets.token_bytes(32)
HASH_KEY_B64 = base64.b64encode(INPUT_HASH_KEY).decode()
PHI_KEY = secrets.token_bytes(32)  # test keys for PHI mode `encrypt` (`encrypt_mode`)
PHI_KEY_B64 = base64.b64encode(PHI_KEY).decode()
# The edit loop, `uv run pytest -m core` (about 20 s; the full suite takes about 15 min): agent
# loading, decisions, policy, the audit and the statistics. Only files in tests/ itself, and
# never a slow test in one: `-m core` replaces the default marker filter, so a browser or slow
# test marked core would run.
CORE = {
    "test_spec.py", "test_spec_02.py", "test_agent_status.py", "test_input_schema.py",
    "test_patterns.py", "test_templates.py", "test_bounded_caps.py", "test_new_agent.py",
    "test_decision.py", "test_providers.py", "test_policy.py", "test_safety_invariant.py",
    "test_governance.py", "test_hold.py", "test_gate.py", "test_audit.py", "test_evals.py",
    "test_registry_engine.py", "test_rater_agreement.py", "test_stats.py", "test_spec_04.py",
    "test_actions.py", "test_caps_together.py", "test_triage_fixture.py",
    "test_fixture_agents.py",
}  # fmt: skip


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        core = item.path.parent == ROOT / "tests" and item.path.name in CORE
        if core and item.get_closest_marker("slow") is None:
            item.add_marker(pytest.mark.core)


@pytest.fixture(scope="session", autouse=True)
def _the_repositorys_own_agents_are_as_they_were() -> Iterator[None]:
    """No test changes an agent of the repository, the bundled demo agent or a test fixture:
    each works on a copy. One test did once, and every test that read the agent after it read
    another agent. A run that leaves one changed fails here, at its end."""
    agents = sorted(
        path
        for folder in (ROOT / "agents", FIXTURE_AGENTS)
        for path in folder.iterdir()
        if (path / "agent.yaml").is_file()
    )
    before = {path: agent_digest(path) for path in agents}
    yield
    changed = [path.name for path, digest in before.items() if agent_digest(path) != digest]
    assert not changed, f"a test changed the repository's own agents: {', '.join(changed)}"


@pytest.fixture(autouse=True)
def _no_postgres_pool_outlives_its_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every Postgres connection pool a test opens is closed when the test ends, whoever opened
    it: an app's own stores included, which `create_app` keeps for the process's life. An open
    pool held its connections until the collector freed it, so a worker's tests ran the
    database out of connections ("too many clients"), and a pool the collector freed on one of
    its own threads failed to stop. A module or session fixture's pool, made before this
    fixture, is that fixture's to close."""
    from mendari.core.db import PostgresBackend

    opened: list[PostgresBackend] = []
    original = PostgresBackend.__init__

    def tracked(self: PostgresBackend, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(PostgresBackend, "__init__", tracked)
    yield
    for backend in opened:
        backend.close()  # closing a closed pool does nothing


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


@pytest.fixture
def release_agents(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The bundled agents as a release has them, the repository's `agents/`, where the tests
    otherwise stand their fixtures: for what runs the bundled agent itself (`mendari demo`)."""
    monkeypatch.setattr(_reference, "BUNDLED_DIRS", (ROOT / "agents",))
    monkeypatch.setattr(_registry, "_BUNDLED_DIRS", (ROOT / "agents",))
    _reference.reference_agents.cache_clear()
    _reference.reference_digests.cache_clear()
    yield
    _reference.reference_agents.cache_clear()  # read again from the fixtures by the next test
    _reference.reference_digests.cache_clear()


@pytest.fixture(autouse=True)
def _own_hub_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test has its own Hub config and agent cache, never the developer's (the cache is on
    the agent search path)."""
    monkeypatch.setenv("MENDARI_CONFIG_DIR", str(tmp_path / "hub-config"))
    monkeypatch.setenv("MENDARI_CACHE_DIR", str(tmp_path / "hub-cache"))
    monkeypatch.delenv("MENDARI_REGISTRY_URL", raising=False)
    monkeypatch.delenv("MENDARI_REGISTRY_TOKEN", raising=False)
    monkeypatch.delenv("MENDARI_REGISTRY_COUNTERSIGNATURE", raising=False)


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
def _cds_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open mode answers CDS Hooks on this machine only (LP-23c), and Starlette's TestClient is
    never this machine: it connects as `testclient`, with Host `testserver` or the test's base
    URL. So the suite's servers run as the compose sandbox's engine does, as if
    `MENDARI_CDS_SANDBOX=1`: in open mode they answer from anywhere. With sign-in on, or as the
    hosted site, where that flag stops startup, they run as a deployment without it. The flag
    itself is never set, so no server a test starts in a subprocess inherits it, nor one from
    the developer's shell. `tests/test_cds_open_mode.py` takes this away, to test the rule and
    the flag as they are."""
    monkeypatch.delenv("MENDARI_CDS_SANDBOX", raising=False)

    def as_the_flag_would(*, signed_in: bool) -> bool:
        return not signed_in and not os.environ.get("VERCEL")

    monkeypatch.setattr(server_app, "cds_sandbox", as_the_flag_would)


@pytest.fixture(autouse=True)
def _no_provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No agent service is set up, and no provider is chosen, unless a test does it (with a
    fake): a developer's own MENDARI_AGENT_URL would send test cases to their agent, and their
    MENDARI_PROVIDERS would change what every agent runs."""
    for name in ("MENDARI_AGENT_URL", "MENDARI_AGENT_TOKEN", "MENDARI_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)


def shared_benchmark(agent_id: str = "mendari-test/triage") -> dict[str, Any]:
    """The `mendari.site-report/1` a site would share of a shipped agent's benchmark run:
    counted from the benchmark's own ledger (synthetic data), as `mendari eval --share` does."""
    from mendari.core.registry import resolve_agent
    from mendari.evals.ledger import Ledger
    from mendari.evals.share import shareable

    root = resolve_agent(agent_id).root / "evals"
    if not (root / "benchmark.json").is_file():
        # A test fixture's agent commits no benchmark: its benchmark set, evaluated once.
        return copy.deepcopy(_evaluated_benchmark(agent_id))
    ledger_id = json.loads((root / "benchmark.json").read_text())["ledger"]["id"]
    path = root / "ledgers" / (ledger_id.removeprefix("sha256:") + ".jsonl")
    return shareable(Ledger.loads(path.read_bytes()))


@functools.cache
def _evaluated_benchmark(agent_id: str) -> dict[str, Any]:
    """What a site would share of a run of an agent's benchmark set (`benchmark_dataset`) on its
    default providers, as `mendari eval --share` counts it: run once a session, for an agent that
    commits no benchmark (the test fixtures' agents)."""
    from mendari.core.registry import resolve_agent
    from mendari.evals.harness import evaluation, resolve_dataset
    from mendari.evals.share import shareable

    agent = resolve_agent(agent_id)
    dataset = resolve_dataset(agent, agent.manifest.evaluation.benchmark_dataset or "test")
    return shareable(evaluation(Decision(agent), dataset).ledger)


def verified_site(registry: Registry, directory: Path, *, org: str = "north-health") -> Any:
    """A verified synthetic site with one key (LP-22): its files in `directory`."""
    from mendari.hub.site_signing import Site, init_site

    store = registry.operator
    record = store.register_site(org=org, name=f"{org} (synthetic)", by="user:1")
    made = init_site(record["id"], directory=directory / f"site-{record['id']}")
    store.add_site_key(record["id"], made["public_pem"], made["proof"], org=None, by="o")
    store.decide_site(record["id"], verify=True, by="user:reviewer", note="ok", by_org=None)
    return Site(directory / f"site-{record['id']}")


def site_ticket(
    registry: Registry, site: Any, report: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    """A ticket for sharing `report` (a mendari.site-report/1): bound to its agent, its
    configuration, its n and consent, and a commitment the site makes (here to any bytes)."""
    from mendari.hub import site_signing

    fields: dict[str, Any] = {
        "agent": report["agent"],
        "configuration": {slot: p["variant"] for slot, p in report["providers"].items()},
        "remote_consent": report["remote_consent"],
        "commitment": site.commitment(json.dumps(report["dataset"]).encode()),
        "n": report["dataset"]["n"],
    }
    fields.update(changes)
    answer = registry.client.post(
        "/v1/tickets", json={"record": site.sign(site_signing.TICKET_REQUEST, fields)}
    )
    assert answer.status_code == 201, answer.text
    return dict(answer.json())


def share_signed(
    registry: Registry, site: Any, report: dict[str, Any], issued: dict[str, Any] | None = None
) -> Any:
    """Ask for a ticket (unless given one) and send `report` signed as its answer: the
    response of `POST .../reports`."""
    from mendari.hub import site_signing

    issued = issued or site_ticket(registry, site, report)
    record = site.sign(
        site_signing.SUBMISSION, {"ticket": issued["ticket"]["id"], "report": report}
    )
    agent = report["agent"]
    return registry.client.post(
        f"/v1/agents/{agent['id']}/{agent['version']}/reports",
        json=record,
        headers={"authorization": f"Bearer {issued['submit_secret']}"},
    )


# The triage fixture's policy automates only while its screen runs an agent (its
# `automation_requires`), and canned answers never automate on a server with sign-in: a test that
# needs AUTO there runs both slots on `calm_agent`, a live agent service in place of the network.
CALM_URL = "https://agent.test/v1/decide"
CALM_VERSION = "2.0.0"  # the version the calm service reports, as the fixture's canned ones do
# What it answers for the triage fixture's questions: a team, confidently (above the policy's
# 0.90), and nothing urgent or abusive.
CALM_ANSWERS: dict[str, dict[str, Any]] = {
    "destination": {"value": "pharmacy", "confidence": 0.97},
    "urgency": {"value": "no", "confidence": 0.98},
    "abusive": {"value": "no", "confidence": 0.98},
}


def calm_agent(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Point `MENDARI_AGENT_URL` at a live agent service for this test, answered in place of the
    network (`CALM_ANSWERS`, by every `agent` variant's call), and return the triage fixture's
    providers that use it: with sign-in on, where canned answers never automate, a decision on
    them is AUTO."""
    monkeypatch.setenv("MENDARI_AGENT_URL", CALM_URL)
    monkeypatch.delenv("MENDARI_AGENT_TOKEN", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        asked = [question["key"] for question in json.loads(request.content)["questions"]]
        if not set(asked) <= set(CALM_ANSWERS):
            return httpx.Response(400)  # only the triage fixture's questions are answered
        answers = {key: CALM_ANSWERS[key] for key in asked}
        body = {"model": "calm-agent", "service_version": CALM_VERSION, "answers": answers}
        return httpx.Response(200, json=body)

    original = AgentProvider.__init__

    def init(self: AgentProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProvider, "__init__", init)
    return {"screen": "agent", "router": "agent"}


@pytest.fixture(scope="session", autouse=True)
def _postgres_worker_db() -> Iterator[None]:
    """Create this xdist worker's own Postgres database once and drop it when the worker exits."""
    if not _WORKER_POSTGRES_DB:
        yield
        return
    import psycopg

    with psycopg.connect(_BASE_POSTGRES_URL, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_WORKER_DB_LOCK,))
        try:
            connection.execute(f'DROP DATABASE IF EXISTS "{_WORKER_POSTGRES_DB}" WITH (FORCE)')
            connection.execute(f'CREATE DATABASE "{_WORKER_POSTGRES_DB}"')
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_WORKER_DB_LOCK,))
    try:
        yield
    finally:
        with psycopg.connect(_BASE_POSTGRES_URL, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{_WORKER_POSTGRES_DB}" WITH (FORCE)')


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


@dataclass
class Registry:
    client: TestClient
    token: str  # the mendari namespace's owned publisher token
    key: archive.Ed25519PrivateKey
    # The operator's own store on the registry's database, as `mendari registry ...` opens it:
    # since LP-19g every write that is not a publisher's goes through it (or a reviewer's routes
    # under the engine), never over the registry's own HTTP routes.
    operator: RegistryStore
    # The test fixtures' namespace's owned publisher token and its key, once made
    # (`fixture_publisher`).
    fixtures: tuple[str, Any] | None = None

    def publish(
        self,
        agent: Agent | None = None,  # the triage fixture
        *,
        key: Any = None,
        token: str | None = None,
        version: str | None = None,
    ) -> Any:
        agent = agent or resolve_agent("mendari-test/triage")
        namespace, name = agent.id.split("/")
        if namespace == FIXTURE_NAMESPACE:  # a test fixture's agent: its own namespace's
            fixture_token, fixture_key = self.fixture_publisher()
            token, key = token or fixture_token, key or fixture_key
        data = archive.build(agent, key or self.key)
        return self.client.put(
            f"/v1/agents/{namespace}/{name}/{version or agent.version}",
            content=data,
            headers={"authorization": f"Bearer {token or self.token}"},
        )

    def namespace(self, name: str, display_name: str = "") -> str:
        """A namespace made by the operator with no claim (listed, with no organization), and an
        owned token for it."""
        self.operator.create_namespace(name, "community", display_name)
        return self.mint(name)

    def mint(self, namespace: str, *, org: str = "tests", days: int = 90) -> str:
        """An owned publisher token for `namespace`, minted by the operator."""
        minted = self.operator.mint_token(
            namespace, org=org, owner="operator", name="tests", days=days, by="operator"
        )
        return str(minted["token"])

    def fixture_publisher(self) -> tuple[str, Any]:
        """`mendari-test`, the test fixtures' agents' namespace, made on first use as `mendari`
        is: its owned publisher token, and the key registered for it (a key belongs to one
        namespace). So `publish` takes a fixture's agent (`TRIAGE_DIR`, `CLINICAL_DIR`) with
        neither."""
        if self.fixtures is None:
            token = self.namespace(FIXTURE_NAMESPACE, "Mendari tests")
            private, public = archive.generate_key()
            added = self.client.post(
                f"/v1/namespaces/{FIXTURE_NAMESPACE}/keys",
                json={"public_pem": public.decode()},
                headers={"authorization": f"Bearer {token}"},
            )
            assert added.status_code == 201, added.text
            self.fixtures = (token, archive.load_private_key(private))
        return self.fixtures


@pytest.fixture
def registry(db_target: str) -> Iterator[Registry]:
    """A registry (hub/registry.py) with the `mendari` namespace, its owned token and a key."""
    operator = RegistryStore(db_target)
    client = TestClient(create_registry_app(db_target))
    client.__enter__()  # runs the app's lifespan, so its store is closed at the end
    try:
        operator.create_namespace("mendari", "community", "Mendari")
        token = operator.mint_token(
            "mendari", org="mendari", owner="operator", name="tests", days=90, by="operator"
        )["token"]
        private, public = archive.generate_key()
        added = client.post(
            "/v1/namespaces/mendari/keys",
            json={"public_pem": public.decode()},
            headers={"authorization": f"Bearer {token}"},
        )
        assert added.status_code == 201
        yield Registry(client, token, archive.load_private_key(private), operator)
    finally:
        client.__exit__(None, None, None)
        operator.close()


@pytest.fixture(scope="session")
def triage_agent() -> Agent:
    """The triage fixture (`mendari-test/triage`): a synthetic message's team and urgency."""
    return load_agent(TRIAGE_DIR)


@pytest.fixture(scope="session")
def triage(triage_agent: Agent) -> Decision:
    """The triage fixture on its default providers: the keyword rules in both slots, so nothing
    is automated (its policy's `automation_requires`)."""
    return Decision(triage_agent)


@pytest.fixture
def agent_copy(tmp_path: Path) -> Callable[..., Path]:
    """A copy of a test fixture's agent (`agent`: its folder, the triage fixture's by default)
    with tiny datasets (the first 20 cases of each), and `manifest` and `policy` applied to its
    manifest and to the policy in it before it is loaded. Each call makes another copy."""

    def make(
        manifest: Callable[[dict[str, Any]], None] | None = None,
        policy: Callable[[dict[str, Any]], None] | None = None,
        agent: Path = TRIAGE_DIR,
    ) -> Path:
        target, number = tmp_path / agent.name, 1
        while target.exists():
            number += 1
            target = tmp_path / f"{agent.name}-{number}"
        shutil.copytree(agent, target, ignore=shutil.ignore_patterns("data"))
        (target / "data").mkdir()
        for dataset in sorted((agent / "data").glob("*.jsonl")):
            # Tiny stand-ins: most tests do not need the whole datasets.
            cases = dataset.read_text(encoding="utf-8").splitlines()[:20]
            (target / "data" / dataset.name).write_text("\n".join(cases) + "\n", encoding="utf-8")
        if manifest is not None or policy is not None:
            path = target / "agent.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if manifest is not None:
                manifest(data)
            if policy is not None:
                assert isinstance(data["policy"], dict), "the policy is the manifest's own section"
                policy(data["policy"])
            path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
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
    single-threaded work (loading an agent, as core/patterns.py budgets a pattern: by
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


def releasing(decision: Mapping[str, Any] | None) -> dict[str, str]:
    """`release` on each text a decision holds for a person (ADR-0027), as its review sends
    them in `texts`: for a test that resolves a review and is about something else. `decision`
    is the decision as the trail shows it (`AuditStore.get`, `GET /api/audit/{id}`, an item of
    the review queue); one that holds no text gives no verdict."""
    return {text["name"]: "release" for text in (decision or {}).get("outputs", ())}


# What every request of Mendari's own pages carries besides its cookie (server/origin.py): the
# session helpers below send it, as the dashboard would. A test of a request without it (another
# site's link in a browser without Fetch Metadata) sends the cookie alone.
DASHBOARD = {OWN_REQUEST: "1"}


def signed_in(
    store: AuditStore, *, role: str, org: str = "default", host: str = "mendari.test"
) -> dict[str, str]:
    """Request headers for a person signed in with `role` in `org` on `host`: a session row as
    `/auth/callback` makes one (of the issuer the server is given, `MENDARI_OIDC_ISSUER`; the
    chain names them `<role>@<org>.example.org`, a verified email), under the cookie name that
    host uses. Review is a person's (keys are viewer or service)."""
    sid = secrets.token_urlsafe(32)
    email = f"{role}@{org}.example.org"
    claims = {"roles": [role], "email": email, "email_verified": True}
    issuer = os.environ.get("MENDARI_OIDC_ISSUER", "").strip().rstrip("/")
    row = {"iss": issuer, "sub": f"{org}-{role}", "name": f"{org} {role}", "actor": email}
    session, expires_at = sessions.begin(
        {**row, "claims": claims, "org": org}, int(time.time()), sessions.Lifetime()
    )
    store.smart_put(sid, "user", session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SESSION, host)}={sid}", **DASHBOARD}


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
    return {"cookie": f"{sessions.cookie_name(sessions.SESSION, host)}={sid}", **DASHBOARD}


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
        "patient": "chart-us-core-1",
        "encounter": None,
        "fhir_user": "Practitioner/pr-1",
        "expires_at": now + 3600,
        "agent": "mendari-test/chart-summary",
        "identity": {"iss": iss, "sub": sub},
        "actor": f"{iss}|{sub}",
        "identified": True,  # made with sign-in on: the callback's checks ran
    }
    session, expires_at = sessions.begin(row, now, sessions.Lifetime(), ends_by=now + 3600)
    store.smart_put(sid, "session", session, expires_at)
    return {"cookie": f"{sessions.cookie_name(sessions.SMART, host)}={sid}", **DASHBOARD}


DEMO_AGENT_DIR = ROOT / "agents" / "portal-message-agent"
