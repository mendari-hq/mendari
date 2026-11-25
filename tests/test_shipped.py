# SPDX-License-Identifier: Apache-2.0
"""The Definition of shipped (notes/vision-audit.md, Phase 4), as inventories: one test per rule
the repository can check by itself (LP-03). A rule's known gap is an expected failure naming the
prompt that closes it, so the gap shows, and the marker has to go when it closes."""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.routing import Route as ServedRoute

import mendari
from mendari.hub.registry import create_registry_app
from mendari.server.app import create_app
from tests.conftest import (
    encrypt_mode,
    encrypted_store,
)
from tests.test_spdx import COMMENTS, IDENTIFIER, NOT_OURS, VERBATIM

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "src/mendari/"
VENDORED = "src/mendari/dashboard/vendor/"
SANDBOX = "src/mendari/sandbox/"
PROVIDERS = "src/mendari/providers/"


def tracked(*paths: str) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", *paths], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [path for path in listed.split("\0") if path]


# §2. No mock, stub, fake or placeholder in product code; fakes live in tests/.

MOCK_MODULES = ("mock", "unittest.mock", "pytest_mock")
DOUBLE = re.compile(r"mock|stub|fake", re.I)
JS_DOUBLE = re.compile(r"\b(class|function)\s+\w*(mock|stub|fake)", re.I)
TO_DO = re.compile(r"\b(TODO|FIXME)\b")


def _texts(*skip: str) -> Iterator[tuple[str, str]]:
    """Each tracked text file of product code (a binary one, the icon, is not read)."""
    for path in tracked(PRODUCT):
        if not path.startswith((VENDORED, *skip)):
            try:
                yield path, (ROOT / path).read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue


def _imports(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return []


def test_no_mock_stub_fake_or_todo_in_product_code() -> None:
    found = []
    for path, text in _texts(SANDBOX):  # the sandbox is synthetic on purpose
        for number, line in enumerate(text.splitlines(), 1):
            if TO_DO.search(line) or (path.endswith(".js") and JS_DOUBLE.search(line)):
                found.append(f"{path}:{number}: {line.strip()}")
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(text)):
            for name in _imports(node):
                if name in MOCK_MODULES or name.startswith("unittest.mock"):
                    found.append(f"{path}:{getattr(node, 'lineno', '?')}: imports {name}")
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and (
                DOUBLE.search(node.name)
            ):
                found.append(f"{path}:{node.lineno}: {node.name}")
    assert found == []


@pytest.mark.parametrize(
    "source",
    ["from unittest import mock", "import unittest.mock as m", "from unittest.mock import patch"],
)
def test_the_scan_sees_each_way_to_import_a_mock(source: str) -> None:
    (node,) = ast.parse(source).body
    assert any(n in MOCK_MODULES or n.startswith("unittest.mock") for n in _imports(node))


# §4. No vendor or model name in product code outside src/mendari/providers/. `llm` is a
# provider type (mendari.providers), not a vendor.

# Distinctive names match as the start of a word (gpt-4o, llama3, OPENAI_API_KEY, ClaudeClient);
# names that begin ordinary words (coherent, grokking) only as whole words; the o-series alone.
VENDOR = re.compile(
    r"(?<![a-z])(anthropic|openai|claude|chatgpt|gpt-?\d|gpt-oss|gemini|gemma|mistral|mixtral|"
    r"llama|deepseek|qwen|ollama|openrouter|typesafe|huggingface)"
    r"|(?<![a-z])(google|cohere|bedrock|grok|sonnet|haiku|opus|groq|perplexity)(?![a-z])"
    r"|(?<![\w.])o[1-9](?:-mini|-pro)?(?![\w.])",
    re.I,
)
# Each a vendor named for something other than a model: a sign-in provider, a library's name.
NAMED_FOR_ANOTHER_REASON = {
    ("src/mendari/core/patterns.py", "google-re2"),  # the RE2 library's package name
    ("src/mendari/server/auth.py", "(Google, say)"),  # an identity provider, for sign-in
    ("src/mendari/server/auth.py", "Google's token and key endpoints"),
    ("src/mendari/server/vercel.py", "e.g. Google's"),
}


