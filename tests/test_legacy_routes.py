# SPDX-License-Identifier: Apache-2.0
"""The routes renamed in 2026-11 (`server/paths.py`) answer at the path they had before, and a
former path is never less protected than the current one, nor more.

- **One handler, two paths**: every renamed route of the engine (with everything it mounts on)
  and of the registry is served at both, the former one left out of the API description.
- **One rule per route**: the permission a route needs, the scope that reaches it, the limit
  it counts against, whether it counts against the daily cap and how it answers an error are
  the same at both paths, checked against this file's own table of what each route needs.
- **The same answers and the same refusals over HTTP**, for callers of every kind: signed out,
  a person of each role, another organization's, a person of no organization, keys with and
  without scopes, another site's request, a caller over a limit or over its daily cap, and one
  who has not acknowledged the notice.

`tests/conftest.py` holds the table of renamed routes, written out and not computed from the
helper; `tests/test_auth.py` and `tests/test_limits.py` hold every principal and every limit
class to it too. On SQLite and Postgres."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.routing import Mount, Route

from mendari.core.audit import AuditStore
from mendari.core.evalset_keys import EvalsetKeys
from mendari.core.pack import load_pack
from mendari.hub import archive
from mendari.hub.registry import _rate_class, create_registry_app
from mendari.server import paths
from mendari.server.app import fhir_route, mounted_routes
from mendari.server.auth import permission_for
from mendari.server.limits import DailyCap, Limiter, limit_settings
from mendari.server.origin import OWN_REQUEST
from mendari.server.presets import served as preset_routes
from mendari.server.scopes import DECIDE, OUTCOME, needed
from tests.conftest import (
    DASHBOARD,
    ENGINE_FORMER_ROUTES,
    FORMER_ROUTES,
    REGISTRY_FORMER_ROUTES,
    REVIEW_FORMER_ROUTES,
    STORE_FORMER_ROUTES,
    Registry,
    encrypted_store,
    evalset_on,
    security_lines,
)
from tests.test_hub_client import SHIPPED
from tests.test_registry_mount import CROSS_SITE, World, bearer, served, sign_in_on
from tests.test_registry_review import REVIEW_ORG, review_org
from tests.test_store_uploads import Store, pack_dir, upload

RouteKey = tuple[str, str]
INBOX = "mendari/patient-inbox-router"
ROUTINE = {"message": {"body": "Can you send a refill for metformin to my pharmacy?"}}

# -- this file's own table: what each renamed route needs, at its current path ------------------
# (permission, the scope that reaches it, the limit it counts against, whether the guard counts
# it against the daily cap, whether it is a FHIR route). Nothing here is read from the code.
Needs = tuple[str, str | None, str | None, bool, bool]
AGENT = "/api/agents/{namespace}/{name}"
STORE = "/api/store/agents"
REVIEW = "/api/registry/review/agents/{namespace}/{name}/{version}"
NEEDS: dict[RouteKey, Needs] = {
    ("GET", "/api/agents"): ("discover", DECIDE, None, False, False),
    ("GET", AGENT): ("read", None, None, False, False),
    ("GET", f"{AGENT}/datasets"): ("read", None, None, False, False),
    ("GET", f"{AGENT}/examples"): ("read", None, None, False, False),
    ("POST", f"{AGENT}/decide"): ("decide", DECIDE, "decide", True, False),
    ("POST", f"{AGENT}/guard"): ("decide", DECIDE, "guard", True, False),
    ("POST", f"{AGENT}/fhir/decide"): ("decide", DECIDE, "decide", True, True),
    ("GET", f"{AGENT}/validation-brief"): ("read", None, None, False, False),
    ("POST", f"{AGENT}/evals"): ("evaluate", None, "evaluate", False, False),
    ("GET", f"{AGENT}/feedback/export"): ("admin", None, "export", False, False),
    ("POST", f"{AGENT}/pas"): ("decide", DECIDE, "decide", False, False),
    ("GET", STORE): ("author", None, None, False, False),
    ("GET", f"{STORE}/{{namespace}}/{{name}}"): ("author", None, None, False, False),
    ("POST", f"{STORE}/{{namespace}}/{{name}}/improvement"): (
        "author",
        None,
        "store",
        False,
        False,
    ),
    ("GET", REVIEW): ("registry_review", None, None, False, False),
    ("POST", REVIEW): ("registry_review", None, "label", False, False),
    ("POST", f"{REVIEW}/recall"): ("registry_review", None, "label", False, False),
    ("POST", f"{REVIEW}/delete"): ("registry_review", None, "label", False, False),
}
# The registry's own limit for each of its renamed routes (`hub/registry.py` `_rate_class`).
REGISTRY_CLASS = {
    route: "registry_read"
    if route[0] == "GET"
    else "registry_report"
    if route[1].endswith("/{version}/reports")
    else "registry_write"
    for route in REGISTRY_FORMER_ROUTES.values()
}


# Routes made after the rename, under a prefix that was renamed: they never had another path,
# so none answers at a former spelling (a former path is for a caller from before, and these
# have none). Written out here, as the renamed routes are in `tests/conftest.py`: the engine's
# and the registry's, as those are.
ENGINE_SINCE_THE_RENAME: set[RouteKey] = {
    ("GET", "/api/agents/{namespace}/{name}/committee-packet"),
}
REGISTRY_SINCE_THE_RENAME: set[RouteKey] = {
    ("GET", "/v1/agents/{namespace}/{name}/evaluation"),
    ("GET", "/v1/agents/{namespace}/{name}/badge.svg"),
    ("GET", "/v1/agents/{namespace}/{name}/{version}/evaluations/{evaluation_id}"),
}
SINCE_THE_RENAME = ENGINE_SINCE_THE_RENAME | REGISTRY_SINCE_THE_RENAME


def concrete(template: str, **values: str) -> str:
    """A route's template as a path a request has, each `{name}` filled in."""
    given = {"namespace": "acme", "name": "inbox", "version": SHIPPED, "report_id": "r1", **values}
    return re.sub(r"\{(\w+)\}", lambda found: given[found[1]], template)


