# SPDX-License-Identifier: Apache-2.0
"""Delete the Neon preview branches no pull request needs (neon-preview-cleanup.yml).

The Vercel-managed Neon integration creates a database branch `preview/<git branch>` for every
preview deployment and deletes it only when Vercel deletes the deployment, months later. At the
plan's branch limit (10, `main` included) every new preview fails with "Resource provisioning
failed".

A preview branch is kept while its Git branch has an open pull request, or still exists and never
had one (pushed, its pull request not open yet). Otherwise it is deleted: its pull request closed,
or its Git branch is gone. Only `preview/<plain name>` branches are touched, never the default or
a protected one.

Deciding from the whole list, not from the pull request that just closed, also catches what a
single delete misses: a preview that finished building after the merge and so recreated its
branch, a run GitHub never started, a branch pushed without a pull request.

Usage (the workflow passes NEON_API_KEY, NEON_PROJECT_ID, GITHUB_TOKEN; Actions sets
GITHUB_REPOSITORY):

    python3 scripts/neon_preview_cleanup.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

NEON_API = "https://console.neon.tech/api/v2"
GITHUB_API = "https://api.github.com"
PREFIX = "preview/"
# The names the integration makes from a Git branch. Any other is left alone: a name is data, and
# the log prints these.
PLAIN = re.compile(r"^preview/[A-Za-z0-9._/-]+$")
PAGE = 100
# Neon answers 423 while another operation on the project runs (a preview creating its branch).
LOCKED_TRIES = 5
DONE = {200, 202, 204, 404}  # 404: already gone

# (method, url) -> (HTTP status, parsed JSON body or None). Bodies of errors are never kept.
Fetch = Callable[[str, str], tuple[int, Any]]


@dataclass(frozen=True)
class Branch:
    id: str
    name: str
    default: bool = False
    protected: bool = False

    @property
    def git_branch(self) -> str:
        return self.name.removeprefix(PREFIX)


def stale(
    branches: Iterable[Branch],
    open_heads: set[str],
    git_branches: set[str],
    had_pull_request: Callable[[str], bool],
) -> list[Branch]:
    """The preview branches to delete: their Git branch has no open pull request, and is gone or
    had one (now closed)."""
    found = []
    for branch in branches:
        if branch.default or branch.protected or not PLAIN.match(branch.name):
            continue
        head = branch.git_branch
        if head in open_heads:
            continue
        if head in git_branches and not had_pull_request(head):
            continue  # pushed, its pull request not open yet
        found.append(branch)
    return found


def _expect(status: int, what: str) -> None:
    if status != 200:
        raise SystemExit(f"::error::{what} answered HTTP {status}")


def neon_branches(neon: Fetch, project: str) -> list[Branch]:
    branches: list[Branch] = []
    cursor = ""
    while True:
        query = {"limit": PAGE, "sort_by": "created_at", "sort_order": "asc"}
        if cursor:
            query["cursor"] = cursor
        url = f"{NEON_API}/projects/{_quote(project)}/branches?{urllib.parse.urlencode(query)}"
        status, body = neon("GET", url)
        _expect(status, "Neon, listing the branches,")
        page = body["branches"]
        branches += [
            Branch(b["id"], b["name"], bool(b.get("default")), bool(b.get("protected")))
            for b in page
        ]
        following = (body.get("pagination") or {}).get("next") or ""
        if not page or not following or following == cursor:
            return branches
        cursor = following


def _pages(github: Fetch, url: str, params: dict[str, str], what: str) -> Iterator[Any]:
    page = 1
    while True:
        query = urllib.parse.urlencode({**params, "per_page": PAGE, "page": page})
        status, body = github("GET", f"{url}?{query}")
        _expect(status, f"GitHub, listing the {what},")
        yield from body
        if len(body) < PAGE:
            return
        page += 1


def open_heads(github: Fetch, repo: str) -> set[str]:
    # Every open pull request's head, a fork's included: a name in doubt is kept.
    pulls = _pages(github, f"{GITHUB_API}/repos/{repo}/pulls", {"state": "open"}, "pull requests")
    return {pull["head"]["ref"] for pull in pulls}


def git_branches(github: Fetch, repo: str) -> set[str]:
    return {
        b["name"] for b in _pages(github, f"{GITHUB_API}/repos/{repo}/branches", {}, "branches")
    }


def had_pull_request(github: Fetch, repo: str, head: str) -> bool:
    owner = repo.split("/")[0]
    query = urllib.parse.urlencode({"state": "all", "head": f"{owner}:{head}", "per_page": 1})
    status, body = github("GET", f"{GITHUB_API}/repos/{repo}/pulls?{query}")
    _expect(status, "GitHub, listing a branch's pull requests,")
    return bool(body)


def delete(
    neon: Fetch, project: str, branch: Branch, sleep: Callable[[float], None] = time.sleep
) -> int:
    """Delete one branch; the last HTTP status (in DONE when it is gone)."""
    url = f"{NEON_API}/projects/{_quote(project)}/branches/{_quote(branch.id)}"
    for attempt in range(LOCKED_TRIES):
        status, _ = neon("DELETE", url)
        if status != 423 or attempt == LOCKED_TRIES - 1:
            return status
        sleep(2**attempt)
    raise AssertionError("unreachable")


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def fetcher(token: str, accept: str) -> Fetch:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": "mendari-neon-preview-cleanup",
    }

    def fetch(method: str, url: str) -> tuple[int, Any]:
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None

    return fetch


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise SystemExit(f"::error::{name} is not set")
    return value


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] = os.environ,
    neon: Fetch | None = None,
    github: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        description="Delete the Neon preview branches no pull request needs."
    )
    parser.add_argument("--dry-run", action="store_true", help="list, delete nothing")
    args = parser.parse_args(argv)
    project = _required(environ, "NEON_PROJECT_ID")
    repo = _required(environ, "GITHUB_REPOSITORY")
    neon = neon or fetcher(_required(environ, "NEON_API_KEY"), "application/json")
    github = github or fetcher(_required(environ, "GITHUB_TOKEN"), "application/vnd.github+json")

    branches = neon_branches(neon, project)
    unusual = [b.id for b in branches if b.name.startswith(PREFIX) and not PLAIN.match(b.name)]
    if unusual:
        print(f"::warning::preview branches with an unusual name, left alone: {unusual}")
    found = stale(
        branches,
        open_heads(github, repo),
        git_branches(github, repo),
        lambda head: had_pull_request(github, repo, head),
    )
    print(f"{len(branches)} Neon branches, {len(found)} no pull request needs")
    failed = 0
    for branch in found:
        if args.dry_run:
            print(f"would delete {branch.name} ({branch.id})")
            continue
        status = delete(neon, project, branch, sleep)
        if status == 404:
            print(f"already gone: {branch.name} ({branch.id})")
        elif status in DONE:
            print(f"deleted {branch.name} ({branch.id})")
        else:
            print(f"::error::{branch.name} ({branch.id}) was not deleted: HTTP {status}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
