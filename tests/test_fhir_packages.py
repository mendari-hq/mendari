# SPDX-License-Identifier: Apache-2.0
"""The FHIR packages the HL7 validator loads in CI are pinned by sha256 (DV-b,
`scripts/fhir_packages.lock`). The lock is what its writer writes and names exactly what `ROOTS`
needs; `scripts/fhir_packages.py install` puts only checked tarballs in a validator's package
cache, where the validator looks for them, only what a run loads and what that needs, and
refuses anything else before it writes; and `scripts/fhir_validate.py --offline` gives each run
a cache of its own (so the US Core run holds US Core 6.1.0 alone), runs the validator with no
HTTP access and fails when the validator put a package the run lacks in its cache; `--tx`, a
terminology server in place of none, keeps HTTP for it and the same cache and check. The
validator itself runs in CI's
`fhir-conformance` job (tests/test_workflow_safety.py pins its steps); these tests need no Java
and no network: their packages are built here."""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import io
import itertools
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


def _install(registry: tuple[Path, Path], cache: Path) -> list[str]:
    downloads, lock = registry
    return [package.ref for package in packages.install(cache, downloads, lock, roots=[GUIDE])]


# -- the lock -------------------------------------------------------------------------------------


def test_the_lock_is_well_formed_and_as_its_writer_writes_it() -> None:
    text = packages.LOCK.read_text(encoding="utf-8")
    locked = packages.parse_lock(text)
    assert packages.format_lock(locked) == text  # sorted, aligned, the header: never by hand
    assert len(locked) == 18


def test_what_the_validator_is_told_to_load_is_a_root_of_the_lock() -> None:
    """US Core (`ARGS`), the R4 definitions (`-version 4.0.1`) and any package tarball CI
    passes are roots, so offline each resolves to its pinned tarball."""
    locked = {package.ref for package in packages.read_lock()}
    assert set(packages.ROOTS) <= locked
    arguments = validate.ARGS
    assert arguments[arguments.index("-ig") + 1] in packages.ROOTS
    assert f"hl7.fhir.r4.core#{arguments[arguments.index('-version') + 1]}" in packages.ROOTS
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = " ".join(s.get("run", "") for s in ci["jobs"]["fhir-conformance"]["steps"])
    passed = re.findall(r"--ig var/fhir-packages/(\S+)-(\d[^-\s]*)\.tgz", runs)
    assert {f"{name}#{version}" for name, version in passed} <= set(packages.ROOTS)


LOCKED = packages.read_lock()
# These read 318 MB of tarballs (slow); fhir-conformance checks the same as each run installs.
needs_the_locked_tarballs = pytest.mark.skipif(
    not all((packages.DOWNLOADS / p.tarball).is_file() for p in LOCKED),
    reason="the locked packages are not in var/fhir-packages (fhir-conformance checks this)",
)


@functools.cache
def _locked_manifests() -> dict[str, dict[str, Any]]:
    return {p.ref: packages.verified(p, packages.DOWNLOADS)[1] for p in LOCKED}


@pytest.mark.slow
@needs_the_locked_tarballs
def test_the_lock_is_exactly_what_the_roots_need() -> None:
    assert packages.closure_problems(_locked_manifests()) == []