def _vendor_lines() -> Iterator[tuple[str, int, str]]:
    for path, text in _texts(PROVIDERS):
        for number, line in enumerate(text.splitlines(), 1):
            if VENDOR.search(line):
                yield path, number, line


def _without_exceptions(path: str, line: str) -> str:
    for where, text in NAMED_FOR_ANOTHER_REASON:
        if where == path:
            line = line.replace(text, "")
    return line


NAMES_ONE = (
    "gpt-4o", "gpt4o", "gpt-35-turbo", "llama3.1:8b", "qwen2.5", "gemma3", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "ClaudeClient", "geminiPro", "opus", '"o1"', "o3-mini",
)  # fmt: skip
NAMES_NONE = ("0o600", "coherent", "grokking", "llm")


@pytest.mark.parametrize(
    ("text", "names_one"), [*((t, True) for t in NAMES_ONE), *((t, False) for t in NAMES_NONE)]
)
def test_the_vendor_pattern(text: str, names_one: bool) -> None:
    assert bool(VENDOR.search(text)) is names_one


def test_no_vendor_or_model_name_in_product_code_outside_the_providers() -> None:
    lines = list(_vendor_lines())
    named = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in lines
        if VENDOR.search(_without_exceptions(path, line))  # what else the line names
    ]
    assert named == []
    # Each exception is still there: one that no longer matches comes off the list.
    for where, text in NAMED_FOR_ANOTHER_REASON:
        assert any(path == where and text in line for path, _, line in lines), (where, text)


# §5. Every state-changing route appends a chained event holding ids and hashes, or says here
# why it appends none. A new route fails until it is listed; a listed one that is gone fails too.

ENGINE = "engine"
REGISTRY = "registry"
Route = tuple[str, str, str]  # (app, method, path)

