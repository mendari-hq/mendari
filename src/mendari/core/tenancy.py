# SPDX-License-Identifier: Apache-2.0
"""The organization a request acts for, when one deployment serves several, and who asked.

The sign-in guard (`server/auth.py`) sets both from the signed-in person, API key, client or EHR
launch for the length of the request. The audit store stamps the organization on new records and
filters every read by it, and names the requester on each new `decision.created` (the
principal's chain name, `Principal.actor`). In open mode neither is ever set: nothing is
filtered, records belong to the default organization, and nobody is named, so single-organization
deployments work as before.

None means open mode or the command line (every organization), never a signed-in principal. A
signed-in principal with no organization (memberships mode, LP-12) acts for nobody instead: then
`current()` raises `NoOrganization`, so every read and write that resolves its organization
through `current()` fails closed where it is reached, never falling back to every organization
or to the default one. Store methods that take the organization as an argument (holds, quotas,
keys, clients) do not consult it: memberships mode must not let a principal of no organization
reach the routes that call them (LP-12e).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from mendari.errors import NoOrganization

DEFAULT_ORG = "default"
_current: ContextVar[str | None] = ContextVar("mendari_org", default=None)
_requester: ContextVar[str | None] = ContextVar("mendari_requester", default=None)
_nobody: ContextVar[bool] = ContextVar("mendari_no_org", default=False)


def current() -> str | None:
    """The organization this request acts for, or None (open mode: no filtering). Raises
    `NoOrganization` for a principal acting for none (`acting_for_nobody`)."""
    if _nobody.get():
        raise NoOrganization()
    return _current.get()


def requester() -> str | None:
    """Who this request is for, as the chain names them (`Principal.actor`), or None (open mode,
    or no request: the command line)."""
    return _requester.get()


@contextmanager
def acting_for(org: str | None, requester: str | None = None) -> Iterator[None]:
    token = _current.set(org)
    asked = _requester.set(requester)
    somebody = _nobody.set(False)
    try:
        yield
    finally:
        _nobody.reset(somebody)
        _requester.reset(asked)
        _current.reset(token)


@contextmanager
def acting_for_nobody(requester: str | None) -> Iterator[None]:
    """A signed-in principal of no organization: any organization data it reaches is refused
    (`current()` raises), never read or written for all of them or for the default one."""
    token = _current.set(None)
    asked = _requester.set(requester)
    nobody = _nobody.set(True)
    try:
        yield
    finally:
        _nobody.reset(nobody)
        _requester.reset(asked)
        _current.reset(token)
