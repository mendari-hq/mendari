# SPDX-License-Identifier: Apache-2.0
"""The secret scan (LP-02, scripts/secret_scan.sh) and its positive control: keys made up at run
time, planted where only a full scan looks (a pull request's head on no branch, a merge's
resolution), must be found; a git error or an ignore file fails the scan rather than passing it."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
KEY = re.compile(r"^aws_access_key_id = (AKIA[A-Z2-7]{16})$")
needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker, as CI")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def scan(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPTS / "secret_scan.sh"), str(repo)], capture_output=True, text=True
    )


def keys(repo: Path) -> list[str]:
    places = (("refs/pull/1/head", "settings.ini"), ("main", "merged.ini"))
    planted = [git(repo, "show", f"{ref}:{name}").strip() for ref, name in places]
    found = [KEY.match(line) for line in planted]
    assert all(found)
    return [match.group(1) for match in found if match]


def test_the_keys_are_made_up_and_planted_where_only_a_full_scan_looks(tmp_path: Path) -> None:
    subprocess.run(["sh", str(SCRIPTS / "plant_fake_secret.sh"), str(tmp_path / "a")], check=True)
    subprocess.run(["sh", str(SCRIPTS / "plant_fake_secret.sh"), str(tmp_path / "b")], check=True)
    first, second = keys(tmp_path / "a"), keys(tmp_path / "b")
    assert len(set(first + second)) == 4  # a new pair each time
    for key in first:  # well above the rule's entropy floor (3.0), so never skipped as noise
        counts = Counter(key).values()
        assert -sum(c / 20 * math.log2(c / 20) for c in counts) > 3.5
    repo = tmp_path / "a"
    assert git(repo, "branch", "--contains", "refs/pull/1/head").strip() == ""  # on no branch
    merge = git(repo, "rev-parse", "main").strip()
    assert len(git(repo, "rev-list", "--parents", "-n", "1", merge).split()) == 3  # a merge
    added = git(repo, "log", "--all", "--format=%H", "--diff-filter=A", "--", "merged.ini").split()
    assert added == []  # no ordinary diff adds it: only the merge's resolution does


def test_an_ignore_file_fails_the_scan(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitleaksignore").write_text("anything\n")
    ran = scan(tmp_path)
    assert ran.returncode == 2 and ".gitleaksignore would hide findings" in ran.stderr


@needs_docker
def test_the_positive_control_finds_both_planted_keys() -> None:
    ran = subprocess.run(
        ["sh", str(SCRIPTS / "secret_scan_control.sh")], capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr[-800:]
    assert "AKIA" not in ran.stdout + ran.stderr  # redacted


@needs_docker
def test_a_clean_history_passes_and_a_broken_one_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["sh", str(SCRIPTS / "plant_fake_secret.sh"), str(repo)], check=True)
    git(repo, "update-ref", "-d", "refs/pull/1/head")
    git(repo, "-c", "user.name=c", "-c", "user.email=c@example.org", "rm", "-q", "merged.ini")
    git(repo, "-c", "user.name=c", "-c", "user.email=c@example.org", "commit", "-q", "-m", "x")
    assert scan(repo).returncode == 3  # removed is not gone: the merge still holds it
    clean = tmp_path / "clean"
    subprocess.run(["git", "init", "-q", str(clean)], check=True)
    (clean / "README").write_text("nothing here\n")
    git(clean, "add", "README")
    git(clean, "-c", "user.name=c", "-c", "user.email=c@example.org", "commit", "-q", "-m", "c")
    assert scan(clean).returncode == 0
    # A git error (here a missing object): gitleaks would log it and call the scan clean.
    blob = git(clean, "rev-parse", "HEAD:README").strip()
    (clean / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    ran = scan(clean)
    assert ran.returncode == 2 and "the history was not fully read" in ran.stderr