def _validator_runs() -> list[tuple[list[str], list[str]]]:
    """Each scripts/fhir_validate.py run in fhir-conformance: its `--ig` values, and the files
    it validates (`--valid`, as globs)."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = []
    for step in ci["jobs"]["fhir-conformance"]["steps"]:
        words = step.get("run", "").split()
        if "scripts/fhir_validate.py" in words:
            igs = [words[i + 1] for i, word in enumerate(words) if word == "--ig"]
            after = words[words.index("--valid") + 1 :]
            runs.append((igs, list(itertools.takewhile(lambda w: not w.startswith("-"), after))))
    return runs


@pytest.mark.slow
@needs_the_locked_tarballs
def test_ci_s_us_core_run_holds_us_core_6_1_0_alone() -> None:
    """The run's package cache is what it loads and what that needs, read from the real lock:
    one US Core, 6.1.0, so a profile a file cites without a version is US Core 6.1.0's,
    whatever the files before it cite. That is the whole lock: what the lock is for."""
    ((igs, _),) = _validator_runs()
    tarballs = [str(ROOT / ig) for ig in igs if ig.endswith(".tgz")]  # a folder is none
    cache = packages.closure(_locked_manifests(), validate.loads(tarballs))
    assert {ref for ref in cache if ref.startswith("hl7.fhir.us.core#")} == {
        "hl7.fhir.us.core#6.1.0"
    }
    assert cache == set(_locked_manifests())


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
    assert _install(registry, cache) == [GUIDE, TERMINOLOGY]
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


def test_install_puts_only_what_a_run_loads_and_what_that_needs(tmp_path: Path) -> None:
    """Never another guide's tree: a resource that cites that guide's canonical would make the
    validator load it, with what it needs, for the files after that one."""
    other = "example.fhir.other#1.0.0"
    tarballs = {
        GUIDE: _tarball("example.fhir.guide", "1.0.0", {"example.fhir.terminology": "2.0.0"}),
        TERMINOLOGY: _tarball("example.fhir.terminology", "2.0.0"),
        other: _tarball("example.fhir.other", "1.0.0", {"example.fhir.guide": "2.0.0"}),
        "example.fhir.guide#2.0.0": _tarball("example.fhir.guide", "2.0.0"),
    }
    downloads = tmp_path / "downloads"
    lock = _registry(tarballs, downloads)
    cache = tmp_path / "cache"
    chosen = packages.install(cache, downloads, lock, [GUIDE, other], loads=[GUIDE])
    assert [package.ref for package in chosen] == [GUIDE, TERMINOLOGY]
    assert sorted(p.name for p in cache.iterdir()) == [GUIDE, TERMINOLOGY]
    everything = packages.install(tmp_path / "all", downloads, lock, [GUIDE, other])
    assert len(everything) == len(tarballs)  # by default, the whole lock


def test_install_refuses_a_run_that_loads_what_the_lock_does_not_pin(
    registry: tuple[Path, Path], tmp_path: Path
) -> None:
    downloads, lock = registry
    with pytest.raises(
        packages.LockError, match=re.escape("the run loads x.y#1.0.0, which the lock does not pin")
    ):
        packages.install(tmp_path / "cache", downloads, lock, [GUIDE], loads=[GUIDE, "x.y#1.0.0"])
    assert not (tmp_path / "cache").exists()


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


def test_the_command_installs_what_a_run_loads(
    registry: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    downloads, lock = registry
    monkeypatch.setattr(packages, "DOWNLOADS", downloads)
    monkeypatch.setattr(packages, "LOCK", lock)
    monkeypatch.setattr(packages, "ROOTS", (GUIDE,))
    cache = tmp_path / "cache"
    assert packages.main(["install", "--cache", str(cache), TERMINOLOGY]) == 0
    assert [p.name for p in cache.iterdir()] == [TERMINOLOGY]
    assert packages.main(["install", "--cache", str(tmp_path / "all")]) == 0
    assert sorted(p.name for p in (tmp_path / "all").iterdir()) == [GUIDE, TERMINOLOGY]
    assert "2 packages in" in capsys.readouterr().out


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

OWN = "hl7.fhir.r4.core#4.0.1"  # what the validator loads by itself, here one package
US_CORE = "hl7.fhir.us.core#6.1.0"  # ARGS
OTHER_GUIDE = "example.fhir.other-guide#2.0.0"
US_CORE_7 = "hl7.fhir.us.core#7.0.0"
HOME = "-Duser.home="


@pytest.fixture
def locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The validator script's lock, small: the validator's own package, US Core 6.1.0 (with a
    terminology package it needs) and another guide, which needs another US Core, 7.0.0.
    Returns where its tarballs were downloaded."""
    tarballs = {
        OWN: _tarball("hl7.fhir.r4.core", "4.0.1"),
        US_CORE: _tarball("hl7.fhir.us.core", "6.1.0", {"example.fhir.terminology": "2.0.0"}),
        TERMINOLOGY: _tarball("example.fhir.terminology", "2.0.0"),
        OTHER_GUIDE: _tarball("example.fhir.other-guide", "2.0.0", {"hl7.fhir.us.core": "7.0.0"}),
        US_CORE_7: _tarball("hl7.fhir.us.core", "7.0.0"),
    }
    downloads = tmp_path / "downloads"
    lock = _registry(tarballs, downloads)
    monkeypatch.setattr(validate.fhir_packages, "DOWNLOADS", downloads)
    monkeypatch.setattr(validate.fhir_packages, "LOCK", lock)
    monkeypatch.setattr(validate.fhir_packages, "VALIDATOR_ROOTS", (OWN,))
    monkeypatch.setattr(validate.fhir_packages, "ROOTS", (OWN, US_CORE, OTHER_GUIDE))
    return downloads


