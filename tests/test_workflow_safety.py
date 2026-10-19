# SPDX-License-Identifier: Apache-2.0
"""The workflows are safe to run on pull requests from forks, once the repository is public: no
trigger runs a fork's code with secrets or a write token, every token is read-only unless a job
publishes from its environment in a workflow only a person starts (the release jobs, whose
environments must have reviewers, and the docs deploy), no
expression is pasted into a shell script, and a pack directory a pull request names is data,
never code. Nothing is published on a tag push. Also: every file has a code owner, the history
is scanned for secrets, and every commit of a pull request is signed off."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
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
ALLOWED_TRIGGERS = {"push", "pull_request", "workflow_dispatch", "schedule"}
# A schedule runs the default branch's own workflow and code, no one's input: only the weekly scan
# of every ref, the sweep of Neon preview branches no pull request needs and the weekly Inferno
# conformance run use one.
SCHEDULED = {"secrets.yml", "neon-preview-cleanup.yml", "conformance.yml"}
# The only write permissions, each on a job that publishes from its own environment in a
# workflow only a manual dispatch starts: the release workflow's publishing jobs (GUARDED below:
# their environments must have reviewers) and the docs deploy (`github-pages`, which allows only
# the default branch; no reviewer check).
ALLOWED_WRITES = {
    ("release.yml", "publish-pypi"): ({"id-token"}, "pypi"),
    ("release.yml", "publish-testpypi"): ({"id-token"}, "testpypi"),
    ("release.yml", "publish-images"): ({"packages"}, "ghcr"),
    ("docs.yml", "deploy"): ({"pages", "id-token"}, "github-pages"),
}
# Publishing jobs that must first check, through the API, that their environment has reviewers.
GUARDED = {"publish-pypi", "publish-testpypi", "publish-images"}
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SECRET = re.compile(r"\$\{\{[^}]*\bsecrets\.(\w+)")
EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")
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
    assert ("schedule" in triggers) == (path.name in SCHEDULED), path.name


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
    assert set(inputs) == {"publish", "target"}
    assert inputs["publish"] == {**inputs["publish"], "type": "boolean", "default": False}
    # A publishing run rehearses on TestPyPI unless PyPI is chosen.
    assert inputs["target"]["options"] == ["testpypi", "pypi"]
    assert inputs["target"]["default"] == "testpypi"
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
    assert publishing == GUARDED
    targets = {"publish-pypi": "pypi", "publish-images": "pypi", "publish-testpypi": "testpypi"}
    for name in publishing:
        job = jobs[name]
        # Images also wait for their corresponding source (test_images_are_pushed_only_...).
        extra = {"image-sources"} if name == "publish-images" else set()
        assert set(job["needs"]) == {"build", "images"} | extra, name
        assert job["if"] == (
            f"inputs.publish && inputs.target == '{targets[name]}' "
            "&& !github.event.repository.private"
        ), name
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
    # Built once for both architectures, and each architecture's image, from that archive, tested.
    assert "--platform linux/amd64,linux/arm64" in images and "type=oci" in images
    assert "for arch in amd64 arm64" in images and '--platform "linux/${arch}"' in images
    assert jobs["images"]["needs"] == "build"
    pushed = "\n".join(step.get("run", "") for step in jobs["publish-images"]["steps"])
    assert 'skopeo copy --all "oci-archive:' in pushed  # the tested archive, as it is
    assert "build" not in pushed
    # The rehearsal: TestPyPI serves the tested wheel byte for byte, and only Mendari comes from
    # it (its dependencies from PyPI, hash-checked from uv.lock).
    verify = jobs["verify-testpypi"]
    assert verify["needs"] == "publish-testpypi" and not _writes(verify.get("permissions"))
    checked = "\n".join(step.get("run", "") for step in verify["steps"])
    assert "--index-url https://test.pypi.org/simple/" in checked and "--no-deps" in checked
    assert 'cmp "$served"' in checked and "--require-hashes" in checked
    assert "mendari test mendari/patient-inbox-router" in checked
    assert "extra-index-url" not in RELEASE.read_text()  # no name on TestPyPI stands in for one


def test_images_are_pushed_only_when_they_comply_and_with_their_source_first() -> None:
    """The owner's decision of 2026-10-07: images wait until they comply. Each image, for each
    architecture, is checked for its licences, notices and Debian's copyright files, and the SBOM
    BuildKit attached to it is scanned for its licences; any failure fails the job the publishing
    job needs. The source is collected from what the checks saw, and pushed before the image."""
    jobs = _workflow(RELEASE)["jobs"]
    images = "\n".join(step.get("run", "") for step in jobs["images"]["steps"])
    assert '--attest "type=sbom,generator=' in images
    loop = images[images.index("for arch in amd64 arm64") :]
    for command in (
        'check_image_licences.py --extract-sbom "/tmp/${NAME}.tar" --arch "$arch"',
        'check_image_licences.py --image "$image"',
        '--sbom "/tmp/sbom/${NAME}-${arch}.attestation.json"',
        '--sources-out "/tmp/sbom/${NAME}-${arch}.sources.json"',
    ):
        assert command in loop, command
    assert not any("continue-on-error" in step or "if" in step for step in jobs["images"]["steps"])
    uploads = [s["with"] for s in jobs["images"]["steps"] if "upload-artifact" in s.get("uses", "")]
    assert {
        "name": "sbom-${{ matrix.name }}",
        "path": "/tmp/sbom/",
        "retention-days": 90,
    } in uploads

    sources = jobs["image-sources"]
    assert sources["needs"] == "images" and not _writes(sources.get("permissions"))
    collected = "\n".join(step.get("run", "") for step in sources["steps"])
    assert (
        "scripts/image_sources.py --out /tmp/sources --sboms /tmp/sbom /tmp/sbom/*.sources.json"
        in collected
    )
    assert "-f licenses/image/sources.Dockerfile" in collected

    pushed = "\n".join(step.get("run", "") for step in jobs["publish-images"]["steps"])
    source = pushed.index('"docker://ghcr.io/${OWNER,,}/${NAME}:${TAG}-sources"')
    image = pushed.index('"docker://ghcr.io/${OWNER,,}/${NAME}:${TAG}"')
    assert source < image  # never an image without its source beside it
    assert "oci-archive:/tmp/sources/image-sources.tar" in pushed


def test_ci_checks_the_images_licences() -> None:
    jobs = _workflow(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]
    runs = "\n".join(step.get("run", "") for step in jobs["docker"]["steps"])
    assert "python3 scripts/check_image_licences.py --image mendari-engine:ci" in runs
    # And the SBOM scan, as a release runs it: a base image bump that changes a licence fails in
    # its own pull request (the generator and builder are release.yml's: test_supply_chain).
    sbom = jobs["image-sbom"]
    assert not sbom.get("if") and not _writes(sbom.get("permissions"))
    # Only the Docker Hub sign-in may be skipped (a fork's pull request pulls anonymously).
    checked = [s for s in sbom["steps"] if not s.get("uses", "").startswith("docker/login-action@")]
    assert len(checked) == len(sbom["steps"]) - 1
    assert not any("continue-on-error" in step or "if" in step for step in checked)
    scanned = "\n".join(step.get("run", "") for step in sbom["steps"])
    for command in (
        '--attest "type=sbom,generator=',
        "check_image_licences.py --extract-sbom /tmp/mendari-engine.tar --arch amd64",
        "check_image_licences.py --image mendari-engine:sbom",
        "--sbom /tmp/sbom/mendari-engine-amd64.attestation.json",
        "--sbom-out /tmp/sbom/mendari-engine-amd64.spdx.json",
    ):
        assert command in scanned, command


def test_the_docs_deploy_by_hand_from_a_strict_checked_build() -> None:
    docs = _workflow(ROOT / ".github" / "workflows" / "docs.yml")
    assert set(_triggers(docs)) == {"workflow_dispatch"}
    jobs = docs["jobs"]
    built = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    for command in ("mkdocs build --strict", "check_excluded.py site", "check_site_notice.py site"):
        assert command in built, command
    (upload,) = [
        step
        for step in jobs["build"]["steps"]
        if step.get("uses", "").split("@")[0] == "actions/upload-pages-artifact"
    ]
    assert upload["with"] == {"path": "site"}  # the checked build, nothing else
    assert docs["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    deploy = jobs["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["if"] == "!github.event.repository.private"
    assert [step["uses"].split("@")[0] for step in deploy["steps"]] == ["actions/deploy-pages"]


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


def _unguarded_secrets(job: dict[str, Any]) -> set[str]:
    # A job may use a secret if the job runs only for this repository's own pull requests, or if
    # each use is guarded where it is made: by its step's `if`, or inside the expression itself
    # (a service's credentials, which have no `if`). The rest of the job still runs for a fork.
    if SAME_REPOSITORY in str(job.get("if", "")):
        return set()
    steps = [s for s in job.get("steps", []) if SAME_REPOSITORY not in str(s.get("if", ""))]
    used: set[str] = set()
    for expression in EXPRESSION.findall(yaml.safe_dump(dict(job, steps=steps), width=10_000)):
        if SAME_REPOSITORY not in expression:
            used |= set(SECRET.findall(expression)) - {"GITHUB_TOKEN"}
    return used


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_never_reach_a_fork_pull_request(path: Path) -> None:
    workflow = _workflow(path)
    if "pull_request" not in _triggers(workflow):
        return
    for name, job in workflow["jobs"].items():
        assert not _unguarded_secrets(job), f"{path.name}:{name}"


def test_the_secret_guard_catches_an_unguarded_use() -> None:
    secret = "${{ secrets.DOCKERHUB_TOKEN }}"
    guarded = f"${{{{ {SAME_REPOSITORY} && secrets.DOCKERHUB_TOKEN || '' }}}}"
    assert _unguarded_secrets({"steps": [{"with": {"password": secret}}]})
    assert _unguarded_secrets({"services": {"db": {"credentials": {"password": secret}}}})
    assert not _unguarded_secrets(
        {"steps": [{"if": SAME_REPOSITORY, "with": {"password": secret}}]}
    )
    assert not _unguarded_secrets({"services": {"db": {"credentials": {"password": guarded}}}})
    assert not _unguarded_secrets(
        {"if": SAME_REPOSITORY, "steps": [{"with": {"password": secret}}]}
    )


def test_every_file_has_a_code_owner() -> None:
    # One person on the `*` rule: the pack check takes that person for the repository's owner.
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    rules = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    assert rules[0] == ["*", "@Junebugg1214"]


SCAN = {"name": "gitleaks over the full history", "run": "sh scripts/secret_scan.sh"}
CONTROL = {
    "name": "The positive control finds the planted keys",
    "run": "sh scripts/secret_scan_control.sh",
}


def _no_way_around(job: dict[str, Any]) -> None:
    assert not {"if", "continue-on-error"} & set(job)
    for step in job["steps"]:
        assert not {"if", "continue-on-error"} & set(step), step


def test_the_history_is_scanned_for_secrets() -> None:
    secrets = _workflow(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["secrets"]
    _no_way_around(secrets)
    checkout, scan, control = secrets["steps"]
    assert checkout["with"]["fetch-depth"] == 0  # every commit, not only the last
    assert (scan, control) == (SCAN, CONTROL)
    script = (ROOT / "scripts" / "secret_scan.sh").read_text()
    # Every ref, merge resolutions included; this repository's rules; a finding is exit 3; a git
    # error, which gitleaks logs and then calls clean, fails; and no ignore file.
    assert "--diff-filter=tuxdb --diff-merges=remerge'" in script  # gitleaks' defaults, and merges
    assert '"$config:/gitleaks.toml:ro"' in script and "--config /gitleaks.toml" in script
    assert "--exit-code 3" in script and "--no-color" in script
    assert "if grep -q ' ERR ' \"$log\"; then" in script and ".gitleaksignore" in script
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert config["extend"] == {"useDefault": True}
    # Each allowance is narrow: one file, one rule, the match itself.
    for allowance in config["allowlists"]:
        assert allowance["condition"] == "AND" and allowance["description"]
        assert allowance["paths"] and allowance["regexes"] and allowance["targetRules"]
    assert [a["paths"] for a in config["allowlists"]] == [[r"^deploy/onprem/\.env\.example$"]]
    # No other allowance: no ignore file, no inline marker.
    assert not (ROOT / ".gitleaksignore").exists()
    marker = "gitleaks" + ":allow"
    found = subprocess.run(["git", "grep", "-l", marker], cwd=ROOT, capture_output=True, text=True)
    assert found.stdout == ""


def test_every_ref_is_scanned_weekly_with_a_positive_control() -> None:
    """LP-02: each branch and tag, and each pull request's head, which the pull request's own
    scan never reaches; and the release scans the history it is built from."""
    secrets = _workflow(ROOT / ".github" / "workflows" / "secrets.yml")
    assert set(_triggers(secrets)) == {"schedule", "workflow_dispatch"}
    assert secrets["permissions"] == {"contents": "read"}
    (job,) = secrets["jobs"].values()
    _no_way_around(job)
    checkout, fetch, scan, control = job["steps"]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    assert fetch["env"] == {"TOKEN": "${{ github.token }}"}  # read-only, given to one command
    assert "'+refs/pull/*/head:refs/pull/*/head'" in fetch["run"]
    assert 'test "$heads" -gt 0 ||' in fetch["run"]  # a fetch that brought none fails
    assert (scan["run"], control["run"]) == (SCAN["run"], CONTROL["run"])
    build = _workflow(RELEASE)["jobs"]["build"]
    assert build["steps"][0]["with"]["fetch-depth"] == 0
    runs = [step.get("run", "") for step in build["steps"]]
    assert runs.index(SCAN["run"]) < runs.index("uv build")


def test_conformance_runs_weekly_or_by_hand_with_no_secret_and_commits_nothing() -> None:
    """conformance.yml (sandbox/inferno/README.md): Inferno against the open engine on synthetic
    data, never on a pull request, with a read-only token and no secret. The checker's verdict
    is the job's: only Inferno's own exit status (3 when a test does not pass) is set aside, and
    the raw output, which can quote payloads, is an artifact for 7 days, never committed."""
    path = ROOT / ".github" / "workflows" / "conformance.yml"
    workflow = _workflow(path)
    assert set(_triggers(workflow)) == {"schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    text = path.read_text()
    for unwanted in (r"secrets\.", r"git (push|commit)", r"\bgh\s", "continue-on-error"):
        assert not re.search(unwanted, text), unwanted
    (job,) = workflow["jobs"].values()
    assert "permissions" not in job and "if" not in job
    steps = job["steps"]
    (execute,) = [s for s in steps if "inferno execute" in s.get("run", "")]
    assert "--groups 1 3 4" in execute["run"]  # group 2, Backend Services, waits for LP-23
    assert execute["run"].rstrip().endswith('|| echo "inferno execute exited $?"')
    (verdict,) = [s for s in steps if "inferno_check.py" in s.get("run", "")]
    assert "if" not in verdict and "||" not in verdict["run"]
    assert "--expected sandbox/inferno/dtr_payer_server_v220_expected.json" in verdict["run"]
    assert steps.index(execute) < steps.index(verdict)
    (upload,) = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload["with"]["path"] == "var/inferno/" and upload["with"]["retention-days"] <= 7
    assert "/var/" in (ROOT / ".gitignore").read_text().splitlines()  # never committed
    assert steps[-1] == {
        "if": "always()",
        "run": "docker compose -f sandbox/inferno/docker-compose.yml down --volumes",
    }


# -- the FHIR packages the HL7 validator loads (DV-b) ---------------------------------------------

PACKAGE_LOCK = ROOT / "scripts" / "fhir_packages.lock"
# `--ig VALUE` to scripts/fhir_validate.py, or `-ig VALUE` (`-defn`) to the validator itself,
# the flag quoted or not, the value after `=` or a space, quoted or not.
IG_ARGUMENT = re.compile(r"(?<!\S)[\"']?(?:--ig|-ig|-defn)[\"']?(?:=|\s+)[\"']?([^\s\"']+)")
# The shell joins a line that ends in a backslash with the next one.
CONTINUED = re.compile(r"\\\r?\n")


def _locked_tarballs() -> set[str]:
    rows = [line.split() for line in PACKAGE_LOCK.read_text().splitlines()]
    return {f"var/fhir-packages/{r[0]}-{r[1]}.tgz" for r in rows if r and not r[0].startswith("#")}


def _unpinned_igs(script: str, locked: set[str]) -> list[str]:
    """What a script asks the validator to load that the lock does not pin: a package by name
    (`name#version`, which the validator would fetch by version alone), a value only the shell
    knows, or a tarball the lock does not name. A folder of resources is the repository's own."""
    return [
        value
        for value in IG_ARGUMENT.findall(CONTINUED.sub(" ", script))
        if "#" in value or "$" in value or (value.endswith(".tgz") and value not in locked)
    ]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_loads_a_fhir_package_the_lock_does_not_pin(path: Path) -> None:
    locked = _locked_tarballs()
    for job in _workflow(path)["jobs"].values():
        for step in job.get("steps", []):
            script = step.get("run", "")
            assert not _unpinned_igs(script, locked), f"{path.name}: {step.get('name', script)}"
            # And the validator runs offline, so a dependency comes from the checked cache too.
            if "scripts/fhir_validate.py" in script:
                assert "--offline" in script.split(), f"{path.name}: {step.get('name', script)}"
            if "validator_cli.jar" in script and "java " in script:
                assert "-no-http-access" in script.split(), f"{path.name}: {script}"


