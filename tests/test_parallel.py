from __future__ import annotations

import base64
import contextvars
import json
import time

from openhealth.parallel import parallel_map
from openhealth.providers.credentials import vercel_oidc_token


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
