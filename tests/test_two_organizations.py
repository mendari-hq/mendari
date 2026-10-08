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
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.core import audit as audit_module
from mendari.core import tenancy
from mendari.core.audit import (
    ADMINISTRATIVE_KINDS,
    BOUND_WHEN_MARKED,
    DECISION_KINDS,
    DEPLOYMENT_KINDS,
    PERSON_KINDS,
)
from mendari.server import presets, smart_routes
from mendari.server.api_docs import FHIR_EXAMPLE, FHIR_EXAMPLE_PACK
from mendari.server.app import create_app
from mendari.server.auth import person_id
from mendari.server.backend_services import ASSERTION_TYPE
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from mendari.standards.search import encode_cursor
from tests.conftest import calm_jev, encrypt_mode, member, message, signed_in
from tests.test_ai_guard_pack import SAFE
from tests.test_backend_services import Signer, assertion, bearer
from tests.test_davinci import COMPLETE, PA, package_request, request
from tests.test_hold import REFILL
from tests.test_memberships import token_of
from tests.test_pas import COVERAGE, INSURER, PATIENT, REQUESTER
from tests.test_prior_auth_pack import _order
from tests.test_smart import CLIENT as SMART_CLIENT
from tests.test_smart import DECIDE_BODY, REFERRAL, TENANTS, FakeEhr, routed, tenant

APP = "https://mendari.test"
IDP = "https://idp.example.org"
INBOX = "mendari/patient-inbox-router"
GUARD = "mendari/ai-guard"
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
    ("GET", "/docs/reference"): "the API reference page",
    ("GET", "/redoc"): "the reference's other renderer",
    ("GET", "/favicon.ico"): "an icon",
    ("GET", "/api/health/ready"): "readiness: fixed codes and the version, from a cached read of "
    "the chain's table and the packs start-up found",
    ("GET", "/invite"): "the invitation page, static: the token stays in the fragment",
    ("GET", "/openapi.json"): "generated from code, settings and the served packs",
    ("GET", "/api/packs"): "the served packs, read from files, never the store",
    ("GET", "/api/packs/{namespace}/{name}"): "a served pack, from its files",
    ("GET", "/api/packs/{namespace}/{name}/datasets"): "the pack's and the deployment's "
    "datasets folder, from files (what it may hold in memberships mode is an owner decision)",
    ("GET", "/api/packs/{namespace}/{name}/examples"): "the pack's examples, from its files",
    ("GET", "/cds-services"): "the CDS services, generated from the packs",
    ("GET", "/fhir/metadata"): "conformance; capability_versions is deployment-wide, unchained",
    ("GET", "/fhir/OperationDefinition/{resource_id}"): "conformance",
    ("GET", "/fhir/.well-known/smart-configuration"): "the endpoints' configuration",
    ("GET", "/fhir/CodeSystem"): "terminology built in memory from the packs",
    ("POST", "/fhir/CodeSystem/_search"): "terminology built in memory from the packs",
    ("GET", "/fhir/CodeSystem/{resource_id}"): "terminology built in memory from the packs",
    ("GET", "/fhir/NamingSystem/{resource_id}"): "terminology built in memory",
    ("GET", "/api/auth/keys/presets"): "the preset table and the route rules",
    ("GET", "/api/notice"): "the notice and whether the caller acknowledged it",
    ("POST", "/api/notice/acknowledge"): "a person's own acknowledgement, across organizations",
    ("GET", "/api/smart/session"): "the launch's own session (the browser's cookie)",
    ("GET", "/smart/launch"): "the launch flow: called and scanned in each organization's seed",
    ("GET", "/smart/callback"): "the launch flow: called and scanned in each organization's seed",
    ("GET", "/api/providers/jev/models"): "the deployment's Jev account. Not called (needs Jev)",
    ("GET", "/api/internal/retention"): "the operator's cron (CRON_SECRET), across every "
    "organization. Not called: tests/test_hosted_data.py",
    ("GET", "/auth/login"): "sign-in: a redirect. Not called: tests/test_auth.py",
    ("GET", "/auth/callback"): "sign-in: a redirect and a cookie. Not called: tests/test_auth.py",
    ("POST", "/auth/logout"): "signing out. Not called: tests/test_auth.py",
    ("POST", "/api/smart/logout"): "ending a launch. Not called: tests/test_smart.py",
}
NOT_CALLED = {route for route, why in DEPLOYMENT_WIDE.items() if "Not called" in why}
# What may appear in an organization's answers although it is not that organization's data:
FIELDS = {
    "prev_hash": "an event's place in the deployment's one chain: the hash of the event before, "
    "often another organization's, never its content (owner decision O1, threat model I4)",
    "verify": "claims mode only: the whole chain's verification (head, events) is unchanged "
    "there; in memberships mode it is the operator's (404)",
    "calls_today": "claims mode only: health's count of the deployment's remote calls today "
    "(Jev, LLM), which every organization's decisions move; the operator's in memberships mode",
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
    "audit_events",
)


