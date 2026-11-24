# SPDX-License-Identifier: Apache-2.0
"""The routes renamed in 2026-11, when what Mendari governs stopped being called a Decision
Pack, and the paths they had before.

| Was | Is |
|---|---|
| `/api/packs…` | `/api/agents…` |
| `/api/store/packs…` | `/api/store/agents…` |
| `/api/registry/review/packs…` | `/api/registry/review/agents…` |
| the registry's `/v1/packs…` (`/registry/v1/packs…` under the engine) | `/v1/agents…` |

**One handler, two paths.** A renamed route is registered once, with `route`: at its path, which
the API description shows, and at the path it had before, which answers the same and is left out
of the description (`include_in_schema=False`). No handler is written twice.

**One rule per route.** Whatever decides by a request's path (the permission a route needs, the
scope that reaches it, the limit it counts against, the format its errors take) reads the path
through `current` first, and names the current path only. So the two spellings cannot be told
apart there: a former path is never less protected than the current one, nor more. The rules
that ask it: `server/auth.py` `permission_for`, `server/scopes.py` `needed`, `server/limits.py`
`Limiter.classify` and `DailyCap.counts`, `server/app.py` `fhir_route` and the registry's
`_rate_class` (`hub/registry.py`). `tests/test_legacy_routes.py` walks every route of both apps
and holds them to it.

Nothing else is renamed here: the keys of a request or an answer (`pack`, `pack_id`), a FHIR
operation's parameters and Mendari's canonical URLs (`https://mendari.co/packs/...`, identifiers
that decisions, signatures and other systems hold) are as they were.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])

# (the path before, the path now), each a whole prefix: the path itself, or it and a `/`.
# The longer prefixes first, so none is read as a shorter one's.
RENAMED: tuple[tuple[str, str], ...] = (
    ("/api/registry/review/packs", "/api/registry/review/agents"),
    ("/api/store/packs", "/api/store/agents"),
    ("/api/packs", "/api/agents"),
    ("/registry/v1/packs", "/registry/v1/agents"),
    ("/v1/packs", "/v1/agents"),
)


def _moved(path: str, pairs: tuple[tuple[str, str], ...]) -> str | None:
    for found, put in pairs:
        if path == found or path.startswith(found + "/"):
            return put + path[len(found) :]
    return None


def current(path: str) -> str:
    """`path` as it is called now: a former path's current one (`/api/packs/a/b/decide` is
    `/api/agents/a/b/decide`), any other path as it is. A route's template reads the same
    way. Every rule that matches on a path asks this first (module docstring)."""
    return _moved(path, RENAMED) or path


_FORMER = tuple((now, before) for before, now in RENAMED)


def former(path: str) -> str | None:
    """The path a renamed route had before (`/api/agents` was `/api/packs`); None for a route
    that was never renamed."""
    return _moved(path, _FORMER)


def is_former(path: str) -> bool:
    """Whether `path` is the former spelling of a renamed route: one that answers, and that
    nothing lists (the API description, a key preset's reach)."""
    return current(path) != path


def route(
    register: Callable[..., Callable[[Endpoint], Endpoint]], path: str, **options: Any
) -> Callable[[Endpoint], Endpoint]:
    """Register a renamed route: `register` is the app's own decorator for the method
    (`app.get`, `app.post`...), `path` the route's current path and `options` what the
    decorator takes. The same handler is registered at the former path too, with the same
    options but `include_in_schema=False`. A path that was never renamed is a mistake here:
    it has the app's own decorator."""
    before = former(path)
    if before is None:
        raise ValueError(f"{path} has no former path: register it with the app's own decorator")

    def decorate(endpoint: Endpoint) -> Endpoint:
        register(path, **options)(endpoint)
        register(before, **{**options, "include_in_schema": False})(endpoint)
        return endpoint

    return decorate
