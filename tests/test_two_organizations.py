# SPDX-License-Identifier: Apache-2.0
"""Two organizations, every route (LP-12g, ADR-0008, threat model I4).

Two organizations on one deployment, each with every kind of principal (people of each role,
API keys, Backend Services clients, an EHR launch), seed data through the API, with a marker in
every free-text field (`zqalpha`, `zqbravo`). Then each calls every mounted route:

- **with the other's live ids** (phase 1): refused as an unknown id is, nothing written;
- **with its own** (phase 2): served, destructive calls last, on spare ids.

Every answer, `Location`/`Content-Disposition`/`ETag` header and NDJSON line is scanned for the
other's markers and ids, after the values the caller sent itself are taken out (a refusal may
echo them) and the fields of the reviewed allow-list below (`FIELDS`), which are not an
organization's data although they may appear. An organization's reads answer the same before
and after the other works again; every event chained during an organization's calls is its own;
no signed-in call reads the store as every organization, nor through a method that takes the
organization as an argument without one. Each call names the route it exercises: a mounted route
that no call exercises and the allow-list (`DEPLOYMENT_WIDE`) does not name fails the test, so a
new route is classified here before it ships. Both account modes, SQLite and Postgres."""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.core import audit as audit_module
from mendari.core import tenancy
from mendari.core.audit import (
    ADMINISTRATIVE_KINDS,
    DECISION_KINDS,
    DEPLOYMENT_KINDS,
    PERSON_KINDS,
)
from mendari.core.pack import Pack, load_pack
from mendari.core.registry import resolve_pack
from mendari.hub import archive
from mendari.hub.client import RegistryClient
from mendari.hub.registry import RegistryStore, create_registry_app
from mendari.providers import reach
from mendari.server import presets, smart_check, smart_routes
from mendari.server.api_docs import FHIR_EXAMPLE
from mendari.server.app import create_app
from mendari.server.auth import person_id
from mendari.server.backend_services import ASSERTION_TYPE
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from mendari.standards.search import encode_cursor
from mendari.standards.terminology import OVERRIDE_REASONS, OVERRIDE_REASONS_SYSTEM
from tests.conftest import (
    DASHBOARD,
    DEMO_AGENT_DIR,
    ROOT,
    TRIAGE_DIR,
    encrypt_mode,
    encrypted_store,
    member,
    message,
    signed_in,
)
from tests.support.fixture_records import SHOWCASE
from tests.support.platform import (
    CHART,
    COMMUNICATION,
    PATIENT,
    SMART_CLIENT,
    SmartEhr,
    smart_routed,
    smart_tenant,
)
from tests.test_backend_services import Signer, assertion, bearer
from tests.test_memberships import token_of

APP = "https://mendari.test"
IDP = "https://idp.example.org"
# The agent each organization decides with: the triage fixture, served as a copy that automates
# on its keyword rules alone (without its `automation_requires`), so that a hold's effect shows
# with sign-in on, where canned answers never automate, and with no agent service set: what
# an organization connects here is the demo agent's service (ADR-0021), never called. A refill
# is automated; a message with chest pain waits for a nurse.
AGENT = "mendari-test/triage-automated"
SERVICE = f"/cds-services/{AGENT.replace('/', '.')}"  # its CDS service
REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"
APPROVABLE = "mendari-test/clinical-review"  # risk level 2: automated under an approval
# An EHR launch decides about its patient's chart, read from the EHR (the chart fixture).
SMART_DECIDE = {"pack_id": CHART, "focus": f"Patient/{SHOWCASE.id}"}
TENANTS = "https://ehrs.example.org"  # one host, two tenants, two organizations
# The agent that asks a service of its own (an `agent` variant), served here beside the others:
# the one whose connection each organization sets (ADR-0021).
PORTAL = "mendari/portal-message-agent"
# The agent the deployment's registry lists (`server/hub_routes.py`): each organization finds
# it there, and in memberships mode adds it to its own store.
LISTED = "listed/portal-agent"
ORGS = ("clinic-zqalpha", "clinic-zqbravo")
PEOPLE = ("admin", "reviewer", "governance", "viewer")
ASKED = ("admin", "reviewer")  # the roles that may decide or review: asked to acknowledge
Route = tuple[str, str]

# -- the reviewed allow-list ----------------------------------------------------------------------

# Routes that hold no organization's data: called once per organization and scanned, unless the
# reason says they are not called (each has its own tests).
DEPLOYMENT_WIDE: dict[Route, str] = {
    ("GET", "/"): "the dashboard page, the same for everyone",
    ("GET", "/docs"): "the API landing page, generated from code and settings",
    ("GET", "/status"): "the status page, static: it reads readiness in the browser",
    ("GET", "/docs/reference"): "the API reference page",
    ("GET", "/redoc"): "the reference's other renderer",
    ("GET", "/favicon.ico"): "an icon",
    ("GET", "/api/health/ready"): "readiness: fixed codes and the version, from a cached read of "
    "the chain's table and the packs start-up found",
    ("GET", "/invite"): "the invitation page, static: the token stays in the fragment",
    ("GET", "/openapi.json"): "generated from code, settings and the served packs",
    ("GET", "/api/agents"): "the served packs, read from files, never the store",
    ("GET", "/api/agents/{namespace}/{name}"): "a served pack, from its files",
    ("GET", "/api/agents/{namespace}/{name}/datasets"): "the pack's and the deployment's "
    "datasets folder, from files (what it may hold in memberships mode is an owner decision)",
    ("GET", "/api/agents/{namespace}/{name}/examples"): "the pack's examples, from its files",
    ("GET", "/cds-services"): "the CDS services, generated from the packs",
    ("GET", "/fhir/metadata"): "conformance; capability_versions is deployment-wide, unchained",
    ("GET", "/fhir/OperationDefinition/{resource_id}"): "conformance",
    ("GET", "/fhir/.well-known/smart-configuration"): "the endpoints' configuration",
    ("GET", "/fhir/CodeSystem"): "terminology built in memory from the packs",
    ("POST", "/fhir/CodeSystem/_search"): "terminology built in memory from the packs",
    ("GET", "/fhir/CodeSystem/{resource_id}"): "terminology built in memory from the packs",
    ("GET", "/fhir/NamingSystem/{resource_id}"): "terminology built in memory",
    ("GET", "/api/auth/keys/presets"): "the preset table and the route rules",
    ("GET", "/api/hub"): "the registry the deployment names, and whether agents can be added "
    "from it: its settings, the same for everyone",
    ("GET", "/api/notice"): "the notice and whether the caller acknowledged it",
    ("POST", "/api/notice/acknowledge"): "a person's own acknowledgement, across organizations",
    ("GET", "/api/smart/session"): "the launch's own session (the browser's cookie)",
    ("GET", "/smart/launch"): "the launch flow: called and scanned in each organization's seed",
    ("GET", "/smart/callback"): "the launch flow: called and scanned in each organization's seed",
    ("GET", "/api/internal/retention"): "the operator's cron (CRON_SECRET), across every "
    "organization. Not called: tests/test_hosted_data.py",
    ("GET", "/api/internal/monitor"): "the operator's cron (CRON_SECRET): each organization on "
    "its own, one alert each, counts only. Not called: tests/test_monitor_cron.py",
    ("GET", "/api/internal/checkpoint"): "the operator's cron (CRON_SECRET): the whole chain, "
    "the deployment's. Not called: tests/test_signed_checkpoint.py",
    ("GET", "/api/audit/checkpoint"): "the signed checkpoint, the deployment's: a hash and a "
    "day, no organization's data. Not called: tests/test_signed_checkpoint.py",
    ("POST", "/api/internal/backup"): "the operator's schedule (CRON_SECRET) reporting a backup "
    "of the whole database: when it ran and whether it worked. Not called: "
    "tests/test_system_routes.py",
    ("GET", "/auth/login"): "sign-in: a redirect. Not called: tests/test_auth.py",
    ("GET", "/auth/callback"): "sign-in: a redirect and a cookie. Not called: tests/test_auth.py",
    ("POST", "/auth/logout"): "signing out. Not called: tests/test_auth.py",
    ("POST", "/api/smart/logout"): "ending a launch. Not called: tests/test_smart.py",
}
NOT_CALLED = {route for route, why in DEPLOYMENT_WIDE.items() if "Not called" in why}
# What the server still mounts of features wave 6 removes, until their code goes: Da Vinci's DTR
# package (G4), the guard (G12) and Jev's model list (G1). No call here exercises them; they
# leave the coverage check (`covered`) with their code.
REMOVED_ROUTES: set[Route] = {
    ("POST", "/fhir/Questionnaire/$questionnaire-package"),
    ("POST", "/api/agents/{namespace}/{name}/guard"),
    ("GET", "/api/providers/jev/models"),
}
# What may appear in an organization's answers although it is not that organization's data:
FIELDS = {
    "prev_hash": "an event's place in the deployment's one chain: the hash of the event before, "
    "often another organization's, never its content (owner decision O1, threat model I4)",
    "verify": "claims mode only: the whole chain's verification (head, events) is unchanged "
    "there; in memberships mode it is the operator's (404)",
    "calls_today": "claims mode only: health's count of the deployment's remote calls today "
    "(an LLM's), which every organization's decisions move; the operator's in memberships mode",
}
# The routes memberships mode adds; claims mode mounts none of them.
MEMBERSHIPS_ONLY: set[Route] = {
    ("GET", "/invite"),
    ("GET", "/api/auth/members"),
    ("PUT", "/api/auth/members/{membership_id}/roles"),
    ("DELETE", "/api/auth/members/{membership_id}"),
    ("GET", "/api/auth/invitations"),
    ("POST", "/api/auth/invitations"),
    ("DELETE", "/api/auth/invitations/{invitation_id}"),
    ("POST", "/api/auth/invitations/accept"),
    ("GET", "/api/auth/access-requests"),
    ("POST", "/api/auth/access-requests"),
    ("POST", "/api/auth/access-requests/{request_id}"),
    ("POST", "/api/auth/organization"),
    # A grader's routes over written output and the rounds' reports (ADR-0025): mounted under
    # PHI mode `encrypt`, which this world runs.
    ("GET", "/api/written/labelling"),
    ("GET", "/api/written/labelling/next"),
    ("POST", "/api/written/labelling/{assignment_id}"),
    ("GET", "/api/written/rounds"),
    # An organization's pack store (LP-20f): MENDARI_AGENT_STORE=on, which this world sets in
    # memberships mode.
    ("GET", "/api/store/agents"),
    ("GET", "/api/store/agents/{namespace}/{name}"),
    ("GET", "/api/store/versions/{digest}"),
    ("GET", "/api/store/versions/{digest}/file"),
    ("POST", "/api/store/versions"),
    ("POST", "/api/store/uploads"),
    ("PUT", "/api/store/uploads/{upload_id}/parts/{n}"),
    ("POST", "/api/store/uploads/{upload_id}/complete"),
    ("POST", "/api/store/copies"),
    ("POST", "/api/store/versions/{digest}/edits"),
    ("POST", "/api/store/versions/{digest}/tests"),
    ("DELETE", "/api/store/versions/{digest}"),
    ("GET", "/api/store/keys"),
    ("POST", "/api/store/keys"),
    ("DELETE", "/api/store/keys/{key_id}"),
    ("GET", "/api/store/datasets"),
    ("POST", "/api/store/datasets"),
    ("GET", "/api/store/datasets/{dataset_id}"),
    ("GET", "/api/store/jobs"),
    ("POST", "/api/store/jobs"),
    ("GET", "/api/store/jobs/{job_id}"),
    ("POST", "/api/store/jobs/{job_id}/advance"),
    ("GET", "/api/store/comparisons"),
    ("GET", "/api/store/promotions"),
    ("POST", "/api/store/promotions"),
    ("POST", "/api/store/promotions/{promotion_id}/retire"),
    ("POST", "/api/store/agents/{namespace}/{name}/improvement"),
    ("GET", "/api/store/versions/{digest}/proposed-tests"),
    ("POST", "/api/store/versions/{digest}/proposed-tests/{dataset_id}/{case_id}"),
    # A version added from the deployment's registry (`server/hub_routes.py`): with the store.
    ("POST", "/api/store/pulls"),
}
# The tables an organization's work writes, compared before and after each refused call.
WRITTEN = (
    "decisions",
    "decision_inputs",
    "notes",
    "feedback",
    "governance_approvals",
    "automation_holds",
    "eval_reports",
    "api_keys",
    "oauth_clients",
    "organizations",
    "memberships",
    "invitations",
    "access_requests",
    "agent_connections",  # an agent's connection, set in the console (ADR-0021)
    "written_rounds",  # grading rounds of written output (ADR-0025): the operator's command's
    "written_items",
    "written_assignments",  # a grader's grades
    "audit_events",
)


# -- the two organizations ------------------------------------------------------------------------


