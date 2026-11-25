# SPDX-License-Identifier: Apache-2.0
"""One wall-clock deadline across a decision's remote calls (LP-16b).

A decision's remote providers (Jev, an LLM) each have a timeout of their own,
and some retry, so a decision with several of them could hold its request past what the
platform allows (Vercel stops a function at `maxDuration`, before the audit is written). The
deployment sets one deadline per decision (`settings.decision_deadline_s`), which
`Decision.decide` starts at its provider loop (`decision_deadline`). A FHIR decision that reads
linked data (`standards/linked.py`, a CDS hook's or the SMART launch's) starts it before those
reads instead, and its providers get what the reads left (`due`, `deadline_until`): one clock
for the EHR's linked reads and the remote providers together. It does not cover everything a
request does: the audit write is outside it, and so are the prefetch reads before it (a CDS
hook's fetch, the SMART launch's decide: threat-model D14, residual), each under the FHIR
client's own timeouts; and a linked read's wait applies to each step (connecting, then the
answer's headers), the clocks being checked only once its body arrives.

- a remote call that would start after it is not made, before any credential is fetched or any
  budget charged (`refuse_if_past`);
- a call under way waits at most what remains of it: each HTTP attempt's timeout is clamped
  to it (`clamp`), no retry waits past it, and a response read in chunks stops at it;
- either way the provider fails with `timeout`, from the closed set, and the policy holds the
  decision for a person (a failure never automates; a missing primary abstains).

Local providers (rules) never look at it: they always answer, so a safety screen's keyword
rules still fire after the deadline has passed.

The deadline lives in a context variable per decision, measured on `time.monotonic`, never on
the shared `Decision` object (one serves every request's thread). It is not the CPU time budget
of a community pack's patterns (`core/patterns.py`), which counts the decision thread's own
work and nothing it waits for.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from mendari.errors import ProviderError

_DEADLINE: ContextVar[float | None] = ContextVar("decision_deadline", default=None)
PASSED = "the decision's deadline passed"


def due(seconds: float | None) -> float | None:
    """The instant (`time.monotonic`) `seconds` from now; None for no deadline."""
    return None if seconds is None else time.monotonic() + seconds


@contextmanager
def deadline_until(instant: float | None) -> Iterator[None]:
    """A deadline at `instant` (`due`) for the remote calls made inside the block (None: none):
    one deadline, opened around the blocks it covers in turn. Inside another deadline, the
    earlier of the two holds."""
    if instant is None:
        yield
        return
    outer = _DEADLINE.get()
    token = _DEADLINE.set(instant if outer is None else min(instant, outer))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


@contextmanager
def decision_deadline(seconds: float | None) -> Iterator[None]:
    """A deadline `seconds` from now for the remote calls made inside the block (None: none).
    Inside another deadline, the earlier of the two holds."""
    with deadline_until(due(seconds)):
        yield


def remaining() -> float | None:
    """Seconds left before the deadline (0 or less once it has passed), or None outside one."""
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def past() -> bool:
    left = remaining()
    return left is not None and left <= 0


def refuse_if_past() -> None:
    """Raise `timeout` once the deadline has passed (remote providers call this before they
    fetch a credential or charge a budget)."""
    if past():
        raise ProviderError("timeout", PASSED)


def clamp(timeout_s: float) -> float:
    """`timeout_s`, or what remains of the deadline when that is less; `timeout` once it has
    passed."""
    left = remaining()
    if left is None:
        return timeout_s
    if left <= 0:
        raise ProviderError("timeout", PASSED)
    return min(timeout_s, left)
