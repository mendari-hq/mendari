"""The supply chain is pinned (threat model E5): every action by commit SHA, every image by
digest, every download by checksum, and the image and build backend from known versions. A tag
can be moved by whoever controls it; a SHA or a digest cannot. These tests keep new workflow
steps, services and images from slipping back to tags."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
COMPOSE_FILES = [ROOT / "docker-compose.yml", ROOT / "deploy" / "onprem" / "docker-compose.yml"]

COMMIT_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
# The comment Dependabot keeps in step with the SHA, and the only way a reader sees the version.
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s+(\S+)(.*)$")
VERSION_COMMENT = re.compile(r"^\s+# v\d+\.\d+\.\d+\s*$")
DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")

# `docker run` options that take a value, and those that do not. An option in neither set fails
# the test, so the parser never silently mistakes an option's value for the image.
RUN_VALUE_OPTIONS = {
    "-v", "--volume", "-e", "--env", "--env-file", "--network", "--name", "-p", "--publish",
    "-w", "--workdir", "--entrypoint", "-u", "--user", "--platform", "--mount",
}  # fmt: skip
RUN_FLAGS = {"--rm", "-i", "-t", "-it", "-d", "--init", "--read-only"}
PUNCTUATION = ";&|()\n"


def _workflow(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), path
    return loaded


def _steps(path: Path) -> Iterator[dict[str, Any]]:
    for job in _workflow(path)["jobs"].values():
        yield from job.get("steps", [])


def _scripts(path: Path) -> Iterator[str]:
    for step in _steps(path):
        if "run" in step:
            yield step["run"]


def _tokens(script: str) -> list[str]:
    """Shell words, with command separators as their own tokens (newlines included, since a
    newline ends a command unless escaped)."""
    lexer = shlex.shlex(script.replace("\\\n", " "), posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _is_separator(token: str) -> bool:
    return set(token) <= set(PUNCTUATION)


def _docker_run_images(script: str) -> Iterator[str]:
    tokens = _tokens(script)
    for i in range(len(tokens) - 1):
        if tokens[i] != "docker" or tokens[i + 1] != "run":
            continue
        j = i + 2
        while tokens[j].startswith("-"):
            option = tokens[j]
            if option in RUN_VALUE_OPTIONS:
                j += 2
            elif option in RUN_FLAGS or "=" in option:
                j += 1
            else:
                pytest.fail(f"unknown docker run option {option!r}: add it to this test")
        yield tokens[j]


def _docker_built_tags(script: str) -> set[str]:
    tokens = _tokens(script)
    tags: set[str] = set()
    for i in range(len(tokens) - 1):
        if tokens[i] == "docker" and tokens[i + 1] == "build":
            j = i + 2
            while j < len(tokens) - 1 and not _is_separator(tokens[j]):
                if tokens[j] in ("-t", "--tag"):
                    tags.add(tokens[j + 1])
                j += 1
    return tags


def test_the_workflows_and_files_are_there() -> None:
    names = {p.name for p in WORKFLOWS}
    assert {"ci.yml", "images.yml", "release.yml", "packs.yml"} <= names
    assert all(p.exists() for p in COMPOSE_FILES)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_with_its_version(path: Path) -> None:
    pinned = 0
    for number, line in enumerate(path.read_text().splitlines(), 1):
        match = USES_LINE.match(line)
        if not match:
            continue
        ref, rest = match.groups()
        if ref.startswith("./"):
            continue  # a local action lives in this repository, at this commit
        assert COMMIT_PIN.match(ref), f"{path.name}:{number}: {ref} is not pinned to a commit"
        assert VERSION_COMMENT.match(rest), f"{path.name}:{number}: no '# vX.Y.Z' comment"
        pinned += 1
    # The line scan saw every step that uses an action (none hides in YAML flow style).
    assert pinned == sum(1 for step in _steps(path) if "uses" in step), path.name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_leave_no_credentials_behind(path: Path) -> None:
    # No workflow pushes, so no later step (or a compromised dependency) needs the token.
    for step in _steps(path):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, path.name


def test_every_workflow_installs_the_same_pinned_uv() -> None:
    versions = {
        step.get("with", {}).get("version")
        for path in WORKFLOWS
        for step in _steps(path)
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    }
    assert len(versions) == 1, versions
    (version,) = versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(version)), version
    # The Dockerfile copies the same uv, so the image is built by the uv CI uses.
    assert f"ghcr.io/astral-sh/uv:{version}@sha256:" in (ROOT / "Dockerfile").read_text()


def _workflow_images(path: Path) -> Iterator[tuple[str, set[str]]]:
    built: set[str] = set()
    for script in _scripts(path):
        built |= _docker_built_tags(script)
    for job in _workflow(path)["jobs"].values():
        for service in job.get("services", {}).values():
            yield service["image"], built
        if "container" in job:
            container = job["container"]
            yield (container if isinstance(container, str) else container["image"]), built
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("docker://"):
                yield step["uses"].removeprefix("docker://"), built
            if "run" in step:
                for image in _docker_run_images(step["run"]):
                    yield image, built


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_image_is_pinned_by_digest(path: Path) -> None:
    for image, built in _workflow_images(path):
        if image in built:
            continue  # built from this repository's Dockerfile earlier in the same workflow
        assert DIGEST_PIN.search(image), f"{path.name}: {image} is not pinned by digest"


def test_the_image_parser_sees_the_images_it_should() -> None:
    # Guards the parser itself: the scan above must find the ZAP image and the services.
    images = {image for path in WORKFLOWS for image, _ in _workflow_images(path)}
    assert any(image.startswith("ghcr.io/zaproxy/zaproxy:") for image in images)
    assert any(image.startswith("postgres:") for image in images)
    assert "mendari-engine:ci" in images
    assert list(_docker_run_images("docker run --rm --network host -v a:b img:1 cmd")) == ["img:1"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_compose_image_is_pinned_by_digest(path: Path) -> None:
    services = yaml.safe_load(path.read_text())["services"]
    built = {s["image"] for s in services.values() if "build" in s and "image" in s}
    for name, service in services.items():
        image = service["image"]
        if image in built:
            continue  # this file builds it from the repository's Dockerfile
        if image.startswith("${"):
            continue  # the operator's own image; .env.example says to pin it by digest
        assert DIGEST_PIN.search(image), f"{path.name}: {name} uses {image}"


def test_every_dockerfile_base_is_pinned_by_digest() -> None:
    stages: set[str] = set()
    froms = 0
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        words = line.split()
        if words[:1] == ["FROM"]:
            froms += 1
            image = words[1]
            assert image in stages or DIGEST_PIN.search(image), line
            if len(words) == 4 and words[2].upper() == "AS":
                stages.add(words[3])
        if words[:1] == ["COPY"] and words[1].startswith("--from="):
            source = words[1].removeprefix("--from=")
            assert source in stages or DIGEST_PIN.search(source), line
    assert froms >= 2


def test_the_image_installs_exactly_the_lock() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "uv sync --frozen --no-dev --extra server --no-editable" in dockerfile
    assert "pip install" not in dockerfile


def test_the_kubernetes_template_says_to_deploy_by_digest() -> None:
    template = (ROOT / "deploy" / "kubernetes" / "mendari.yaml").read_text()
    assert "Deploy by digest" in template
    env_example = (ROOT / "deploy" / "onprem" / ".env.example").read_text()
    assert "Pin a pulled image by digest" in env_example


def _downloads(script: str) -> Iterator[str]:
    """The files a script downloads with curl or wget (fetches to stdout or /dev/null, such as
    health probes, are reads, not downloads)."""
    tokens = _tokens(script)
    for i, token in enumerate(tokens):
        if token not in ("curl", "wget"):
            continue
        j = i + 1
        while j < len(tokens) and not _is_separator(tokens[j]):
            word = tokens[j]
            if word in ("-o", "--output", "-O", "--output-document") and j + 1 < len(tokens):
                if token == "curl" and word == "-O":
                    pytest.fail("curl -O names the file after the URL; use -o <path>")
                if tokens[j + 1] not in ("/dev/null", "-"):
                    yield tokens[j + 1]
                j += 1
            elif word == ">" and j + 1 < len(tokens) and tokens[j + 1] != "/dev/null":
                yield tokens[j + 1]
            elif word.startswith(">") and word != ">/dev/null" and len(word) > 1:
                yield word[1:]
            j += 1
        if token == "wget" and "-O" not in tokens[i:j] and "--output-document" not in tokens[i:j]:
            pytest.fail("wget without -O: say where the download goes, then check its sha256")
        if j + 1 < len(tokens) and tokens[j] == "|" and tokens[j + 1] in ("sh", "bash", "sudo"):
            pytest.fail("a download piped into a shell runs whatever the server sends")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_download_is_checked_against_a_pinned_sha256(path: Path) -> None:
    for script in _scripts(path):
        lines = script.splitlines()
        for target in _downloads(script):
            first = next(i for i, line in enumerate(lines) if target in line)
            assert any(
                re.search(r"\b(sha256sum|shasum -a 256)\b.*(-c|--check)\b", line) and target in line
                for line in lines[first + 1 :]
            ), f"{path.name}: {target} is downloaded without a sha256 check after it"


def test_the_download_scan_finds_the_validator() -> None:
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    found = {target for script in _scripts(ci) for target in _downloads(script)}
    assert found == {"var/validator/validator_cli.jar"}
    assert list(_downloads("curl -sf http://127.0.0.1:8000/api/health > /dev/null")) == []


def test_the_python_side_is_pinned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    # The build backend is not in uv.lock, so it is pinned where every frontend reads it.
    assert all("==" in req for req in project["build-system"]["requires"])
    # FastAPI 0.132 made JSON routes refuse other content types (part of the CSRF defence).
    assert "fastapi>=0.132" in project["project"]["dependencies"]
    # CI's audit tool comes from the lock, with its own dependencies, not from a fresh resolve.
    assert any(dep.startswith("pip-audit==") for dep in project["dependency-groups"]["dev"])
    assert 'name = "pip-audit"' in (ROOT / "uv.lock").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "uv run --frozen pip-audit" in ci and "uvx" not in ci


def test_dependabot_watches_every_manifest() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    covered: dict[str, set[str]] = {}
    for update in config["updates"]:
        dirs = update.get("directories") or [update["directory"]]
        covered.setdefault(update["package-ecosystem"], set()).update(dirs)
        assert update["schedule"]["interval"] == "monthly"
        assert update["open-pull-requests-limit"] <= 3
        assert update.get("groups"), update["package-ecosystem"]
        assert update["cooldown"]["default-days"] >= 7, update["package-ecosystem"]
    assert covered["uv"] == {"/"}
    assert covered["github-actions"] == {"/"}
    assert covered["docker"] == {"/"}
    assert covered["docker-compose"] == {"/", "/deploy/onprem"}
    # Every package.json in the repository (a vendored package included) has an npm entry.
    manifests = {
        "/" + str(p.parent.relative_to(ROOT))
        for p in ROOT.glob("**/package.json")
        if not {"node_modules", ".venv", ".git", "var"} & set(p.relative_to(ROOT).parts)
    }
    assert manifests and manifests <= covered["npm"], manifests - covered.get("npm", set())


def test_dependabot_branches_get_no_vercel_preview() -> None:
    # A preview would also create a Neon branch, and Dependabot's own runs cannot clean it up.
    vercel = json.loads((ROOT / "vercel.json").read_text())
    assert vercel["git"]["deploymentEnabled"] == {"dependabot/**": False}
