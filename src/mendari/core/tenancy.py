# SPDX-License-Identifier: Apache-2.0
"""The organization a request acts for, when one deployment serves several, and who asked.

The sign-in guard (`server/auth.py`) sets both from the signed-in person, API key, client or EHR
launch for the length of the request. The audit store stamps the organization on new records and
filters every read by it, and names the requester on each new `decision.created` (the
principal's chain name, `Principal.actor`). In open mode neither is ever set: nothing is
filtered, records belong to the default organization, and nobody is named, so single-organization
deployments work as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_ORG = "default"
_current: ContextVar[str | None] = ContextVar("mendari_org", default=None)
_requester: ContextVar[str | None] = ContextVar("mendari_requester", default=None)


def current() -> str | None:
    """The organization this request acts for, or None (open mode: no filtering)."""
    return _current.get()


def requester() -> str | None:
    """Who this request is for, as the chain names them (`Principal.actor`), or None (open mode,
    or no request: the command line)."""
    return _requester.get()


@contextmanager
def acting_for(org: str | None, requester: str | None = None) -> Iterator[None]:
    token = _current.set(org)
    asked = _requester.set(requester)
    try:
        yield
    finally:
        _requester.reset(asked)
        _current.reset(token)
