# SPDX-License-Identifier: Apache-2.0
"""The workflows are safe to run on pull requests from forks, once the repository is public: no
trigger runs a fork's code with secrets or a write token, every token is read-only unless a job
publishes from a protected environment in the release workflow, which only a person starts, no
expression is pasted into a shell script, and a pack directory a pull request names is data,
never code. Nothing is published on a tag push. Also: every file has a code owner, the history
is scanned for secrets, and every commit of a pull request is signed off."""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# Triggers that run in the base repository's context (its secrets, a write token) with code or
# text a fork controls. None is used, and a new one needs a deliberate change here.
UNSAFE_TRIGGERS = {"pull_request_target", "workflow_run", "issue_comment", "pull_request_review"}
ALLOWED_TRIGGERS = {"push", "pull_request", "workflow_dispatch"}
# The only write permissions: each on a job that publishes from a protected environment, in the
# release workflow, which only a manual dispatch starts.
ALLOWED_WRITES = {
    ("release.yml", "publish-pypi"): ({"id-token"}, "pypi"),
    ("release.yml", "publish-images"): ({"packages"}, "ghcr"),
}
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SECRET = re.compile(r"\$\{\{[^}]*\bsecrets\.(\w+)")
SAME_REPOSITORY = "github.event.pull_request.head.repo.full_name == github.repository"


def _workflow(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), path
    return loaded


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    assert isinstance(on, dict)
    return on