def _host(path: str) -> Path:
    """A path the validator was given, on this machine (the container has the repository at
    /work)."""
    return ROOT / path.removeprefix("/work/") if path.startswith("/work/") else Path(path)


class FakeValidator:
    """Stands in for `java -jar validator_cli.jar`: notes what its package cache (under
    user.home) holds when it runs offline or with a terminology server, writes an empty report
    (or none), and may put a package in the cache, as a validator that fetched one would."""

    def __init__(self, fetches: str | None = None, report: bool = True) -> None:
        self.fetches, self.report = fetches, report
        self.commands: list[list[str]] = []
        self.cached: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> None:
        self.commands.append(command)
        kwargs["stdout"].write(b"Loading...\n  Unable to find package x.y#1.0.0\n")
        if "-no-http-access" in command or command[command.index("-tx") + 1] != "n/a":
            (home,) = [_host(arg.removeprefix(HOME)) for arg in command if arg.startswith(HOME)]
            cache = home / ".fhir" / "packages"
            self.cached.append(sorted(p.name for p in cache.iterdir()))
            if self.fetches:
                (cache / self.fetches / "package").mkdir(parents=True, exist_ok=True)
        if self.report:
            out = _host(command[command.index("-output") + 1])
            out.write_text(json.dumps({"resourceType": "OperationOutcome", "issue": []}))


@pytest.fixture
def resource(tmp_path: Path) -> Path:
    path = tmp_path / "patient.json"
    path.write_text('{"resourceType": "Patient"}')
    return path


def _fake(
    monkeypatch: pytest.MonkeyPatch, fetches: str | None = None, report: bool = True
) -> FakeValidator:
    fake = FakeValidator(fetches, report)
    monkeypatch.setattr(validate.subprocess, "run", fake)
    return fake


@pytest.mark.usefixtures("locked")
def test_offline_runs_the_validator_with_no_http_access(
    monkeypatch: pytest.MonkeyPatch, resource: Path
) -> None:
    fake = _fake(monkeypatch)
    assert validate.run_validator([resource], docker=False, offline=True) == {str(resource): []}
    (command,) = fake.commands
    assert "-no-http-access" in command
    assert command[command.index("-tx") + 1] == "n/a"
    validate.run_validator([resource], docker=False)  # by hand, online, as before: ~/.fhir
    assert "-no-http-access" not in fake.commands[1]
    assert f"{HOME}{Path.home()}" in fake.commands[1]


