# SPDX-License-Identifier: Apache-2.0
"""The Neon preview cleanup (scripts/neon_preview_cleanup.py, neon-preview-cleanup.yml): a preview
branch is deleted once no pull request needs it (its pull request closed, or its Git branch
gone), whatever closed it and whenever its preview finished; the branches of open pull requests,
of branches not proposed yet, and every other branch stay."""

from __future__ import annotations

import importlib.util
import io
import sys
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "neon_preview_cleanup.py"
WORKFLOW = ROOT / ".github" / "workflows" / "neon-preview-cleanup.yml"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("neon_preview_cleanup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cleanup = _script()
Branch = cleanup.Branch
REPO = "mendari-hq/mendari"
ENV = {"NEON_PROJECT_ID": "proj-1", "GITHUB_REPOSITORY": REPO}


class Neon:
    """The branches endpoints of one project; `statuses` scripts a branch's DELETE answers."""

    def __init__(self, branches: list[Any], statuses: dict[str, list[int]] | None = None):
        self.branches = {b.id: b for b in branches}
        self.statuses = statuses or {}
        self.deleted: list[str] = []
        self.page_size = cleanup.PAGE

    def __call__(self, method: str, url: str) -> tuple[int, Any]:
        parsed = urllib.parse.urlsplit(url)
        assert parsed.path.startswith("/api/v2/projects/proj-1/branches"), url
        if method == "GET":
            query = dict(urllib.parse.parse_qsl(parsed.query))
            names = sorted(self.branches)
            start = int(query.get("cursor", 0))
            page = names[start : start + self.page_size]
            more = start + self.page_size < len(names)
            return 200, {
                "branches": [
                    {
                        "id": b.id,
                        "name": b.name,
                        "default": b.default,
                        "protected": b.protected,
                    }
                    for b in (self.branches[n] for n in page)
                ],
                "pagination": {"next": str(start + self.page_size)} if more else {},
            }
        branch_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[1])
        scripted = self.statuses.get(branch_id)
        status = scripted.pop(0) if scripted else 200
        if status == 200:
            self.deleted.append(branch_id)
        return status, None


class GitHub:
    def __init__(self, open_heads: list[str], branches: list[str], closed_heads: list[str]):
        self.open_heads = open_heads
        self.branches = branches
        self.closed_heads = closed_heads
        self.asked: list[str] = []

    def __call__(self, method: str, url: str) -> tuple[int, Any]:
        assert method == "GET"
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == f"/repos/{REPO}/branches":
            return 200, self._page([{"name": n} for n in self.branches], query)
        assert parsed.path == f"/repos/{REPO}/pulls", url
        if query["state"] == "open":
            pulls = [{"head": {"ref": h}} for h in self.open_heads]
            return 200, self._page(pulls, query)
        owner, head = query["head"].split(":", 1)
        assert owner == "mendari-hq" and query["state"] == "all"
        self.asked.append(head)
        found = head in self.closed_heads or head in self.open_heads
        return 200, [{"head": {"ref": head}}] if found else []

    @staticmethod
    def _page(items: list[Any], query: dict[str, str]) -> list[Any]:
        size, page = int(query["per_page"]), int(query["page"])
        return items[(page - 1) * size : page * size]


# What the project held on 2026-10-08, by kind: names here are made up.
BRANCHES = [
    Branch("br-main", "main", default=True),
    Branch("br-open", "preview/claude/open-pr"),
    Branch("br-merged", "preview/claude/merged"),  # merged, its Git branch deleted
    Branch("br-kept", "preview/claude/merged-kept"),  # merged, its Git branch kept
    Branch("br-late", "preview/feat/rebuilt-after-merge"),  # the preview finished after the merge
    Branch("br-pushed", "preview/claude/pushed"),  # pushed, its pull request not open yet
    Branch("br-gone", "preview/claude/no-pr-gone"),  # pushed, never proposed, deleted
    Branch("br-protected", "preview/claude/protected", protected=True),
    Branch("br-odd", "preview/odd name;rm -rf"),
    Branch("br-dev", "dev"),
]
OPEN = ["claude/open-pr", "claude/other-open-pr"]
GIT = ["main", "claude/open-pr", "claude/other-open-pr", "claude/merged-kept", "claude/pushed"]
CLOSED = ["claude/merged", "claude/merged-kept", "feat/rebuilt-after-merge"]
STALE = ["br-gone", "br-kept", "br-late", "br-merged"]


def _github() -> GitHub:
    return GitHub(OPEN, GIT, CLOSED)


def test_a_branch_is_deleted_once_no_pull_request_needs_it() -> None:
    github = _github()
    found = cleanup.stale(
        BRANCHES, set(OPEN), set(GIT), lambda head: cleanup.had_pull_request(github, REPO, head)
    )
    assert sorted(b.id for b in found) == STALE
    # Only a branch that still exists, with no open pull request, is looked up.
    assert sorted(github.asked) == ["claude/merged-kept", "claude/pushed"]