def test_the_package_check_catches_a_package_by_name() -> None:
    locked = {"var/fhir-packages/hl7.fhir.us.davinci-crd-2.2.1.tgz"}
    assert _unpinned_igs("x --ig hl7.fhir.us.davinci-crd#2.2.1 --ig var/a", locked) == [
        "hl7.fhir.us.davinci-crd#2.2.1"
    ]
    assert _unpinned_igs("java -jar v.jar -ig hl7.fhir.us.core#6.1.0", locked)
    assert _unpinned_igs("x --ig=hl7.fhir.us.core#6.1.0", locked)
    assert _unpinned_igs('x --ig "$PACKAGE"', locked)
    assert _unpinned_igs("x --ig var/fhir-packages/hl7.fhir.us.core-6.1.0.tgz", locked)
    assert not _unpinned_igs(
        "x --ig var/fhir-terminology --ig var/fhir-packages/hl7.fhir.us.davinci-crd-2.2.1.tgz",
        locked,
    )
    # The value on the next line, after a backslash, and a quoted flag.
    continued = "uv run python scripts/fhir_validate.py --offline \\\n  --ig \\\n  {}\n"
    assert _unpinned_igs(continued.format("hl7.fhir.us.davinci-crd#2.2.1"), locked) == [
        "hl7.fhir.us.davinci-crd#2.2.1"
    ]
    assert _unpinned_igs("x --ig \\\r\n hl7.fhir.us.core#6.1.0", locked)
    assert not _unpinned_igs(continued.format(next(iter(locked))), locked)
    assert _unpinned_igs('java -jar v.jar "-ig" "hl7.fhir.us.core#6.1.0"', locked) == [
        "hl7.fhir.us.core#6.1.0"
    ]
    assert _unpinned_igs("x '--ig' hl7.fhir.us.core#6.1.0", locked)
    assert _unpinned_igs("x '--ig=hl7.fhir.us.core#6.1.0'", locked)
    assert not _unpinned_igs("x --ignore a#1 --igloo b#2", locked)  # not the flag