@dataclass
class Org:
    name: str
    signer: Signer
    ehr: SmartEhr
    people: dict[str, TestClient] = field(default_factory=dict)
    keys: dict[str, TestClient] = field(default_factory=dict)  # viewer, service
    tokens: dict[str, TestClient] = field(default_factory=dict)  # Backend Services clients
    launch: TestClient | None = None
    launch_id: str = ""
    labeller: TestClient | None = None  # memberships mode: a grader of written output
    ids: dict[str, list[str]] = field(default_factory=dict)  # kind of id: ids, in order made

    @property
    def marker(self) -> str:
        return self.name.removeprefix("clinic-")

    def made(self, kind: str, value: Any) -> str:
        self.ids.setdefault(kind, []).append(str(value))
        return str(value)

    def one(self, kind: str, index: int = 0) -> str:
        return self.ids[kind][index]


@dataclass
class Exchange:
    """One call: who made it, what it sent, and what came back."""

    org: str
    route: Route
    sent: list[str]
    status: int
    text: str


@dataclass
class World:
    app: Any
    store: AuditStore
    accounts: str
    orgs: dict[str, Org]
    exchanges: list[Exchange] = field(default_factory=list)
    exercised: set[Route] = field(default_factory=set)
    chained: list[tuple[str, Route, int, int]] = field(default_factory=list)  # (org, route, after)
    signed_in: bool = False  # a credentialed call is under way (the probes read it)
    probed: list[str] = field(default_factory=list)
    # From phase 2 on, every call to /api/ names the other organization in its query and body
    # (`org`): no route may honour it (critique item 10).
    pinned: bool = False

    def head(self) -> int:
        """The chain's last `seq` (0: empty)."""
        with self.store._db.session(exclusive=False) as db:
            row = db.one("SELECT MAX(seq) AS seq FROM audit_events")
        return int(row["seq"] or 0) if row else 0

    def other(self, org: Org) -> Org:
        return self.orgs[ORGS[1] if org.name == ORGS[0] else ORGS[0]]

    def call(
        self,
        org: Org,
        who: TestClient,
        route: Route,
        url: str,
        *,
        expect: int | tuple[int, ...],
        sent: tuple[Any, ...] = (),
        **options: Any,
    ) -> Any:
        """`who` calls `url` (the route `route` exercises) for `org`; the answer is recorded for
        the scan, with every value the caller sent (`sent`, the URL, query, form and body)."""
        start = self.head()
        sending = dict(options)
        if self.pinned and url.startswith("/api/") and not url.startswith("/api/agents/"):
            name = self.other(org).name  # never in `sent`: an answer echoing it is a leak
            sending["params"] = {"org": name, **(options.get("params") or {})}
            if isinstance(options.get("json"), dict):
                sending["json"] = {"org": name, **options["json"]}
        self.signed_in = True
        try:
            answer = who.request(route[0], url, follow_redirects=False, **sending)
        finally:
            self.signed_in = False
        self.chained.append((org.name, route, start, self.head()))
        expected = (expect,) if isinstance(expect, int) else expect
        assert answer.status_code in expected, (route, url, answer.status_code, answer.text[:500])
        values = [url, *(str(v) for v in sent), *leaves(options.get("params"))]
        values += [*leaves(options.get("json")), *leaves(options.get("data"))]
        shown = [answer.headers.get(h, "") for h in ("location", "content-disposition", "etag")]
        self.exchanges.append(
            Exchange(org.name, route, values, answer.status_code, answer.text + " ".join(shown))
        )
        self.exercised.add(route)
        return answer


def leaves(value: Any) -> Iterator[str]:
    """Every string and number in a sent value, nested or not."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from leaves(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from leaves(item)
    elif value is not None:
        yield str(value)


def client_of(app: Any, headers: Mapping[str, str] | None = None) -> TestClient:
    """A browser or a machine of its own (its own cookies), with `headers` on every request."""
    client = TestClient(app, base_url=APP)
    client.headers.update(headers or {})
    return client


# -- the probes: no signed-in call reads as every organization ------------------------------------

# Store methods that take the organization as an argument (None: every organization; `everyone`:
# the deployment's holds too), and the deployment-wide reads; each with the modes and the reason
# it may run during a signed-in call without an organization.
EXPLICIT = (
    "access_requests",
    "hold_automation",
    "api_keys",
    "clients",
    "lift_hold",
    "rotate_api_key",
    "revoke_api_key",
    "revoke_client",
    "replace_client_keys",
    "subject_history",
    "administrative_events",
    "iter_administrative_events",
    "usage_ledger",  # the usage ledger (LP-21g): an organization's rows, or the operator's all
)
DEPLOYMENT_READS = {
    "verify": ("claims", "the whole chain's verification: unchanged in claims mode"),
    "usage": ("claims", "today's calls in health: unchanged in claims mode"),
    "deployments": ("claims", "the deployment records: unchanged in claims mode"),
    "configuration_started": (
        "claims memberships",
        "monitoring: when this build and these providers started, a time that filters the "
        "organization's own decisions (shown in claims mode only), never a record",
    ),
    "last_purge": ("claims memberships", "the last purge in health (its counts claims-only)"),
}


def owner_of(name: str) -> tuple[type, str]:
    """The class a probed method is on, and its name."""
    return AuditStore, name


@contextmanager
def probing(world: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """During every credentialed call: no `tenancy.current()` answers None (every organization),
    no store method is given None (or `everyone=True`) for its organization, and the
    deployment-wide reads run only where the allow-list says."""
    real_current = tenancy.current

    def current() -> str | None:
        found = real_current()
        if world.signed_in and found is None:
            world.probed.append("tenancy.current() is None")
        return found

    monkeypatch.setattr(tenancy, "current", current)

    def explicit(name: str) -> Callable[..., Any]:
        method = getattr(*owner_of(name))
        signature = inspect.signature(method)

        @functools.wraps(method)
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            if world.signed_in:
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                if bound.arguments.get("org") is None:
                    world.probed.append(f"{name} for every organization")
            return method(self, *args, **kwargs)

        return wrapped

    for name in EXPLICIT:
        monkeypatch.setattr(*owner_of(name), explicit(name))
    real_holds = AuditStore.holds

    def holds(self: AuditStore, org: str | None = None, *, everyone: bool = False) -> Any:
        if world.signed_in and (everyone or org is None):
            world.probed.append("holds for every organization")
        return real_holds(self, org, everyone=everyone)

    monkeypatch.setattr(AuditStore, "holds", holds)
    real_memberships = AuditStore.memberships

    def memberships(self: AuditStore, *, org: str | None = None, user_id: str | None = None) -> Any:
        if world.signed_in and org is None and user_id is None:
            world.probed.append("memberships for every organization")
        return real_memberships(self, org=org, user_id=user_id)

    monkeypatch.setattr(AuditStore, "memberships", memberships)

    def deployment(name: str) -> Callable[..., Any]:
        method = getattr(*owner_of(name))

        @functools.wraps(method)
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            modes, _ = DEPLOYMENT_READS[name]
            if world.signed_in and world.accounts not in modes.split():
                world.probed.append(f"{name}, deployment-wide, in {world.accounts} mode")
            return method(self, *args, **kwargs)

        return wrapped

    for name in DEPLOYMENT_READS:
        monkeypatch.setattr(*owner_of(name), deployment(name))
    yield


# -- the deployment and each organization's seed --------------------------------------------------


def issuer_of(org: str) -> str:
    return f"{TENANTS}/tenant-{org.removeprefix('clinic-')}/fhir"


def no_call() -> Any:
    raise AssertionError("no connection an organization set is called from this test")


def automated(base: Path) -> Path:
    """`base` holding the triage fixture as `AGENT`: a copy without its policy's
    `automation_requires`, so its keyword rules alone automate a confident team."""
    root = base / "triage-automated"
    shutil.copytree(TRIAGE_DIR, root)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["id"] = AGENT
    del manifest["policy"]["automation_requires"]
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return base


# -- a registry served over HTTP, to find agents in and add them from ---------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Hub:
    """A registry served on this machine (`hub`): its address and its database, where the
    operator's commands act."""

    def __init__(self, url: str, db: Path) -> None:
        self.url = url
        self.db = db
        self.clients: list[RegistryClient] = []

    def client(self, token: str | None = None) -> RegistryClient:
        """A client closed when the test ends."""
        opened = RegistryClient(self.url, token)
        self.clients.append(opened)
        return opened

    def namespace(self, name: str) -> tuple[str, archive.Ed25519PrivateKey]:
        """A namespace the operator made (no claim: listed), its owned token and a key."""
        with RegistryStore(self.db) as store:
            store.create_namespace(name, "community", "")
            token = store.mint_token(
                name, org=name, owner="operator", name="tests", days=90, by="operator"
            )["token"]
        private, public = archive.generate_key()
        self.client(token).add_key(name, public)
        return token, archive.load_private_key(private)


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[Hub]:
    port = free_port()
    db = tmp_path / "registry.db"
    app = create_registry_app(db)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with httpx.Client() as probe:
                probe.get(f"{url}/v1/health").raise_for_status()
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    registry = Hub(url, db)
    yield registry
    for opened in registry.clients:
        opened.close()
    server.should_exit = True
    thread.join(timeout=5)


def as_published(base: Path, pack_id: str) -> Pack:
    """The bundled demo agent under a publisher's own id, as a publisher puts one in a
    registry."""
    target = base / "published" / pack_id.replace("/", "-")
    shutil.copytree(DEMO_AGENT_DIR, target)
    manifest = yaml.safe_load((target / "agent.yaml").read_text())
    manifest["id"] = pack_id
    (target / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return load_pack(target)


@pytest.fixture(params=["claims", "memberships"])
def world(
    request: pytest.FixtureRequest,
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    hub: Hub,
) -> Iterator[World]:
    accounts = request.param
    # The agent whose connection each organization sets (ADR-0021), served beside the others;
    # what a connection leads to is never called here. And the agent each decides with.
    served = (DEMO_AGENT_DIR.parent, automated(tmp_path / "agents"))
    monkeypatch.setenv("MENDARI_AGENTS_PATH", os.pathsep.join(str(path) for path in served))
    monkeypatch.setattr(reach, "public_client", no_call)
    # A registry to find agents in and add them from (`server/hub_routes.py`), listing one.
    monkeypatch.setenv("MENDARI_REGISTRY_URL", hub.url)
    publisher, signing_key = hub.namespace(LISTED.split("/")[0])
    hub.client(publisher).push(as_published(tmp_path, LISTED), signing_key)
    encrypt_mode(monkeypatch)  # inputs and notes sealed, read by a reader and chained
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    monkeypatch.setenv("MENDARI_ACCOUNTS", accounts)
    if accounts == "memberships":  # an organization's pack store (LP-20f): memberships only
        monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    # Each organization's quota, and each principal's cap, counted (health shows them).
    monkeypatch.setenv("MENDARI_ORG_DAILY_LIMITS", "decisions=100000")
    monkeypatch.setenv("MENDARI_PRINCIPAL_DAILY_LIMIT", "100000")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", SMART_CLIENT)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", TENANTS)
    monkeypatch.setenv(
        "MENDARI_SMART_EHRS", ",".join(f"{issuer_of(org)}={org}:decide" for org in ORGS)
    )
    ehrs = {org: smart_tenant(issuer_of(org)) for org in ORGS}
    for name, ehr in ehrs.items():  # alike (the same ids), but its own tokens and patient's name
        marker = name.removeprefix("clinic-")
        ehr.token_prefix = f"access-{marker}"
        ehr.resources = [
            {**r, "name": [{"family": f"Synthetic {marker}", "given": ["Pat"]}]}
            if r["resourceType"] == "Patient"
            else r
            for r in ehr.resources
        ]
    transport = smart_routed(*ehrs.values())
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    # The System view's EHR check asks an EHR's own servers: here it answers from nothing but
    # the base it was given, so no test reaches a network.
    monkeypatch.setattr(
        smart_check, "check", lambda base: [smart_check.Finding("ok", "registration key", base)]
    )
    from cryptography.hazmat.primitives.asymmetric import rsa

    orgs = {
        org: Org(
            org,
            Signer(
                "RS384", f"{org}-1", rsa.generate_private_key(public_exponent=65537, key_size=2048)
            ),
            ehrs[org],
        )
        for org in ORGS
    }
    # Each hold set here clears the server's look-up of holds at once.
    app = create_app(db_path=db_target)
    with AuditStore(db_target) as store:
        store.purge(30, source="command")  # a last purge, with its counts, for health
        world = World(app, store, accounts, orgs)
        with probing(world, monkeypatch):
            for org in orgs.values():
                seed(world, org)
                if world.accounts == "memberships":
                    labellers(world, org)
                    written_round(world, org, db_target)
            yield world


