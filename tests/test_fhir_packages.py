# SPDX-License-Identifier: Apache-2.0
"""The FHIR packages the HL7 validator loads in CI are pinned by sha256 (DV-b,
`scripts/fhir_packages.lock`). The lock is what its writer writes and names exactly what `ROOTS`
needs; `scripts/fhir_packages.py install` puts only checked tarballs in the validator's package
cache, where the validator looks for them, and refuses anything else before it writes; and
`scripts/fhir_validate.py --offline` runs the validator with no HTTP access and fails when the
validator put an unpinned package in its cache. The validator itself runs in CI's
`fhir-conformance` job (tests/test_workflow_safety.py pins its steps); these tests need no Java
and no network: their packages are built here."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


packages = _script("fhir_packages.py")
validate = _script("fhir_validate.py")


def _tarball(
    name: str,
    version: str,
    dependencies: dict[str, str] | None = None,
    extra: list[tarfile.TarInfo | tuple[str, bytes]] | None = None,
) -> bytes:
    """A FHIR package as the registries serve it: `package/package.json` and more."""
    manifest = {"name": name, "version": version, "dependencies": dependencies or {}}
    files: list[tarfile.TarInfo | tuple[str, bytes]] = [
        ("package/package.json", json.dumps(manifest).encode()),
        ("package/StructureDefinition-x.json", b'{"resourceType": "StructureDefinition"}'),
        ("other/spec.internals", b"{}"),
        *(extra or []),
    ]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for item in files:
            if isinstance(item, tarfile.TarInfo):
                archive.addfile(item)
                continue
            path, data = item
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _registry(tarballs: dict[str, bytes], downloads: Path) -> Path:
    """`tarballs` (by `name#version`) downloaded to `downloads`, and a lock that pins them."""
    downloads.mkdir(parents=True, exist_ok=True)
    pinned = []
    for ref, data in tarballs.items():
        name, version = ref.split("#")
        package = packages.Package(
            name,
            version,
            hashlib.sha256(data).hexdigest(),
            packages.REGISTRIES[0].format(name=name, version=version),
        )
        (downloads / package.tarball).write_bytes(data)
        pinned.append(package)
    lock = downloads.parent / "fhir_packages.lock"
    lock.write_text(packages.format_lock(pinned), encoding="utf-8")
    return lock


GUIDE = "example.fhir.guide#1.0.0"
TERMINOLOGY = "example.fhir.terminology#2.0.0"


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Path, Path]:
    """A guide that needs a terminology package (and R4 core, which is never locked)."""
    tarballs = {
        GUIDE: _tarball(
            "example.fhir.guide",
            "1.0.0",
            {"hl7.fhir.r4.core": "4.0.1", "example.fhir.terminology": "2.0.0"},
        ),
        TERMINOLOGY: _tarball("example.fhir.terminology", "2.0.0"),
    }
    downloads = tmp_path / "downloads"
    return downloads, _registry(tarballs, downloads)


def _install(registry: tuple[Path, Path], cache: Path) -> int:
    downloads, lock = registry
    result: int = packages.install(cache, downloads, lock, roots=[GUIDE])
    return result


# -- the lock -------------------------------------------------------------------------------------


def test_the_lock_is_well_formed_and_as_its_writer_writes_it() -> None:
    text = packages.LOCK.read_text(encoding="utf-8")
    locked = packages.parse_lock(text)
    assert packages.format_lock(locked) == text  # sorted, aligned, the header: never by hand
    assert len(locked) == 55


def test_what_the_validator_is_told_to_load_is_a_root_of_the_lock() -> None:
    """US Core (`ARGS`), the R4 definitions (`-version 4.0.1`) and the Da Vinci tarballs CI
    passes are roots, so offline each resolves to its pinned tarball."""
    locked = {package.ref for package in packages.read_lock()}
    assert set(packages.ROOTS) <= locked
    arguments = validate.ARGS
    assert arguments[arguments.index("-ig") + 1] in packages.ROOTS
    assert f"hl7.fhir.r4.core#{arguments[arguments.index('-version') + 1]}" in packages.ROOTS
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = " ".join(s.get("run", "") for s in ci["jobs"]["fhir-conformance"]["steps"])
    passed = re.findall(r"--ig var/fhir-packages/(\S+)-(\d[^-\s]*)\.tgz", runs)
    assert {f"{name}#{version}" for name, version in passed} == {
        "hl7.fhir.us.davinci-crd#2.2.1",
        "hl7.fhir.us.davinci-dtr#2.2.0",
        "hl7.fhir.us.davinci-pas#2.2.1",
    }
    assert {f"{name}#{version}" for name, version in passed} <= set(packages.ROOTS)


LOCKED = packages.read_lock()


@pytest.mark.slow  # reads 318 MB of tarballs; CI's fhir-conformance job checks it as it installs
@pytest.mark.skipif(
    not all((packages.DOWNLOADS / p.tarball).is_file() for p in LOCKED),
    reason="the locked packages are not in var/fhir-packages (fhir-conformance checks this)",
)
def test_the_lock_is_exactly_what_the_roots_need() -> None:
    manifests = {p.ref: packages.verified(p, packages.DOWNLOADS)[1] for p in LOCKED}
    assert packages.closure_problems(manifests) == []


def _line(name: str = "a.b", version: str = "1.0.0", digest: str = "0" * 64, url: str = "") -> str:
    return f"{name} {version} {digest} {url or f'https://packages.fhir.org/{name}/{version}'}"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "names no package"),
        ("# only a comment\n", "names no package"),
        ("a.b 1.0.0 " + "0" * 64 + "\n", "expected name, version, sha256 and url"),
        (_line(digest="0" * 63) + "\n", "not a sha256"),
        (_line(digest="G" * 64) + "\n", "not a sha256"),
        (_line(name="a/b") + "\n", "not a package name and version"),
        (_line(version="1.0.0/..") + "\n", "not a package name and version"),
        (_line(url="https://example.org/a.b/1.0.0") + "\n", "not a.b#1.0.0 on a registry"),
        (_line(url="https://packages.fhir.org/a.b/2.0.0") + "\n", "not a.b#1.0.0 on a registry"),
        (_line(url="http://packages.fhir.org/a.b/1.0.0") + "\n", "not a.b#1.0.0 on a registry"),
        (_line("c.d") + "\n" + _line("a.b") + "\n", "sorted by name and version"),
        (_line() + "\n" + _line() + "\n", "each package once"),
    ],
)
def test_the_lock_refuses_what_it_cannot_pin(text: str, message: str) -> None:
    with pytest.raises(packages.LockError, match=re.escape(message)):
        packages.parse_lock(text)


def test_a_line_on_the_second_registry_is_pinned_too() -> None:
    url = "https://packages2.fhir.org/packages/a.b/1.0.0"
    (package,) = packages.parse_lock(_line(url=url) + "\n")
    assert (package.ref, package.url, package.tarball) == ("a.b#1.0.0", url, "a.b-1.0.0.tgz")


# -- install --------------------------------------------------------------------------------------


def test_install_unpacks_each_package_where_the_validator_looks_for_it(
    registry: tuple[Path, Path], tmp_path: Path
) -> None:
    cache = tmp_path / "home" / ".fhir" / "packages"
    assert _install(registry, cache) == 0
    assert sorted(p.name for p in cache.iterdir()) == [GUIDE, TERMINOLOGY]
    found = json.loads((cache / GUIDE / "package" / "package.json").read_text())
    assert (found["name"], found["version"]) == ("example.fhir.guide", "1.0.0")
    assert (cache / GUIDE / "package" / "StructureDefinition-x.json").is_file()
    assert (cache / GUIDE / "other" / "spec.internals").is_file()  # every entry, as named


def test_install_replaces_what_the_cache_held_for_a_locked_package(
    registry: tuple[Path, Path], tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    stale = cache / GUIDE / "package" / "planted.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    _install(registry, cache)
    assert not stale.exists()
    assert (cache / GUIDE / "package" / "package.json").is_file()


def test_install_refuses_a_tarball_the_lock_does_not_pin(
    registry: tuple[Path, Path], tmp_path: Path
) -> None:
    downloads, _ = registry
    tarball = downloads / "example.fhir.terminology-2.0.0.tgz"
    tarball.write_bytes(_tarball("example.fhir.terminology", "2.0.0", {"x.y": "1.0.0"}))
    cache = tmp_path / "cache"
    with pytest.raises(packages.LockError, match=rf"{re.escape(str(tarball))}: sha256 \w+, but"):
        _install(registry, cache)
    assert not cache.exists()  # checked before anything is written


def test_install_refuses_a_tarball_that_is_another_package(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    lock = _registry({GUIDE: _tarball("example.fhir.other", "1.0.0")}, downloads)
    with pytest.raises(
        packages.LockError, match=re.escape(f"holds example.fhir.other#1.0.0, not {GUIDE}")
    ):
        packages.install(tmp_path / "cache", downloads, lock, roots=[GUIDE])


def test_a_missing_tarball_says_where_ci_downloads_it(
    registry: tuple[Path, Path], tmp_path: Path
) -> None:
    downloads, _ = registry
    (downloads / "example.fhir.guide-1.0.0.tgz").unlink()
    with pytest.raises(
        packages.LockError, match=re.escape("missing (CI downloads it from https://")
    ):
        _install(registry, tmp_path / "cache")


def test_install_refuses_a_lock_that_is_not_exactly_what_the_roots_need(tmp_path: Path) -> None:
    guide = _tarball("example.fhir.guide", "1.0.0", {"example.fhir.terminology": "2.0.0"})
    unneeded = _tarball("example.fhir.unused", "3.0.0")
    downloads = tmp_path / "downloads"
    lock = _registry({GUIDE: guide, "example.fhir.unused#3.0.0": unneeded}, downloads)
    with pytest.raises(packages.LockError) as refused:
        packages.install(tmp_path / "cache", downloads, lock, roots=[GUIDE, "example.fhir.x#9"])
    assert str(refused.value).splitlines() == [
        "example.fhir.x#9 is in ROOTS but not in the lock",
        f"{GUIDE} needs {TERMINOLOGY}, which the lock does not pin",
        "example.fhir.unused#3.0.0 is in the lock but nothing in ROOTS needs it",
    ]
    assert not (tmp_path / "cache").exists()


def test_a_core_package_needs_no_line_in_the_lock() -> None:
    """The validator never loads a core package as a dependency (it loads R4 core itself)."""
    found = {"dependencies": {"hl7.fhir.r4.core": "4.0.1", "hl7.fhir.r5.core": "5.0.0", "a.b": "1"}}
    assert packages.dependencies(found) == {"a.b#1"}
    assert packages.dependencies({"name": "a.b"}) == set()


@pytest.mark.parametrize(
    "entry",
    [
        ("package/../../escaped.json", b"{}"),
        ("/etc/escaped.json", b"{}"),
        tarfile.TarInfo("package/link.json"),  # made a symbolic link below
    ],
    ids=["dot-dot", "absolute", "symlink"],
)
def test_install_refuses_an_entry_outside_the_package_and_leaves_nothing_behind(
    tmp_path: Path, entry: tarfile.TarInfo | tuple[str, bytes]
) -> None:
    if isinstance(entry, tarfile.TarInfo):
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc/passwd"
    downloads = tmp_path / "downloads"
    lock = _registry({GUIDE: _tarball("example.fhir.guide", "1.0.0", extra=[entry])}, downloads)
    cache = tmp_path / "cache"
    with pytest.raises(packages.LockError, match=r"an entry outside the package|not a file"):
        packages.install(cache, downloads, lock, roots=[GUIDE])
    assert not (cache / GUIDE).exists()
    assert not (tmp_path / "escaped.json").exists()


def test_the_command_says_what_is_wrong_and_exits_1(
    registry: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    downloads, lock = registry
    (downloads / "example.fhir.guide-1.0.0.tgz").write_bytes(b"not a tarball")
    monkeypatch.setattr(packages, "DOWNLOADS", downloads)
    monkeypatch.setattr(packages, "LOCK", lock)
    monkeypatch.setattr(packages, "ROOTS", (GUIDE,))
    assert packages.main(["install", "--cache", str(tmp_path / "cache")]) == 1
    assert "example.fhir.guide-1.0.0.tgz: sha256" in capsys.readouterr().err
    assert not (tmp_path / "cache").exists()


# -- resolving the closure (the `lock` command, without the network) ------------------------------


def test_resolve_follows_the_dependencies_and_records_where_each_came_from() -> None:
    served = {
        ("example.fhir.guide", "1.0.0"): _tarball(
            "example.fhir.guide",
            "1.0.0",
            {"hl7.fhir.r4.core": "4.0.1", "example.fhir.terminology": "2.0.0"},
        ),
        ("example.fhir.terminology", "2.0.0"): _tarball(
            "example.fhir.terminology", "2.0.0", {"example.fhir.guide": "1.0.0"}
        ),
    }
    asked: list[tuple[str, str]] = []

    def fetch(name: str, version: str) -> tuple[bytes, str]:
        asked.append((name, version))
        return served[(name, version)], f"https://packages2.fhir.org/packages/{name}/{version}"

    resolved = packages.resolve([GUIDE], fetch)
    assert asked == [("example.fhir.guide", "1.0.0"), ("example.fhir.terminology", "2.0.0")]
    assert [(p.ref, p.url.split("/")[2]) for p in resolved] == [
        (GUIDE, "packages2.fhir.org"),
        (TERMINOLOGY, "packages2.fhir.org"),
    ]
    assert resolved[0].sha256 == hashlib.sha256(served[("example.fhir.guide", "1.0.0")]).hexdigest()
    assert packages.parse_lock(packages.format_lock(resolved)) == resolved


def test_resolve_refuses_a_registry_answer_that_is_another_package() -> None:
    def fetch(name: str, version: str) -> tuple[bytes, str]:
        return _tarball("example.fhir.other", "1.0.0"), "https://packages.fhir.org/x"

    with pytest.raises(packages.LockError, match=re.escape("holds example.fhir.other#1.0.0")):
        packages.resolve([GUIDE], fetch)


# -- scripts/fhir_validate.py ---------------------------------------------------------------------


class FakeValidator:
    """Stands in for `java -jar validator_cli.jar`: writes an empty report (or none), and may
    put a package in the cache, as a validator that fetched one would."""

    def __init__(self, cache: Path, fetches: str | None = None, report: bool = True) -> None:
        self.cache, self.fetches, self.report = cache, fetches, report
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> None:
        self.commands.append(command)
        kwargs["stdout"].write(b"Loading...\n  Unable to find package x.y#1.0.0\n")
        if self.fetches:
            (self.cache / self.fetches / "package").mkdir(parents=True)
        if self.report:
            named = command[command.index("-output") + 1]  # in the container, under /work
            out = ROOT / named.removeprefix("/work/") if named.startswith("/work/") else Path(named)
            out.write_text(json.dumps({"resourceType": "OperationOutcome", "issue": []}))


@pytest.fixture
def resource(tmp_path: Path) -> Path:
    path = tmp_path / "patient.json"
    path.write_text('{"resourceType": "Patient"}')
    return path


def _fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fetches: str | None = None, report: bool = True
) -> FakeValidator:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "hl7.fhir.us.core#6.1.0").mkdir()  # installed from the lock before the run
    fake = FakeValidator(cache, fetches, report)
    monkeypatch.setattr(validate.fhir_packages, "CACHE", cache)
    monkeypatch.setattr(validate.subprocess, "run", fake)
    return fake


def test_offline_runs_the_validator_with_no_http_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resource: Path
) -> None:
    fake = _fake(monkeypatch, tmp_path)
    assert validate.run_validator([resource], docker=False, offline=True) == {str(resource): []}
    (command,) = fake.commands
    assert "-no-http-access" in command
    assert command[command.index("-tx") + 1] == "n/a"
    validate.run_validator([resource], docker=False)  # by hand, online, as before
    assert "-no-http-access" not in fake.commands[1]


def test_offline_fails_when_the_validator_put_an_unpinned_package_in_its_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resource: Path
) -> None:
    _fake(monkeypatch, tmp_path, fetches="hl7.terminology#9.9.9")
    with pytest.raises(SystemExit, match=r"put hl7\.terminology#9\.9\.9 in its package cache"):
        validate.run_validator([resource], docker=False, offline=True)


def test_offline_accepts_a_locked_package_the_validator_unpacked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resource: Path
) -> None:
    _fake(monkeypatch, tmp_path, fetches="hl7.fhir.us.davinci-crd#2.2.1")
    assert validate.run_validator([resource], docker=False, offline=True) == {str(resource): []}


def test_offline_in_docker_runs_with_no_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch, tmp_path)
    inside = ROOT / "var" / "test-fhir-packages-resource.json"
    inside.parent.mkdir(exist_ok=True)
    inside.write_text('{"resourceType": "Patient"}')
    try:
        validate.run_validator([inside], docker=True, offline=True)
    finally:
        inside.unlink()
    (command,) = fake.commands
    network, image = command.index("--network"), command.index(validate.IMAGE)
    assert command[network + 1] == "none" and network < image  # docker's option, not Java's
    assert "-no-http-access" in command[image:]


def test_no_report_shows_the_end_of_the_validators_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake(monkeypatch, tmp_path, report=False)
    with pytest.raises(SystemExit, match="produced no report"):
        validate.run_validator([resource], docker=False, offline=True)
    assert "Unable to find package x.y#1.0.0" in capsys.readouterr().err


def test_a_missing_package_tarball_is_refused_before_the_validator_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _fake(monkeypatch, tmp_path)
    missing = "var/fhir-packages/hl7.fhir.us.davinci-crd-0.0.0.tgz"
    argv = ["fhir_validate.py", "--offline", "--ig", missing, "--valid", str(resource)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        validate.main()
    assert f"--ig {missing}: no such package tarball" in capsys.readouterr().err
    assert fake.commands == []