CHAINED: dict[Route, tuple[str, ...]] = {
    (ENGINE, "POST", "/api/notice/acknowledge"): ("notice.acknowledged",),
    (ENGINE, "POST", "/api/agents/{namespace}/{name}/decide"): ("decision.created",),
    (ENGINE, "POST", "/api/agents/{namespace}/{name}/fhir/decide"): ("decision.created",),
    (ENGINE, "POST", "/fhir/$decide"): ("decision.created",),
    (ENGINE, "POST", "/cds-services/{service_id}"): ("decision.created",),
    (ENGINE, "POST", "/cds-services/{service_id}/feedback"): ("card.feedback",),
    (ENGINE, "POST", "/api/smart/decide"): ("decision.created",),
    # A caller's question about one action for a recorded decision (spec 0.4): chained the
    # first time it is asked; asking again answers how it stands and chains nothing.
    (ENGINE, "POST", "/api/agents/{namespace}/{name}/actions"): ("action.proposed",),
    # The registry's own chain (`hub_events`, LP-18b): only a namespace's publisher writes over
    # its routes (LP-19g); every other act is a reviewer's, below, or the operator's commands.
    (REGISTRY, "POST", "/v1/namespaces/{namespace}/keys"): ("key.added",),
    (REGISTRY, "PUT", "/v1/agents/{namespace}/{name}/{version}"): ("agent.published",),
    # A verified site's signed report, sent with its ticket's secret (LP-22f).
    (REGISTRY, "POST", "/v1/agents/{namespace}/{name}/{version}/reports"): (
        "report.shared",
        "site.ticket_reported",
    ),
    (REGISTRY, "POST", "/v1/agents/{namespace}/{name}/{version}/yank"): ("agent.yanked",),
    # A publisher's signed evaluation of a listed version (ADR-0020); a later one supersedes.
    (REGISTRY, "POST", "/v1/agents/{namespace}/{name}/{version}/evaluations"): (
        "evaluation.published",
        "evaluation.superseded",
    ),
    # A verified site, signing with its own key (LP-22e).
    (REGISTRY, "POST", "/v1/tickets"): ("site.ticket_issued", "site.correction_opened"),
    (REGISTRY, "POST", "/v1/tickets/{ticket_id}/withdraw"): ("site.ticket_withdrawn",),
    # The registry under the engine (LP-19d): an organization's administrators, in the
    # registry's own chain. The mounted registry's routes are the registry's, above.
    (ENGINE, "POST", "/api/registry/namespaces"): ("namespace.created", "namespace.claimed"),
    (ENGINE, "POST", "/api/registry/namespaces/{namespace}/keys"): ("key.added",),
    (ENGINE, "POST", "/api/registry/tokens"): ("token.issued",),
    (ENGINE, "DELETE", "/api/registry/tokens/{token_id}"): ("token.revoked",),
    # Sites (LP-22i): an organization's administrators register them and their keys.
    (ENGINE, "POST", "/api/registry/sites"): ("site.registered",),
    (ENGINE, "POST", "/api/registry/sites/{site_id}/keys"): ("site.key_added",),
    (ENGINE, "DELETE", "/api/registry/sites/{site_id}/keys/{key_id}"): ("site.key_revoked",),
    # Its reviewers (LP-19e): the registry's acts on any namespace, by a person.
    (ENGINE, "POST", "/api/registry/review/namespaces/{namespace}"): (
        "namespace.approved",
        "namespace.refused",
    ),
    (ENGINE, "POST", "/api/registry/review/namespaces/{namespace}/publisher-type"): (
        "namespace.publisher_type_set",
    ),
    (ENGINE, "POST", "/api/registry/review/sites/{site_id}"): ("site.verified", "site.refused"),
    (ENGINE, "POST", "/api/registry/review/agents/{namespace}/{name}/{version}"): (
        "agent.reviewed",
    ),
    (ENGINE, "POST", "/api/registry/review/agents/{namespace}/{name}/{version}/recall"): (
        "agent.yanked",
    ),
    (ENGINE, "POST", "/api/registry/review/agents/{namespace}/{name}/{version}/delete"): (
        "agent.deleted",
    ),
    (ENGINE, "POST", "/api/governance"): ("governance.approved",),
    (ENGINE, "POST", "/api/registry-approvals"): ("registry_agent.approved",),
    (ENGINE, "DELETE", "/api/registry-approvals/{approval_id}"): ("registry_agent.revoked",),
    (ENGINE, "DELETE", "/api/governance/{approval_id}"): ("governance.revoked",),
    (ENGINE, "POST", "/api/holds"): ("automation.held",),
    (ENGINE, "DELETE", "/api/holds/{hold_id}"): ("automation.lifted",),
    (ENGINE, "POST", "/api/review/{decision_id}"): ("review.resolved",),
    (ENGINE, "POST", "/api/audit/{decision_id}/erase"): ("input.erased", "note.erased"),
    (ENGINE, "POST", "/api/audit/{decision_id}/outcome"): ("outcome.recorded",),
    (ENGINE, "POST", "/api/audit/{decision_id}/feedback"): ("feedback.recorded",),
    (ENGINE, "POST", "/api/auth/keys"): ("key.created",),
    (ENGINE, "POST", "/api/auth/keys/{key_id}/rotate"): ("key.created", "key.rotated"),
    (ENGINE, "DELETE", "/api/auth/keys/{key_id}"): ("key.revoked",),
    (ENGINE, "POST", "/api/auth/clients"): ("client.registered",),
    (ENGINE, "POST", "/api/auth/clients/{client_id}/keys"): ("client.keys_replaced",),
    (ENGINE, "DELETE", "/api/auth/clients/{client_id}"): ("client.revoked",),
    # Memberships mode: the request, and the person who asked the first time.
    (ENGINE, "POST", "/api/auth/access-requests"): ("access.requested", "user.recorded"),
    # Memberships mode: an organization's administrators managing its people; accepting an
    # invitation, with the person who accepts the first time and their open request to join
    # that organization, closed.
    (ENGINE, "POST", "/api/auth/invitations"): ("invitation.created",),
    (ENGINE, "DELETE", "/api/auth/invitations/{invitation_id}"): ("invitation.revoked",),
    (ENGINE, "POST", "/api/auth/invitations/accept"): (
        "invitation.accepted",
        "membership.created",
        "user.recorded",
        "access.handled",
    ),
    (ENGINE, "PUT", "/api/auth/members/{membership_id}/roles"): ("membership.roles_changed",),
    (ENGINE, "DELETE", "/api/auth/members/{membership_id}"): (
        "membership.removed",
        "invitation.revoked",
    ),
    (ENGINE, "POST", "/api/auth/access-requests/{request_id}"): (
        "access.handled",
        "membership.created",
    ),
    (ENGINE, "POST", "/api/agents/{namespace}/{name}/evals"): ("eval.recorded",),
    # Memberships mode under PHI mode encrypt: a grader's grades of a written output, as their
    # commitment (ADR-0025).
    (ENGINE, "POST", "/api/written/labelling/{assignment_id}"): ("written.graded",),
    # An organization's agent store (LP-20f; MENDARI_AGENT_STORE=on, memberships mode): each new
    # version; a draft withdrawn; authors' keys.
    (ENGINE, "POST", "/api/store/versions"): ("store_version.created",),
    (ENGINE, "POST", "/api/store/uploads/{upload_id}/complete"): ("store_version.created",),
    (ENGINE, "POST", "/api/store/copies"): ("store_version.created",),
    (ENGINE, "POST", "/api/store/versions/{digest}/edits"): ("store_version.created",),
    (ENGINE, "DELETE", "/api/store/versions/{digest}"): ("store_version.withdrawn",),
    (ENGINE, "POST", "/api/store/keys"): ("store_key.added",),
    (ENGINE, "DELETE", "/api/store/keys/{key_id}"): ("store_key.revoked",),
    # Its datasets and evaluation jobs (LP-20g): a job's last slice records its report.
    (ENGINE, "POST", "/api/store/datasets"): ("store_dataset.created",),
    (ENGINE, "POST", "/api/store/jobs"): ("store_job.created",),
    (ENGINE, "POST", "/api/store/jobs/{job_id}/advance"): ("eval.recorded",),
    # Promotion (LP-20h): the version served, and the one it replaced retired with it.
    (ENGINE, "POST", "/api/store/promotions"): ("store_version.promoted", "store_version.retired"),
    (ENGINE, "POST", "/api/store/promotions/{promotion_id}/retire"): ("store_version.retired",),
    # The improvement dataset (LP-20i): each input read chained, the dataset with counts only;
    # a proposed test accepted or dismissed.
    (ENGINE, "POST", "/api/store/agents/{namespace}/{name}/improvement"): (
        "store_dataset.created",
        "input.read",
    ),
    (ENGINE, "POST", "/api/store/versions/{digest}/proposed-tests/{dataset_id}/{case_id}"): (
        "store_dataset.proposal_accepted",
        "store_dataset.proposal_dismissed",
    ),
    # A version added from the deployment's registry (MENDARI_REGISTRY_URL, with the store on):
    # verified, then kept as any new version is.
    (ENGINE, "POST", "/api/store/pulls"): ("store_version.created",),
    # An agent's connection for an organization (ADR-0021): set or changed, and cleared.
    # An operator may allow its origin too, where the deployment's list does not name it.
    (ENGINE, "PUT", "/api/connections/{namespace}/{name}"): (
        "agent_connection.set",
        "agent_connection.origin_allowed",
    ),
    (ENGINE, "DELETE", "/api/connections/{namespace}/{name}"): ("agent_connection.cleared",),
}
# A GET that writes is not found by its method: these are listed by hand. That every read of a
# stored input is chained is checked over every GET route by tests/test_hosted_data.py
# (test_no_route_shows_a_stored_case_but_to_a_reader_and_every_read_is_chained); an agent's first
# use on a deployment chains `deployment.recorded` on whichever route meets it.
CHAINED_READS: dict[Route, tuple[str, ...]] = {
    (ENGINE, "GET", "/api/internal/retention"): ("input.purged", "note.erased", "retention.purged"),
    # Memberships mode: the person recorded, what waited for their verified address bound (and
    # their open request to join that organization, if any, closed); and, once, a fresh
    # install's first administrator (MENDARI_BOOTSTRAP_ADMIN): the organization's record and
    # their administrator's membership, or their roles when they held another.
    (ENGINE, "GET", "/auth/callback"): (
        "user.recorded",
        "membership.bound",
        "access.handled",
        "organization.created",
        "membership.created",
        "membership.roles_changed",
    ),
    (ENGINE, "GET", "/api/audit/{decision_id}"): ("input.read",),  # encrypt mode, a reader
    (ENGINE, "GET", "/api/review"): ("input.read",),
    (ENGINE, "GET", "/api/feedback"): ("input.read",),
    (ENGINE, "GET", "/api/agents/{namespace}/{name}/feedback/export"): ("input.read",),
    # A grader shown their next written output, each time, before it is sent (ADR-0025).
    (ENGINE, "GET", "/api/written/labelling/next"): ("written.item_read",),
}
NOT_CHAINED: dict[Route, str] = {
    (ENGINE, "POST", "/auth/logout"): "sign-out: ends a session (security log: auth.logout)",
    (ENGINE, "POST", "/api/auth/organization"): (
        "a session's state: which of the person's own memberships it acts for (security log: "
        "auth.organization.selected)"
    ),
    (ENGINE, "POST", "/api/smart/logout"): "sign-out: ends an EHR launch's session",
    (ENGINE, "POST", "/auth/token"): (
        "a five-minute access token for a registered client, like a session; the client's "
        "registration and revocation are chained (security log: auth.token.issued)"
    ),
    (ENGINE, "POST", "/fhir/Task/_search"): "a read: FHIR search by POST carries the query",
    (ENGINE, "POST", "/fhir/AuditEvent/_search"): "a read: FHIR search by POST",
    (ENGINE, "POST", "/fhir/Provenance/_search"): "a read: FHIR search by POST",
    (ENGINE, "POST", "/fhir/CodeSystem/_search"): "a read: FHIR search by POST",
    (ENGINE, "POST", "/api/store/uploads"): (
        "an archive begun in parts, kept until it is joined or a day old; the version it makes "
        "is chained (LP-20f)"
    ),
    (ENGINE, "PUT", "/api/store/uploads/{upload_id}/parts/{n}"): (
        "one part of an archive under way; the version it makes is chained (LP-20f)"
    ),
    (ENGINE, "POST", "/api/store/versions/{digest}/tests"): (
        "a read: runs a draft's behaviour tests, stores nothing"
    ),
    (ENGINE, "POST", "/api/connections/{namespace}/{name}/check"): (
        "a read: sends one synthetic example to the agent's service and says what came back; "
        "stores nothing (security log: admin.connection.checked)"
    ),
    # The System view's actions (the deployment's operators): none changes what an organization
    # owns. What they leave is outside the chain, as the alerts sent are.
    (ENGINE, "POST", "/api/system/audit/verify"): (
        "a read of the whole chain: when it ran and how it ended are kept outside it "
        "(`system_runs`), and a chain that does not verify is alerted (security log: "
        "system.audit.verified, audit.chain.broken)"
    ),
    (ENGINE, "POST", "/api/system/alerts/test"): (
        "one test alert to the operator's webhook, recorded with the alerts sent "
        "(`alert_deliveries`, outside the chain; security log: system.alert.tested)"
    ),
    (ENGINE, "POST", "/api/system/ehr-check"): (
        "a read of an EHR's public configuration and keys: nothing changes but when it was "
        "last checked (`system_runs`; security log: system.ehr.checked)"
    ),
    (ENGINE, "POST", "/api/internal/backup"): (
        "a backup reported by the schedule that made it: when it ran and whether it worked "
        "(`system_runs`, outside the chain; security log: backup.reported)"
    ),
}
# Writes with no chained event yet, each with the prompt that chains it (tasks/todo.md, LP-03).
NOT_CHAINED_YET: dict[Route, str] = {}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
REGISTRY_PAGES = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def _routes(app: FastAPI, name: str, mounted: set[Route] | None = None) -> set[Route]:
    """Every route by its methods (an APIRoute is a starlette Route). Anything else, a mount, a
    websocket or a host, would hide its routes here: only the dashboard's /static is allowed,
    and the registry under the engine (LP-19d), whose routes must be the registry's own
    (`mounted`), which are listed as the registry's."""
    found: set[Route] = set()
    for route in app.routes:
        if isinstance(route, ServedRoute):
            methods = route.methods if route.methods is not None else UNSAFE | {"GET"}
            found |= {(name, method, route.path) for method in methods}
        elif isinstance(route, Mount) and route.path == "/registry" and mounted is not None:
            assert isinstance(route.app, FastAPI), route
            within = {(method, path) for _, method, path in _routes(route.app, name)}
            alone = {(method, path) for _, method, path in mounted}
            # All of them, but FastAPI's own pages, which a mounted registry does not serve.
            assert within <= alone and {path for _, path in alone - within} == REGISTRY_PAGES
        else:
            assert isinstance(route, Mount) and route.path == "/static", (name, route)
    return found


