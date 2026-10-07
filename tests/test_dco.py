"""The DCO check (scripts/check_dco.py, CONTRIBUTING.md): every commit a pull request brings is
signed off by its author; a bot's and a merge are not checked, nor history before the rule."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_dco.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("check_dco", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_dco = _script()
AUTHOR = ("Ada Contributor", "ada@example.org")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")


def git(repo: Path, *args: str, author: tuple[str, str] = AUTHOR) -> str:
    env = {
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, name: str, *, signed: bool, author: tuple[str, str] = AUTHOR) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name, author=author)
    git(repo, "commit", "-q", *(["-s"] if signed else []), "-m", f"add {name}", author=author)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "start", signed=False)  # before the rule: never checked
    return tmp_path


def found(repo: Path, base: str, head: str = "HEAD", **kwargs: Any) -> list[str]:
    commits = check_dco.commits(base, head, cwd=str(repo), **kwargs)
    return [line.split(" ")[0] for line in check_dco.problems(commits)]


def test_a_signed_commit_passes_and_an_unsigned_one_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "signed", signed=True)
    unsigned = commit(repo, "unsigned", signed=False)
    assert found(repo, base) == [unsigned[:8]]


def test_the_sign_off_must_be_the_authors(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "x").write_text("x")
    git(repo, "add", "x")
    message = "add x\n\nSigned-off-by: Someone Else <else@example.org>"
    git(repo, "commit", "-q", "-m", message)
    assert found(repo, base) == [git(repo, "rev-parse", "HEAD")[:8]]


def test_a_bots_commit_and_a_merge_are_not_checked(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "topic")
    commit(repo, "topic", signed=True)
    git(repo, "checkout", "-q", "main")
    commit(repo, "bump", signed=False, author=BOT)
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "topic")  # a merge commit, unsigned
    assert found(repo, base) == []


def test_history_before_the_rule_is_exempt(repo: Path) -> None:
    old = commit(repo, "old", signed=False)
    root = git(repo, "rev-list", "--max-parents=0", "HEAD")
    new = commit(repo, "new", signed=False)
    assert found(repo, root, effective=old) == [new[:8]]  # `old` is before the rule
    assert found(repo, root, effective="0" * 40) == [old[:8], new[:8]]  # no such commit: all


def test_the_command_fails_an_unsigned_commit_and_says_how_to_fix_it(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "unsigned", signed=False)
    ran = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 1
    assert "no 'Signed-off-by: Ada Contributor <ada@example.org>'" in ran.stderr
    assert "git commit -s" in ran.stderr


def test_the_rule_begins_at_a_commit_of_this_repository() -> None:
    """The start point is a real commit (a typo would exempt nothing); skipped in a shallow clone,
    which has no history to look in."""
    if (
        subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "true"
    ):
        pytest.skip("a shallow clone holds no history")
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{check_dco.EFFECTIVE_AFTER}^{{commit}}"], cwd=ROOT
    )
    assert present.returncode == 0
