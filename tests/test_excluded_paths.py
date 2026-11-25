# SPDX-License-Identifier: Apache-2.0
"""The excluded-path check (scripts/check_excluded.py): notes, plans, agent instructions and CI
configuration are refused at any depth in a source distribution, a wheel, the docs site and an
image, and what is published passes. CI runs it on the real artifacts (jobs hub-e2e, docs,
docker and release)."""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_excluded.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("check_excluded", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()
LEAKS = [
    "CLAUDE.md",
    "AGENTS.md",
    ".claude/launch.json",
    "tasks/todo.md",
    "notes/vision-audit.md",
    ".github/workflows/ci.yml",
]
CLEAN = [
    "mendari/__init__.py",
    "mendari/_agents/portal-message-agent/agent.yaml",
    "LICENSE",
    "NOTICE",
]


def tar(path: Path, names: list[str]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            archive.addfile(info, io.BytesIO(b""))
    return path


def wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "")
    return path


def site(path: Path, names: list[str]) -> Path:
    for name in names:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("")
    return path


BUILDERS = {
    "sdist": lambda tmp, names: tar(
        tmp / "mendari-0.1.0.tar.gz", [f"mendari-0.1.0/{n}" for n in names]
    ),
    "wheel": lambda tmp, names: wheel(tmp / "mendari-0.1.0-py3-none-any.whl", names),
    "site": lambda tmp, names: site(tmp / "site", names),
}


@pytest.mark.parametrize("kind", BUILDERS)
@pytest.mark.parametrize("leak", LEAKS)
def test_each_excluded_path_is_refused_at_any_depth(tmp_path: Path, kind: str, leak: str) -> None:
    for depth, where in enumerate((leak, f"mendari/_agents/portal-message-agent/{leak}")):
        (tmp_path / str(depth)).mkdir()
        built = BUILDERS[kind](tmp_path / str(depth), [*CLEAN, where])
        found = check.excluded(check.members(built))
        # A directory's own entry may be listed too (a folder in the site, a tar's folder entry).
        assert found and found[-1].endswith(where), (kind, where, found)
        assert all(where.startswith(path.removeprefix("mendari-0.1.0/")) for path in found)


@pytest.mark.parametrize("kind", BUILDERS)
def test_a_clean_artifact_passes(
    tmp_path: Path, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    built = BUILDERS[kind](tmp_path, [*CLEAN, "mendari/notes.py", "docs/tasks.html"])
    assert check.main([str(built)]) == 0  # `notes.py` and `tasks.html` are not `notes/`, `tasks/`
    assert "ok" in capsys.readouterr().out


def test_the_command_names_what_it_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = BUILDERS["wheel"](tmp_path, [*CLEAN, "mendari/.claude/settings.json"])
    assert check.main([str(built)]) == 1
    assert "excluded path in" in capsys.readouterr().err


def exported(names: list[str]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in names:
            archive.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
    buffer.seek(0)
    return buffer


def test_an_image_is_checked_in_its_application_paths_and_for_agent_files_anywhere() -> None:
    package = "opt/venv/lib/python3.12/site-packages"
    base = [
        "usr/share/doc/libfoo/notes/README",  # a base image's own folder: not the application's
        "usr/lib/python3/tasks/x.py",
        f"{package}/fastapi/__init__.py",
        f"{package}/mendari/__init__.py",
        f"{package}/mendari-0.1.0.dist-info/licenses/NOTICE",
        "home/mendari/.profile",
    ]
    assert check.image_problems(exported(base)) == []
    leaks = [
        f"{package}/mendari/notes/plan.md",
        f"{package}/mendari-0.1.0.dist-info/.github/x",
        "home/mendari/tasks/todo.md",
        "data/notes/a.md",
        "srv/CLAUDE.md",  # agent files are refused anywhere
        "./root/.claude/settings.json",
    ]
    assert check.image_problems(exported(base + leaks)) == sorted(leaks)