def test_the_validator_loads_only_the_locked_packages_each_checked() -> None:
    """The fhir-conformance job downloads each package the lock pins and checks it with
    `sha256sum -c`, every run (a cached tarball too), before the validator first runs; each
    validator run then builds a package cache of its own from them (`--offline`), so no step
    fills a cache the runs share. Its cache key follows the lock, and no validator's package
    cache is ever restored."""
    job = _workflow(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["fhir-conformance"]
    steps = job["steps"]
    (cache,) = [s for s in steps if s.get("uses", "").startswith("actions/cache@")]
    assert "hashFiles('scripts/fhir_packages.lock')" in cache["with"]["key"]
    assert "${{ env.VALIDATOR_VERSION }}" in cache["with"]["key"]
    assert cache["with"]["path"].split() == ["var/validator/validator_cli.jar", "var/fhir-packages"]
    runs = [step.get("run", "") for step in steps]
    (download,) = [i for i, run in enumerate(runs) if "scripts/fhir_packages.lock" in run]
    script = runs[download]
    assert "done < <(grep -Ev '^(#|$)' scripts/fhir_packages.lock)" in script
    assert "while read -r name version sha256 url; do" in script
    assert 'curl -sSfL --retry 3 -o "$file" "$url"' in script
    assert 'echo "${sha256}  $file" | sha256sum -c -' in script
    assert script.index("fi\n") < script.index("sha256sum -c")  # checked, downloaded or cached
    assert not [run for run in runs if "scripts/fhir_packages.py" in run]
    validations = [i for i, run in enumerate(runs) if "scripts/fhir_validate.py" in run]
    assert len(validations) == 2 and download < min(validations)
    assert all("--offline" in runs[i].split() for i in validations)
    # The validator these packages were read from is the one the job downloads.
    lock_script = (ROOT / "scripts" / "fhir_packages.py").read_text()
    assert f'VALIDATOR = "{job["env"]["VALIDATOR_VERSION"]}"' in lock_script


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
    # Who made the change reaches the script through `env` only. The author is the pull
    # request's, never `github.actor` (whoever caused the run: the owner updating a fork's branch
    # would be it); the branch must be in this repository and the pull request into the default
    # branch. Who owns the repository is the person the base's CODEOWNERS names (the script
    # reads it), never `github.repository_owner`: since the move to mendari-hq, an organization,
    # which no pull request's author is.
    assert community["env"] == {
        "BASE": "${{ github.event.pull_request.base.sha }}",
        "BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "AUTHOR": "${{ github.event.pull_request.user.login }}",
        "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
        "REPO": "${{ github.repository }}",
    }
    for argument in (
        '--base "$BASE"',
        '--actor "$AUTHOR"',
        '--head-repo "$HEAD_REPO"',
        '--repo "$REPO"',
        '--base-ref "$BASE_REF"',
        '--default-branch "$DEFAULT_BRANCH"',
    ):
        assert argument in community["run"]
    step = json.dumps(community)
    for unwanted in ("github.actor", "repository_owner", "--owner"):
        assert unwanted not in step, unwanted


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