def test_the_table_of_what_each_route_needs_covers_every_renamed_route() -> None:
    assert set(NEEDS) == set(ENGINE_FORMER_ROUTES.values())
    assert len(ENGINE_FORMER_ROUTES) == 18 and len(REGISTRY_FORMER_ROUTES) == 12
    assert set(REGISTRY_CLASS.values()) == {"registry_read", "registry_report", "registry_write"}


# -- the helper against the tests' own table ----------------------------------------------------


def test_the_helper_reads_each_former_path_as_its_current_one_and_back() -> None:
    renamed = {**ENGINE_FORMER_ROUTES, **REGISTRY_FORMER_ROUTES}
    for (method, old), (same_method, now) in renamed.items():
        assert method == same_method
        assert paths.current(old) == now and paths.former(now) == old, (old, now)
        assert paths.is_former(old) and not paths.is_former(now)
        assert paths.current(now) == now and paths.former(old) is None  # never twice
        assert paths.current(concrete(old)) == concrete(now)
    # Under the engine the registry is mounted at /registry.
    for (_, old), (_, now) in REGISTRY_FORMER_ROUTES.items():
        assert paths.current(f"/registry{old}") == f"/registry{now}"
    # Whole segments only, from the start of the path, as it is written.
    for other in (
        "/api/packsaddle",
        "/api/packs-old/a/b/decide",
        "/x/api/packs",
        "/API/PACKS",
        "/api//packs",
        "/api/store/packs2",
        "/v1/packs.json",
        "/api/health",
        "/",
        "",
    ):
        assert paths.current(other) == other and not paths.is_former(other), other
    assert paths.current("/api/packs/") == "/api/agents/"  # a trailing slash stays one
    assert paths.former("/api/health") is None


def test_only_a_renamed_route_is_registered_through_the_helper() -> None:
    with pytest.raises(ValueError, match="no former path"):
        paths.route(FastAPI().get, "/api/health")


# -- one handler, two paths ---------------------------------------------------------------------