# -- the two organizations ------------------------------------------------------------------------


@dataclass
class Org:
    name: str
    signer: Signer
    ehr: FakeEhr
    people: dict[str, TestClient] = field(default_factory=dict)
    keys: dict[str, TestClient] = field(default_factory=dict)  # viewer, service
    tokens: dict[str, TestClient] = field(default_factory=dict)  # Backend Services clients
    launch: TestClient | None = None
    launch_id: str = ""
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
        if self.pinned and url.startswith("/api/") and not url.startswith("/api/packs/"):
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
)
DEPLOYMENT_READS = {
    "verify": ("claims", "the whole chain's verification: unchanged in claims mode"),
    "usage": ("claims", "today's calls in health: unchanged in claims mode"),
    "deployments": ("claims", "the deployment records: unchanged in claims mode"),
    "last_purge": ("claims memberships", "the last purge in health (its counts claims-only)"),
}


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
        method = getattr(AuditStore, name)
        signature = inspect.signature(method)

        @functools.wraps(method)
        def wrapped(self: AuditStore, *args: Any, **kwargs: Any) -> Any:
            if world.signed_in:
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                if bound.arguments.get("org") is None:
                    world.probed.append(f"{name} for every organization")
            return method(self, *args, **kwargs)

        return wrapped

    for name in EXPLICIT:
        monkeypatch.setattr(AuditStore, name, explicit(name))
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
        method = getattr(AuditStore, name)

        @functools.wraps(method)
        def wrapped(self: AuditStore, *args: Any, **kwargs: Any) -> Any:
            modes, _ = DEPLOYMENT_READS[name]
            if world.signed_in and world.accounts not in modes.split():
                world.probed.append(f"{name}, deployment-wide, in {world.accounts} mode")
            return method(self, *args, **kwargs)

        return wrapped

    for name in DEPLOYMENT_READS:
        monkeypatch.setattr(AuditStore, name, deployment(name))
    yield


# -- the deployment and each organization's seed --------------------------------------------------


def issuer_of(org: str) -> str:
    return f"{TENANTS}/tenant-{org.removeprefix('clinic-')}/fhir"