@pytest.fixture
def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[Route]:
    """Every route of every app, the engine with sign-in, Backend Services and memberships mode
    on (its most)."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    monkeypatch.setenv("MENDARI_REGISTRY", "on")  # the registry mounted, with its people's routes
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", "registry-review")  # and its reviewers'
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")  # organizations' agent stores (LP-20f)
    # A registry to add agents from (never reached here: no route is called).
    monkeypatch.setenv("MENDARI_REGISTRY_URL", "https://hub.example.org")
    with encrypted_store(tmp_path / "engine.db") as before:
        before.create_organization(org="registry-review", name="Registry review", created_by="t")
    found: set[Route] = set()
    apps = {
        ENGINE: create_app(db_path=tmp_path / "engine.db"),
        REGISTRY: create_registry_app(tmp_path / "registry.db"),
    }
    with TestClient(apps[REGISTRY]):  # runs the app's lifespan, which closes its store
        registrys = _routes(apps[REGISTRY], REGISTRY)
    for name, app in apps.items():
        with TestClient(app):
            found |= _routes(app, name, registrys if name == ENGINE else None)
    apps[ENGINE].state.registry_store.close()
    return found


def _recorded_events() -> set[str]:
    """The event names the code chains (as tests/test_audit_fhir.py reads them)."""
    recorded: set[str] = set()
    for path in (ROOT / "src" / "mendari").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name):
                # A store's method, or the registry's own `record_event` (`hub/registry.py`).
                called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                position = {"_append": 2, "_append_many": 1, "record_event": 1}.get(called)
                if position is not None and len(node.args) > position:
                    event = node.args[position]
                    if isinstance(event, ast.Constant):
                        recorded.add(str(event.value))
    return recorded


def test_every_state_changing_route_is_in_the_inventory(routes: set[Route]) -> None:
    listed = [*CHAINED, *NOT_CHAINED, *NOT_CHAINED_YET]
    assert len(listed) == len(set(listed))  # each route in one list only
    unsafe = {route for route in routes if route[1] in UNSAFE}
    assert unsafe - set(listed) == set(), "a new route: list it with its chained event"
    assert set(listed) - unsafe == set(), "a listed route is gone: take it off"
    assert set(CHAINED_READS) <= routes


def test_the_inventory_names_events_the_code_chains() -> None:
    recorded = _recorded_events()
    for route, events in {**CHAINED, **CHAINED_READS}.items():
        assert set(events) <= recorded, route


def test_every_write_appends_a_chained_event() -> None:
    assert NOT_CHAINED_YET == {}


# §11. One version: the package is the project.


def test_the_package_version_is_the_projects() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert mendari.__version__ == project["version"]


# §10. Every tracked source file names its licence (tests/test_spdx.py holds the rule for src,
# tests, scripts and hub; this covers the whole repository).


def test_every_tracked_source_file_names_its_licence() -> None:
    missing = []
    for path in tracked("."):
        if Path(path).suffix not in COMMENTS or path.startswith(NOT_OURS + VERBATIM):
            continue
        head = (ROOT / path).read_text(encoding="utf-8").splitlines()[:2]
        if not any(IDENTIFIER in line for line in head):
            missing.append(path)
    assert missing == []