def person(world: World, org: Org, role: str) -> TestClient:
    """`org`'s signed-in person with `role`, the notice acknowledged."""
    sub = f"{role}-{org.marker}"
    if world.accounts == "memberships":
        headers = member(
            world.store,
            org=org.name,
            roles=[role],
            sub=sub,
            email=f"{role}@{org.marker}.example.org",
        )
        org.made("person", person_id(IDP, sub))
    else:
        headers = signed_in(world.store, role=role, org=org.name)
    client = client_of(world.app, headers)
    if role in ASKED:  # who may decide or review acknowledges the notice first (notices.py)
        world.call(
            org,
            client,
            ("POST", "/api/notice/acknowledge"),
            "/api/notice/acknowledge",
            json={},
            expect=200,
        )
    return client


def seed(world: World, org: Org) -> None:
    """Every kind of `org`'s data, made through the API as its principals, every free text
    carrying its marker; each id recorded."""
    m = org.marker
    for role in PEOPLE:
        org.people[role] = person(world, org, role)
    admin, reviewer, governance = (org.people[r] for r in ("admin", "reviewer", "governance"))
    # API keys: a viewer and a service key, and spares the destructive calls use.
    for name, role in (
        ("viewer", "viewer"),
        ("service", "service"),
        ("rotate", "service"),
        ("revoke", "service"),
    ):
        made = world.call(
            org,
            admin,
            ("POST", "/api/auth/keys"),
            "/api/auth/keys",
            json={"name": f"{name} key {m}", "role": role},
            expect=201,
        ).json()
        org.made("key", made["id"])
        if name in ("viewer", "service"):
            org.keys[name] = client_of(world.app, bearer(made["key"]))
    # Backend Services clients, signed with this organization's own key, and a spare.
    for name, role in (("viewer", "viewer"), ("service", "service"), ("spare", "viewer")):
        body = {"name": f"{name} client {m}", "role": role, "jwks": {"keys": [org.signer.jwk]}}
        made = world.call(
            org, admin, ("POST", "/api/auth/clients"), "/api/auth/clients", json=body, expect=201
        ).json()
        client_id = org.made("client", made["client_id"])
        if name != "spare":
            answer = world.call(
                org,
                client_of(world.app),
                ("POST", "/auth/token"),
                "/auth/token",
                data=token_form(org.signer, client_id, made["scope"]),
                expect=200,
            )
            org.tokens[name] = client_of(world.app, bearer(answer.json()["access_token"]))
    # An EHR launch, from this organization's own EHR tenant: the dashboard window it opens.
    org.launch = client_of(world.app, DASHBOARD)
    started = world.call(
        org,
        org.launch,
        ("GET", "/smart/launch"),
        "/smart/launch",
        params={"iss": org.ehr.iss, "launch": "xyz", "pack": CHART},
        expect=302,
    )
    location = started.headers["location"]
    world.call(
        org,
        org.launch,
        ("GET", "/smart/callback"),
        "/smart/callback",
        params={
            "code": org.ehr.authorize(location),
            "state": parse_qs(urlsplit(location).query)["state"][0],
        },
        expect=302,
    )
    session = org.launch.get("/api/smart/session").json()
    org.launch_id = str(session["launch_id"])
    # Decisions: pending (a review's), routine (an outcome's), spare (an erasure's); FHIR, CDS
    # Hooks (its card's feedback names it) and the launch.
    service = org.keys["service"]
    for kind, words in (
        ("pending", f"Crushing chest pain, left arm numb {m}"),
        ("routine", f"Can I get a refill of my lisinopril {m}?"),
        ("spare", f"Please call me back about my results {m}"),
    ):
        made = world.call(
            org,
            service,
            ("POST", "/api/agents/{namespace}/{name}/decide"),
            f"/api/agents/{AGENT}/decide",
            json=message(words),
            expect=200,
        ).json()
        org.made("decision", made["id"])
        org.made(kind, made["id"])
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": AGENT},
            {"name": "data", "resource": FHIR_EXAMPLE},
        ],
    }
    bundle = world.call(
        org, service, ("POST", "/fhir/$decide"), "/fhir/$decide", json=parameters, expect=200
    ).json()
    org.made("decision", bundle["entry"][0]["resource"]["identifier"][0]["value"])
    packed = world.call(
        org,
        org.people["reviewer"],
        ("POST", "/api/agents/{namespace}/{name}/fhir/decide"),
        f"/api/agents/{AGENT}/fhir/decide",
        json=FHIR_EXAMPLE,
        expect=200,
    ).json()
    org.made("decision", packed["decision"]["id"])
    hook = world.call(
        org,
        service,
        ("POST", "/cds-services/{service_id}"),
        SERVICE,
        json=hook_request(f"I have had chest pain since last night {m}"),
        expect=200,
    ).json()
    org.made("decision", org.made("hooked", hook["cards"][0]["extension"]["mendari.decision_id"]))
    assert org.launch is not None
    smart = world.call(
        org,
        org.launch,
        ("POST", "/api/smart/decide"),
        "/api/smart/decide",
        json=SMART_DECIDE,
        headers={"x-mendari-launch": org.launch_id},
        expect=200,
    ).json()
    org.made("decision", smart["decision"]["id"])
    # A review, an outcome and a rating, with notes.
    world.call(
        org,
        reviewer,
        ("POST", "/api/review/{decision_id}"),
        f"/api/review/{org.one('pending')}",
        json={"final_label": "nurse", "reviewer": "x", "note": f"review note {m}"},
        expect=200,
    )
    rated = world.call(
        org,
        reviewer,
        ("POST", "/api/audit/{decision_id}/feedback"),
        f"/api/audit/{org.one('routine')}/feedback",
        json={"rating": "down", "note": f"rating note {m}"},
        expect=200,
    ).json()
    org.made("feedback", rated["id"])
    # Approvals and holds (a spare of each for the destructive calls); an evaluation.
    for _ in range(2):
        approval = world.call(
            org,
            governance,
            ("POST", "/api/governance"),
            "/api/governance",
            json={"pack_id": APPROVABLE, "reference": f"minutes {m}", "days": 30},
            expect=201,
        ).json()
        org.made("approval", approval["id"])
        hold = world.call(
            org,
            governance,
            ("POST", "/api/holds"),
            "/api/holds",
            json={"pack_id": APPROVABLE, "reason": f"hold {m}"},
            expect=201,
        ).json()
        org.made("hold", hold["id"])
        # A pulled pack's approval (LP-18d): none is served here, so made in the store.
        with tenancy.acting_for(org.name):
            pulled = world.store.approve_registry_pack(
                pack_id="someone/pulled",
                pack_digest=f"sha256:{len(org.ids.get('registry_approval', []))}{'0' * 63}",
                registry="https://hub.example.org",
                approver="cmo",
                reference=f"pulled minutes {m}",
            )
        org.made("registry_approval", pulled["id"])
    # Only a pack served from a registry is approved this way.
    world.call(
        org,
        governance,
        ("POST", "/api/registry-approvals"),
        "/api/registry-approvals",
        json={"pack_id": APPROVABLE, "pack_digest": "sha256:" + "0" * 64, "reference": "minutes"},
        expect=422,
    )
    run = world.call(
        org,
        admin,
        ("POST", "/api/agents/{namespace}/{name}/evals"),
        f"/api/agents/{AGENT}/evals",
        json={"dataset": "test", "limit": 2},
        expect=200,
    ).json()
    org.made("run", run["run"]["id"])
    if world.accounts == "memberships":
        seed_people(world, org)


def seed_people(world: World, org: Org) -> None:
    """Memberships mode: invitations (one to accept, one to revoke), a member to change and one to
    remove (each joined by an invitation), a request to join to handle."""
    m, admin = org.marker, org.people["admin"]
    for name in ("joiner", "spare", "changed", "removed"):
        made = world.call(
            org,
            admin,
            ("POST", "/api/auth/invitations"),
            "/api/auth/invitations",
            json={"email": f"{name}@{m}.example.org", "roles": ["viewer"]},
            expect=201,
        ).json()
        org.made("invitation", made["id"])
        org.made(f"token-{name}", token_of(made))
    for name in ("changed", "removed"):
        joiner = client_of(
            world.app,
            member(world.store, org=None, sub=f"{name}-{m}", email=f"{name}@{m}.example.org"),
        )
        joined = world.call(
            org,
            joiner,
            ("POST", "/api/auth/invitations/accept"),
            "/api/auth/invitations/accept",
            json={"token": org.one(f"token-{name}")},
            expect=200,
        ).json()
        org.made("person", person_id(IDP, f"{name}-{m}"))
        assert joined["org"] == org.name
    for found in world.call(
        org, admin, ("GET", "/api/auth/members"), "/api/auth/members", expect=200
    ).json():
        org.made("membership", found["id"])
        if found.get("email", "").startswith(("changed@", "removed@")):
            org.made(found["email"].split("@")[0], found["id"])
    asker = client_of(
        world.app, member(world.store, org=None, sub=f"asker-{m}", email=f"asker@{m}.example.org")
    )
    org.made("person", person_id(IDP, f"asker-{m}"))
    world.call(
        org,
        asker,
        ("POST", "/api/auth/access-requests"),
        "/api/auth/access-requests",
        json={"org": org.name},
        expect=202,
    )
    for found in world.call(
        org, admin, ("GET", "/api/auth/access-requests"), "/api/auth/access-requests", expect=200
    ).json():
        org.made("request", found["id"])


def labellers(world: World, org: Org) -> None:
    """Memberships mode: two of `org`'s labellers, who grade its written output (`written_round`);
    the second is `org.labeller`, signed in. Their user ids are `org`'s."""
    for n in range(2):
        sub = f"labeller-{n}-{org.marker}"
        member(world.store, org=org.name, roles=["labeller"], sub=sub)
        org.made("labeller", person_id(IDP, sub))
        org.made("person", person_id(IDP, sub))
    org.labeller = client_of(
        world.app, member(world.store, org=org.name, sub=f"labeller-1-{org.marker}")
    )


def hook_request(words: str) -> dict[str, Any]:
    """The triage fixture's hook, a message saying `words` prefetched with its patient."""
    communication = {**COMMUNICATION, "payload": [{"contentString": words}]}
    return {
        "hook": "mendari-message-received",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {"communicationId": "msg-1", "patientId": "pat-1"},
        "prefetch": {"communication": communication, "patient": PATIENT},
    }


def card_feedback(*items: dict[str, Any]) -> dict[str, Any]:
    """A CDS Hooks feedback request (LP-23f)."""
    return {"feedback": list(items)}


def overridden(card: str, code: str) -> dict[str, Any]:
    """One card's feedback: overridden, with one of the reasons the card offered (`code`)."""
    reason = {"system": OVERRIDE_REASONS_SYSTEM, "code": code}
    reason["display"] = OVERRIDE_REASONS.get(code, ("x",))[0]
    return {
        "card": card,
        "outcome": "overridden",
        "outcomeTimestamp": "2026-10-18T12:00:00-04:00",
        "overrideReason": {"reason": reason},
    }