@pytest.fixture
def world(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The engine with everything it mounts on: sign-in in memberships mode, Backend Services,
    the registry and its reviewers, the agent store, hidden evaluation sets."""
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_REGISTRY", "on")
    monkeypatch.setenv("MENDARI_REGISTRY_REVIEW_ORG", REVIEW_ORG)
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    evalset_on(monkeypatch)
    review_org(db_target)
    with served(db_target, tmp_path) as served_world:
        yield served_world


def _routes(app: Any) -> dict[RouteKey, Route]:
    return {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, Route)
        for method in route.methods or ()
        if method != "HEAD"
    }


def _held_to_one_handler(app: Any, renamed: dict[RouteKey, RouteKey]) -> None:
    found = _routes(app)
    for old, now in renamed.items():
        assert old in found and now in found, (old, now)
        before, after = found[old], found[now]
        assert before.endpoint is after.endpoint, old  # one handler, never a copy
        assert before.methods == after.methods
        assert (before.include_in_schema, after.include_in_schema) == (False, True), old
        for what in (
            "status_code",
            "response_model",
            "response_class",
            "dependencies",
            "responses",
            "openapi_extra",
        ):
            assert getattr(before, what) == getattr(after, what), (old, what)
        # The same path parameters, in the same places.
        assert re.findall(r"\{[^}]+\}", before.path) == re.findall(r"\{[^}]+\}", after.path)
    # Nothing else answers at a former path, and nothing renamed is left without its twin: every
    # other route under a renamed prefix is one made since (`SINCE_THE_RENAME`). Such a route
    # never had another path: it has no twin, and no route is where a former spelling would be.
    assert {route for route in found if paths.is_former(route[1])} == set(renamed)
    current = {route for route in found if paths.former(route[1]) is not None}
    assert current - SINCE_THE_RENAME == set(renamed.values())
    assert not SINCE_THE_RENAME & set(renamed.values())
    former = {paths.former(path) for _, path in SINCE_THE_RENAME & current}
    assert not former & {path for _, path in found}
    # The former paths are tried in the order the current ones are (`…/tickets` before
    # `…/{version}`), so the same request matches the same route under either.
    tried = [route for route in found if route in renamed or route in renamed.values()]
    assert [renamed[route] for route in tried if route in renamed] == [
        route for route in tried if route not in renamed
    ]


def test_every_renamed_route_is_served_at_both_paths_by_one_handler(world: World) -> None:
    _held_to_one_handler(world.app, ENGINE_FORMER_ROUTES)
    (mounted,) = [r for r in world.app.routes if isinstance(r, Mount) and r.path == "/registry"]
    _held_to_one_handler(mounted.app, REGISTRY_FORMER_ROUTES)


def test_the_registry_alone_serves_both_paths_by_one_handler(registry: Registry) -> None:
    _held_to_one_handler(registry.client.app, REGISTRY_FORMER_ROUTES)


def test_a_route_made_since_the_rename_has_no_former_path(registry: Registry) -> None:
    """Each is served by the registry at its one path, and its former spelling is no route:
    it answers as any path nothing serves, with the registry's own headers."""
    found = _routes(registry.client.app)
    assert set(found) >= REGISTRY_SINCE_THE_RENAME
    assert registry.publish().status_code == 201
    values = {"namespace": "mendari", "name": "referral-router", "evaluation_id": "0" * 64}
    for method, path in sorted(REGISTRY_SINCE_THE_RENAME):
        now = registry.client.request(method, concrete(path, **values))
        assert now.status_code in (200, 404) and now.content != b'{"detail":"Not Found"}', path
        before = paths.former(path)
        assert before is not None and (method, before) not in found
        gone = registry.client.request(method, concrete(before, **values))
        if path.endswith("/{name}/evaluation") or path.endswith("/badge.svg"):
            # Read by the former `/{version}` route as a version, which it is not.
            assert gone.status_code == 404 and "no agent" in gone.json()["detail"], path
        else:
            assert (gone.status_code, gone.json()) == (404, {"detail": "Not Found"}), path
        assert gone.headers["cache-control"] == "no-store", path
    # A limit counts them as it counts every read, at any spelling.
    for method, path in sorted(REGISTRY_SINCE_THE_RENAME):
        asked = _rate_class(_registry_request(method, concrete(path, **values)), lambda _: None)
        assert asked == ("registry_read", "ip:203.0.113.9")


def test_a_former_path_is_listed_nowhere(world: World, registry: Registry) -> None:
    """Not in the API description (the engine's, with the mounted registry's routes, nor the
    registry's own), the developer landing's table of routes, or a key preset's reach."""
    reader = world.person("acme", "viewer")
    listed = set(world.http.get("/openapi.json", headers=reader).json()["paths"])
    assert not [path for path in listed if paths.is_former(path)]
    assert {path for _, path in ENGINE_FORMER_ROUTES.values()} <= listed
    assert {f"/registry{path}" for _, path in REGISTRY_FORMER_ROUTES.values()} <= listed
    alone = set(registry.client.get("/openapi.json").json()["paths"])
    assert not [path for path in alone if paths.is_former(path)]
    assert {path for _, path in REGISTRY_FORMER_ROUTES.values()} <= alone
    assert not set(ENGINE_FORMER_ROUTES) & set(mounted_routes(world.app))
    assert not set(ENGINE_FORMER_ROUTES) & set(preset_routes(world.app.routes))
    assert set(ENGINE_FORMER_ROUTES.values()) <= set(mounted_routes(world.app))
    landing = world.http.get("/docs").text
    assert "/api/agents" in landing and "/api/packs" not in landing
    presets = world.http.get("/api/auth/keys/presets", headers=world.person("acme")).json()
    reaches = [path for preset in presets for _, path in preset["reaches"] + preset["calls"]]
    assert reaches and not [path for path in reaches if paths.is_former(path)]


# -- one rule per route ---------------------------------------------------------------------------


def test_every_rule_holds_a_former_path_to_what_its_route_needs(tmp_path: Path) -> None:
    """The permission, the scope, the limit, the daily cap and the error format, each asked for
    the route's template and for a path a request has, at both spellings, against `NEEDS`."""
    limiter = Limiter(AuditStore(tmp_path / "classes.db"), limit_settings(sign_in=True))
    for old, now in ENGINE_FORMER_ROUTES.items():
        permission, scope, limit, counted, fhir = NEEDS[now]
        for method, path in (now, old, (now[0], concrete(now[1])), (old[0], concrete(old[1]))):
            where = (method, path)
            assert permission_for(method, path) == permission, where
            assert needed(method, path) == scope, where
            assert limiter.classify(method, path, permission) == limit, where
            assert DailyCap.counts(method, path) is counted, where
            assert fhir_route(path) is fhir, where
        # HEAD needs what GET needs, at either path.
        if now[0] == "GET":
            assert permission_for("HEAD", old[1]) == permission_for("HEAD", now[1]) == permission
    # A method the route does not serve needs whatever any unlisted route does, at either path.
    for old, now in ENGINE_FORMER_ROUTES.items():
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            assert permission_for(method, old[1]) == permission_for(method, now[1]), (method, old)
            assert needed(method, old[1]) == needed(method, now[1]), (method, old)
    # Where the registry is not mounted its rules are skipped, for both spellings alike.
    for old, now in REVIEW_FORMER_ROUTES.items():
        unmounted = permission_for(*old, registry=False)
        assert unmounted == permission_for(*now, registry=False) != "registry_review"


def _registry_request(method: str, path: str, root: str = "", token: str = "") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {
        "type": "http",
        "method": method,
        "path": f"{root}{path}",
        "root_path": root,
        "headers": headers,
        "client": ("203.0.113.9", 1),
    }
    return Request(scope)


def test_the_registry_counts_a_former_path_against_the_same_limit() -> None:
    def known(token: str) -> str | None:
        return "namespace:acme" if token == "ohr_known" else None

    for old, now in REGISTRY_FORMER_ROUTES.items():
        for root in ("", "/registry"):  # alone, and mounted under the engine
            for token in ("", "ohr_known", "ohr_made_up"):
                asked = [
                    _rate_class(_registry_request(method, concrete(path), root, token), known)
                    for method, path in (now, old)
                ]
                assert asked[0] == asked[1], (old, root, token)
                assert asked[0][0] == REGISTRY_CLASS[now], (old, root, token)
    # Not vacuous: a report has its own limit, by namespace for a token the registry issued.
    report = "/v1/packs/acme/inbox/1.0.0/reports"
    assert _rate_class(_registry_request("POST", report, token="ohr_known"), known) == (
        "registry_report",
        "namespace:acme",
    )


# -- the same answers and the same refusals -------------------------------------------------------

VOLATILE = {
    "id",
    "request_id",
    "created_at",
    "decision_id",
    "latency_ms",
    "resolved_at",
    "at",
    "generated_at",  # a validation brief says when it was made, to the second
}
# The same in an answer's bytes: the one thing two reads of one route may differ in.
MADE_AT = re.compile(rb'"generated_at":\s*"[^"]*"')
KEPT_HEADERS = (
    "content-type",
    "www-authenticate",
    "cache-control",
    "mendari-notice-required",
    "content-disposition",
)


def _steady(value: Any) -> Any:
    """An answer without what differs between any two calls (ids, times)."""
    if isinstance(value, dict):
        return {key: _steady(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [_steady(item) for item in value]
    return value


def _said(answer: Any) -> tuple[int, Any, dict[str, str | None]]:
    try:
        body = _steady(answer.json())
    except ValueError:
        body = answer.text
    return answer.status_code, body, {name: answer.headers.get(name) for name in KEPT_HEADERS}


def _body(route: RouteKey) -> dict[str, Any] | None:
    """A body that changes nothing where the route is reached: a decision of an input its schema
    refuses is recorded as a BLOCK (twice here, alike); every other write is refused as invalid
    before it does anything."""
    method, path = route
    if method != "POST":
        return None
    return {"dataset": "no-such-dataset"} if path.endswith("/evals") else {}


def _path(template: str) -> str:
    if template.startswith(("/api/agents", "/api/packs")):
        return concrete(template, namespace="mendari", name="patient-inbox-router")
    if template.startswith("/api/store"):
        return concrete(template, name="inbox-router")
    return concrete(template)


@pytest.fixture
def people(world: World) -> dict[str, dict[str, str] | None]:
    """Callers of every kind. `acme` has published `acme/inbox` to the registry (waiting for
    review) and holds `acme/inbox-router` in its store; `other` is another organization."""
    world.published()
    with encrypted_store(world.db) as db:
        seeded = Store(world.http, db, EvalsetKeys(b"k" * 32, b"i" * 32), {}, None, world.tmp_path)
        private, public = archive.generate_key()
        added = world.http.post(
            "/api/store/keys",
            json={"public_pem": public.decode()},
            headers=world.person("acme"),
        )
        assert added.status_code == 201, added.text
        seeded.key = archive.load_private_key(private)
        made = upload(seeded, pack_dir(world.tmp_path, "acme/inbox-router"), org="acme")
        assert made.status_code == 201, made.text
        keys = {
            name: db.create_api_key(name=name, role=role, org="acme", created_by="t", scopes=scope)
            for name, role, scope in [
                ("a service key", "service", None),
                ("a viewer key", "viewer", None),
                ("a key scoped to decide", "service", DECIDE),
                ("a key scoped to outcomes", "service", OUTCOME),
                ("a key scoped to FHIR reads", "viewer", "system/Task.rs"),
            ]
        }
    callers: dict[str, dict[str, str] | None] = {
        "signed out": None,
        "a junk key": bearer("ohk_not-a-key"),
        "a person of no organization": world.person(None, sub="nobody"),
        "another organization's administrator": world.person("other", "admin"),
        "another organization's author": world.person("other", "pack_author"),
        "a registry reviewer": world.person(REVIEW_ORG, "registry_reviewer", sub="rita"),
    }
    for role in ("viewer", "reviewer", "governance", "admin", "labeller", "pack_author"):
        callers[f"acme's {role}"] = world.person("acme", role)
    for name, key in keys.items():
        callers[name] = bearer(key["key"])
    return callers


def test_every_caller_gets_the_same_answer_at_both_paths(
    world: World, people: dict[str, dict[str, str] | None]
) -> None:
    """Every kind of caller on every renamed route of the engine, at the former path and at the
    current one: the same status, the same body and the same headers that say why."""
    seen: dict[RouteKey, dict[str, int]] = {}
    for who, headers in people.items():
        for old, now in ENGINE_FORMER_ROUTES.items():
            answers = [
                world.http.request(method, _path(path), json=_body(now), headers=headers)
                for method, path in (old, now)
            ]
            assert _said(answers[0]) == _said(answers[1]), (who, old)
            seen.setdefault(now, {})[who] = answers[0].status_code
    for route, statuses in seen.items():
        permission = NEEDS[route][0]
        # Not vacuous: on every route someone is turned away signed out, someone for what they
        # may not do, and someone gets through the guard.
        assert statuses["signed out"] == statuses["a junk key"] == 401, route
        assert statuses["a person of no organization"] == 403, route
        assert 403 in statuses.values(), route
        assert set(statuses.values()) - {401, 403}, route  # and someone reaches the route
        if permission != "registry_review":
            assert statuses["a registry reviewer"] == 403, route  # the review organization's only
        else:
            assert statuses["acme's admin"] == 403, route  # no administrator reviews by role
            assert statuses["a registry reviewer"] != 403, route
    # The refusals this change must not move, each by name.
    decide, export = ("POST", f"{AGENT}/decide"), ("GET", f"{AGENT}/feedback/export")
    evals, listed = ("POST", f"{AGENT}/evals"), ("GET", "/api/agents")
    assert seen[decide]["acme's viewer"] == 403 and seen[decide]["acme's reviewer"] == 200
    assert seen[decide]["a key scoped to decide"] == 200
    assert seen[decide]["a key scoped to outcomes"] == seen[decide]["a viewer key"] == 403
    assert seen[export]["acme's reviewer"] == seen[export]["a service key"] == 403
    assert seen[export]["acme's admin"] == 200
    assert seen[evals]["a service key"] == seen[evals]["a key scoped to decide"] == 403
    assert seen[listed]["a key scoped to decide"] == seen[listed]["acme's viewer"] == 200
    assert seen[listed]["acme's labeller"] == 403  # a labeller labels, and lists nothing
    assert seen[listed]["a key scoped to FHIR reads"] == 403  # no scope of its reaches it
    assert seen[("GET", AGENT)]["a key scoped to decide"] == 403  # a list, never a detail
    # Another organization's store, and its agent, do not exist for this one.
    mine, theirs = "acme's pack_author", "another organization's author"
    one = ("GET", f"{STORE}/{{namespace}}/{{name}}")
    assert (seen[one][mine], seen[one][theirs]) == (200, 404)
    assert seen[("GET", STORE)]["acme's admin"] == 403  # the author's role, which no admin holds


def test_a_scoped_refusal_names_the_scope_at_both_paths(
    world: World, people: dict[str, dict[str, str] | None]
) -> None:
    outcomes = people["a key scoped to outcomes"]
    for path in (f"/api/packs/{INBOX}/decide", f"/api/agents/{INBOX}/decide"):
        refused = world.http.post(path, json=ROUTINE, headers=outcomes)
        assert refused.status_code == 403, path
        challenge = refused.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge and f'scope="{DECIDE}"' in challenge


def test_reads_are_the_same_bytes_at_both_paths(
    world: World, people: dict[str, dict[str, str] | None]
) -> None:
    decided = world.http.post(
        f"/api/packs/{INBOX}/decide", json=ROUTINE, headers=people["acme's reviewer"]
    )
    assert decided.status_code == 200, decided.text
    readers = {
        **{route: "acme's admin" for route in FORMER_ROUTES if route[0] == "GET"},
        **{route: "acme's pack_author" for route in STORE_FORMER_ROUTES if route[0] == "GET"},
        **{route: "a registry reviewer" for route in REVIEW_FORMER_ROUTES if route[0] == "GET"},
    }
    assert len(readers) == 9
    for old, who in readers.items():
        now = ENGINE_FORMER_ROUTES[old]
        before = world.http.get(_path(old[1]), headers=people[who])
        after = world.http.get(_path(now[1]), headers=people[who])
        assert before.status_code == after.status_code == 200, (old, before.text, after.text)
        # Byte for byte, the second a brief was made aside: nothing names the path.
        assert MADE_AT.sub(b"", before.content) == MADE_AT.sub(b"", after.content), old
        assert before.headers["content-type"] == after.headers["content-type"]
        assert before.headers["cache-control"] == after.headers["cache-control"] == "no-store"


def test_a_decision_is_the_same_decision_at_either_path(
    world: World, people: dict[str, dict[str, str] | None], capsys: pytest.CaptureFixture[str]
) -> None:
    reviewer = people["acme's reviewer"]
    capsys.readouterr()
    answers = [
        world.http.post(f"/api/{name}/{INBOX}/decide", json=ROUTINE, headers=reviewer)
        for name in ("packs", "agents")
    ]
    assert [answer.status_code for answer in answers] == [200, 200]
    before, after = (answer.json() for answer in answers)
    assert before["id"] != after["id"]  # two decisions, each recorded
    assert _steady(before) == _steady(after)
    assert before["pack"]["id"] == INBOX  # the answer's keys are as they were
    for decision in (before, after):
        record = world.http.get(f"/api/audit/{decision['id']}", headers=reviewer)
        assert record.status_code == 200 and record.json()["outcome"] == decision["outcome"]
    # The access line names the route as it was asked for, so a former path's use can be seen.
    asked = [
        line["route"]
        for line in security_lines(capsys.readouterr().out)
        if line["event"] == "request" and line["route"].endswith("/decide")
    ]
    assert asked == [
        "/api/packs/{namespace}/{name}/decide",
        "/api/agents/{namespace}/{name}/decide",
    ]


def test_another_sites_request_is_refused_at_both_paths(
    world: World, people: dict[str, dict[str, str] | None]
) -> None:
    """A browser request another site starts, with a person's session cookie: refused before
    the guard reads the cookie, on every renamed route, at either path."""
    admin = people["acme's admin"]
    assert admin is not None
    cookie = {name: value for name, value in admin.items() if name != OWN_REQUEST}
    for old, now in ENGINE_FORMER_ROUTES.items():
        if now[0] == "GET" and NEEDS[now][0] in ("public", "page"):
            continue
        answers = [
            world.http.request(m, _path(p), json=_body(now), headers={**cookie, **CROSS_SITE})
            for m, p in (old, now)
        ]
        assert _said(answers[0]) == _said(answers[1]), old
        assert answers[0].status_code == 403, old
        assert "cross-site request refused" in answers[0].text, old  # JSON, or an OperationOutcome


def test_an_error_on_the_fhir_route_is_an_operation_outcome_at_both_paths(
    world: World, people: dict[str, dict[str, str] | None]
) -> None:
    reviewer = {**(people["acme's reviewer"] or {})}
    cases: list[dict[str, Any]] = [
        {"json": {"resourceType": "Bundle", "entry": "not a list"}},  # not FHIR R4: a 400
        {"content": b"<Bundle/>", "headers": {"content-type": "application/fhir+xml"}},  # a 415
        {"json": {"resourceType": "Patient"}, "headers": {"accept": "application/fhir+xml"}},
        {"content": b"x", "headers": {"content-length": str(64 * 1024 * 1024)}},  # too large
    ]
    for case in cases:
        sent: dict[str, Any] = {**case, "headers": {**reviewer, **case.get("headers", {})}}
        answers = [
            world.http.post(f"/api/{name}/{INBOX}/fhir/decide", **sent)
            for name in ("packs", "agents")
        ]
        assert _said(answers[0]) == _said(answers[1]), case
        assert answers[0].status_code in (400, 406, 413, 415), (case, answers[0].text)
        assert answers[0].json()["resourceType"] == "OperationOutcome", case
    # Signed out, the refusal is an OperationOutcome too.
    signed_out = [
        world.http.post(f"/api/{n}/{INBOX}/fhir/decide", json={}) for n in ("packs", "agents")
    ]
    assert _said(signed_out[0]) == _said(signed_out[1])
    assert signed_out[0].status_code == 401
    assert signed_out[0].json()["resourceType"] == "OperationOutcome"


# -- limits: one bucket for both paths ------------------------------------------------------------


@pytest.fixture
def limited(db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    sign_in_on(monkeypatch)
    monkeypatch.setenv(
        "MENDARI_RATE_LIMITS", "on,decide=2/h,guard=1/h,evaluate=1/h,export=1/h,store=1/h"
    )
    monkeypatch.setenv("MENDARI_AGENT_STORE", "on")
    evalset_on(monkeypatch)
    with served(db_target, tmp_path) as served_world:
        yield served_world


def _buckets(world: World) -> list[str]:
    with encrypted_store(world.db) as db, db._db.session(exclusive=False) as session:
        return sorted(row["bucket"] for row in session.all("SELECT bucket FROM rate_limits"))


def _refused_over_the_limit(answer: Any) -> tuple[Any, ...]:
    assert answer.status_code == 429, answer.text
    assert re.fullmatch(r"[1-9]\d*", answer.headers["retry-after"])
    said = _steady(answer.json())
    said["detail"] = re.sub(r"retry in \d+ s", "retry in N s", said["detail"])
    return said, answer.headers["cache-control"], answer.headers["content-type"]


def test_a_former_path_is_no_second_allowance(limited: World) -> None:
    """Each limit counts a route's calls in one bucket, named by its class and the caller, never
    the path: two decisions an hour is two, whichever paths they are asked at, and the third is
    refused at both in the same words."""
    with encrypted_store(limited.db) as db:
        made = db.create_api_key(name="ehr", role="service", org="acme", created_by="t")
    limited.person("acme")  # the organization's record
    key = bearer(made["key"])
    old, now = f"/api/packs/{INBOX}/decide", f"/api/agents/{INBOX}/decide"
    assert limited.http.post(old, json=ROUTINE, headers=key).status_code == 200
    assert limited.http.post(now, json=ROUTINE, headers=key).status_code == 200
    over = [limited.http.post(path, json=ROUTINE, headers=key) for path in (old, now, old)]
    refusals = [_refused_over_the_limit(answer) for answer in over]
    assert refusals[0] == refusals[1] == refusals[2]
    assert "decide: 2/h" in over[0].json()["detail"]
    assert _buckets(limited) == [f"decide|key:{made['id']}"]
    # The FHIR route's decisions are the same class: refused too, as an OperationOutcome.
    fhir = [
        limited.http.post(path.replace("/decide", "/fhir/decide"), json={}, headers=key)
        for path in (old, now)
    ]
    assert [answer.status_code for answer in fhir] == [429, 429]
    assert _steady(fhir[0].json()) == _steady(fhir[1].json())
    assert fhir[0].json()["resourceType"] == "OperationOutcome"


def test_each_limit_class_counts_both_paths_together(limited: World) -> None:
    """One call an hour of each class: asked at one path, the next at the other is over, in
    either order. (The route's own answer to the first does not matter here: the limit counts
    before the route runs.)"""
    calls: list[tuple[str, str, str, str, dict[str, Any] | None]] = [
        ("guard", "reviewer", "POST", "/api/{}/mendari/ai-guard/guard", {}),
        ("evaluate", "admin", "POST", f"/api/{{}}/{INBOX}/evals", {"dataset": "no-such-dataset"}),
        ("export", "admin", "GET", f"/api/{{}}/{INBOX}/feedback/export", None),
        ("store", "pack_author", "POST", "/api/store/{}/acme/inbox-router/improvement", {}),
    ]
    for limit, role, method, template, body in calls:
        for first, second in (("packs", "agents"), ("agents", "packs")):
            # A new caller for each order: a limit is counted per caller.
            caller = limited.person("acme", role, sub=f"{limit}-{first}")
            allowed = limited.http.request(
                method, template.format(first), json=body, headers=caller
            )
            assert allowed.status_code != 429, (limit, first, allowed.text)
            refused = limited.http.request(
                method, template.format(second), json=body, headers=caller
            )
            assert refused.status_code == 429, (limit, second, refused.text)
            assert f"{limit}: 1/h" in refused.json()["detail"]


def test_the_daily_cap_counts_both_paths_together(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_PRINCIPAL_DAILY_LIMIT", "2")
    monkeypatch.setenv("MENDARI_RATE_LIMITS", "on")
    with served(db_target, tmp_path) as world:
        reviewer = world.person("acme", "reviewer")
        old, now = f"/api/packs/{INBOX}/decide", f"/api/agents/{INBOX}/decide"
        assert world.http.post(old, json=ROUTINE, headers=reviewer).status_code == 200
        assert world.http.post(now, json=ROUTINE, headers=reviewer).status_code == 200
        over = [world.http.post(path, json=ROUTINE, headers=reviewer) for path in (old, now)]
        assert [answer.status_code for answer in over] == [429, 429]
        assert "daily limit of 2 decisions" in over[0].json()["detail"]
        assert len({_refused_over_the_limit(answer)[0]["error"] for answer in over}) == 1
        used = world.http.get("/api/health", headers=reviewer).json()["principal_quota"]
        assert used == {"daily_limit": 2, "used_today": 2}  # a refused request never counts


def test_an_unacknowledged_notice_refuses_both_paths_alike(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in_on(monkeypatch)
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    with served(db_target, tmp_path) as world:
        reviewer = world.person("acme", "reviewer")
        asked = [
            world.http.post(f"/api/{name}/{INBOX}/decide", json=ROUTINE, headers=reviewer)
            for name in ("packs", "agents")
        ]
        assert _said(asked[0]) == _said(asked[1])
        assert asked[0].status_code == 403 and asked[0].headers["mendari-notice-required"]
        assert world.http.post("/api/notice/acknowledge", headers=reviewer).status_code == 200
        again = [
            world.http.post(f"/api/{name}/{INBOX}/decide", json=ROUTINE, headers=reviewer)
            for name in ("packs", "agents")
        ]
        assert [answer.status_code for answer in again] == [200, 200]


# -- the registry ---------------------------------------------------------------------------------


def test_the_registry_answers_the_same_at_both_paths(registry: Registry) -> None:
    """Reads byte for byte, and writes refused the same, for the public, a namespace's own
    publisher and another's."""
    published = registry.publish()
    assert published.status_code == 201, published.text
    version = published.json()["version"]
    stranger = registry.namespace("stranger")
    callers = {
        "the public": {},
        "the namespace's publisher": bearer(registry.token),
        "another namespace's publisher": bearer(stranger),
        "a made-up token": bearer("ohr_made-up"),
    }
    seen: dict[RouteKey, set[int]] = {}
    for who, headers in callers.items():
        for old, now in REGISTRY_FORMER_ROUTES.items():
            values = {"namespace": "mendari", "name": "referral-router", "version": version}
            answers = [
                registry.client.request(
                    method,
                    concrete(path, **values),
                    content=b"{}" if method != "GET" else None,
                    headers=headers,
                )
                for method, path in (old, now)
            ]
            assert _said(answers[0]) == _said(answers[1]), (who, old)
            assert answers[0].content == answers[1].content, (who, old)
            assert answers[0].headers.get("x-archive-sha256") == answers[1].headers.get(
                "x-archive-sha256"
            )
            seen.setdefault(now, set()).add(answers[0].status_code)
    # Not vacuous: every read was served, and no write passed without the namespace's token.
    for route, statuses in seen.items():
        assert (200 in statuses) if route[0] == "GET" and "report_id" not in route[1] else True
        if route[0] != "GET":
            assert statuses & {401, 403} and not statuses & {200, 201}, (route, statuses)
    archive_route = ("GET", "/v1/agents/{namespace}/{name}/{version}/archive")
    assert seen[archive_route] == {200}


def test_a_publish_at_the_former_path_is_the_same_publish(registry: Registry) -> None:
    pack = load_pack(Path(__file__).parent / "fixtures" / "reference-agents" / "referral-router")
    data = archive.build(pack, registry.key)
    former = registry.client.put(
        f"/v1/packs/mendari/referral-router/{pack.version}",
        content=data,
        headers=bearer(registry.token),
    )
    assert former.status_code == 201, former.text
    again = registry.client.put(
        f"/v1/agents/mendari/referral-router/{pack.version}",
        content=data,
        headers=bearer(registry.token),
    )
    assert again.status_code == 409  # the version it already holds, whichever path sent it
    shown = [
        registry.client.get(f"/v1/{name}/mendari/referral-router/{pack.version}").json()
        for name in ("packs", "agents")
    ]
    assert shown[0] == shown[1] and shown[0]["digest"] == pack.digest


def test_the_registrys_limits_count_both_paths_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_REGISTRY_RATE_LIMITS", "on,registry_write=2/h,registry_report=1/h")
    with TestClient(create_registry_app(tmp_path / "limited.db")) as client:
        sent: dict[str, Any] = {"content": b"x", "headers": bearer("ohr_made-up")}
        writes = ["/v1/packs/a/b/1.0.0", "/v1/agents/a/b/1.0.0", "/v1/packs/a/b/1.0.0/yank"]
        assert [client.put(path, **sent).status_code for path in writes[:2]] == [403, 403]
        over = [client.post(writes[2], **sent), client.put(writes[1], **sent)]
        assert [answer.status_code for answer in over] == [429, 429]
        assert over[0].json() == over[1].json()
        # A site's report has a limit of its own, counted for both paths together too.
        reports = ["/v1/packs/a/b/1.0.0/reports", "/v1/agents/a/b/1.0.0/reports"]
        assert client.post(reports[0], **sent).status_code == 401
        refused = client.post(reports[1], **sent)
        assert refused.status_code == 429 and "registry_report" in refused.json()["detail"]


def test_the_mounted_registry_answers_the_same_at_both_paths(world: World) -> None:
    """Under the engine: the same reads, the same refusals (the engine's guard reads no
    credential there; the registry checks its own publisher token), another site's request
    refused alike, and the registry's own headers on every answer."""
    token, pack = world.published()
    world.registry.decide_claim("acme", approve=True, by="operator", note="read")
    callers: dict[str, dict[str, str]] = {
        "the public": {},
        "the publisher": bearer(token),
        "a person's session": world.person("acme"),
        "another site, with a session": {**world.person("acme"), **CROSS_SITE},
        "an engine key": bearer("ohk_not-a-publisher-token"),
    }
    for who, headers in callers.items():
        for old, now in REGISTRY_FORMER_ROUTES.items():
            answers = [
                world.http.request(
                    method,
                    f"/registry{concrete(path, version=pack.version)}",
                    content=b"{}" if method != "GET" else None,
                    headers=headers,
                )
                for method, path in (old, now)
            ]
            assert _said(answers[0]) == _said(answers[1]), (who, old)
            if answers[0].status_code == 200:  # a refusal of the engine's carries a request id
                assert answers[0].content == answers[1].content, (who, old)
            for answer in answers:
                assert answer.headers["content-security-policy"].startswith("default-src 'none'")
    served_archive = world.http.get(f"/registry/v1/packs/acme/inbox/{pack.version}/archive")
    assert served_archive.status_code == 200
    assert json.loads(world.http.get("/registry/v1/packs/acme/inbox").text)["id"] == "acme/inbox"


def test_the_dashboard_and_the_client_ask_at_the_current_paths() -> None:
    """Every caller in the repository moved: the dashboard's scripts, the API examples and the
    registry client ask at the current paths (a former path is for callers outside the
    repository, and for the Hub until the registry it reads is deployed with both)."""
    root = Path(__file__).resolve().parents[1]
    callers = [
        *sorted((root / "src" / "mendari" / "dashboard").glob("*.js")),
        *sorted((root / "src" / "mendari" / "dashboard").glob("*.html")),
        *sorted((root / "src" / "mendari" / "dashboard" / "snippets").glob("*")),
        root / "src" / "mendari" / "hub" / "client.py",
        # The Hub (`hub/lib/registry.ts`) deploys apart from the registry, so it keeps the former
        # paths for one release: it moves once every deployed registry answers the current ones.
        root / "deploy" / "status" / "checks.yaml",
        *sorted((root / "scripts").glob("*.py")),
        *sorted((root / ".github" / "workflows").glob("*.yml")),
    ]
    former = re.compile(r"/api/packs\b|/api/store/packs\b|/api/registry/review/packs\b|/v1/packs\b")
    found = [
        f"{path.relative_to(root)}: {line.strip()}"
        for path in callers
        for line in path.read_text(encoding="utf-8").splitlines()
        if former.search(line)
    ]
    assert not found, found
    assert DASHBOARD  # the dashboard's own header, sent by every session above
