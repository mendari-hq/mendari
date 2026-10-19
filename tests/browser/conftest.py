# SPDX-License-Identifier: Apache-2.0
"""A live dashboard for browser tests: the real app on a free port, seeded through its own API.

The server runs the packs' default providers only (rules, naive Bayes, criteria, dosing), so the
browser suite never calls Jev or a language model and costs nothing.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
import uvicorn
from fastapi.dependencies import models as fastapi_models
from playwright.sync_api import Browser, Page

from mendari.sandbox import example_datasets_dir
from mendari.server.app import create_app

INBOX = "mendari/patient-inbox-router"


def _forget_endpoints() -> None:
    """Lets stopped servers' apps go. FastAPI caches what each endpoint is in module-level LRU
    caches (4,096 entries) keyed by the endpoint, and every Mendari endpoint is a closure over its
    app: about 47 apps (their stores, packs and models) stayed alive at once, and the suite's
    memory and full garbage collections grew with every test that serves one. Production builds
    one app, so only tests need this."""
    for cached in vars(fastapi_models).values():
        if callable(getattr(cached, "cache_clear", None)):
            cached.cache_clear()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed(base: str) -> None:
    """At least one decision per pack (a behaviour-test example), plus inbox decisions that land
    in the review queue and a thumbs-down, so every view has something to render."""
    with httpx.Client(base_url=base, timeout=30) as client:
        for pack in client.get("/api/packs").json():
            examples = client.get(f"/api/packs/{pack['id']}/examples").json()
            for example in examples[:2]:
                client.post(f"/api/packs/{pack['id']}/decide", json=example["input"])
        messages = [
            "Having tight chest pressure and I'm sweating a lot right now.",
            "Hi, I need a refill on my lisinopril. I have about three pills left.",
            "hello?",
        ]
        decided = [
            client.post(
                f"/api/packs/{INBOX}/decide",
                json={"message": {"body": body, "channel": "portal"}},
            ).json()
            for body in messages
        ]
        client.post(
            f"/api/audit/{decided[1]['id']}/feedback",
            json={"rating": "down", "reporter": "browser-test", "note": "Should be front desk"},
        )


@contextmanager
def serving(app: Any, port: int | None = None) -> Iterator[int]:
    """`app` on a free port of 127.0.0.1 (or on `port`, chosen before the app is built, when
    its settings name it) until the block ends; yields the port."""
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the dashboard server did not start")
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        _forget_endpoints()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = Path(tmp_path_factory.mktemp("dashboard")) / "mendari.db"
    with serving(create_app(db_path=db, datasets_dir=example_datasets_dir())) as port:
        base = f"http://127.0.0.1:{port}"
        _seed(base)
        yield base


@pytest.fixture(scope="session")
def base_url(live_server: str) -> str:
    """pytest-playwright resolves relative `page.goto` URLs against this."""
    return live_server


@pytest.fixture(scope="session")
def pack_ids(live_server: str) -> list[str]:
    return [pack["id"] for pack in httpx.get(f"{live_server}/api/packs").json()]


@pytest.fixture
def axe_page(browser: Browser, base_url: str, request: pytest.FixtureRequest) -> Page:
    """A page in a context that lets a test inject axe (tests/browser/test_accessibility.py; the
    CSP stays on for everything else the page loads), in the colour scheme asked for."""
    scheme: Literal["light", "dark"] = (
        "dark" if getattr(request, "param", "light") == "dark" else "light"
    )
    context = browser.new_context(base_url=base_url, bypass_csp=True, color_scheme=scheme)
    request.addfinalizer(context.close)
    return context.new_page()
