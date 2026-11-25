# SPDX-License-Identifier: Apache-2.0
"""The test suite reaches nothing outside itself (tests/conftest.py, LIVE_SETTINGS): with every
system a developer's shell can name (a deployment's database, the AI Gateway, an LLM endpoint,
the alert webhook, the registry) pointed at a local server that records each request,
the tests that once reached them run and the server hears nothing."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from mendari.errors import ProviderError
from mendari.providers import credentials, llm
from tests.conftest import LIVE_SETTINGS

ROOT = Path(__file__).resolve().parents[1]
# The tests that built an app on the shell's database, sent a case to an LLM, or posted an
# alert, when the shell named one.
ONCE_REACHED = [
    "tests/test_vercel.py",
    "tests/test_bounded_re2.py::test_a_deployment_without_bounded_agents_never_imports_re2",
    "tests/test_limits.py::test_over_its_quota_an_emergency_still_reaches_a_nurse_at_once",
    "tests/test_monitoring.py::test_the_cli_watches_one_organization",
    "tests/test_security_log.py",
]


@pytest.fixture
def listener() -> Iterator[tuple[str, list[str]]]:
    """A local server that answers 500 to everything and records what it was sent."""
    heard: list[str] = []

    class Record(BaseHTTPRequestHandler):
        def _any(self) -> None:
            heard.append(f"{self.command} {self.path}")
            self.send_response(500)
            self.end_headers()

        do_GET = do_POST = do_PUT = _any

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Record)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", heard
    finally:
        server.shutdown()
        server.server_close()


def test_the_tests_reach_nothing_the_shell_names(
    listener: tuple[str, list[str]], tmp_path: Path
) -> None:
    url, heard = listener
    database = "postgresql://mendari:never@127.0.0.1:9/hosted"  # unreachable: a try would fail
    env = {
        **os.environ,
        "DATABASE_URL": database,
        "DATABASE_URL_UNPOOLED": database,
        "AI_GATEWAY_API_KEY": "shell-key",
        "VERCEL_OIDC_TOKEN": "shell-token",
        "LLM_BASE_URL": f"{url}/v1",
        "LLM_API_KEY": "shell-key",
        "MENDARI_ALERT_WEBHOOK": f"{url}/hook",
        "MENDARI_REGISTRY_URL": url,
    }
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *ONCE_REACHED]
    ran = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout[-3000:]
    assert heard == []


def test_every_setting_the_listener_names_is_removed() -> None:
    for name in ("DATABASE_URL", "AI_GATEWAY_API_KEY", "LLM_BASE_URL", "MENDARI_ALERT_WEBHOOK"):
        assert name in LIVE_SETTINGS and name not in os.environ


def test_no_test_mints_a_vercel_oidc_token() -> None:
    for module in (credentials, llm):
        with pytest.raises(ProviderError, match="credential_unavailable"):
            module.vercel_oidc_token()
