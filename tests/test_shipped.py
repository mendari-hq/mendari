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
from mendari.sandbox.payer import create_payer_app
from mendari.server.app import create_app
from tests.conftest import encrypt_mode, encrypted_store, evalset_on, evalsets_are_on, member
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


# §4. No vendor or model name in product code outside src/mendari/providers/. `jev` and `llm`
# are provider types (mendari.providers), not vendors.

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
NAMES_NONE = ("0o600", "coherent", "grokking", "jev-1.13.0", "llm")


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
PAYER = "payer"
Route = tuple[str, str, str]  # (app, method, path)

CHAINED: dict[Route, tuple[str, ...]] = {
    (ENGINE, "POST", "/api/notice/acknowledge"): ("notice.acknowledged",),
    (ENGINE, "POST", "/api/packs/{namespace}/{name}/decide"): ("decision.created",),
    (ENGINE, "POST", "/api/packs/{namespace}/{name}/guard"): ("decision.created",),
    (ENGINE, "POST", "/api/packs/{namespace}/{name}/fhir/decide"): ("decision.created",),
    (ENGINE, "POST", "/fhir/$decide"): ("decision.created",),
    (ENGINE, "POST", "/cds-services/{service_id}"): ("decision.created",),
    (ENGINE, "POST", "/api/smart/decide"): ("decision.created",),
    (ENGINE, "POST", "/api/packs/{namespace}/{name}/pas"): ("pas.submitted",),  # on submit
    # The registry's own chain (`hub_events`, LP-18b).
    (REGISTRY, "POST", "/v1/namespaces"): ("namespace.created",),
    (REGISTRY, "PATCH", "/v1/namespaces/{namespace}"): ("namespace.publisher_type_set",),
    (REGISTRY, "POST", "/v1/namespaces/{namespace}/tokens"): ("token.issued",),
    (REGISTRY, "POST", "/v1/namespaces/{namespace}/keys"): ("key.added",),
    (REGISTRY, "PUT", "/v1/packs/{namespace}/{name}/{version}"): ("pack.published",),
    (REGISTRY, "POST", "/v1/packs/{namespace}/{name}/{version}/reports"): ("report.shared",),
    (REGISTRY, "POST", "/v1/packs/{namespace}/{name}/{version}/review"): ("pack.reviewed",),
    (REGISTRY, "POST", "/v1/packs/{namespace}/{name}/{version}/yank"): ("pack.yanked",),
    (REGISTRY, "POST", "/v1/packs/{namespace}/{name}/{version}/delete"): ("pack.deleted",),
    (ENGINE, "POST", "/api/governance"): ("governance.approved",),
    (ENGINE, "POST", "/api/registry-approvals"): ("registry_pack.approved",),
    (ENGINE, "DELETE", "/api/registry-approvals/{approval_id}"): ("registry_pack.revoked",),
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
    (ENGINE, "POST", "/api/packs/{namespace}/{name}/evals"): ("eval.recorded",),
}
# A GET that writes is not found by its method: these are listed by hand. That every read of a
# stored input is chained is checked over every GET route by tests/test_hosted_data.py
# (test_no_route_shows_a_stored_case_but_to_a_reader_and_every_read_is_chained); a pack's first
# use on a deployment chains `deployment.recorded` on whichever route meets it.
CHAINED_READS: dict[Route, tuple[str, ...]] = {
    (ENGINE, "GET", "/api/internal/retention"): ("input.purged", "note.erased", "retention.purged"),
    # Memberships mode: the person recorded, what waited for their verified address bound (and
    # their open request to join that organization, if any, closed).
    (ENGINE, "GET", "/auth/callback"): ("user.recorded", "membership.bound", "access.handled"),
    (ENGINE, "GET", "/api/audit/{decision_id}"): ("input.read",),  # encrypt mode, a reader
    (ENGINE, "GET", "/api/review"): ("input.read",),
    (ENGINE, "GET", "/api/feedback"): ("input.read",),
    (ENGINE, "GET", "/api/packs/{namespace}/{name}/feedback/export"): ("input.read",),
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
    (
        ENGINE,
        "POST",
        "/fhir/Questionnaire/$questionnaire-package",
    ): "a read: builds, stores nothing",
    (ENGINE, "POST", "/fhir/Task/_search"): "a read: FHIR search by POST carries the query",
    (ENGINE, "POST", "/fhir/AuditEvent/_search"): "a read: FHIR search by POST",
    (ENGINE, "POST", "/fhir/Provenance/_search"): "a read: FHIR search by POST",
    (ENGINE, "POST", "/fhir/CodeSystem/_search"): "a read: FHIR search by POST",
    (PAYER, "POST", "/Claim/$submit"): "the sandbox's synthetic payer: no store, writes nothing",
}
# Writes with no chained event yet, each with the prompt that chains it (tasks/todo.md, LP-03).
NOT_CHAINED_YET: dict[Route, str] = {}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _routes(app: FastAPI, name: str) -> set[Route]:
    """Every route by its methods (an APIRoute is a starlette Route). Anything else, a mount, a
    websocket or a host, would hide its routes here: only the dashboard's /static is allowed."""
    found: set[Route] = set()
    for route in app.routes:
        if isinstance(route, ServedRoute):
            methods = route.methods if route.methods is not None else UNSAFE | {"GET"}
            found |= {(name, method, route.path) for method in methods}
        else:
            assert isinstance(route, Mount) and route.path == "/static", (name, route)
    return found


@pytest.fixture
def routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[Route]:
    """Every route of every app, the engine with sign-in, Backend Services, memberships mode and
    hidden evaluation sets on (its most)."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://mendari.test")
    monkeypatch.setenv("MENDARI_BACKEND_SERVICES", "on")
    monkeypatch.setenv("MENDARI_ACCOUNTS", "memberships")
    evalset_on(monkeypatch)
    found: set[Route] = set()
    apps = {
        ENGINE: create_app(db_path=tmp_path / "engine.db"),
        REGISTRY: create_registry_app(tmp_path / "registry.db", admin_token="t"),
        PAYER: create_payer_app(),
    }
    with encrypted_store(tmp_path / "engine.db") as served:
        reader = member(served, org="default", roles=["viewer"], sub="evalsets-reader")
    evalsets_are_on(TestClient(apps[ENGINE], base_url="https://mendari.test"), reader)
    for name, app in apps.items():
        with TestClient(app):  # runs each app's lifespan, which closes its store
            found |= _routes(app, name)
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

# The mendari.co page (web/, its own Vercel project): its header goes in with the owner's next
# change there, so a comment alone does not redeploy the public site (tasks/todo.md, LP-03).
LICENCE_LATER = {"web/index.html", "web/style.css"}


def test_every_tracked_source_file_names_its_licence() -> None:
    missing = []
    for path in tracked("."):
        if Path(path).suffix not in COMMENTS or path.startswith(NOT_OURS + VERBATIM):
            continue
        head = (ROOT / path).read_text(encoding="utf-8").splitlines()[:2]
        if not any(IDENTIFIER in line for line in head) and path not in LICENCE_LATER:
            missing.append(path)
    assert missing == []
    for path in LICENCE_LATER:  # each still a gap: once it has its line, it comes off the list
        head = (ROOT / path).read_text(encoding="utf-8").splitlines()[:2]
        assert not any(IDENTIFIER in line for line in head), f"{path}: take it off LICENCE_LATER"
