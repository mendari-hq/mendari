# SPDX-License-Identifier: Apache-2.0
"""Thread pools that keep the caller's context.

Worker threads do not inherit context variables. On Vercel, the OIDC token that authenticates
remote providers (Jev through Vercel Connect, the AI Gateway) is read from the request's
headers, which live in a context variable; a decision run in a bare worker thread cannot see
it and fails with `credential_unavailable` unless a key happens to be cached already. Each task
here runs in its own copy of the submitting thread's context.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """`[fn(item) for item in items]`, on up to `workers` threads, results in input order. An
    error or Ctrl-C while waiting cancels what has not started (a remote provider is not called
    for the rest of a dataset) and waits only for what is running."""
    items = list(items)
    if workers <= 1:
        return [fn(item) for item in items]
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
        results = [future.result() for future in futures]
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return results
