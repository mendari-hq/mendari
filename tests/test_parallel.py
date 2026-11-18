# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import contextvars
import json
import time
from collections.abc import Iterator
from threading import Event

import pytest

from mendari.parallel import parallel_map
from mendari.providers.credentials import vercel_oidc_token


def test_workers_keep_the_callers_context() -> None:
    request = contextvars.ContextVar("request", default="none")

    def run() -> list[str]:
        request.set("request-1")
        return parallel_map(lambda _: request.get(), range(20), workers=4)

    assert contextvars.copy_context().run(run) == ["request-1"] * 20


def test_workers_see_the_requests_vercel_oidc_token(monkeypatch: object) -> None:
    # On Vercel the token arrives in the request's headers (a context variable); evaluations
    # ran decisions in bare worker threads, so on a cold instance every Jev call failed with
    # credential_unavailable.
    from vercel.headers import set_headers

    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    token = ".".join([part({"alg": "none"}), part({"exp": int(time.time()) + 3600}), "sig"])

    def run() -> list[str]:
        set_headers({"x-vercel-oidc-token": token})
        return parallel_map(lambda _: vercel_oidc_token(), range(8), workers=4)

    assert contextvars.copy_context().run(run) == [token] * 8


def test_results_keep_input_order() -> None:
    assert parallel_map(lambda n: n * n, range(50), workers=8) == [n * n for n in range(50)]
    assert parallel_map(lambda n: n, [3, 1, 2], workers=1) == [3, 1, 2]


def test_an_interrupt_cancels_what_has_not_started() -> None:
    """Ctrl-C (or any error) while waiting stops the rest of a dataset: a remote provider is not
    called for every case left in the queue."""
    import threading
    import time

    started: list[int] = []
    lock = threading.Lock()

    def slow(item: int) -> int:
        with lock:
            started.append(item)
        if item == 0:
            raise KeyboardInterrupt  # the first result waited on is an interrupt
        time.sleep(0.05)
        return item

    with pytest.raises(KeyboardInterrupt):
        parallel_map(slow, range(200), workers=4)
    time.sleep(0.2)  # what was running finishes; nothing queued starts
    assert len(started) < 20


def test_a_later_failure_stops_dispatch_while_the_first_case_is_pending() -> None:
    release = Event()

    def cases() -> Iterator[int]:
        yield 0
        yield 1
        pytest.fail("read the next case while both worker slots were occupied")

    def run(case: int) -> int:
        if case == 1:
            raise ValueError("evaluation stopped")
        assert release.wait(timeout=5)
        return case

    try:
        with pytest.raises(ValueError, match="evaluation stopped"):
            parallel_map(run, cases(), workers=2)
    finally:
        release.set()