def _writes(permissions: Any) -> set[str]:
    if permissions in (None, {}):
        return set()
    assert isinstance(permissions, dict), f"permissions must be listed, not {permissions!r}"
    return {scope for scope, level in permissions.items() if level == "write"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_trigger_runs_fork_code_in_the_base_context(path: Path) -> None:
    triggers = set(_triggers(_workflow(path)))
    assert not triggers & UNSAFE_TRIGGERS, f"{path.name}: {triggers & UNSAFE_TRIGGERS}"
    assert triggers <= ALLOWED_TRIGGERS, f"{path.name}: review {triggers - ALLOWED_TRIGGERS}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_tokens_are_read_only_unless_a_protected_release_job_publishes(path: Path) -> None:
    workflow = _workflow(path)
    assert "permissions" in workflow, f"{path.name}: say what the token may do"
    assert _writes(workflow["permissions"]) == set(), f"{path.name}: write at workflow level"
    for name, job in workflow["jobs"].items():
        writes = _writes(job.get("permissions"))
        if not writes:
            continue
        allowed, environment = ALLOWED_WRITES.get((path.name, name), (set(), None))
        assert writes <= allowed, f"{path.name}:{name} writes {writes - allowed}"
        assert job.get("environment") == environment, f"{path.name}:{name} needs its environment"
        assert set(_triggers(workflow)) == {"workflow_dispatch"}, path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_nothing_starts_on_a_tag_push(path: Path) -> None:
    push = _triggers(_workflow(path)).get("push") or {}
    assert "tags" not in push and "tags-ignore" not in push, path.name


def test_a_release_publishes_only_by_hand_after_its_build_from_a_guarded_environment() -> None:
    release = _workflow(RELEASE)
    assert set(_triggers(release)) == {"workflow_dispatch"}
    inputs = _triggers(release)["workflow_dispatch"]["inputs"]
    # No tag input: the run itself is on the tag (its `github.ref`, which the environments' `v*`
    # rule checks), and every job checks out that run's one commit.
    assert set(inputs) == {"publish"}
    assert inputs["publish"] == {**inputs["publish"], "type": "boolean", "default": False}
    jobs = release["jobs"]
    checkouts = [
        step["with"]
        for job in jobs.values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert len(checkouts) == len(jobs)
    assert all(checkout["ref"] == "${{ github.sha }}" for checkout in checkouts)
    assert "inputs.tag" not in RELEASE.read_text()
    publishing = {name for name, job in jobs.items() if _writes(job.get("permissions"))}
    assert publishing == {"publish-pypi", "publish-images"}
    for name in publishing:
        job = jobs[name]
        assert set(job["needs"]) == {"build", "images"}, name
        assert job["if"] == "inputs.publish && !github.event.repository.private", name
        steps = job["steps"]
        guard = next(i for i, step in enumerate(steps) if "check_release.py" in step.get("run", ""))
        assert (
            steps[guard]["run"]
            == f"python3 scripts/check_release.py environment {job['environment']}"
        )
        assert steps[guard]["env"] == {"GH_TOKEN": "${{ github.token }}"}
        # Only the checkout comes before the guard: nothing is downloaded or pushed until it passes.
        assert [step["uses"].split("@")[0] for step in steps[:guard]] == ["actions/checkout"], name
    build_steps = jobs["build"]["steps"]
    first = next(step for step in build_steps if "run" in step)
    # The run's ref and the version are checked first, before anything is built.
    assert first["run"] == 'python3 scripts/check_release.py version "$REF"'
    assert first["env"] == {"REF": "${{ github.ref }}"}
    build = [step.get("run", "") for step in build_steps]
    assert any("uv run pytest" in run for run in build)
    assert any("check_sdist.py" in run and "check_excluded.py" in run for run in build)
    images = "\n".join(step.get("run", "") for step in jobs["images"]["steps"])
    assert "mendari test" in images and 'check_excluded.py --image "$image"' in images
    assert jobs["images"]["needs"] == "build"
    pushed = "\n".join(step.get("run", "") for step in jobs["publish-images"]["steps"])
    assert "docker load" in pushed and "docker build" not in pushed  # the tested image, unrebuilt


def test_ci_checks_what_would_be_published_for_excluded_paths() -> None:
    ci = _workflow(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]

    def runs(job: str) -> str:
        return "\n".join(step.get("run", "") for step in ci[job]["steps"])

    assert "check_excluded.py /tmp/dist/mendari-*.tar.gz /tmp/dist/*.whl" in runs("hub-e2e")
    assert "check_excluded.py site" in runs("docs")
    assert "check_excluded.py --image mendari-engine:ci" in runs("docker")


def test_every_commit_of_a_pull_request_is_signed_off() -> None:
    dco = _workflow(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["dco"]
    assert dco["if"] == "github.event_name == 'pull_request'"
    assert dco["steps"][0]["with"]["fetch-depth"] == 0  # the pull request's whole range
    (check,) = [step for step in dco["steps"] if "run" in step]
    assert check["env"] == {
        "BASE": "${{ github.event.pull_request.base.sha }}",
        "HEAD": "${{ github.event.pull_request.head.sha }}",
    }
    assert check["run"] == 'python3 scripts/check_dco.py --base "$BASE" --head "$HEAD"'


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_is_pasted_into_a_shell_script(path: Path) -> None:
    # `${{ … }}` is substituted before the shell parses the script, so a branch name or a title
    # would run as code. Values reach scripts through `env:` instead.
    for job in _workflow(path)["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", ""), f"{path.name}: {step.get('name', step)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_never_reach_a_fork_pull_request(path: Path) -> None:
    workflow = _workflow(path)
    if "pull_request" not in _triggers(workflow):
        return
    for name, job in workflow["jobs"].items():
        used = set(SECRET.findall(yaml.safe_dump(job, width=10_000))) - {"GITHUB_TOKEN"}
        if used:
            assert SAME_REPOSITORY in str(job.get("if", "")), f"{path.name}:{name}"


def test_every_file_has_a_code_owner() -> None:
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    rules = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    assert rules[0] == ["*", "@Junebugg1214"]


def test_the_history_is_scanned_for_secrets() -> None:
    ci = _workflow(ROOT / ".github" / "workflows" / "ci.yml")
    steps = ci["jobs"]["secrets"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0  # every commit, not only the last
    (scan,) = [step["run"] for step in steps if "run" in step]
    assert "ghcr.io/gitleaks/gitleaks:" in scan and "--config /repo/.gitleaks.toml" in scan
    assert "--exit-code 1" in scan
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"] == {"useDefault": True}
    # Each allowance is narrow: one file, one rule, the match itself.
    for allowance in config["allowlists"]:
        assert allowance["condition"] == "AND" and allowance["description"]
        assert allowance["paths"] and allowance["regexes"] and allowance["targetRules"]
    assert [a["paths"] for a in config["allowlists"]] == [[r"^deploy/onprem/\.env\.example$"]]


# -- the pack workflow ----------------------------------------------------------------------------


def _changed_packs() -> ModuleType:
    path = ROOT / "scripts" / "changed_packs.py"
    spec = importlib.util.spec_from_file_location("changed_packs", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pack_directory_is_data_never_code() -> None:
    packs = (ROOT / ".github" / "workflows" / "packs.yml").read_text()
    assert "scripts/changed_packs.py" in packs
    assert "read -r -d ''" in packs  # NUL-separated names, read whole
    for unsafe in ("resolve_pack('$", '"$dir")', "cut -d/", "$(git diff", "for name in $("):
        assert unsafe not in packs, unsafe


def test_pack_check_runs_on_every_pull_request() -> None:
    # A required check must report on every pull request: a path filter would leave it pending.
    workflow = _workflow(ROOT / ".github" / "workflows" / "packs.yml")
    assert _triggers(workflow) == {"pull_request": None}
    assert list(workflow["jobs"]) == ["pack-check"]
    steps = workflow["jobs"]["pack-check"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0  # the base commit, for the diff and benchmarks
    find = next(step for step in steps if step.get("name") == "Find the changed packs")
    assert "no pack changes" in find["run"]
    maintained = next(step for step in steps if "pack_gate.py" in step.get("run", ""))
    assert 'case "$dir" in packs/*)' in maintained["run"]  # community packs go the other way
    (community,) = [step for step in steps if "community_check.py" in step.get("run", "")]
    # Who made the change and who owns the repository reach the script through `env` only. The
    # author is the pull request's, never `github.actor` (whoever caused the run: the owner
    # updating a fork's branch would be it), and the branch must be in this repository.
    assert community["env"] == {
        "BASE": "${{ github.event.pull_request.base.sha }}",
        "AUTHOR": "${{ github.event.pull_request.user.login }}",
        "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
        "REPO": "${{ github.repository }}",
        "OWNER": "${{ github.repository_owner }}",
    }
    for argument in (
        '--actor "$AUTHOR"',
        '--owner "$OWNER"',
        '--head-repo "$HEAD_REPO"',
        '--repo "$REPO"',
    ):
        assert argument in community["run"]
    assert "github.actor" not in json.dumps(community)


def test_ci_runs_every_community_packs_tests() -> None:
    ci = _workflow(ROOT / ".github" / "workflows" / "ci.yml")
    scripts = [step.get("run", "") for step in ci["jobs"]["test"]["steps"]]
    (loop,) = [run for run in scripts if "community-packs/*/pack.yaml" in run]
    assert 'uv run mendari test "$(dirname "$manifest")"' in loop and "exit $failed" in loop


def test_changed_files_map_to_their_pack(tmp_path: Path) -> None:
    for pack in ("packs/inbox", "packs/my pack; $(touch x)", "packs/group/nested", "packs/a/b/c"):
        (tmp_path / pack).mkdir(parents=True)
        (tmp_path / pack / "pack.yaml").write_text("id: x/y\n")
    changed = [
        "packs/inbox/rules/red_flags.yaml",
        "packs/inbox/pack.yaml",
        "packs/my pack; $(touch x)/tests/cases.yaml",
        "packs/group/nested/data/test.jsonl",
        "packs/a/b/c/d/e/deep.yaml",  # deeper than a pack.yaml can be: belongs to packs/a/b/c
        "packs/removed/pack.yaml",  # gone from the tree: reported as its top folder
        "packs/README.md",  # directly in packs/: no pack
        "src/mendari/cli.py",
        "community-packs/visit-sorter/logic/rules.yaml",
        "community-packs/visit-sorter/nested/pack.yaml",  # one folder is one community pack
        "community-packs/gone; $(touch x)/pack.yaml",
        "community-packs/MAINTAINERS.yaml",  # directly in community-packs/: no pack
    ]
    assert _changed_packs().pack_dirs(changed, tmp_path) == [
        "community-packs/gone; $(touch x)",
        "community-packs/visit-sorter",
        "packs/a/b/c",
        "packs/group/nested",
        "packs/inbox",
        "packs/my pack; $(touch x)",
        "packs/removed",
    ]
