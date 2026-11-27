# SPDX-License-Identifier: Apache-2.0
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
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
INFERNO = ROOT / "sandbox" / "inferno"
# The scripted Inferno suites' image and compose project (LP-23l): the CRD kit with the Bulk
# Data kit beside it, on their own lock.
SCRIPTED = INFERNO / "scripted"
COMPOSE_FILES = [
    ROOT / "docker-compose.yml",
    ROOT / "deploy" / "onprem" / "docker-compose.yml",
    ROOT / "deploy" / "site" / "docker-compose.yml",
    SCRIPTED / "docker-compose.yml",
]
# Each Dockerfile, with the least number of FROM lines it has (the engine's is multi-stage).
DOCKERFILES = {ROOT / "Dockerfile": 2, SCRIPTED / "Dockerfile": 1}
# Each Inferno image: its folder, the kit it builds from source (the one gem of its lock
# without a checksum: it is fetched by commit), and the Linux platforms its lock lists (the
# images CI builds, amd64, and a local run on Apple silicon, arm64).
INFERNO_IMAGES = {
    "scripted": (
        SCRIPTED,
        "davinci_crd_test_kit (0.15.0)",
        ["aarch64-linux", "aarch64-linux-gnu", "arm-linux", "x86-linux", "x86_64-linux"],
    ),
}

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


def _shell_name(text: str) -> str:
    """`$image` and `${image}` are the same variable (and `${a}b` stays apart from `$ab`)."""
    return re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: "${%s}" % (m[1] or m[2]), text)


def _skopeo_copies(script: str) -> Iterator[tuple[str, str]]:
    """The source and destination of each `skopeo copy`: its last two words."""
    tokens = _tokens(script)
    for i in range(len(tokens) - 1):
        if tokens[i] == "skopeo" and tokens[i + 1] == "copy":
            j = i + 2
            while j < len(tokens) and not _is_separator(tokens[j]):
                j += 1
            words = [word for word in tokens[i + 2 : j] if not word.startswith("-")]
            if len(words) >= 2:
                yield words[-2], words[-1]


def _docker_built_tags(script: str) -> set[str]:
    """Tags a script builds (`docker build -t`), or loads from an archive it built
    (`skopeo copy oci-archive:<file> docker-daemon:<tag>`; a registry source is not built)."""
    tokens = _tokens(script)
    tags: set[str] = {
        _shell_name(destination.removeprefix("docker-daemon:"))
        for source, destination in _skopeo_copies(script)
        if destination.startswith("docker-daemon:")
        and source.startswith(("oci-archive:", "docker-archive:"))
    }
    for i in range(len(tokens) - 1):
        if tokens[i] == "docker" and tokens[i + 1] == "build":
            j = i + 2
            while j < len(tokens) - 1 and not _is_separator(tokens[j]):
                if tokens[j] in ("-t", "--tag"):
                    tags.add(_shell_name(tokens[j + 1]))
                j += 1
    return tags


def test_the_workflows_and_files_are_there() -> None:
    names = {p.name for p in WORKFLOWS}
    assert {"ci.yml", "release.yml", "agents.yml", "conformance.yml", "terminology.yml"} <= names
    assert "images.yml" not in names  # images are published by release.yml, after its build
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
    # The line scan saw every step that uses an action (none hides in YAML flow style): each
    # but the local one (`uses: ./`, the repository's own action.yml, whose own steps
    # tests/test_ci_action.py holds to the same rule).
    used = [step["uses"] for step in _steps(path) if "uses" in step]
    assert pinned == sum(1 for action in used if not action.startswith("./")), path.name


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
            uses, given = step.get("uses", ""), step.get("with", {})
            if uses.startswith("docker://"):
                yield uses.removeprefix("docker://"), built
            # Actions that pull an image of their own: their defaults are moving tags.
            if uses.startswith("docker/setup-qemu-action@"):
                yield given.get("image", "tonistiigi/binfmt:latest"), built
            if uses.startswith("docker/setup-buildx-action@"):
                options = dict(o.split("=", 1) for o in given.get("driver-opts", "").split())
                yield options.get("image", "moby/buildkit:buildx-stable-1"), built
            if "run" in step:
                for image in _docker_run_images(step["run"]):
                    yield image, built
                for source, _ in _skopeo_copies(step["run"]):
                    if source.startswith("docker://"):
                        yield source.removeprefix("docker://"), built


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_image_is_pinned_by_digest(path: Path) -> None:
    for image, built in _workflow_images(path):
        if _shell_name(image) in built:
            continue  # built from this repository's Dockerfile earlier in the same workflow
        assert DIGEST_PIN.search(image), f"{path.name}: {image} is not pinned by digest"


