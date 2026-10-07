# SPDX-License-Identifier: Apache-2.0
"""The release workflow's gates (scripts/check_release.py): the version is the same wherever it is
written and has its CHANGELOG.md section, a tag is a release tag naming the commit built, and a
publishing job runs only on a public repository from an environment with required reviewers,
refusing whenever the API cannot show it."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script() -> Any:
    path = ROOT / "scripts" / "check_release.py"
    spec = importlib.util.spec_from_file_location("check_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _script()
VERSION = check.versions()["pyproject.toml"].pop()


def test_the_version_is_the_same_wherever_it_is_written() -> None:
    assert check.versions() == {
        "pyproject.toml": {VERSION},
        "src/mendari/__init__.py": {VERSION},
        "uv.lock": {VERSION},
        "docker-compose.yml": {VERSION},
    }


def test_the_changelog_has_a_section_for_this_version_and_it_is_the_newest() -> None:
    sections = check.changelog_versions((ROOT / "CHANGELOG.md").read_text())
    assert sections and sections[0] == VERSION
    assert check.version_problems(f"v{VERSION}") == []


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", "docker-compose.yml", "CHANGELOG.md"):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "src/mendari").mkdir(parents=True)
    shutil.copy(ROOT / "src/mendari/__init__.py", tmp_path / "src/mendari/__init__.py")
    return tmp_path


def test_a_tag_must_be_a_release_tag_for_this_version(tree: Path) -> None:
    for tag in ("0.1.0", "v0.1", "main", f"v{VERSION}; rm -rf /", f"release-{VERSION}"):
        assert "is not a release tag" in check.version_problems(tag, tree)[0]
    assert check.version_problems("v9.9.9", tree) == [
        f"pyproject.toml says {VERSION}, the tag 9.9.9",
        f"src/mendari/__init__.py says {VERSION}, the tag 9.9.9",
        f"uv.lock says {VERSION}, the tag 9.9.9",
        f"docker-compose.yml says {VERSION}, the tag 9.9.9",
        "CHANGELOG.md has no '## 9.9.9' section",
    ]


def test_a_version_out_of_step_or_without_its_changelog_section_is_refused(tree: Path) -> None:
    init = tree / "src/mendari/__init__.py"
    init.write_text(init.read_text().replace(f'"{VERSION}"', '"0.0.9"'))
    (tree / "CHANGELOG.md").write_text("# Changelog\n\n## 0.0.9\n\n- Old.\n")
    assert check.version_problems(f"v{VERSION}", tree) == [
        f"src/mendari/__init__.py says 0.0.9, the tag {VERSION}",
        f"CHANGELOG.md has no '## {VERSION}' section",
    ]
    (tree / "CHANGELOG.md").write_text(f"# Changelog\n\n## 9.0.0\n\n## {VERSION} - 2026-10-07\n")
    assert check.version_problems(f"v{VERSION}", tree)[-1] == (
        f"CHANGELOG.md's newest section is 9.0.0, not {VERSION}"
    )
    (tree / "CHANGELOG.md").unlink()
    assert check.version_problems(f"v{VERSION}", tree)[-1].startswith("CHANGELOG.md has no")


def test_the_tag_must_name_the_commit_built(tree: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
            cwd=tree, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "release")
    git("tag", f"v{VERSION}")
    assert check.version_problems(f"v{VERSION}", tree, head=git("rev-parse", "HEAD")) == []
    git("commit", "-q", "--allow-empty", "-m", "after")
    assert check.version_problems(f"v{VERSION}", tree, head=git("rev-parse", "HEAD")) == [
        f"the tag v{VERSION} is not the commit checked out"
    ]


PUBLIC = {"private": False}
PROTECTED = {
    "name": "pypi",
    "protection_rules": [
        {"type": "branch_policy"},
        {"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"id": 1}}]},
    ],
}


def test_publishing_needs_a_public_repository_and_a_reviewed_environment() -> None:
    assert check.reviewer_problems(PUBLIC, PROTECTED) == []
    assert check.reviewer_problems({"private": True}, PROTECTED) == ["the repository is not public"]
    assert check.reviewer_problems({}, PROTECTED) == ["the repository is not public"]
    unprotected = [
        {"name": "pypi"},  # created by the job itself: no rules
        {"name": "pypi", "protection_rules": [{"type": "wait_timer", "wait_timer": 5}]},
        {"name": "pypi", "protection_rules": [{"type": "required_reviewers", "reviewers": []}]},
    ]
    for environment in unprotected:
        assert check.reviewer_problems(PUBLIC, environment) == [
            "the environment 'pypi' has no required reviewer"
        ]


def test_the_environment_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "Junebugg1214/mendari")
    monkeypatch.setenv("GH_TOKEN", "test-token")

    def unreachable(path: str, token: str) -> dict[str, Any]:
        raise urllib.error.HTTPError(path, 404, "Not Found", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(check, "_get", unreachable)
    assert check.main(["environment", "pypi"]) == 1
    assert "no evidence of the environment's protection (HTTPError)" in capsys.readouterr().err
    answers = {
        "/repos/Junebugg1214/mendari": PUBLIC,
        "/repos/Junebugg1214/mendari/environments/pypi": PROTECTED,
    }
    monkeypatch.setattr(check, "_get", lambda path, token: answers[path])
    assert check.main(["environment", "pypi"]) == 0
    for repository, name in (("x", "pypi"), ("a/b/../c", "pypi"), ("a/b", "../secrets")):
        monkeypatch.setenv("GITHUB_REPOSITORY", repository)
        assert check.main(["environment", name]) == 1
    assert check.main(["publish", "now"]) == 2
