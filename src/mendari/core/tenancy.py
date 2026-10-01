"""The organization a request acts for, when one deployment serves several.

The sign-in guard (`server/auth.py`) sets it from the signed-in person or API key for the length
of the request, and the audit store stamps it on new records and filters every read by it. In
open mode (no sign-in) it is never set: nothing is filtered, and records belong to the default
organization, so single-organization deployments work as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_ORG = "default"
_current: ContextVar[str | None] = ContextVar("mendari_org", default=None)


def current() -> str | None:
    """The organization this request acts for, or None (open mode: no filtering)."""
    return _current.get()


@contextmanager
def acting_for(org: str | None) -> Iterator[None]:
    token = _current.set(org)
    try:
        yield
    finally:
        _current.reset(token)