def test_the_run_deletes_them_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    neon = Neon(BRANCHES)
    assert cleanup.main([], ENV, neon, _github()) == 0
    assert sorted(neon.deleted) == STALE
    out = capsys.readouterr().out
    assert "10 Neon branches, 4 no pull request needs" in out
    assert "deleted preview/claude/merged (br-merged)" in out
    # A name that is not plain is left alone and only its id is printed.
    assert "::warning::preview branches with an unusual name, left alone: ['br-odd']" in out
    assert "rm -rf" not in out


def test_a_dry_run_deletes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    neon = Neon(BRANCHES)
    assert cleanup.main(["--dry-run"], ENV, neon, _github()) == 0
    assert neon.deleted == []
    out = capsys.readouterr().out
    assert out.count("would delete ") == len(STALE)


def test_a_locked_project_is_retried_and_a_branch_already_gone_is_done(
    capsys: pytest.CaptureFixture[str],
) -> None:
    slept: list[float] = []
    neon = Neon(BRANCHES, {"br-merged": [423, 423, 200], "br-kept": [404]})
    assert cleanup.main([], ENV, neon, _github(), sleep=slept.append) == 0
    assert slept == [1, 2]
    assert sorted(neon.deleted) == ["br-gone", "br-late", "br-merged"]
    assert "already gone: preview/claude/merged-kept (br-kept)" in capsys.readouterr().out


def test_a_failed_delete_fails_the_run_after_the_others(capsys: pytest.CaptureFixture[str]) -> None:
    neon = Neon(BRANCHES, {"br-merged": [500], "br-late": [423] * cleanup.LOCKED_TRIES})
    assert cleanup.main([], ENV, neon, _github(), sleep=lambda _: None) == 1
    assert sorted(neon.deleted) == ["br-gone", "br-kept"]
    out = capsys.readouterr().out
    assert "::error::preview/claude/merged (br-merged) was not deleted: HTTP 500" in out
    assert "::error::preview/feat/rebuilt-after-merge (br-late) was not deleted: HTTP 423" in out


def test_every_page_is_read() -> None:
    many = [Branch(f"br-{i:03}", f"preview/claude/b{i:03}") for i in range(7)]
    neon = Neon([Branch("br-main", "main", default=True), *many])
    neon.page_size = 3
    assert len(cleanup.neon_branches(neon, "proj-1")) == 8
    heads = [f"claude/b{i:03}" for i in range(cleanup.PAGE + 1)]
    github = GitHub(heads, heads, [])
    assert cleanup.open_heads(github, REPO) == set(heads)
    assert cleanup.git_branches(github, REPO) == set(heads)


def test_an_api_that_refuses_stops_the_run() -> None:
    def refused(method: str, url: str) -> tuple[int, Any]:
        return 401, None

    with pytest.raises(SystemExit, match=r"Neon, listing the branches, answered HTTP 401"):
        cleanup.main([], ENV, refused, _github())
    with pytest.raises(SystemExit, match=r"GitHub, listing the pull requests, answered HTTP 401"):
        cleanup.main([], ENV, Neon(BRANCHES), refused)


@pytest.mark.parametrize("missing", ["NEON_PROJECT_ID", "GITHUB_REPOSITORY", "NEON_API_KEY"])
def test_a_missing_setting_fails_loudly(missing: str) -> None:
    environ = {**ENV, "NEON_API_KEY": "k", "GITHUB_TOKEN": "t"}
    del environ[missing]
    with pytest.raises(SystemExit, match=f"{missing} is not set"):
        cleanup.main([], environ)


def test_an_error_answer_keeps_neither_its_body_nor_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def urlopen(request: Any, timeout: float) -> Any:
        seen.append(request)
        body = io.BytesIO(b'{"message": "remote detail"}')
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", Message(), body)

    monkeypatch.setattr(cleanup.urllib.request, "urlopen", urlopen)
    fetch = cleanup.fetcher("api-key", "application/json")
    assert fetch("GET", f"{cleanup.NEON_API}/projects/p/branches") == (403, None)
    assert seen[0].get_header("Authorization") == "Bearer api-key"


def test_the_workflow_runs_mains_script_on_close_on_a_schedule_and_by_hand() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    on = workflow[True]  # YAML 1.1 reads a bare `on` as true
    assert on["pull_request"] == {"types": ["closed"]}
    assert on["schedule"] == [{"cron": "41 */6 * * *"}]
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is False
    (job,) = workflow["jobs"].values()
    assert "github.actor != 'dependabot[bot]'" in job["if"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    checkout, run = job["steps"]
    # The script that holds the key is main's, never a closing pull request's.
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["sparse-checkout"] == "scripts/neon_preview_cleanup.py"
    assert 'python3 scripts/neon_preview_cleanup.py "$@"' in run["run"]
    assert set(run["env"]) == {"NEON_API_KEY", "NEON_PROJECT_ID", "GITHUB_TOKEN", "DRY_RUN"}
