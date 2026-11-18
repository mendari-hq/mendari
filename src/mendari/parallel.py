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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from itertools import islice
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """`[fn(item) for item in items]`, on up to `workers` threads, results in input order. An
    error or Ctrl-C while waiting cancels what has not started (a remote provider is not called
    for the rest of a dataset) and waits only for what is running."""
    if workers <= 1:
        return [fn(item) for item in items]
    pool = ThreadPoolExecutor(max_workers=workers)
    indexed = enumerate(items)
    pending: dict[Future[R], int] = {}
    results: dict[int, R] = {}
    try:
        while True:
            # Only consume input when a worker is available. A failed case stops dispatch
            # even when a lower-numbered case is still waiting on its provider.
            for index, item in islice(indexed, workers - len(pending)):
                future = pool.submit(contextvars.copy_context().run, fn, item)
                pending[future] = index
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                results[pending.pop(future)] = future.result()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return [results[index] for index in range(len(results))]
