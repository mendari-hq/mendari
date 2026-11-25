# SPDX-License-Identifier: Apache-2.0
"""Routes at one path each. The paths routes had before 2026-11 (`/api/packs…`,
`/api/store/packs…`, `/api/registry/review/packs…`, the registry's `/v1/packs…`) are served no
more: a route answers at its path, the one the API description shows, and nowhere else.

What is left here keeps the registry's routes (`hub/registry.py`) working while they still
register through it: `route` is the app's own decorator, `current` a path as it is, and no path
is a former one. The module goes once nothing calls it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])


def current(path: str) -> str:
    """`path` as it is: every route has one path."""
    return path


def former(path: str) -> str | None:
    """None: no route has a path it had before."""
    return None


def is_former(path: str) -> bool:
    """False: no path is a former route's."""
    return False


def route(
    register: Callable[..., Callable[[Endpoint], Endpoint]], path: str, **options: Any
) -> Callable[[Endpoint], Endpoint]:
    """Register a route at `path` alone: `register` is the app's own decorator for the method
    (`app.get`, `app.post`...) and `options` what it takes."""
    return register(path, **options)
