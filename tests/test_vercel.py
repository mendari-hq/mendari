# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mendari.errors import MendariError
from mendari.settings import database_target


def test_database_url_is_used_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    assert database_target() == "postgresql://example/db"


def test_vercel_refuses_to_run_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(MendariError, match="DATABASE_URL is not set"):
        database_target()


def test_local_runs_fall_back_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    assert database_target() == "var/mendari.db"


ROOT = Path(__file__).resolve().parents[1]
# A database no test can reach: a test that tried would fail, not pass by accident.
UNREACHABLE = "postgresql://mendari:never@127.0.0.1:9/hosted"


def test_collecting_these_tests_builds_no_app() -> None:
    """The entrypoint builds its app on DATABASE_URL at import: these tests import only the
    setting, never the entrypoint."""
    code = "import sys, tests.test_vercel; print('mendari.server.vercel' in sys.modules)"
    ran = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert ran.stdout.strip() == "False"


def test_the_tests_never_reach_a_database_the_shell_exports() -> None:
    """With a deployment's database exported in the developer's shell, the tests that build the
    Vercel app, in this process or in one of their own, still pass: none of them reaches it."""
    env = {**os.environ, "DATABASE_URL": UNREACHABLE, "DATABASE_URL_UNPOOLED": UNREACHABLE}
    tests = [
        "tests/test_vercel.py",
        "tests/test_settings.py::test_the_vercel_app_runs_the_jev_defaults",
        "tests/test_community_re2.py::test_a_deployment_without_community_packs_never_imports_re2",
    ]
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *tests]
    ran = subprocess.run(
        [*command, "-k", "not reach_a_database"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stdout[-3000:]