def test_each_run_has_a_package_cache_of_its_own_with_only_what_it_loads(
    monkeypatch: pytest.MonkeyPatch,
    locked: Path,
    resource: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With one cache for every run, a resource in the US Core run that cites another guide's
    canonical made the validator load that guide (the newest in its cache) and its tree, another
    US Core among it, so the files after it were checked against that US Core. The US Core run's
    cache holds US Core 6.1.0 and what it needs, and nothing else; a run given another guide
    holds that guide and its tree too."""
    fake = _fake(monkeypatch)
    validate.run_validator([resource], docker=False, offline=True)
    assert fake.cached[0] == sorted([OWN, US_CORE, TERMINOLOGY])
    assert "package cache (3 locked packages)" in capsys.readouterr().out
    folder = tmp_path / "terminology"  # a folder of resources is no package
    folder.mkdir()
    other = str(locked / "example.fhir.other-guide-2.0.0.tgz")
    validate.run_validator([resource], docker=False, igs=[str(folder), other], offline=True)
    assert fake.cached[1] == sorted([OWN, US_CORE, TERMINOLOGY, OTHER_GUIDE, US_CORE_7])
    homes = [arg.removeprefix(HOME) for c in fake.commands for arg in c if arg.startswith(HOME)]
    assert len(set(homes)) == 2 and str(Path.home()) not in homes
    assert not any(Path(home).exists() for home in homes)  # removed with the run


@pytest.mark.parametrize(
    "fetches", ["hl7.terminology#9.9.9", OTHER_GUIDE], ids=["unpinned", "not-run"]
)
@pytest.mark.usefixtures("locked")
def test_offline_fails_when_the_validator_put_a_package_the_run_lacks_in_its_cache(
    monkeypatch: pytest.MonkeyPatch, resource: Path, fetches: str
) -> None:
    _fake(monkeypatch, fetches=fetches)
    with pytest.raises(SystemExit, match=rf"put {re.escape(fetches)} in its package cache"):
        validate.run_validator([resource], docker=False, offline=True)


@pytest.mark.usefixtures("locked")
def test_offline_accepts_a_package_of_the_run_the_validator_unpacked_again(
    monkeypatch: pytest.MonkeyPatch, resource: Path
) -> None:
    _fake(monkeypatch, fetches=US_CORE)
    assert validate.run_validator([resource], docker=False, offline=True) == {str(resource): []}


def test_offline_refuses_a_package_the_lock_does_not_pin_before_the_validator_runs(
    monkeypatch: pytest.MonkeyPatch, locked: Path, resource: Path, tmp_path: Path
) -> None:
    fake = _fake(monkeypatch)
    stray = tmp_path / "example.fhir.other-guide-2.0.0.tgz"  # the same name, not the download
    stray.write_bytes((locked / stray.name).read_bytes())
    with pytest.raises(SystemExit, match=re.escape("not a tarball scripts/fhir_packages.lock")):
        validate.run_validator([resource], docker=False, igs=[str(stray)], offline=True)
    with pytest.raises(SystemExit, match=re.escape("loads x.y#1.0.0, which the lock does not")):
        validate.run_validator([resource], docker=False, igs=["x.y#1.0.0"], offline=True)
    assert fake.commands == []


@pytest.mark.usefixtures("locked")
def test_offline_in_docker_runs_with_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake(monkeypatch)
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
    (home,) = [arg.removeprefix(HOME) for arg in command[image:] if arg.startswith(HOME)]
    assert f"HOME={home}" in command[:image] and home.startswith("/work/var/")  # the run's own
    assert fake.cached == [sorted([OWN, US_CORE, TERMINOLOGY])]


TX = "https://tx.example.org/r4"  # a terminology server (terminology.yml: tx.fhir.org)


def _tx(command: list[str]) -> list[str]:
    """Each value the command gives the validator's `-tx`."""
    return [command[i + 1] for i, arg in enumerate(command) if arg == "-tx"]


@pytest.mark.usefixtures("locked")
def test_offline_is_the_default_and_tx_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, resource: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With neither option the validator has no terminology server (`-tx n/a`), as with
    `--offline`. `--tx` puts the server in its place, once, with HTTP (a server needs it) and
    the run's own package cache from the lock; it is refused with `--offline`, before anything
    runs. `ARGS` itself never changes."""
    fake = _fake(monkeypatch)

    def run(*options: str) -> int:
        monkeypatch.setattr(sys, "argv", ["fhir_validate.py", *options, "--valid", str(resource)])
        return validate.main()

    assert run() == 0
    assert _tx(fake.commands[0]) == ["n/a"] and "-no-http-access" not in fake.commands[0]
    assert run("--offline") == 0
    assert _tx(fake.commands[1]) == ["n/a"] and "-no-http-access" in fake.commands[1]
    assert run("--tx", TX) == 0
    command = fake.commands[2]
    assert _tx(command) == [TX] and "-no-http-access" not in command
    assert fake.cached == [sorted([OWN, US_CORE, TERMINOLOGY])] * 2  # --offline's, then --tx's
    assert f"{HOME}{Path.home()}" not in command
    assert validate.ARGS[validate.ARGS.index("-tx") + 1] == "n/a"
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:
        run("--offline", "--tx", TX)
    assert refused.value.code == 2
    assert "argument --tx: not allowed with argument --offline" in capsys.readouterr().err
    assert len(fake.commands) == 3
    with pytest.raises(ValueError, match="cannot go with --offline"):
        validate.run_validator([resource], docker=False, offline=True, tx=TX)
    assert len(fake.commands) == 3


@pytest.mark.parametrize(
    "fetches", ["hl7.terminology#9.9.9", OTHER_GUIDE], ids=["unpinned", "not-run"]
)
@pytest.mark.usefixtures("locked")
def test_tx_fails_when_the_validator_put_a_package_the_run_lacks_in_its_cache(
    monkeypatch: pytest.MonkeyPatch, resource: Path, fetches: str
) -> None:
    """With HTTP on for the terminology server, the validator could fetch a package too: the
    run's cache is checked as an offline run's is."""
    _fake(monkeypatch, fetches=fetches)
    with pytest.raises(SystemExit, match=rf"put {re.escape(fetches)} in its package cache"):
        validate.run_validator([resource], docker=False, tx=TX)


@pytest.mark.usefixtures("locked")
def test_tx_in_docker_has_a_network_and_the_runs_own_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(monkeypatch)
    inside = ROOT / "var" / "test-fhir-packages-tx-resource.json"
    inside.parent.mkdir(exist_ok=True)
    inside.write_text('{"resourceType": "Patient"}')
    try:
        validate.run_validator([inside], docker=True, tx=TX)
    finally:
        inside.unlink()
    (command,) = fake.commands
    image = command.index(validate.IMAGE)
    assert "--network" not in command[:image] and "-no-http-access" not in command
    assert _tx(command[image:]) == [TX]
    (home,) = [arg.removeprefix(HOME) for arg in command[image:] if arg.startswith(HOME)]
    assert home.startswith("/work/var/") and home != validate._mounted(validate.DOCKER_HOME)
    assert fake.cached == [sorted([OWN, US_CORE, TERMINOLOGY])]


@pytest.mark.parametrize(
    "value",
    [
        "tx.fhir.org/r4",  # no scheme
        "ftp://tx.example.org/r4",
        "https:///r4",  # no host
        "https://user:secret@tx.example.org/r4",
        "https://tx.example.org/r4?token=x",
        "https://tx.example.org/r4#x",
        "n/a",  # the default is no option at all
    ],
)
@pytest.mark.usefixtures("locked")
def test_a_terminology_server_is_an_http_url_with_no_credentials(
    monkeypatch: pytest.MonkeyPatch, resource: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    fake = _fake(monkeypatch)
    argv = ["fhir_validate.py", "--tx", value, "--valid", str(resource)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        validate.main()
    err = capsys.readouterr().err
    assert "a terminology server is an http(s) URL" in err and "secret" not in err
    assert fake.commands == []


@pytest.mark.usefixtures("locked")
def test_no_report_shows_the_end_of_the_validators_output(
    monkeypatch: pytest.MonkeyPatch, resource: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake(monkeypatch, report=False)
    with pytest.raises(SystemExit, match="produced no report"):
        validate.run_validator([resource], docker=False, offline=True)
    assert "Unable to find package x.y#1.0.0" in capsys.readouterr().err


@pytest.mark.usefixtures("locked")
def test_a_missing_package_tarball_is_refused_before_the_validator_runs(
    monkeypatch: pytest.MonkeyPatch, resource: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _fake(monkeypatch)
    missing = "var/fhir-packages/example.fhir.other-guide-0.0.0.tgz"
    argv = ["fhir_validate.py", "--offline", "--ig", missing, "--valid", str(resource)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        validate.main()
    assert f"--ig {missing}: no such package tarball" in capsys.readouterr().err
    assert fake.commands == []


def _globbed(path: str, pattern: str) -> bool:
    """Whether the shell's glob `pattern` names `path` (`*` stays within a folder)."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", "[^/]*"), path) is not None