def test_the_image_parser_sees_the_images_it_should() -> None:
    # Guards the parser itself: the scan above must find the ZAP image and the services.
    images = {image for path in WORKFLOWS for image, _ in _workflow_images(path)}
    assert any(image.startswith("ghcr.io/zaproxy/zaproxy:") for image in images)
    assert any(image.startswith("postgres:") for image in images)
    assert "mendari-engine:ci" in images
    assert any(image.startswith("moby/buildkit:") for image in images)  # an action's own image
    assert list(_docker_run_images("docker run --rm --network host -v a:b img:1 cmd")) == ["img:1"]
    # Only a tag loaded from an archive counts as built here; a registry's never does.
    assert _docker_built_tags('skopeo copy oci-archive:/a.tar "docker-daemon:${image}"') == {
        "${image}"
    }
    assert _docker_built_tags("skopeo copy docker://postgres:16 docker-daemon:pg:16") == set()
    assert _shell_name("$image") == _shell_name("${image}") != _shell_name("${ima}ge")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_sbom_generator_is_pinned_by_digest(path: Path) -> None:
    """BuildKit runs the generator an `--attest type=sbom` names on the finished image; its
    default is a moving tag."""
    for script in _scripts(path):
        if "type=sbom" not in script:
            continue
        generators = re.findall(r"type=sbom,generator=([^\s\",]+)", script)
        assert generators, f"{path.name}: an SBOM attestation without a pinned generator"
        for image in generators:
            assert DIGEST_PIN.search(image), f"{path.name}: {image} is not pinned by digest"


def test_the_release_attaches_an_sbom_from_a_pinned_generator() -> None:
    release = ROOT / ".github" / "workflows" / "release.yml"
    generators = [g for s in _scripts(release) for g in re.findall(r"generator=([^\s\",]+)", s)]
    assert len(generators) == 1 and generators[0].startswith(
        "docker.io/docker/buildkit-syft-scanner:"
    )


def _builders(path: Path, job: str) -> list[str]:
    steps = _workflow(path)["jobs"][job]["steps"]
    return [
        s["with"]["driver-opts"]
        for s in steps
        if s.get("uses", "").startswith("docker/setup-buildx-action@")
    ]


def test_ci_scans_the_sbom_from_the_releases_generator_and_builder() -> None:
    """CI's image-sbom job runs the release's SBOM scan on every change; it proves something
    only while it runs the same generator on the same builder (a bump changes both files)."""
    workflows = ROOT / ".github" / "workflows"
    found = {}
    for name in ("ci.yml", "release.yml"):
        found[name] = {
            g for s in _scripts(workflows / name) for g in re.findall(r"generator=([^\s\",]+)", s)
        }
    assert len(found["ci.yml"]) == 1 and found["ci.yml"] == found["release.yml"]
    assert _builders(workflows / "ci.yml", "image-sbom") == _builders(
        workflows / "release.yml", "images"
    )


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


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_dockerfile_base_is_pinned_by_digest(path: Path) -> None:
    stages: set[str] = set()
    froms = 0
    for line in path.read_text().splitlines():
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
    assert froms >= DOCKERFILES[path]


def _gem_specs(lock: str, section: str, indent: int) -> list[str]:
    """The lines a Gemfile.lock section lists at one indent: each gem's `name (version)`, with
    its checksum in CHECKSUMS."""
    block = lock.split(f"\n{section}\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in block.splitlines() if re.match(rf"^ {{{indent}}}\S", line)]


@pytest.mark.parametrize("image", INFERNO_IMAGES)
def test_the_inferno_image_takes_the_kit_by_commit_and_every_gem_by_checksum(image: str) -> None:
    """The Inferno kits publish no image and their own Dockerfiles pin nothing (LP-23 reuses this
    pattern): each kit's source is fetched by commit SHA and checked against it, Bundler is
    checked against its sha256 before it is installed, and every gem is installed from
    Mendari's Gemfile.lock, whose CHECKSUMS Bundler checks (a changed sha256 fails the build),
    frozen."""
    folder, kit, platforms_wanted = INFERNO_IMAGES[image]
    dockerfile = (folder / "Dockerfile").read_text()
    commit = re.search(r"^ARG KIT_COMMIT=([0-9a-f]{40})$", dockerfile, re.M)
    assert commit is not None
    for line in (
        'git fetch --quiet --depth 1 "$KIT_REPOSITORY" "$KIT_COMMIT"',
        'test "$(git rev-parse HEAD)" = "$KIT_COMMIT"',
        'echo "${BUNDLER_SHA256}  /tmp/bundler.gem" | sha256sum -c -',
        "gem install --local --no-document /tmp/bundler.gem",
        "BUNDLE_FROZEN=true",
        "COPY Gemfile.lock ./Gemfile.lock",
    ):
        assert line in dockerfile, line
    assert re.search(r"^ARG BUNDLER_SHA256=[0-9a-f]{64}$", dockerfile, re.M)
    assert "gem install bundler" not in dockerfile  # never an unchecked download
    bundler = re.search(r"^ARG BUNDLER_VERSION=(\S+)$", dockerfile, re.M)
    assert bundler is not None
    lock = (folder / "Gemfile.lock").read_text()
    assert lock.rstrip().endswith(f"BUNDLED WITH\n   {bundler[1]}")
    gems = _gem_specs(lock, "GEM\n  remote: https://rubygems.org/\n  specs:", 4)
    listed = _gem_specs(lock, "CHECKSUMS", 2)
    checked = dict(line.rsplit(" sha256=", 1) for line in listed if " sha256=" in line)
    assert len(gems) > 100 and set(gems) == set(checked)
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in checked.values())
    # The gems without a checksum: the kit itself, fetched by commit above, and inferno_core,
    # its release checked against its sha256 before two pins move (`INFERNO_CORE_SHA256`).
    assert [line for line in listed if " sha256=" not in line] == [kit, "inferno_core (1.6.0)"]
    assert re.search(r"^ARG INFERNO_CORE_SHA256=[0-9a-f]{64}$", dockerfile, re.M)
    assert 'echo "${INFERNO_CORE_SHA256}  $gem" | sha256sum -c -' in dockerfile
    platforms = lock.split("\nPLATFORMS\n", 1)[1].split("\n\n", 1)[0].split()
    assert platforms == platforms_wanted and all("linux" in p for p in platforms)