@pytest.fixture(params=["claims", "memberships"])
def world(
    request: pytest.FixtureRequest, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    accounts = request.param
    encrypt_mode(monkeypatch)  # inputs and notes sealed, read by a reader and chained
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-org")
    monkeypatch.setenv("MENDARI_ACCOUNTS", accounts)
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
    ehrs = {org: tenant(issuer_of(org)) for org in ORGS}
    for name, ehr in ehrs.items():  # alike (the same ids), but its own tokens and patient's name
        marker = name.removeprefix("clinic-")
        ehr.token_prefix = f"access-{marker}"
        ehr.resources = [
            {**r, "name": [{"family": f"Synthetic {marker}", "given": ["Pat"]}]}
            if r["resourceType"] == "Patient"
            else r
            for r in ehr.resources
        ]
    transport = routed(*ehrs.values())
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
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
    # Providers that automate the inbox (a calm Jev), so a hold's effect shows; each hold set
    # here clears the server's look-up of holds at once.
    app = create_app(db_path=db_target, providers=calm_jev(monkeypatch))
    with AuditStore(db_target) as store:
        store.purge(30, source="command")  # a last purge, with its counts, for health
        world = World(app, store, accounts, orgs)
        with probing(world, monkeypatch):
            for org in orgs.values():
                seed(world, org)
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
    # An EHR launch, from this organization's own EHR tenant.
    org.launch = client_of(world.app)
    started = world.call(
        org,
        org.launch,
        ("GET", "/smart/launch"),
        "/smart/launch",
        params={"iss": org.ehr.iss, "launch": "xyz", "pack": REFERRAL},
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
    # Decisions: pending (a review's), routine (an outcome's), spare (an erasure's); FHIR, the
    # guard, CDS Hooks (PAS and DTR read it) and the launch.
    service = org.keys["service"]
    for kind, words in (
        ("pending", f"Crushing chest pain, left arm numb {m}"),
        ("routine", f"Can I get a refill of my lisinopril {m}?"),
        ("spare", f"Please call me back about my results {m}"),
    ):
        made = world.call(
            org,
            service,
            ("POST", "/api/packs/{namespace}/{name}/decide"),
            f"/api/packs/{INBOX}/decide",
            json=message(words),
            expect=200,
        ).json()
        org.made("decision", made["id"])
        org.made(kind, made["id"])
    parameters = {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "pack", "valueString": FHIR_EXAMPLE_PACK},
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
        ("POST", "/api/packs/{namespace}/{name}/fhir/decide"),
        f"/api/packs/{FHIR_EXAMPLE_PACK}/fhir/decide",
        json=FHIR_EXAMPLE,
        expect=200,
    ).json()
    org.made("decision", packed["decision"]["id"])
    guarded = world.call(
        org,
        service,
        ("POST", "/api/packs/{namespace}/{name}/guard"),
        f"/api/packs/{GUARD}/guard",
        json={**SAFE, "question": f"{SAFE['question']} {m}"},
        expect=200,
    ).json()
    org.made("decision", guarded["decision_id"])
    hook = world.call(
        org,
        service,
        ("POST", "/cds-services/{service_id}"),
        "/cds-services/mendari.prior-auth-preflight",
        json=request("acme", COMPLETE, _order("mri", "FX101")),
        expect=200,
    ).json()
    org.made("decision", org.made("prior", hook["cards"][0]["extension"]["mendari.decision_id"]))
    org.made("order", json.dumps(hook["systemActions"][0]["resource"]))
    assert org.launch is not None
    smart = world.call(
        org,
        org.launch,
        ("POST", "/api/smart/decide"),
        "/api/smart/decide",
        json=DECIDE_BODY,
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
            json={"pack_id": GUARD, "reference": f"minutes {m}", "days": 30},
            expect=201,
        ).json()
        org.made("approval", approval["id"])
        hold = world.call(
            org,
            governance,
            ("POST", "/api/holds"),
            "/api/holds",
            json={"pack_id": GUARD, "reason": f"hold {m}"},
            expect=201,
        ).json()
        org.made("hold", hold["id"])
    run = world.call(
        org,
        admin,
        ("POST", "/api/packs/{namespace}/{name}/evals"),
        f"/api/packs/{INBOX}/evals",
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
    pas = {
        "decision_id": o.one("prior"),
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
    }
    refused(
        service,
        ("POST", "/api/packs/{namespace}/{name}/pas"),
        f"/api/packs/{PA}/pas",
        o.one("prior"),
        404,
        json=pas,
    )
    package = world.call(
        x,
        service,
        ("POST", "/fhir/Questionnaire/$questionnaire-package"),
        "/fhir/Questionnaire/$questionnaire-package",
        json=package_request({"name": "order", "resource": json.loads(o.one("order"))}),
        expect=200,
    ).json()
    assert [p["name"] for p in package["parameter"]] == ["outcome"]  # no decision of these orders
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
            {"params": {"pack_id": DECIDE_BODY["pack_id"]}},
        ),
        (("GET", "/api/smart/patient"), "/api/smart/patient", {}),
        (("POST", "/api/smart/decide"), "/api/smart/decide", {"json": DECIDE_BODY}),
    ):
        refused(x.launch, route, url, o.launch_id, 409, headers=pinned, **options)
    if world.accounts == "memberships":
        refused_people(world, x, o, refused)


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
    pack = {"pack_id": INBOX}
    total = {"_total": "accurate", "_count": 3}
    asked: list[tuple[TestClient, Route, str, dict[str, Any]]] = [
        (viewer, ("GET", "/api/audit"), "/api/audit", pack),
        (viewer, ("GET", "/api/audit"), "/api/audit", {}),
        (viewer, ("GET", "/api/audit"), "/api/audit", {**pack, "outcome": "HUMAN_REVIEW"}),
        (viewer, ("GET", "/api/audit"), "/api/audit", {**pack, "limit": 2, "offset": 2}),
        (viewer, ("GET", "/api/review"), "/api/review", pack),
        (viewer, ("GET", "/api/review"), "/api/review", {}),
        (viewer, ("GET", "/api/feedback"), "/api/feedback", {}),
        (viewer, ("GET", "/api/feedback"), "/api/feedback", {**pack, "rating": "down"}),
        (viewer, ("GET", "/api/holds"), "/api/holds", {}),
        (viewer, ("GET", "/api/governance"), "/api/governance", {}),
        (viewer, ("GET", "/api/governance"), "/api/governance", {"pack_id": GUARD}),
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
        ("GET", "/api/packs/{namespace}/{name}/feedback/export"),
        f"/api/packs/{INBOX}/feedback/export",
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
    assert x.launch is not None
    pinned = {"x-mendari-launch": x.launch_id}
    world.call(x, x.launch, ("GET", "/api/smart/session"), "/api/smart/session", expect=200)
    world.call(
        x,
        x.launch,
        ("GET", "/api/smart/focus"),
        "/api/smart/focus",
        params={"pack_id": DECIDE_BODY["pack_id"]},
        headers=pinned,
        expect=200,
    )
    world.call(
        x, x.launch, ("GET", "/api/smart/patient"), "/api/smart/patient", headers=pinned, expect=200
    )
    pas = {
        "decision_id": x.one("prior"),
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
    }
    world.call(
        x,
        service,
        ("POST", "/api/packs/{namespace}/{name}/pas"),
        f"/api/packs/{PA}/pas",
        json=pas,
        expect=200,
    )
    package = world.call(
        x,
        service,
        ("POST", "/fhir/Questionnaire/$questionnaire-package"),
        "/fhir/Questionnaire/$questionnaire-package",
        json=package_request({"name": "order", "resource": json.loads(x.one("order"))}),
        expect=200,
    ).json()
    assert "packagebundle" in [p["name"] for p in package["parameter"]]
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
    if world.accounts == "memberships":
        served_people(world, x)


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
        (viewer, ("GET", "/docs/reference"), "/docs/reference", {}),
        (viewer, ("GET", "/redoc"), "/redoc", {}),
        (viewer, ("GET", "/favicon.ico"), "/favicon.ico", {}),
        (viewer, ("GET", "/openapi.json"), "/openapi.json", {}),
        (viewer, ("GET", "/api/packs"), "/api/packs", {}),
        (viewer, ("GET", "/api/packs/{namespace}/{name}"), f"/api/packs/{INBOX}", {}),
        (
            viewer,
            ("GET", "/api/packs/{namespace}/{name}/datasets"),
            f"/api/packs/{INBOX}/datasets",
            {},
        ),
        (
            viewer,
            ("GET", "/api/packs/{namespace}/{name}/examples"),
            f"/api/packs/{INBOX}/examples",
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
    decision, then a hold of the inbox and a decision it holds (citing it, while the other's
    automation runs on); a review, an outcome and a rating; an approval, an evaluation, a key and
    a client; in memberships mode an invitation and a request to join."""
    decide, url = ("POST", "/api/packs/{namespace}/{name}/decide"), f"/api/packs/{INBOX}/decide"
    service, reviewer = x.keys["service"], x.people["reviewer"]
    free = world.call(x, service, decide, url, json=message(f"{REFILL} {x.marker}"), expect=200)
    assert free.json()["outcome"] == "AUTO", free.json()["reasons"]  # no one else's hold here
    x.made("decision", free.json()["id"])
    hold = world.call(
        x,
        x.people["governance"],
        ("POST", "/api/holds"),
        "/api/holds",
        json={"pack_id": INBOX, "reason": f"again {x.marker}"},
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
        json={"pack_id": GUARD, "reference": f"again {x.marker}", "days": 30},
        expect=201,
    ).json()
    x.made("approval", approval["id"])
    run = world.call(
        x,
        x.people["admin"],
        ("POST", "/api/packs/{namespace}/{name}/evals"),
        f"/api/packs/{INBOX}/evals",
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
    found = {value for kind, values in org.ids.items() if kind != "order" for value in values}
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
    aside), and names it as the mode says: every one in memberships mode; in claims mode those
    that named it before LP-12g, and no other."""
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
            if world.accounts == "memberships" or kind not in BOUND_WHEN_MARKED:
                assert payload["org"] == org, (route, kind)
            else:
                assert "org" not in payload, (route, kind)


def covered(world: World) -> None:
    """Every mounted route is exercised by a call here or is one the allow-list does not call;
    and the allow-list names only mounted routes, each called unless it says why not."""
    mounted = set(presets.served(world.app.routes))
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
    for url, params in (("/api/audit", {"pack_id": INBOX}), ("/api/holds", {}),
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