def written_round(world: World, org: Org, db_target: str) -> None:
    """A grading round of written output of `org`'s (ADR-0025), as the operator's command makes
    one: two texts the fixture agent wrote for messages carrying `org`'s marker, each given to
    its first two labellers. The round's id, each item's id and each assignment's are `org`'s,
    which the other never sees. Left open, for phase 2's grader to grade through the routes."""
    from mendari import Decision, load_pack
    from mendari.core.written import WrittenStore
    from mendari.evals import written
    from mendari.evals.harness import load_dataset
    from mendari.providers.judge import Judge

    m = org.marker
    pack = load_pack(ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini")
    lines = [
        {"id": f"written-{m}-{n}", "input": {"message": {"body": f"Where do I park {m} {n}?"}}}
        for n in range(2)
    ]
    raw = "".join(json.dumps(line) + "\n" for line in lines).encode()
    run = written.run(
        Decision(pack, origin_cap=False),
        load_dataset("written.jsonl", name="written", raw=raw),
        "reply_quality",
        judge=Judge(pack, "reply_quality", mode="canned"),
    )
    # The operator's command opens the database under the deployment's PHI mode and keys.
    with encrypted_store(db_target) as sealing:
        rounds = WrittenStore(sealing)
        made = rounds.create_round(
            org.name, pack, "reply_quality", run, sample=2, created_by="operator"
        )
        round_id = org.made("written round", made["round"])
        for item in run.items:
            org.made("written item", item.item)
        rounds.assign(round_id, org.ids["labeller"][:2], double=1.0)
    with world.store._db.session(exclusive=False) as db:
        for row in db.all(
            "SELECT id FROM written_assignments WHERE round_id = ? ORDER BY id", (round_id,)
        ):
            org.made("written assignment", row["id"])


def served_written(world: World, x: Org) -> None:
    """Memberships mode: `x`'s grader reads their own counts, is shown their own next text and
    grades it (ADR-0025); and a reader of `x` lists its rounds, never the other's."""
    assert x.labeller is not None
    counts = ("GET", "/api/written/labelling")
    nxt = ("GET", "/api/written/labelling/next")
    grade = ("POST", "/api/written/labelling/{assignment_id}")
    counted = world.call(x, x.labeller, counts, counts[1], expect=200).json()
    assert [found["set"] for found in counted["sets"]] == [x.one("written round")]
    shown = world.call(x, x.labeller, nxt, nxt[1], expect=200).json()
    assert shown["assignment"] in x.ids["written assignment"]
    assert x.marker in json.dumps(shown["input"])
    body = {
        "labels": {"safe_to_send": "yes", "addresses_request": "fully", "plain_language": "yes"}
    }
    url = f"/api/written/labelling/{shown['assignment']}"
    told = world.call(x, x.labeller, grade, url, json=body, expect=200)
    assert told.json() == {"remaining": 1}
    listed = world.call(
        x, x.people["viewer"], ("GET", "/api/written/rounds"), "/api/written/rounds", expect=200
    ).json()
    assert [found["id"] for found in listed] == [x.one("written round")]


def token_form(signer: Signer, client_id: str, scope: str = "system/*.rs") -> dict[str, str]:
    """The form `POST /auth/token` takes: a client assertion signed by `signer`, for `scope`."""
    return {
        "grant_type": "client_credentials",
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": assertion(signer, client_id),
        "scope": scope,
    }


# -- phase 1: the other's live ids ----------------------------------------------------------------


def written(store: AuditStore) -> str:
    """Every row of every table an organization's work writes, as text."""
    with store._db.session(exclusive=False) as db:
        return json.dumps(
            {table: db.all(f"SELECT * FROM {table} ORDER BY 1") for table in WRITTEN}, default=str
        )


def first_seq(store: AuditStore, chain_id: str) -> int:
    with store._db.session(exclusive=False) as db:
        row = db.one(
            "SELECT seq FROM audit_events WHERE decision_id = ? ORDER BY seq LIMIT 1", (chain_id,)
        )
    assert row is not None, chain_id
    return int(row["seq"])


def unknown_like(value: str) -> str:
    """An id of the same shape that names nothing."""
    return re.sub(r"[0-9a-f]", "0", value)


def refusals(world: World, x: Org, o: Org) -> None:
    """Phase 1: `x`'s principals call with `o`'s live ids. Each is refused as an id that names
    nothing is (the same status, and the same body once the id itself is taken out), and writes
    nothing."""

    def refused(
        who: TestClient, route: Route, url: str, theirs: str, expect: int, **options: Any
    ) -> Any:
        writes = route[0] != "GET"
        before = written(world.store) if writes else ""
        answer = world.call(x, who, route, url, expect=expect, sent=(theirs,), **options)
        assert not writes or written(world.store) == before, (route, url)
        nothing = unknown_like(theirs)
        twin = world.call(
            x,
            who,
            route,
            url.replace(theirs, quote(nothing, safe="")),
            expect=expect,
            sent=(nothing,),
            **swapped(options, theirs, nothing),
        )
        assert same(answer.text, theirs) == same(twin.text, nothing), (route, url)
        return answer

    admin, reviewer, viewer = x.people["admin"], x.people["reviewer"], x.people["viewer"]
    service = x.keys["service"]
    for decision in o.ids["decision"]:
        for who in (admin, viewer, x.keys["viewer"]):  # a client token reads FHIR only
            refused(
                who, ("GET", "/api/audit/{decision_id}"), f"/api/audit/{decision}", decision, 404
            )
        refused(
            viewer,
            ("GET", "/api/audit/{decision_id}/fhir"),
            f"/api/audit/{decision}/fhir",
            decision,
            404,
        )
        refused(
            admin,
            ("POST", "/api/audit/{decision_id}/erase"),
            f"/api/audit/{decision}/erase",
            decision,
            404,
            json={"reason": "request"},
        )
        refused(
            reviewer,
            ("POST", "/api/audit/{decision_id}/feedback"),
            f"/api/audit/{decision}/feedback",
            decision,
            409,
            json={"rating": "down"},
        )
        refused(
            service,
            ("POST", "/api/audit/{decision_id}/outcome"),
            f"/api/audit/{decision}/outcome",
            decision,
            409,
            json={"final_label": "nurse", "source": "ehr"},
        )
        refused(
            reviewer,
            ("POST", "/api/review/{decision_id}"),
            f"/api/review/{decision}",
            decision,
            409,
            json={"final_label": "nurse", "reviewer": "x"},
        )
    # Read as FHIR (a person and a client): two of theirs, the first and the last made.
    own = x.one("decision")
    own_seq = first_seq(world.store, own)
    for decision in (o.ids["decision"][0], o.ids["decision"][-1]):
        for who in (viewer, x.tokens["viewer"], x.tokens["service"]):
            refused(
                who, ("GET", "/fhir/Task/{resource_id}"), f"/fhir/Task/{decision}", decision, 404
            )
            refused(
                who,
                ("GET", "/fhir/Task/{resource_id}/_history/{version_id}"),
                f"/fhir/Task/{decision}/_history/1",
                decision,
                404,
            )
            seq = first_seq(world.store, decision)
            refused(
                who,
                ("GET", "/fhir/AuditEvent/{resource_id}"),
                f"/fhir/AuditEvent/{decision}-{seq}",
                decision,
                404,
            )
            refused(
                who,
                ("GET", "/fhir/Provenance/{resource_id}"),
                f"/fhir/Provenance/{decision}-p{seq}",
                decision,
                404,
            )
            # An event needs both halves of its id: one's own decision with their seq (and
            # theirs with one's own seq) names nothing.
            for kind, mark in (("AuditEvent", ""), ("Provenance", "p")):
                for mixed in (f"{own}-{mark}{seq}", f"{decision}-{mark}{own_seq}"):
                    refused(
                        who,
                        ("GET", f"/fhir/{kind}/{{resource_id}}"),
                        f"/fhir/{kind}/{mixed}",
                        mixed,
                        404,
                    )
            for route, url, params in (
                (("GET", "/fhir/Task"), "/fhir/Task", {"_id": decision}),
                (("GET", "/fhir/Task"), "/fhir/Task", {"identifier": decision}),
                (("GET", "/fhir/AuditEvent"), "/fhir/AuditEvent", {"entity": f"Task/{decision}"}),
                (("GET", "/fhir/Provenance"), "/fhir/Provenance", {"target": f"Task/{decision}"}),
            ):
                found = world.call(
                    x, who, route, url, params={**params, "_total": "accurate"}, expect=200
                ).json()
                assert matched(found) == 0 and found.get("total") == 0, (url, params)
            for path in (
                "/fhir/Task/_search",
                "/fhir/AuditEvent/_search",
                "/fhir/Provenance/_search",
            ):
                key = {
                    "/fhir/Task/_search": "_id",
                    "/fhir/AuditEvent/_search": "entity",
                    "/fhir/Provenance/_search": "target",
                }[path]
                value = decision if key == "_id" else f"Task/{decision}"
                found = world.call(
                    x,
                    who,
                    ("POST", path),
                    path,
                    data={key: value, "_total": "accurate"},
                    expect=200,
                ).json()
                assert matched(found) == 0 and found.get("total") == 0, (path, value)
            # Theirs beside one's own, in one criterion: one's own only.
            both = world.call(
                x,
                who,
                ("GET", "/fhir/Task"),
                "/fhir/Task",
                params={"_id": f"{own},{decision}", "_total": "accurate"},
                sent=(decision,),
                expect=200,
            ).json()
            assert (matched(both), both.get("total")) == (1, 1), both
    # Their next page, followed by this organization's viewer: its own resources only.
    for kind in ("Task", "AuditEvent", "Provenance"):
        theirs = world.call(
            o,
            o.people["viewer"],
            ("GET", f"/fhir/{kind}"),
            f"/fhir/{kind}",
            params={"_count": 1},
            expect=200,
        ).json()
        following = [link["url"] for link in theirs.get("link", []) if link["relation"] == "next"]
        assert following, kind
        target = urlsplit(following[0])
        cursor = dict(parse_qs(target.query)).get("cursor", [""])[0]
        replayed = world.call(
            x,
            viewer,
            ("GET", f"/fhir/{kind}"),
            f"{target.path}?{target.query}",
            sent=(cursor,),
            expect=(200, 400),
        ).json()
        owned = tuple(x.ids["decision"])
        for entry in replayed.get("entry", []):
            if entry.get("search", {}).get("mode") == "match":
                assert entry["resource"]["id"].startswith(owned), (kind, entry["resource"]["id"])
    # Authorizing an action for their decision (spec 0.4): unknown here, and nothing is chained.
    refused(
        service,
        ("POST", "/api/agents/{namespace}/{name}/actions"),
        f"/api/agents/{AGENT}/actions",
        o.one("hooked"),
        404,
        json={"decision_id": o.one("hooked"), "name": "send_reply", "arguments": {}},
    )
    # Feedback on their card (its uuid is their decision's id): the one answer an unknown card
    # gets, and nothing chained (LP-23f).
    refused(
        service,
        ("POST", "/cds-services/{service_id}/feedback"),
        f"{SERVICE}/feedback",
        o.one("hooked"),
        404,
        json=card_feedback(overridden(o.one("hooked"), "not-relevant")),
    )
    # Credentials and governance of the other's.
    for key_id in o.ids["key"]:
        refused(
            admin,
            ("GET", "/api/auth/keys/{key_id}/history"),
            f"/api/auth/keys/{key_id}/history",
            key_id,
            404,
        )
        refused(
            admin,
            ("POST", "/api/auth/keys/{key_id}/rotate"),
            f"/api/auth/keys/{key_id}/rotate",
            key_id,
            404,
            json={},
        )
        refused(
            admin, ("DELETE", "/api/auth/keys/{key_id}"), f"/api/auth/keys/{key_id}", key_id, 404
        )
    for client_id in o.ids["client"]:
        refused(
            admin,
            ("GET", "/api/auth/clients/{client_id}/history"),
            f"/api/auth/clients/{client_id}/history",
            client_id,
            404,
        )
        refused(
            admin,
            ("POST", "/api/auth/clients/{client_id}/keys"),
            f"/api/auth/clients/{client_id}/keys",
            client_id,
            404,
            json={"jwks": {"keys": [x.signer.jwk]}},
        )
        refused(
            admin,
            ("DELETE", "/api/auth/clients/{client_id}"),
            f"/api/auth/clients/{client_id}",
            client_id,
            404,
        )
        # Their client's id, asserted with this organization's own key (under its own kid, and
        # under theirs, which is no secret: refused by the signature itself), or this one's
        # valid assertion with a form client_id naming theirs: no token.
        before = written(world.store)
        posing = Signer(x.signer.alg, o.signer.kid, x.signer.private)
        for form in (
            token_form(x.signer, client_id),
            token_form(posing, client_id),
            {**token_form(x.signer, x.one("client")), "client_id": client_id},
        ):
            world.call(
                x,
                client_of(world.app),
                ("POST", "/auth/token"),
                "/auth/token",
                data=form,
                expect=401,
                sent=(client_id,),
            )
        assert written(world.store) == before
    for hold in o.ids["hold"]:
        refused(
            x.people["governance"],
            ("DELETE", "/api/holds/{hold_id}"),
            f"/api/holds/{hold}",
            hold,
            404,
        )
    for approval in o.ids["approval"]:
        refused(
            x.people["governance"],
            ("DELETE", "/api/governance/{approval_id}"),
            f"/api/governance/{approval}",
            approval,
            404,
        )
    for approval in o.ids["registry_approval"]:
        refused(
            x.people["governance"],
            ("DELETE", "/api/registry-approvals/{approval_id}"),
            f"/api/registry-approvals/{approval}",
            approval,
            404,
        )
    # Their administrative events, by subject or by cursor.
    theirs = f"key:{o.one('key')}"
    listed = world.call(
        x,
        admin,
        ("GET", "/api/audit/administrative"),
        "/api/audit/administrative",
        params={"subject": theirs},
        expect=200,
    ).json()
    assert listed["items"] == []
    with world.store._db.session(exclusive=False) as db:
        digest = db.one("SELECT hash FROM audit_events WHERE decision_id = ?", (theirs,))
    cursor = encode_cursor({"subject": theirs, "hash": digest["hash"]})
    world.call(
        x,
        admin,
        ("GET", "/api/audit/administrative"),
        "/api/audit/administrative",
        params={"after": cursor},
        expect=400,
        sent=(theirs, digest["hash"]),
    )
    # Their EHR launch: a window pinned to it is refused before anything is read.
    assert x.launch is not None
    pinned = {"x-mendari-launch": o.launch_id}
    for route, url, options in (
        (
            ("GET", "/api/smart/focus"),
            "/api/smart/focus",
            {"params": {"pack_id": SMART_DECIDE["pack_id"]}},
        ),
        (("GET", "/api/smart/patient"), "/api/smart/patient", {}),
        (("POST", "/api/smart/decide"), "/api/smart/decide", {"json": SMART_DECIDE}),
    ):
        refused(x.launch, route, url, o.launch_id, 409, headers=pinned, **options)
    if world.accounts == "memberships":
        refused_people(world, x, o, refused)
        assert x.labeller is not None
        for assignment in o.ids["written assignment"]:  # another organization's grader's
            refused(
                x.labeller,
                ("POST", "/api/written/labelling/{assignment_id}"),
                f"/api/written/labelling/{assignment}",
                assignment,
                404,
                json={
                    "labels": {
                        "safe_to_send": "yes",
                        "addresses_request": "fully",
                        "plain_language": "yes",
                    }
                },
            )


def refused_people(world: World, x: Org, o: Org, refused: Callable[..., Any]) -> None:
    admin = x.people["admin"]
    for membership in o.ids["membership"]:
        refused(
            admin,
            ("PUT", "/api/auth/members/{membership_id}/roles"),
            f"/api/auth/members/{membership}/roles",
            membership,
            404,
            json={"roles": ["admin"]},
        )
        refused(
            admin,
            ("DELETE", "/api/auth/members/{membership_id}"),
            f"/api/auth/members/{membership}",
            membership,
            404,
        )
    for invitation in o.ids["invitation"]:
        refused(
            admin,
            ("DELETE", "/api/auth/invitations/{invitation_id}"),
            f"/api/auth/invitations/{invitation}",
            invitation,
            404,
        )
    for request_id in o.ids["request"]:
        refused(
            admin,
            ("POST", "/api/auth/access-requests/{request_id}"),
            f"/api/auth/access-requests/{request_id}",
            request_id,
            404,
            json={"outcome": "granted", "roles": ["admin"]},
        )
    # Their invitation, made out to an address of theirs: refused, left open.
    before = written(world.store)
    world.call(
        x,
        admin,
        ("POST", "/api/auth/invitations/accept"),
        "/api/auth/invitations/accept",
        json={"token": o.one("token-joiner")},
        expect=403,
    )
    assert written(world.store) == before
    # Acting for the other: neither chosen nor pinned, as for an organization that is not.
    refused(
        admin,
        ("POST", "/api/auth/organization"),
        "/api/auth/organization",
        o.name,
        403,
        json={"org": o.name},
    )
    refused(
        admin, ("GET", "/api/holds"), "/api/holds", o.name, 409, headers={"x-mendari-org": o.name}
    )


def swapped(options: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    """`options` with every `old` value in its body or query replaced by `new`."""
    return json.loads(json.dumps(options).replace(old, new)) if options else {}


def same(text: str, sent: str) -> str:
    """An answer with the id it echoes and its request id taken out, to compare two refusals."""
    return re.sub(r'"request_id":"[^"]*"', "", text.replace(sent, "<id>"))


def matched(bundle: Mapping[str, Any]) -> int:
    """How many entries of a searchset are matches (an OperationOutcome may come with none)."""
    return sum(1 for e in bundle.get("entry", []) if e.get("search", {}).get("mode") == "match")


# -- phase 2: its own ------------------------------------------------------------------------------


def reads(world: World, x: Org) -> dict[str, str]:
    """`x`'s reads that open no input, as its viewer and its administrator, bare and in the
    shapes the dashboard sends, every page of each: what the other's work must not change (the
    differential). The answers by URL, steadied (`steady`)."""
    viewer, admin = x.people["viewer"], x.people["admin"]
    pack = {"pack_id": AGENT}
    total = {"_total": "accurate", "_count": 3}
    asked: list[tuple[TestClient, Route, str, dict[str, Any]]] = [
        (viewer, ("GET", "/api/audit"), "/api/audit", pack),
        (viewer, ("GET", "/api/audit"), "/api/audit", {}),
        (viewer, ("GET", "/api/audit"), "/api/audit", {**pack, "outcome": "HUMAN_REVIEW"}),
        (viewer, ("GET", "/api/audit"), "/api/audit", {**pack, "limit": 2, "offset": 2}),
        (viewer, ("GET", "/api/review"), "/api/review", pack),
        (viewer, ("GET", "/api/review"), "/api/review", {}),
        (viewer, ("GET", "/api/review/summary"), "/api/review/summary", {}),
        (
            viewer,
            ("GET", "/api/agents/{namespace}/{name}/validation-brief"),
            f"/api/agents/{AGENT}/validation-brief",
            {},
        ),
        # The committee packet: every aggregate read about an agent in one answer, so the
        # differential holds each of them to its own organization at once.
        (
            viewer,
            ("GET", "/api/agents/{namespace}/{name}/committee-packet"),
            f"/api/agents/{AGENT}/committee-packet",
            {},
        ),
        (viewer, ("GET", "/api/feedback"), "/api/feedback", {}),
        (viewer, ("GET", "/api/feedback"), "/api/feedback", {**pack, "rating": "down"}),
        (viewer, ("GET", "/api/holds"), "/api/holds", {}),
        (viewer, ("GET", "/api/governance"), "/api/governance", {}),
        (viewer, ("GET", "/api/governance"), "/api/governance", {"pack_id": APPROVABLE}),
        (viewer, ("GET", "/api/registry-approvals"), "/api/registry-approvals", {}),
        (
            viewer,
            ("GET", "/api/registry-approvals"),
            "/api/registry-approvals",
            {"pack_id": APPROVABLE},
        ),
        (viewer, ("GET", "/api/evals"), "/api/evals", {}),
        (viewer, ("GET", "/api/evals"), "/api/evals", pack),
        (viewer, ("GET", "/api/stats"), "/api/stats", pack),
        (viewer, ("GET", "/api/stats"), "/api/stats", {}),
        (viewer, ("GET", "/api/monitoring"), "/api/monitoring", pack),
        (viewer, ("GET", "/api/health"), "/api/health", {}),
        (viewer, ("GET", "/api/health/ready"), "/api/health/ready", {}),
        (viewer, ("GET", "/api/auth/me"), "/api/auth/me", {}),
        (admin, ("GET", "/api/health"), "/api/health", {}),
        (viewer, ("GET", "/fhir/Task"), "/fhir/Task", total),
        (viewer, ("GET", "/fhir/AuditEvent"), "/fhir/AuditEvent", total),
        (viewer, ("GET", "/fhir/Provenance"), "/fhir/Provenance", total),
        (admin, ("GET", "/api/audit/export/fhir"), "/api/audit/export/fhir", {}),
        (admin, ("GET", "/api/audit/export/fhir"), "/api/audit/export/fhir", pack),
        (admin, ("GET", "/api/audit/administrative"), "/api/audit/administrative", {"limit": 4}),
        (
            admin,
            ("GET", "/api/audit/administrative/export/fhir"),
            "/api/audit/administrative/export/fhir",
            {},
        ),
        (admin, ("GET", "/api/auth/keys"), "/api/auth/keys", {}),
        (admin, ("GET", "/api/auth/clients"), "/api/auth/clients", {}),
        (admin, ("GET", "/api/usage"), "/api/usage", {}),  # the organization's usage ledger
        # Its agents' connections (ADR-0021), as a reader and as who may set them; and what the
        # deployment's registry lists, which says what this organization has of each agent.
        (viewer, ("GET", "/api/connections"), "/api/connections", {}),
        (admin, ("GET", "/api/connections"), "/api/connections", {}),
        (viewer, ("GET", "/api/hub/search"), "/api/hub/search", {}),
    ]
    if world.accounts == "memberships":
        for path in ("/api/auth/members", "/api/auth/invitations", "/api/auth/access-requests"):
            asked.append((admin, ("GET", path), path, {}))
    answers = {}
    for who, route, url, params in asked:
        key = f"{id(who)}{url}?{json.dumps(params)}"
        answer = world.call(x, who, route, url, params=params, expect=200)
        answers[key] = steady(answer.text)
        for page in range(2, 100):  # every following page, scanned and compared too
            following = next_page(route, answer, params)
            if following is None:
                break
            url_next, params_next = following
            answer = world.call(x, who, route, url_next, params=params_next, expect=200)
            answers[f"{key}#{page}"] = steady(answer.text)
    return answers


def next_page(route: Route, answer: Any, params: dict[str, Any]) -> tuple[str, dict] | None:
    """The request for the page after `answer` (a FHIR searchset's `next` link, or the
    administrative list's `next`), or None."""
    if route[1].startswith("/fhir/"):
        links = [
            link["url"] for link in answer.json().get("link", []) if link["relation"] == "next"
        ]
        if not links:
            return None
        target = urlsplit(links[0])
        return f"{target.path}?{target.query}", {}
    if route == ("GET", "/api/audit/administrative") and answer.json().get("next"):
        return route[1], {**params, "after": answer.json()["next"]}
    return None


def steady(text: str) -> str:
    """An answer without what differs between two identical reads: request ids, and a search
    Bundle's own time and id."""
    text = re.sub(r'"request_id":\s*"[^"]*"', "", text)
    text = re.sub(r'"calls_today":\s*\d+', "", text)  # FIELDS["calls_today"]
    text = re.sub(r'"timestamp":\s*"[^"]*"', "", text)
    text = re.sub(r'"generated_at":\s*"[^"]*"', "", text)
    # A committee packet's own hash, its last key: it covers the time it was generated.
    text = re.sub(r',"sha256":"sha256:[0-9a-f]{64}"\}$', "}", text)
    return re.sub(r'"resourceType":"Bundle","id":"[^"]*"', '"resourceType":"Bundle"', text)


def served(world: World, x: Org) -> None:
    """Phase 2: `x`'s principals call with its own ids; reads first, then what changes things,
    on spare ids last."""
    admin, reviewer, viewer = x.people["admin"], x.people["reviewer"], x.people["viewer"]
    service, governance = x.keys["service"], x.people["governance"]
    reads(world, x)
    for decision in x.ids["decision"]:
        # Each principal phase 1 refuses is served its own, so no refusal there is one for all.
        for who in (reviewer, admin, viewer, x.keys["viewer"]):
            world.call(
                x, who, ("GET", "/api/audit/{decision_id}"), f"/api/audit/{decision}", expect=200
            )
        world.call(
            x,
            viewer,
            ("GET", "/api/audit/{decision_id}/fhir"),
            f"/api/audit/{decision}/fhir",
            expect=200,
        )
        seq = first_seq(world.store, decision)
        for who in (viewer, x.tokens["viewer"], x.tokens["service"]):
            world.call(
                x, who, ("GET", "/fhir/Task/{resource_id}"), f"/fhir/Task/{decision}", expect=200
            )
            world.call(
                x,
                who,
                ("GET", "/fhir/Task/{resource_id}/_history/{version_id}"),
                f"/fhir/Task/{decision}/_history/1",
                expect=200,
            )
            world.call(
                x,
                who,
                ("GET", "/fhir/AuditEvent/{resource_id}"),
                f"/fhir/AuditEvent/{decision}-{seq}",
                expect=200,
            )
            world.call(
                x,
                who,
                ("GET", "/fhir/Provenance/{resource_id}"),
                f"/fhir/Provenance/{decision}-p{seq}",
                expect=200,
            )
        for path, key in (
            ("/fhir/Task/_search", "_id"),
            ("/fhir/AuditEvent/_search", "entity"),
            ("/fhir/Provenance/_search", "target"),
        ):
            value = decision if key == "_id" else f"Task/{decision}"
            for who in (viewer, x.tokens["viewer"], x.tokens["service"]):
                found = world.call(x, who, ("POST", path), path, data={key: value}, expect=200)
                assert matched(found.json()) >= 1, (path, value)
    for key_id in x.ids["key"]:
        world.call(
            x,
            admin,
            ("GET", "/api/auth/keys/{key_id}/history"),
            f"/api/auth/keys/{key_id}/history",
            expect=200,
        )
    for client_id in x.ids["client"]:
        world.call(
            x,
            admin,
            ("GET", "/api/auth/clients/{client_id}/history"),
            f"/api/auth/clients/{client_id}/history",
            expect=200,
        )
    world.call(
        x,
        admin,
        ("GET", "/api/audit/administrative"),
        "/api/audit/administrative",
        params={"subject": f"key:{x.one('key')}"},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("GET", "/api/agents/{namespace}/{name}/feedback/export"),
        f"/api/agents/{AGENT}/feedback/export",
        expect=200,
    )
    for who in (viewer, admin):
        world.call(x, who, ("GET", "/api/auth/me"), "/api/auth/me", expect=200)
        health = world.call(x, who, ("GET", "/api/health"), "/api/health", expect=200).json()
        # The last purge's counts are every organization's: the operator's in memberships mode.
        counts = {"inputs", "notes"} & set(health["last_purge"])
        assert counts == (set() if world.accounts == "memberships" else {"inputs", "notes"})
        # Its quota counts its own decisions, and no one else's.
        with world.store._db.session(exclusive=False) as db:
            row = db.one("SELECT COUNT(*) AS n FROM decisions WHERE org = ?", (x.name,))
        assert health["org_quotas"]["used_today"] == {"decisions": int(row["n"])}, health
    world.call(
        x,
        admin,
        ("GET", "/api/audit/verify"),
        "/api/audit/verify",
        expect=200 if world.accounts == "claims" else 404,
    )
    world.call(
        x,
        viewer,
        ("GET", "/api/deployments"),
        "/api/deployments",
        expect=200 if world.accounts == "claims" else 404,
    )
    # The deployment's operators' routes (the System view). In claims mode every administrator
    # operates it: its readiness holds nothing of the other organization, and the EHR check
    # refuses the other's EHR as it refuses an unknown one. In memberships mode nobody does
    # until MENDARI_OPERATOR_ORG names their organization, which this world does not set
    # (tests/test_operators.py and tests/test_system_routes.py have that).
    operates = world.accounts == "claims"
    theirs = world.other(x).name
    system: list[tuple[Route, dict[str, Any], int]] = [
        (("GET", "/api/system/checks"), {}, 200),
        (("POST", "/api/system/audit/verify"), {}, 200),
        (("POST", "/api/system/alerts/test"), {}, 409),  # no alert webhook is set here
        (("POST", "/api/system/ehr-check"), {"json": {"base": issuer_of(theirs)}}, 404),
        (("POST", "/api/system/ehr-check"), {"json": {"base": issuer_of(x.name)}}, 200),
    ]
    for route, options, answered in system:
        world.call(x, admin, route, route[1], expect=answered if operates else 403, **options)
    assert x.launch is not None
    pinned = {"x-mendari-launch": x.launch_id}
    world.call(x, x.launch, ("GET", "/api/smart/session"), "/api/smart/session", expect=200)
    world.call(
        x,
        x.launch,
        ("GET", "/api/smart/focus"),
        "/api/smart/focus",
        params={"pack_id": SMART_DECIDE["pack_id"]},
        headers=pinned,
        expect=200,
    )
    world.call(
        x, x.launch, ("GET", "/api/smart/patient"), "/api/smart/patient", headers=pinned, expect=200
    )
    # An action for its own decision: answered (blocked: this agent declares none), and chained.
    asked = world.call(
        x,
        service,
        ("POST", "/api/agents/{namespace}/{name}/actions"),
        f"/api/agents/{AGENT}/actions",
        json={"decision_id": x.one("hooked"), "name": "send_reply", "arguments": {}},
        expect=200,
    ).json()
    assert (asked["disposition"], asked["reason"]) == ("blocked", "undeclared")
    world.call(
        x,
        service,
        ("POST", "/cds-services/{service_id}/feedback"),
        f"{SERVICE}/feedback",
        json=card_feedback(overridden(x.one("hooked"), "already-addressed")),
        expect=200,
    )
    deployment_wide(world, x)
    # What changes things: an outcome on the reviewed decision, then the spares.
    world.call(
        x,
        service,
        ("POST", "/api/audit/{decision_id}/outcome"),
        f"/api/audit/{x.one('pending')}/outcome",
        json={"final_label": "nurse", "source": "ehr"},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("POST", "/api/audit/{decision_id}/erase"),
        f"/api/audit/{x.one('spare')}/erase",
        json={"reason": "request"},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("POST", "/api/auth/keys/{key_id}/rotate"),
        f"/api/auth/keys/{x.one('key', 2)}/rotate",
        json={},
        expect=201,
    )
    world.call(
        x,
        admin,
        ("DELETE", "/api/auth/keys/{key_id}"),
        f"/api/auth/keys/{x.one('key', 3)}",
        expect=204,
    )
    world.call(
        x,
        admin,
        ("POST", "/api/auth/clients/{client_id}/keys"),
        f"/api/auth/clients/{x.one('client', 2)}/keys",
        json={"jwks": {"keys": [x.signer.jwk]}},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("DELETE", "/api/auth/clients/{client_id}"),
        f"/api/auth/clients/{x.one('client', 2)}",
        expect=204,
    )
    world.call(
        x,
        governance,
        ("DELETE", "/api/holds/{hold_id}"),
        f"/api/holds/{x.one('hold', 1)}",
        expect=204,
    )
    world.call(
        x,
        governance,
        ("DELETE", "/api/governance/{approval_id}"),
        f"/api/governance/{x.one('approval', 1)}",
        expect=204,
    )
    world.call(
        x,
        governance,
        ("DELETE", "/api/registry-approvals/{approval_id}"),
        f"/api/registry-approvals/{x.one('registry_approval', 1)}",
        expect=204,
    )
    if world.accounts == "memberships":
        served_people(world, x)
        served_written(world, x)
        served_store(world, x)
    served_agents(world, x)


def connection_of(x: Org, path: str) -> dict[str, str]:
    """A connection of `x`'s, its marker in the address and in the token."""
    return {"endpoint": f"https://1.1.1.1/{x.marker}/{path}", "token": f"token-{x.marker}-{path}"}


def served_agents(world: World, x: Org) -> None:
    """The console's Agents view (`server/hub_routes.py`, `server/connection_routes.py`,
    ADR-0021). `x` reads the deployment's registry, which says what `x` has of the agent it
    lists and nothing of what the other has; in memberships mode its author adds that agent to
    its own store. Its administrator checks, sets, reads, clears and sets again the connection
    of the agent that asks a service, with an address and a token that carry its marker, and
    leaves it set: the other organization, which reads the same agent's connection after (or
    before), is shown none of it. The other's store agents are no agents here."""
    o = world.other(x)
    admin, viewer = x.people["admin"], x.people["viewer"]
    at = f"/api/connections/{PORTAL}"
    one: Route = ("GET", "/api/connections/{namespace}/{name}")
    setting: Route = ("PUT", "/api/connections/{namespace}/{name}")
    clearing: Route = ("DELETE", "/api/connections/{namespace}/{name}")
    checking: Route = ("POST", "/api/connections/{namespace}/{name}/check")
    listing: Route = ("GET", "/api/hub/agents/{namespace}/{name}")
    # Before anything of its own: nothing, whatever the other has set or added by now.
    for who in (viewer, admin):
        shown = world.call(x, who, ("GET", "/api/connections"), "/api/connections", expect=200)
        assert all(agent["connection"] is None for agent in shown.json()["agents"])
        assert world.call(x, who, one, at, expect=200).json()["source"] == "none"
    found = world.call(x, viewer, ("GET", "/api/hub/search"), "/api/hub/search", expect=200)
    assert [(a["id"], a["installed"]) for a in found.json()["agents"]] == [(LISTED, None)]
    read = world.call(x, viewer, listing, f"/api/hub/agents/{LISTED}", expect=200).json()
    assert read["installed"] is None and read["agent"]["id"] == LISTED
    # Nothing is connected: the check says so, and calls nothing.
    checked = world.call(x, admin, checking, f"{at}/check", expect=200).json()
    assert [slot["problem"] for slot in checked["checked"]] == ["canned"]
    # A reader does not set one; nor does a key.
    before = written(world.store)
    for who in (viewer, x.keys["service"]):
        world.call(x, who, setting, at, json=connection_of(x, "decide"), expect=403)
        world.call(x, who, clearing, at, expect=403)
        world.call(x, who, checking, f"{at}/check", expect=403)
    assert written(world.store) == before
    first = connection_of(x, "decide")
    x.made("connection token", first["token"])
    made = world.call(x, admin, setting, at, json=first, expect=200).json()
    assert made["connection"]["endpoint"] == first["endpoint"] and first["token"] not in str(made)
    assert world.call(x, admin, one, at, expect=200).json()["source"] == "organization"
    seen = world.call(x, viewer, one, at, expect=200).json()["connection"]
    assert seen["origin"] == "https://1.1.1.1" and "endpoint" not in seen
    world.call(x, admin, clearing, at, expect=204)
    assert world.call(x, viewer, one, at, expect=200).json()["connection"] is None
    kept = connection_of(x, "kept")
    x.made("connection token", kept["token"])
    world.call(x, admin, setting, at, json=kept, expect=200)  # and left set
    with world.store._db.session(exclusive=False) as db:
        row = db.one("SELECT id FROM agent_connections WHERE org = ?", (x.name,))
    x.made("connection", row["id"])
    if world.accounts != "memberships":
        return
    # The other's store agents (if it has made them by now): no agents here, nothing written.
    if o.ids.get("store version"):
        theirs = f"/api/connections/{o.name}/triage-copy"
        before = written(world.store)
        world.call(x, admin, one, theirs, expect=404)
        world.call(x, admin, setting, theirs, json=connection_of(x, "theirs"), expect=404)
        world.call(x, admin, clearing, theirs, expect=404)
        world.call(x, admin, checking, f"{theirs}/check", expect=404)
        assert written(world.store) == before
    # Its author adds the agent the registry lists: a draft of its own store, whatever the
    # other added (the same version, so the same digest: no organization's secret).
    author = person(world, x, "pack_author")
    world.call(x, viewer, ("POST", "/api/store/pulls"), "/api/store/pulls",
               json={"ref": LISTED}, expect=403)  # fmt: skip
    added = world.call(
        x, author, ("POST", "/api/store/pulls"), "/api/store/pulls", json={"ref": LISTED},
        expect=201,
    ).json()  # fmt: skip
    assert (added["pack_id"], added["state"], added["source"]) == (LISTED, "draft", "hub")
    found = world.call(x, viewer, ("GET", "/api/hub/search"), "/api/hub/search", expect=200)
    assert [a["installed"]["state"] for a in found.json()["agents"]] == ["draft"]
    with world.store._db.session(exclusive=False) as db:
        mine = db.all("SELECT org, state FROM store_versions WHERE digest = ?", (added["digest"],))
    assert (x.name, "draft") in {(row["org"], row["state"]) for row in mine}


def _store_archive(org: str, key: Any, note: str) -> bytes:
    """A signed archive of the triage fixture in `org`'s namespace, its tests carrying `note`."""
    import shutil
    import tempfile
    from pathlib import Path

    import yaml

    from mendari.core.pack import load_pack
    from mendari.hub import archive

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "pack"
        shutil.copytree(resolve_pack(AGENT).root, root)
        manifest = yaml.safe_load((root / "agent.yaml").read_text())
        manifest["id"] = f"{org}/triage-upload"
        (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        cases = root / "tests" / "cases.yaml"
        cases.write_text(cases.read_text() + f"\n# {note}\n")
        return archive.build(load_pack(root), key)


def served_store(world: World, x: Org) -> None:
    """Memberships mode, the pack store on (LP-20f): `x`'s administrator registers an author
    key; its author copies the triage fixture, edits the copy, runs its tests, uploads a signed
    archive whole and another in parts, lists and reads them, and withdraws one; the key is
    revoked. Every digest, key and upload of the other organization's answers `404` here."""
    import hashlib

    from mendari.hub import archive

    o = world.other(x)
    admin = x.people["admin"]
    author = person(world, x, "pack_author")
    for kind, route, template in (
        ("store version", "/api/store/versions/{d}", ("GET", "/api/store/versions/{digest}")),
        ("store version", "/api/store/versions/{d}/tests",
         ("POST", "/api/store/versions/{digest}/tests")),
        ("store version", "/api/store/versions/{d}", ("DELETE", "/api/store/versions/{digest}")),
        ("store upload", "/api/store/uploads/{d}/complete",
         ("POST", "/api/store/uploads/{upload_id}/complete")),
    ):  # fmt: skip
        for found in o.ids.get(kind, []):
            world.call(x, author, template, route.format(d=found), expect=404)
    for found in o.ids.get("store version", []):
        world.call(
            x,
            author,
            ("GET", "/api/store/versions/{digest}/file"),
            f"/api/store/versions/{found}/file",
            params={"path": "agent.yaml"},
            expect=404,
        )
    for found in o.ids.get("store version", []):
        world.call(
            x,
            author,
            ("POST", "/api/store/versions/{digest}/edits"),
            f"/api/store/versions/{found}/edits",
            json={"files": [{"path": "agent.yaml", "content": "x"}]},
            expect=404,
        )
    for found in o.ids.get("store upload", []):
        world.call(
            x,
            author,
            ("PUT", "/api/store/uploads/{upload_id}/parts/{n}"),
            f"/api/store/uploads/{found}/parts/0",
            content=b"x",
            expect=404,
        )
    for found in o.ids.get("store key", []):
        world.call(
            x, admin, ("DELETE", "/api/store/keys/{key_id}"), f"/api/store/keys/{found}", expect=404
        )
    if o.ids.get("store version"):
        world.call(
            x,
            author,
            ("GET", "/api/store/agents/{namespace}/{name}"),
            f"/api/store/agents/{o.name}/triage-copy",
            expect=404,
        )

    private, public = archive.generate_key()
    key = archive.load_private_key(private)
    added = world.call(
        x,
        admin,
        ("POST", "/api/store/keys"),
        "/api/store/keys",
        json={"public_pem": public.decode()},
        expect=201,
    ).json()
    x.made("store key", added["key_id"])
    world.call(x, admin, ("GET", "/api/store/keys"), "/api/store/keys", expect=200)
    copy = world.call(
        x,
        author,
        ("POST", "/api/store/copies"),
        "/api/store/copies",
        json={"pack": AGENT, "name": "triage-copy"},
        expect=201,
    ).json()
    x.made("store version", copy["digest"])
    rules = resolve_pack(AGENT).read_bytes("logic/router_rules.yaml").decode()
    rules += f"\n# {x.marker}\n"
    world.call(
        x,
        author,
        ("GET", "/api/store/versions/{digest}/file"),
        f"/api/store/versions/{copy['digest']}/file",
        params={"path": "logic/router_rules.yaml"},
        expect=200,
    )
    edited = world.call(
        x,
        author,
        ("POST", "/api/store/versions/{digest}/edits"),
        f"/api/store/versions/{copy['digest']}/edits",
        json={"files": [{"path": "logic/router_rules.yaml", "content": rules}]},
        expect=201,
    ).json()
    x.made("store version", edited["digest"])
    world.call(
        x,
        author,
        ("POST", "/api/store/versions/{digest}/tests"),
        f"/api/store/versions/{edited['digest']}/tests",
        expect=200,
    )
    whole = world.call(
        x,
        author,
        ("POST", "/api/store/versions"),
        "/api/store/versions",
        content=_store_archive(x.name, key, f"whole {x.marker}"),
        expect=201,
    ).json()
    x.made("store version", whole["digest"])
    data = _store_archive(x.name, key, f"parts {x.marker}")
    begun = world.call(
        x,
        author,
        ("POST", "/api/store/uploads"),
        "/api/store/uploads",
        json={"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        expect=201,
    ).json()
    x.made("store upload", begun["upload"])
    world.call(
        x,
        author,
        ("PUT", "/api/store/uploads/{upload_id}/parts/{n}"),
        f"/api/store/uploads/{begun['upload']}/parts/0",
        content=data,
        expect=200,
    )
    parts = world.call(
        x,
        author,
        ("POST", "/api/store/uploads/{upload_id}/complete"),
        f"/api/store/uploads/{begun['upload']}/complete",
        expect=201,
    ).json()
    x.made("store version", parts["digest"])
    listed = world.call(x, author, ("GET", "/api/store/agents"), "/api/store/agents", expect=200)
    assert {lineage["pack_id"] for lineage in listed.json()} == {
        f"{x.name}/triage-copy",
        f"{x.name}/triage-upload",
    }
    world.call(
        x,
        author,
        ("GET", "/api/store/agents/{namespace}/{name}"),
        f"/api/store/agents/{x.name}/triage-copy",
        expect=200,
    )
    world.call(
        x,
        author,
        ("GET", "/api/store/versions/{digest}"),
        f"/api/store/versions/{whole['digest']}",
        expect=200,
    )
    world.call(
        x,
        author,
        ("DELETE", "/api/store/versions/{digest}"),
        f"/api/store/versions/{parts['digest']}",
        expect=200,
    )
    served_store_jobs(world, x, author, edited["digest"])
    world.call(
        x,
        admin,
        ("DELETE", "/api/store/keys/{key_id}"),
        f"/api/store/keys/{added['key_id']}",
        expect=200,
    )


def served_store_jobs(world: World, x: Org, author: TestClient, digest: str) -> None:
    """LP-20g: `x`'s author uploads a dataset, runs a job on its draft and one on the pack it
    was copied from, and compares them; the other organization's dataset, jobs and comparison
    answer `404` here."""
    o = world.other(x)
    for found in o.ids.get("store dataset", []):
        world.call(
            x,
            author,
            ("GET", "/api/store/datasets/{dataset_id}"),
            f"/api/store/datasets/{found}",
            expect=404,
        )
    for found in o.ids.get("store job", []):
        world.call(
            x, author, ("GET", "/api/store/jobs/{job_id}"), f"/api/store/jobs/{found}", expect=404
        )
        world.call(
            x,
            author,
            ("POST", "/api/store/jobs/{job_id}/advance"),
            f"/api/store/jobs/{found}/advance",
            expect=404,
        )
    others = o.ids.get("store job", [])
    if len(others) >= 2:
        world.call(
            x,
            author,
            ("GET", "/api/store/comparisons"),
            "/api/store/comparisons",
            params={"draft": others[0], "current": others[1]},
            expect=404,
        )
    lines = resolve_pack(AGENT).read_bytes("data/test.jsonl").decode().splitlines()[:4]
    dataset = world.call(
        x,
        author,
        ("POST", "/api/store/datasets"),
        "/api/store/datasets",
        json={"name": "sample", "version": digest, "content": "\n".join(lines) + "\n"},
        expect=201,
    ).json()
    x.made("store dataset", dataset["id"])
    world.call(x, author, ("GET", "/api/store/datasets"), "/api/store/datasets", expect=200)
    world.call(
        x,
        author,
        ("GET", "/api/store/datasets/{dataset_id}"),
        f"/api/store/datasets/{dataset['id']}",
        expect=200,
    )
    made = []
    for target in ({"version": digest}, {"pack": AGENT}):
        job = world.call(
            x,
            author,
            ("POST", "/api/store/jobs"),
            "/api/store/jobs",
            json={**target, "dataset": {"id": dataset["id"]}},
            expect=201,
        ).json()
        x.made("store job", job["id"])
        while job["state"] not in ("done", "failed"):
            job = world.call(
                x,
                author,
                ("POST", "/api/store/jobs/{job_id}/advance"),
                f"/api/store/jobs/{job['id']}/advance",
                expect=200,
            ).json()
        assert job["state"] == "done", job
        x.made("store report", job["report_id"])
        made.append(job["id"])
    world.call(x, author, ("GET", "/api/store/jobs"), "/api/store/jobs", expect=200)
    world.call(
        x, author, ("GET", "/api/store/jobs/{job_id}"), f"/api/store/jobs/{made[0]}", expect=200
    )
    world.call(
        x,
        author,
        ("GET", "/api/store/comparisons"),
        "/api/store/comparisons",
        params={"draft": made[0], "current": made[1]},
        expect=200,
    )
    served_store_promotion(world, x, digest, made[0])
    served_store_improvement(world, x, digest)


def served_store_promotion(world: World, x: Org, digest: str, job_id: str) -> None:
    """LP-20h: `x`'s governance promotes the draft on its job's report; the version is served
    to `x` (listed and decided) and to no other organization; then retired. The other
    organization's promotions answer `404` here."""
    o = world.other(x)
    governance = x.people["governance"]
    for found in o.ids.get("store promotion", []):
        world.call(
            x,
            governance,
            ("POST", "/api/store/promotions/{promotion_id}/retire"),
            f"/api/store/promotions/{found}/retire",
            expect=404,
        )
    listed = world.call(
        x, governance, ("GET", "/api/store/promotions"), "/api/store/promotions", expect=200
    ).json()
    report = next(v for v in listed["versions"] if v["digest"] == digest)["reports"]
    assert report, listed
    made = world.call(
        x,
        governance,
        ("POST", "/api/store/promotions"),
        "/api/store/promotions",
        json={"version": digest, "reports": report[:1], "reference": f"cab {x.marker}"},
        expect=201,
    ).json()
    x.made("store promotion", made["id"])
    pack_id = f"{x.name}/triage-copy"
    served = world.call(x, x.people["viewer"], ("GET", "/api/agents"), "/api/agents", expect=200)
    assert pack_id in {p["id"] for p in served.json()}
    world.call(
        x,
        governance,
        ("POST", "/api/store/promotions/{promotion_id}/retire"),
        f"/api/store/promotions/{made['id']}/retire",
        expect=200,
    )


def served_store_improvement(world: World, x: Org, digest: str) -> None:
    """LP-20i: `x`'s administrator (who may read inputs) gives itself the author role, builds the
    improvement dataset from `x`'s reviewed decisions and accepts a proposed test; the other
    organization's dataset answers `404` here."""
    o = world.other(x)
    sub = f"author-reader-{x.marker}"
    reader = client_of(
        world.app,
        member(world.store, org=x.name, roles=["admin", "pack_author"], sub=sub),
    )
    x.made("person", person_id(IDP, sub))
    for found in o.ids.get("store improvement", []):
        world.call(
            x,
            reader,
            ("GET", "/api/store/versions/{digest}/proposed-tests"),
            f"/api/store/versions/{digest}/proposed-tests",
            params={"dataset": found},
            expect=404,
        )
    made = world.call(
        x,
        reader,
        ("POST", "/api/store/agents/{namespace}/{name}/improvement"),
        f"/api/store/agents/{x.name}/triage-copy/improvement",
        json={"version": digest, "limit": 5},
        expect=(201, 422),
    )
    if made.status_code != 201:
        return  # no reviewed decision to build from in this world's round
    dataset = made.json()["id"]
    x.made("store improvement", dataset)
    shown = world.call(
        x,
        reader,
        ("GET", "/api/store/versions/{digest}/proposed-tests"),
        f"/api/store/versions/{digest}/proposed-tests",
        params={"dataset": dataset},
        expect=200,
    ).json()
    case = shown["cases"][0]["case"]
    world.call(
        x,
        reader,
        ("POST", "/api/store/versions/{digest}/proposed-tests/{dataset_id}/{case_id}"),
        f"/api/store/versions/{digest}/proposed-tests/{dataset}/{case}",
        json={"accept": True},
        expect=200,
    )


def served_people(world: World, x: Org) -> None:
    admin, m = x.people["admin"], x.marker
    world.call(
        x,
        admin,
        ("PUT", "/api/auth/members/{membership_id}/roles"),
        f"/api/auth/members/{x.one('changed')}/roles",
        json={"roles": ["reviewer"]},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("DELETE", "/api/auth/members/{membership_id}"),
        f"/api/auth/members/{x.one('removed')}",
        expect=204,
    )
    world.call(
        x,
        admin,
        ("DELETE", "/api/auth/invitations/{invitation_id}"),
        f"/api/auth/invitations/{x.one('invitation', 1)}",
        expect=204,
    )
    joiner = client_of(
        world.app, member(world.store, org=None, sub=f"joiner-{m}", email=f"joiner@{m}.example.org")
    )
    x.made("person", person_id(IDP, f"joiner-{m}"))
    world.call(
        x,
        joiner,
        ("POST", "/api/auth/invitations/accept"),
        "/api/auth/invitations/accept",
        json={"token": x.one("token-joiner")},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("POST", "/api/auth/access-requests/{request_id}"),
        f"/api/auth/access-requests/{x.one('request')}",
        json={"outcome": "refused"},
        expect=200,
    )
    world.call(
        x,
        admin,
        ("POST", "/api/auth/organization"),
        "/api/auth/organization",
        json={"org": x.name},
        expect=200,
    )


def deployment_wide(world: World, x: Org) -> None:
    """The routes of the allow-list that are called: once by each organization, scanned."""
    viewer, admin = x.people["viewer"], x.people["admin"]
    calls: list[tuple[TestClient, Route, str, dict[str, Any]]] = [
        (viewer, ("GET", "/"), "/", {}),
        (viewer, ("GET", "/docs"), "/docs", {}),
        (viewer, ("GET", "/status"), "/status", {}),
        (viewer, ("GET", "/docs/reference"), "/docs/reference", {}),
        (viewer, ("GET", "/redoc"), "/redoc", {}),
        (viewer, ("GET", "/favicon.ico"), "/favicon.ico", {}),
        (viewer, ("GET", "/openapi.json"), "/openapi.json", {}),
        (viewer, ("GET", "/api/agents"), "/api/agents", {}),
        (viewer, ("GET", "/api/agents/{namespace}/{name}"), f"/api/agents/{AGENT}", {}),
        (
            viewer,
            ("GET", "/api/agents/{namespace}/{name}/datasets"),
            f"/api/agents/{AGENT}/datasets",
            {},
        ),
        (
            viewer,
            ("GET", "/api/agents/{namespace}/{name}/examples"),
            f"/api/agents/{AGENT}/examples",
            {},
        ),
        (viewer, ("GET", "/cds-services"), "/cds-services", {}),
        (viewer, ("GET", "/fhir/metadata"), "/fhir/metadata", {}),
        (
            viewer,
            ("GET", "/fhir/OperationDefinition/{resource_id}"),
            "/fhir/OperationDefinition/decide",
            {},
        ),
        (
            viewer,
            ("GET", "/fhir/.well-known/smart-configuration"),
            "/fhir/.well-known/smart-configuration",
            {},
        ),
        (viewer, ("GET", "/fhir/CodeSystem"), "/fhir/CodeSystem", {}),
        (viewer, ("GET", "/fhir/CodeSystem/{resource_id}"), "/fhir/CodeSystem/audit-event", {}),
        (viewer, ("GET", "/fhir/NamingSystem/{resource_id}"), "/fhir/NamingSystem/decision-id", {}),
        (admin, ("GET", "/api/auth/keys/presets"), "/api/auth/keys/presets", {}),
        (viewer, ("GET", "/api/notice"), "/api/notice", {}),
        (viewer, ("GET", "/api/hub"), "/api/hub", {}),
    ]
    if world.accounts == "memberships":
        calls.append((client_of(world.app), ("GET", "/invite"), "/invite", {}))
    for who, route, url, params in calls:
        moved = url == "/redoc"  # to the reference's one renderer
        world.call(x, who, route, url, params=params, expect=308 if moved else 200)
    world.call(
        x,
        viewer,
        ("POST", "/fhir/CodeSystem/_search"),
        "/fhir/CodeSystem/_search",
        data={"url": "https://mendari.co/fhir/CodeSystem/audit-event"},
        expect=200,
    )


# -- the test --------------------------------------------------------------------------------------


def test_every_route_called_as_two_organizations_shows_neither_the_others_data(
    world: World,
) -> None:
    alpha, bravo = (world.orgs[org] for org in ORGS)
    for x in (alpha, bravo):
        refusals(world, x, world.other(x))
    world.pinned = True  # from here on, every own call also names the other organization
    for x in (alpha, bravo):
        served(world, x)
    if world.accounts == "memberships":
        both(world, alpha, bravo)
    # The differential: an organization's reads are the same before and after the other works.
    before = {x.name: reads(world, x) for x in (alpha, bravo)}
    for x in (alpha, bravo):
        other = world.other(x)
        again(world, x)
        assert reads(world, other) == before[other.name], other.name
        before[x.name] = reads(world, x)  # after its own second round, for the next
    scan(world)
    attribute(world)
    assert world.probed == []
    assert world.store.verify()["ok"]
    covered(world)


def again(world: World, x: Org) -> None:
    """A second round of `x`'s work, one write of every kind an answer counts: an automated
    decision, then a hold of the triage fixture and a decision it holds (citing it, while the
    other's automation runs on); a review, an outcome and a rating; an approval, an evaluation, a
    key and a client; in memberships mode an invitation and a request to join."""
    decide, url = ("POST", "/api/agents/{namespace}/{name}/decide"), f"/api/agents/{AGENT}/decide"
    service, reviewer = x.keys["service"], x.people["reviewer"]
    free = world.call(x, service, decide, url, json=message(f"{REFILL} {x.marker}"), expect=200)
    assert free.json()["outcome"] == "AUTO", free.json()["reasons"]  # no one else's hold here
    x.made("decision", free.json()["id"])
    hold = world.call(
        x,
        x.people["governance"],
        ("POST", "/api/holds"),
        "/api/holds",
        json={"pack_id": AGENT, "reason": f"again {x.marker}"},
        expect=201,
    ).json()
    x.made("hold", hold["id"])
    held = world.call(x, service, decide, url, json=message(f"{REFILL} {x.marker}"), expect=200)
    assert held.json()["outcome"] == "HUMAN_REVIEW"
    assert f"automation is on hold: hold {hold['id']}" in held.json()["reasons"]
    x.made("decision", held.json()["id"])
    world.call(
        x,
        reviewer,
        ("POST", "/api/review/{decision_id}"),
        f"/api/review/{held.json()['id']}",
        json={"final_label": "nurse", "reviewer": "x", "note": f"again {x.marker}"},
        expect=200,
    )
    world.call(
        x,
        service,
        ("POST", "/api/audit/{decision_id}/outcome"),
        f"/api/audit/{held.json()['id']}/outcome",
        json={"final_label": "nurse", "source": "ehr"},
        expect=200,
    )
    rated = world.call(
        x,
        reviewer,
        ("POST", "/api/audit/{decision_id}/feedback"),
        f"/api/audit/{free.json()['id']}/feedback",
        json={"rating": "down", "note": f"again {x.marker}"},
        expect=200,
    ).json()
    x.made("feedback", rated["id"])
    approval = world.call(
        x,
        x.people["governance"],
        ("POST", "/api/governance"),
        "/api/governance",
        json={"pack_id": APPROVABLE, "reference": f"again {x.marker}", "days": 30},
        expect=201,
    ).json()
    x.made("approval", approval["id"])
    run = world.call(
        x,
        x.people["admin"],
        ("POST", "/api/agents/{namespace}/{name}/evals"),
        f"/api/agents/{AGENT}/evals",
        json={"dataset": "test", "limit": 1},
        expect=200,
    ).json()
    x.made("run", run["run"]["id"])
    key = world.call(
        x,
        x.people["admin"],
        ("POST", "/api/auth/keys"),
        "/api/auth/keys",
        json={"name": f"again {x.marker}", "role": "viewer"},
        expect=201,
    ).json()
    x.made("key", key["id"])
    client = world.call(
        x,
        x.people["admin"],
        ("POST", "/api/auth/clients"),
        "/api/auth/clients",
        json={"name": f"again {x.marker}", "role": "viewer", "jwks": {"keys": [x.signer.jwk]}},
        expect=201,
    ).json()
    x.made("client", client["client_id"])
    changed = connection_of(x, "again")  # its agent's connection, changed (ADR-0021)
    x.made("connection token", changed["token"])
    world.call(
        x,
        x.people["admin"],
        ("PUT", "/api/connections/{namespace}/{name}"),
        f"/api/connections/{PORTAL}",
        json=changed,
        expect=200,
    )
    if world.accounts == "memberships":
        invited = world.call(
            x,
            x.people["admin"],
            ("POST", "/api/auth/invitations"),
            "/api/auth/invitations",
            json={"email": f"again@{x.marker}.example.org", "roles": ["viewer"]},
            expect=201,
        ).json()
        x.made("invitation", invited["id"])
        asker = client_of(
            world.app,
            member(
                world.store,
                org=None,
                sub=f"again-{x.marker}",
                email=f"again@{x.marker}.example.org",
            ),
        )
        x.made("person", person_id(IDP, f"again-{x.marker}"))
        world.call(
            x,
            asker,
            ("POST", "/api/auth/access-requests"),
            "/api/auth/access-requests",
            json={"org": x.name},
            expect=202,
        )


# -- the checks ------------------------------------------------------------------------------------


def secrets_of(world: World, org: Org) -> set[str]:
    """Every id and secret of `org`'s that the other must never see: what it made, its people,
    its credentials, and the hash of each event chained about its rows."""
    found = {value for values in org.ids.values() for value in values}
    for client in (*org.keys.values(), *org.tokens.values()):
        found.add(client.headers["authorization"].removeprefix("Bearer "))
    found |= {org.launch_id, org.name}
    record = world.store.organization(org.name)
    if record is not None:  # its display name, as an organization's record holds it
        found.add(str(record["name"]))
    with world.store._db.session(exclusive=False) as db:
        decided = [
            row["id"] for row in db.all("SELECT id FROM decisions WHERE org = ?", (org.name,))
        ]
        marks = ", ".join("?" for _ in decided)
        if decided:
            found |= {
                row["hash"]
                for row in db.all(
                    f"SELECT hash FROM audit_events WHERE decision_id IN ({marks})", decided
                )
            }
    found |= {item["hash"] for item in world.store.iter_administrative_events(org.name)}
    return {value for value in found if len(value) >= 6}


def visible(exchange: Exchange) -> str:
    """What an answer shows, without the allow-list's fields (FIELDS) and the values the caller
    sent itself (a refusal may echo them)."""
    if exchange.route == ("GET", "/api/audit/verify") and exchange.status == 200:
        return ""  # FIELDS["verify"]: claims mode, the whole chain's verification
    parts = []
    for line in exchange.text.splitlines() or [exchange.text]:
        try:
            parts.append(json.dumps(without_chain_position(json.loads(line))))
        except ValueError:
            parts.append(line)
    text = "\n".join(parts)
    for value in sorted(exchange.sent, key=len, reverse=True):
        if len(value) >= 6:
            text = text.replace(value, "")
    return text


def without_chain_position(value: Any) -> Any:
    """FIELDS["prev_hash"]: an event's link to the one before, as a key or an AuditEvent detail."""
    if isinstance(value, Mapping):
        return {k: without_chain_position(v) for k, v in value.items() if k != "prev_hash"}
    if isinstance(value, list):
        return [
            without_chain_position(v)
            for v in value
            if not (isinstance(v, Mapping) and v.get("type") == "prev_hash")
        ]
    return value


def scan(world: World) -> None:
    """No answer to an organization shows the other's marker, ids or secrets."""
    hidden = {name: secrets_of(world, org) for name, org in world.orgs.items()}
    for exchange in world.exchanges:
        other = world.other(world.orgs[exchange.org])
        text = visible(exchange).lower()  # a display name ("Clinic-Zqbravo") is caught too
        assert other.marker not in text, (exchange.org, exchange.route, text[:300])
        shown = sorted(value for value in hidden[other.name] if value.lower() in text)
        assert shown == [], (exchange.org, exchange.route, shown)


def attribute(world: World) -> None:
    """Every event chained during an organization's calls is its own (the deployment's kinds
    aside), and names it, in either accounts mode."""
    with world.store._db.session(exclusive=False) as db:
        events = {row["seq"]: row for row in db.all("SELECT * FROM audit_events")}
        decided = {row["id"]: row["org"] for row in db.all("SELECT id, org FROM decisions")}
        subjects = audit_module._subject_orgs(db)
    for org, route, start, end in world.chained:
        for seq in range(start + 1, end + 1):
            event = events[seq]
            kind, chain = event["event"], str(event["decision_id"])
            payload = json.loads(event["payload"])
            if kind in DEPLOYMENT_KINDS:
                assert "org" not in payload, (route, kind)
                continue
            if kind in PERSON_KINDS:
                owner = payload["org"]
            elif kind in DECISION_KINDS:
                owner = decided[chain]
            else:
                owner = subjects[chain]
                assert ADMINISTRATIVE_KINDS[kind] == chain.split(":")[0], (route, kind)
            assert owner == org, (route, kind, chain, owner)
            assert payload["org"] == org, (route, kind)


def covered(world: World) -> None:
    """Every mounted route is exercised by a call here or is one the allow-list does not call;
    and the allow-list names only mounted routes, each called unless it says why not."""
    mounted = set(presets.served(world.app.routes)) - REMOVED_ROUTES
    assert mounted - world.exercised - NOT_CALLED == set(), "a route no call here exercises"
    assert world.exercised <= mounted
    assert (set(DEPLOYMENT_WIDE) - MEMBERSHIPS_ONLY) <= mounted | NOT_CALLED
    if world.accounts == "claims":
        assert not mounted & MEMBERSHIPS_ONLY
    else:
        assert mounted >= MEMBERSHIPS_ONLY


def both(world: World, a: Org, b: Org) -> None:
    """Memberships mode: one person, a viewer of `a` acting for it and an administrator of `b`.
    Acting for `a`, they have `a`'s roles and see `a`'s data only; a window still showing `b` is
    refused; choosing `b` gives `b`'s roles, and a window still showing `a` is then refused.
    Their identity names neither organization (`both@shared.example.org`), and their membership
    list, in /api/auth/me, is theirs: it is read here, not scanned."""
    member(world.store, org=b.name, roles=["admin"], sub="both", email="both@shared.example.org")
    who = client_of(
        world.app,
        member(
            world.store, org=a.name, roles=["viewer"], sub="both", email="both@shared.example.org"
        ),
    )
    shown = who.get("/api/auth/me").json()
    assert (shown["principal"]["org"], shown["principal"]["roles"]) == (a.name, ["viewer"])
    assert sorted(m["org"] for m in shown["memberships"]) == sorted((a.name, b.name))
    world.call(a, who, ("GET", "/api/auth/keys"), "/api/auth/keys", expect=403)  # a's roles
    for url, params in (("/api/audit", {"pack_id": AGENT}), ("/api/holds", {}),
                        ("/api/feedback", {}), ("/api/evals", {})):  # fmt: skip
        world.call(a, who, ("GET", url), url, params=params, expect=200)
    before = written(world.store)
    world.call(
        a,
        who,
        ("POST", "/api/holds"),
        "/api/holds",
        json={"reason": "x"},
        headers={"x-mendari-org": b.name},
        expect=409,
    )
    assert written(world.store) == before
    world.call(
        b,
        who,
        ("POST", "/api/auth/organization"),
        "/api/auth/organization",
        json={"org": b.name},
        expect=200,
    )
    world.call(b, who, ("GET", "/api/auth/keys"), "/api/auth/keys", expect=200)  # b's roles now
    world.call(
        b, who, ("GET", "/api/holds"), "/api/holds", headers={"x-mendari-org": a.name}, expect=409
    )


def test_every_store_method_that_may_read_every_organization_is_probed() -> None:
    """A store method whose organization may be None (every organization) is wrapped by the
    probes, so a new one fails here until it is."""
    found = {
        name
        for name, method in inspect.getmembers(AuditStore, inspect.isfunction)
        if not name.startswith("_")
        and (parameter := inspect.signature(method).parameters.get("org")) is not None
        and "None" in str(parameter.annotation)
    }
    assert found == {*EXPLICIT, "holds", "memberships"}