def test_the_scripted_image_adds_the_bulk_kit_to_the_crd_kits_gemfile() -> None:
    """The scripted image's changes to the CRD kit: its Gemfile gains the Bulk Data kit at one
    version, which the lock holds with its checksum, and a loader in lib/ (Inferno loads only
    lib/*.rb) registers its suites; and it names inferno_core by path, its release with two pins
    moved to fixed releases (`tests/test_inferno_setup.py`)."""
    dockerfile = (SCRIPTED / "Dockerfile").read_text()
    assert "COPY Gemfile ./Gemfile" in dockerfile
    assert "COPY lib/mendari_kits.rb lib/mendari_kits.rb" in dockerfile
    gemfile = (SCRIPTED / "Gemfile").read_text()
    assert "\ngemspec\n\ngem 'bulk_data_test_kit', '0.14.0'\n" in gemfile
    lock = (SCRIPTED / "Gemfile.lock").read_text()
    assert "\n  bulk_data_test_kit (= 0.14.0)\n" in lock  # DEPENDENCIES
    assert re.search(r"^  bulk_data_test_kit \(0\.14\.0\) sha256=[0-9a-f]{64}$", lock, re.M)
    assert re.search(r"^    inferno_core \(1\.6\.0\)$", lock, re.M)
    loader = (SCRIPTED / "lib" / "mendari_kits.rb").read_text()
    assert [line for line in loader.splitlines() if line.startswith("require")] == [
        "require 'bulk_data_test_kit'"
    ]


@pytest.mark.parametrize("image", INFERNO_IMAGES)
def test_the_inferno_kit_validates_without_a_terminology_server(image: str) -> None:
    """No resource Inferno validates goes to tx.fhir.org, the kits' default
    (sandbox/inferno/README.md, "What leaves the machine"): each image puts
    offline_terminology.rb last in the kit's lib/, where Inferno loads it at boot, after the
    kits, and the override takes the terminology server from every validator of every suite,
    group and test. It requires every other file in lib/ first, whichever kits the image
    carries."""
    folder = INFERNO_IMAGES[image][0]
    copies = [
        line.split()[1:]
        for line in (folder / "Dockerfile").read_text().splitlines()
        if line.split()[:1] == ["COPY"]
    ]
    (target,) = [words[-1] for words in copies if words[0] == "offline_terminology.rb"]
    assert re.fullmatch(r"lib/zz_[\w.-]+\.rb", target), target  # last in name order
    loaded = [words[-1] for words in copies if words[-1].startswith("lib/")]
    assert max(loaded) == target
    override = (folder / "offline_terminology.rb").read_text()
    assert "Dir.glob(File.join(__dir__, '*.rb')).sort.each do |path|" in override
    assert "require_relative path unless File.realpath(path) == File.realpath(__FILE__)" in (
        override
    )
    kinds = re.search(r"^\[(.+)\]\.each do \|kind\|$", override, re.M)
    assert kinds is not None
    entities = {f"Inferno::Entities::{kind}" for kind in ("TestSuite", "TestGroup", "Test")}
    assert {kind.strip() for kind in kinds[1].split(",")} == entities
    assert "kind.descendants.each do |runnable|" in override
    assert "validator.validation_context { txServer nil }" in override


