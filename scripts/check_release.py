# SPDX-License-Identifier: Apache-2.0
"""The release workflow's gates (.github/workflows/release.yml). Standard library only.

`version REF`: the run is on a release tag (REF is `refs/tags/v<version>`, the workflow's
`github.ref`; a bare `v<version>` locally), the tag names the commit checked out, and the version
is the same everywhere it is written (pyproject.toml, mendari.__version__, uv.lock, the docker
compose image tags) and has its own section in CHANGELOG.md, the newest.

`environment NAME`: a publishing job goes on only when the repository is public and the
deployment environment it uses has required reviewers. GitHub creates an environment named by a
job, unprotected, if it does not exist, and runs the job with no approval; this refuses that, and
refuses whenever the API cannot show the evidence (fail closed). Reads GITHUB_REPOSITORY and
GH_TOKEN.

    python3 scripts/check_release.py version refs/tags/v0.1.0
    python3 scripts/check_release.py environment pypi
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$")
API = "https://api.github.com"
# owner/name, as GitHub sets it; never `.` or `..`, which would leave the repository path.
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/(?!\.\.?$)[\w.-]+")


def versions(root: Path = ROOT) -> dict[str, set[str]]:
    """Each place the version is written, with what it says there."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    init = re.search(
        r'^__version__ = "([^"]+)"$', (root / "src/mendari/__init__.py").read_text(), re.M
    )
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = {p["version"] for p in lock["package"] if p["name"] == "mendari"}
    compose = set(
        re.findall(
            r"^\s+image: mendari-engine:(\S+)$", (root / "docker-compose.yml").read_text(), re.M
        )
    )
    return {
        "pyproject.toml": {pyproject},
        "src/mendari/__init__.py": {init.group(1)} if init else set(),
        "uv.lock": locked,
        "docker-compose.yml": compose,
    }


def changelog_versions(text: str) -> list[str]:
    """The versions CHANGELOG.md has a section for (`## 0.1.0` or `## 0.1.0 - 2026-10-07`)."""
    return re.findall(r"^## (\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)(?:\s|$)", text, re.M)


def version_problems(tag: str, root: Path = ROOT, head: str | None = None) -> list[str]:
    found = []
    match = TAG.match(tag)
    if not match:
        return [f"{tag!r} is not a release tag (v and a version, e.g. v0.1.0)"]
    wanted = match["version"]
    for where, said in versions(root).items():
        if said != {wanted}:
            found.append(f"{where} says {', '.join(sorted(said)) or 'nothing'}, the tag {wanted}")
    changelog = root / "CHANGELOG.md"
    sections = changelog_versions(changelog.read_text()) if changelog.exists() else []
    if wanted not in sections:
        found.append(f"CHANGELOG.md has no '## {wanted}' section")
    elif sections[0] != wanted:
        found.append(f"CHANGELOG.md's newest section is {sections[0]}, not {wanted}")
    if head is not None:
        try:
            tagged = _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
        except subprocess.CalledProcessError:
            found.append(f"the tag {tag} does not exist here")
        else:
            if tagged != head:
                found.append(f"the tag {tag} is not the commit checked out")
    return found


def reviewer_problems(repository: dict[str, Any], environment: dict[str, Any]) -> list[str]:
    """Why a publishing job must not run, from the repository's and the environment's API
    records."""
    found = []
    if repository.get("private") is not False:
        found.append("the repository is not public")
    rules = environment.get("protection_rules") or []
    reviewers = [
        reviewer
        for rule in rules
        if rule.get("type") == "required_reviewers"
        for reviewer in rule.get("reviewers") or []
    ]
    if not reviewers:
        found.append(f"the environment {environment.get('name')!r} has no required reviewer")
    return found


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _get(path: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        loaded = json.load(response)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: not an object")
    return loaded


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"version", "environment"}:
        print(__doc__, file=sys.stderr)
        return 2
    command, value = argv
    if command == "version":
        tag = value.removeprefix("refs/tags/")
        if tag != value or not value.startswith("refs/"):
            found = version_problems(tag, head=_git(ROOT, "rev-parse", "HEAD"))
        else:  # a branch: the environments allow only tags, and nothing names the release
            found = [f"the run is on {value}, not a tag: choose the tag under 'Use workflow from'"]
    else:
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        if not REPOSITORY.fullmatch(repository) or not re.fullmatch(r"[\w-]+", value):
            print(
                "refused: GITHUB_REPOSITORY or the environment name is malformed", file=sys.stderr
            )
            return 1
        try:
            found = reviewer_problems(
                _get(f"/repos/{repository}", token),
                _get(f"/repos/{repository}/environments/{value}", token),
            )
        except (OSError, ValueError) as error:
            found = [f"no evidence of the environment's protection ({type(error).__name__})"]
    for line in found:
        print(f"refused: {line}", file=sys.stderr)
    if not found:
        print(f"ok  {command} {value}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
