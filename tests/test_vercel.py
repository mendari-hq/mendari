# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import fnmatch
import json
import os
import re
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
        "tests/test_bounded_re2.py::test_a_deployment_without_bounded_packs_never_imports_re2",
    ]
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *tests]
    ran = subprocess.run(
        [*command, "-k", "not reach_a_database"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stdout[-3000:]


# The registry under the engine and the Hub's own project (LP-19j). Read as files: importing the
# entrypoint would build its app.
VERCEL_JSON = json.loads((ROOT / "vercel.json").read_text())
HUB_JSON_TEXT = (ROOT / "hub" / "vercel.json").read_text()
# What a publish does besides the check, in the same request: read and verify the archive,
# extract and load the pack, render its card, write the version. Generous on purpose.
PUBLISH_MARGIN_S = 60


def test_the_function_outlasts_a_publishs_regression_check() -> None:
    """A publish under the engine runs the regression check in its request (LP-18g, at most
    `TIMEOUT_S`): the function's deadline leaves the rest of the publish time after it, so the
    check fails on its own clock and the publish still answers, rather than the host ending the
    request. The settings the registry reads are listed where the deployment's are."""
    from mendari.hub import regression

    deadline = VERCEL_JSON["functions"]["app.py"]["maxDuration"]
    assert deadline >= regression.TIMEOUT_S + PUBLISH_MARGIN_S
    listed = (ROOT / "src" / "mendari" / "server" / "vercel.py").read_text()
    for setting in (
        "MENDARI_REGISTRY",
        "MENDARI_REGISTRY_REVIEW_ORG",
        "MENDARI_REGISTRY_SIGNING_KEY",
        "MENDARI_REGISTRY_RATE_LIMITS",
        "MENDARI_REGISTRY_REGRESSION",
    ):
        assert f"`{setting}`" in listed, setting


def _excluded(path: str, pattern: str) -> bool:
    """Whether a `{a/**,b/**}` excludeFiles pattern (anchored at the repository's root) leaves
    out `path`."""
    inner = pattern.removeprefix("{").removesuffix("}")
    return any(fnmatch.fnmatchcase(path, glob) for glob in inner.split(","))


def test_the_engine_function_ships_the_registry_and_not_the_hub_site() -> None:
    """The engine's bundle leaves out the Hub's site (the root `hub/`, a project of its own) and
    keeps the registry's code (`src/mendari/hub/`), which the mounted registry runs."""
    pattern = VERCEL_JSON["functions"]["app.py"]["excludeFiles"]
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    files = [name for name in listed.split("\0") if name]
    site = [name for name in files if name.startswith("hub/")]
    source = [name for name in files if name.startswith("src/")]
    assert site and "src/mendari/hub/registry.py" in source
    assert [name for name in site if not _excluded(name, pattern)] == []
    assert [name for name in source if _excluded(name, pattern)] == []


def test_the_hubs_project_file_names_no_host_or_project() -> None:
    """`hub/vercel.json` holds the build only: the project, its domain and `REGISTRY_URL` are the
    owner's (deploy/vercel/README.md), so no hostname, project or variable is written here. A
    commit that leaves `hub/` alone builds nothing there."""
    hub = json.loads(HUB_JSON_TEXT)
    assert set(hub) <= {
        "$schema",
        "framework",
        "installCommand",
        "buildCommand",
        "ignoreCommand",
        "git",
    }
    assert hub["framework"] == "nextjs"
    assert hub["ignoreCommand"] == "git diff --quiet HEAD^ HEAD -- ."  # `.` is the root, `hub`
    assert re.findall(r"https?://[^\"]+", HUB_JSON_TEXT) == [
        "https://openapi.vercel.sh/vercel.json"
    ]
    assert "REGISTRY_URL" not in HUB_JSON_TEXT
    committed = subprocess.run(
        ["git", "ls-files", "hub/.vercel"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert committed.stdout == ""  # a linked project's ids stay on the owner's machine