def _no_way_out(services: dict[str, Any]) -> None:
    """What a compose file of Inferno's may not give a service: another network's address, a
    file of settings or secrets, a host's name; and what it publishes binds this machine."""
    for name, service in services.items():
        assert not {"network_mode", "env_file", "secrets", "extra_hosts"} & set(service), name
        for port in service.get("ports", []):
            assert isinstance(port, str) and port.startswith("127.0.0.1:"), (name, port)


def _inferno_urls(services: dict[str, Any], name: str) -> dict[str, Any]:
    """Each URL Inferno's service `name` is configured with names a service of the file, or this
    machine; INFERNO_HOST is the internal nginx."""
    loopback = {"127.0.0.1", "localhost", "::1"}
    environment = services[name]["environment"]
    urls = {key: urlsplit(str(value)) for key, value in environment.items() if "://" in value}
    assert {"INFERNO_HOST", "FHIR_REFERENCE_SERVER"} <= set(urls), name
    for key, url in urls.items():
        assert url.hostname in set(services) | loopback, (name, key, url.geturl())
    assert urls["INFERNO_HOST"].geturl() == "http://nginx"
    return urls


def test_the_scripted_harness_reaches_only_its_own_services() -> None:
    """The scripted suites' project (sandbox/inferno/scripted): the same rules, with the Bulk
    client suite's reference server this project's own HAPI (`fhir`), the web app and its worker
    on one database (a named volume) with one INFERNO_HOST, and one engine, for the SMART and
    Bulk clients: open, PHI mode off, nothing that signs in, and the agent its image bundles
    alone, with nothing of the checkout mounted."""
    services = yaml.safe_load((SCRIPTED / "docker-compose.yml").read_text())["services"]
    _no_way_out(services)
    for name in ("inferno", "worker"):
        urls = _inferno_urls(services, name)
        assert urls["FHIR_REFERENCE_SERVER"].geturl() == "http://fhir:8080/fhir"
        assert services[name]["volumes"] == ["inferno-data:/opt/inferno/data"]
    assert services["inferno"]["environment"] == services["worker"]["environment"]
    assert services["fhir"]["profiles"] == ["bulk"]
    engine = services["engine"]
    assert engine["build"] == {"context": "../../.."}  # the repository's own Dockerfile
    assert set(engine["environment"]) == {
        "MENDARI_PHI_MODE",
        "MENDARI_AGENTS",
        "MENDARI_PUBLIC_URL",
        "MENDARI_FHIR_ALLOWED_SERVERS",
        "MENDARI_SMART_CLIENT_ID",
        "MENDARI_SMART_SCOPES",
    }
    assert engine["environment"]["MENDARI_PHI_MODE"] == "off"
    assert engine["environment"]["MENDARI_AGENTS"] == "mendari/portal-message-agent"
    assert "volumes" not in engine  # the image's own agent: nothing of the checkout
    assert urlsplit(engine["environment"]["MENDARI_PUBLIC_URL"]).port == 8000
    assert [name for name in services if name.startswith("engine")] == ["engine"]


def test_the_image_installs_exactly_the_lock() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
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
    # And `$file`: each FHIR package scripts/fhir_packages.lock pins, in a loop over its lines.
    assert found == {"var/validator/validator_cli.jar", "$file"}
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
    assert covered["docker-compose"] == {"/", "/deploy/onprem", "/deploy/site"}
    # Every package.json in the repository (a vendored package included) has an npm entry.
    manifests = {
        "/" + str(p.parent.relative_to(ROOT))
        for p in ROOT.glob("**/package.json")
        if not {"node_modules", ".venv", ".git", "var", ".next"} & set(p.relative_to(ROOT).parts)
    }
    assert manifests and manifests <= covered["npm"], manifests - covered.get("npm", set())


def test_dependabot_branches_get_no_vercel_preview() -> None:
    # A preview would also create a Neon branch, and Dependabot's own runs cannot clean it up.
    vercel = json.loads((ROOT / "vercel.json").read_text())
    assert vercel["git"]["deploymentEnabled"] == {"dependabot/**": False}


def test_the_secret_scan_is_pinned_in_one_place() -> None:
    """The gitleaks image every scan runs (scripts/secret_scan.sh) is pinned by digest there, and no
    workflow runs another (Dependabot does not see it: bump it by hand)."""
    script = (ROOT / "scripts" / "secret_scan.sh").read_text()
    (image,) = re.findall(r'^image="([^"]+)"$', script, re.M)
    assert image.startswith("ghcr.io/gitleaks/gitleaks:") and DIGEST_PIN.search(image)
    for path in WORKFLOWS:
        assert "gitleaks/gitleaks" not in path.read_text(), path.name
