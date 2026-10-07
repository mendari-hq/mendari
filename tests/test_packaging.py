# SPDX-License-Identifier: Apache-2.0
"""The source distribution ships only what a source build needs. PyPI files can never be
replaced, so the include list in pyproject.toml is checked here (fast, no build), and the built
archive is checked in CI by scripts/check_sdist.py (jobs `hub-e2e` and `release`)."""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/Junebugg1214/mendari"


def _check_sdist() -> ModuleType:
    path = ROOT / "scripts" / "check_sdist.py"
    spec = importlib.util.spec_from_file_location("check_sdist", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sdist_includes_exactly_the_build_inputs() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert set(sdist) == {"include"}, "an exclude or only-include would change what ships"
    assert {entry.strip("/") for entry in sdist["include"]} == _check_sdist().INCLUDED
    # Every include is anchored at the project root, so a nested `docs/` can never match.
    assert all(entry.startswith("/") for entry in sdist["include"])


def test_the_wheel_sources_are_inside_the_sdist() -> None:
    # A wheel built from the sdist needs the package and everything force-included into it.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    sources = [*wheel["packages"], *wheel["force-include"]]
    assert {source.split("/", 1)[0] for source in sources} <= _check_sdist().INCLUDED


def test_the_check_refuses_project_notes_and_configuration() -> None:
    check = _check_sdist()
    leaked = [
        "CLAUDE.md",
        ".claude/launch.json",
        "tasks/todo.md",
        "tasks/lessons.md",
        "notes/overnight-log.md",
        "docs/index.md",
        "hub/package.json",
        "web/index.html",
        "deploy/onprem/.env.example",
        ".github/workflows/ci.yml",
        "scripts/pack_gate.py",
        "uv.lock",
        # Many tests read the workflows, scripts, docs or lock above: from an sdist they would
        # fail at collection, so the tests run from a clone only.
        "tests/test_cli.py",
        "tests/conftest.py",
    ]
    found = check.problems([f"mendari-0.1.0/{path}" for path in leaked])
    assert found == sorted(f"not part of a source build: {path}" for path in leaked)


def test_the_check_accepts_a_clean_sdist_and_notices_a_missing_file() -> None:
    check = _check_sdist()
    names = [
        "mendari-0.1.0/PKG-INFO",
        "mendari-0.1.0/.gitignore",
        "mendari-0.1.0/pyproject.toml",
        "mendari-0.1.0/README.md",
        "mendari-0.1.0/LICENSE",
        "mendari-0.1.0/src/mendari/__init__.py",
    ]
    tracked = ["src/mendari/__init__.py", "src/mendari/cli.py", "docs/index.md", "CLAUDE.md"]
    assert check.problems(names) == []
    assert check.problems(names, tracked) == ["tracked but missing: src/mendari/cli.py"]


def test_the_readme_links_work_on_pypi() -> None:
    """The README is the package's long description (`readme` in pyproject.toml). PyPI resolves a
    relative link against pypi.org, where it is a 404, so every link is absolute or an anchor,
    and each link into this repository names a path that exists, as a file or a folder."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)\s]+)\)", readme)
    assert len(targets) > 40  # the links were found
    relative = [t for t in targets if not re.match(r"(https?://|#|mailto:)", t)]
    assert relative == [], "relative links break on pypi.org"
    for target in targets:
        if not target.startswith(f"{REPOSITORY}/blob/") and not target.startswith(
            f"{REPOSITORY}/tree/"
        ):
            continue
        kind, branch, path = target.removeprefix(REPOSITORY + "/").split("/", 2)[0:3]
        path = path.partition("#")[0]
        assert branch == "main", target
        expected = "tree" if (ROOT / path).is_dir() else "blob"
        assert (ROOT / path).exists() and kind == expected, target
