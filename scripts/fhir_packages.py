# SPDX-License-Identifier: Apache-2.0
"""The FHIR packages the HL7 validator loads in CI, pinned by sha256 (`scripts/fhir_packages.lock`).

    uv run python scripts/fhir_packages.py install [--cache DIR]
    uv run python scripts/fhir_packages.py lock

`install` puts the package tarballs CI downloaded and checked with `sha256sum -c`
(`var/fhir-packages/<name>-<version>.tgz`, job `fhir-conformance`) into the validator's package
cache: `~/.fhir/packages` by default, `var/validator/cache/.fhir/packages` for
`scripts/fhir_validate.py --docker`. Each goes where the validator would put it,
`<name>#<version>/`. Before it writes anything it checks each tarball's sha256 again, that the
tarball is the package the lock names, and that the lock is exactly what `ROOTS` needs: every
dependency of every locked package is locked (a core package aside, which the validator never
loads as a dependency), and every locked package is needed. `scripts/fhir_validate.py --offline`
then runs the validator with no HTTP access, so it loads these packages and nothing else.

`lock` writes the lock again after a change to `ROOTS` (a validator or guide version). It
downloads every package of the closure from packages.fhir.org, else packages2.fhir.org, to read
its dependencies and compute its sha256. A tarball already in `var/fhir-packages` stands in for
the download only if the lock already pins it with that sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "scripts" / "fhir_packages.lock"
DOWNLOADS = ROOT / "var" / "fhir-packages"
CACHE = Path.home() / ".fhir" / "packages"
REGISTRIES = (
    "https://packages.fhir.org/{name}/{version}",
    "https://packages2.fhir.org/packages/{name}/{version}",
)
# The validator release `ROOTS` was read from (ci.yml's VALIDATOR_VERSION must match).
VALIDATOR = "6.10.4"
ROOTS = (
    # What the validator loads by itself (ValidationEngine and ValidationService at its tag): the
    # R4 definitions for `-version 4.0.1`, the cross-version extensions, and its working versions
    # of the terminology and the extensions.
    "hl7.fhir.r4.core#4.0.1",
    "hl7.fhir.xver-extensions#0.1.0",
    "hl7.terminology.r4#6.2.0",
    "hl7.fhir.uv.extensions.r4#5.2.0",
    # It also asks for these two without a version: online it takes the newest, offline the one
    # in its cache. These are the newest of 2026-10-18.
    "hl7.terminology#7.4.0",
    "hl7.fhir.uv.extensions#5.3.0",
    # US Core (`ARGS` in scripts/fhir_validate.py) and the Da Vinci guides (ci.yml).
    "hl7.fhir.us.core#6.1.0",
    "hl7.fhir.us.davinci-crd#2.2.1",
    "hl7.fhir.us.davinci-dtr#2.2.0",
    "hl7.fhir.us.davinci-pas#2.2.1",
)
# The validator never loads a core package as a dependency (VersionUtilities.isCorePackage): it
# loads hl7.fhir.r4.core itself, as the definitions, and no other.
CORE = frozenset(
    {
        "hl7.fhir.core",
        "hl7.fhir.r2.core",
        "hl7.fhir.r2b.core",
        "hl7.fhir.r3.core",
        "hl7.fhir.r4.core",
        "hl7.fhir.r4b.core",
        "hl7.fhir.r5.core",
        "hl7.fhir.r6.core",
    }
)
# A package name and a version, as the validator's package cache reads them.
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+$")
VERSION = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEADER = """\
# The FHIR packages the HL7 validator loads in CI (job fhir-conformance), pinned by sha256.
# One package per line: its name, its version, the sha256 of its tarball, and the URL CI
# downloads it from (packages.fhir.org; packages2.fhir.org for what only it serves).
# CI checks each download with `sha256sum -c` and fails on a mismatch. Then
# `scripts/fhir_packages.py install` puts them in the validator's package cache, and the
# validator runs with no HTTP access (`scripts/fhir_validate.py --offline`): it loads these
# packages and nothing else. The list is what `ROOTS` in scripts/fhir_packages.py needs, with
# every dependency of theirs (core packages aside, which the validator never loads as a
# dependency); `install` refuses a lock that is not exactly that.
# Written by `uv run python scripts/fhir_packages.py lock`; do not edit by hand.
"""


class LockError(Exception):
    """The lock, a tarball or a package's manifest is not what it should be."""


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    sha256: str
    url: str

    @property
    def ref(self) -> str:
        return f"{self.name}#{self.version}"

    @property
    def tarball(self) -> str:
        return f"{self.name}-{self.version}.tgz"


def parse_lock(text: str) -> list[Package]:
    """The locked packages, in the lock's order (sorted by name, then version)."""
    packages: list[Package] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise LockError(f"line {number}: expected name, version, sha256 and url")
        package = Package(*fields)
        if not NAME.match(package.name) or not VERSION.match(package.version):
            raise LockError(f"line {number}: not a package name and version: {package.ref}")
        if not SHA256.match(package.sha256):
            raise LockError(f"line {number}: not a sha256: {package.sha256}")
        urls = [r.format(name=package.name, version=package.version) for r in REGISTRIES]
        if package.url not in urls:
            raise LockError(f"line {number}: {package.url} is not {package.ref} on a registry")
        packages.append(package)
    if not packages:
        raise LockError("the lock names no package")
    refs = [(p.name, p.version) for p in packages]
    if refs != sorted(set(refs)):
        raise LockError("the lock must name each package once, sorted by name and version")
    return packages


def read_lock(path: Path = LOCK) -> list[Package]:
    return parse_lock(path.read_text(encoding="utf-8"))


def format_lock(packages: Iterable[Package]) -> str:
    rows = sorted(packages, key=lambda p: (p.name, p.version))
    name = max(len(p.name) for p in rows)
    version = max(len(p.version) for p in rows)
    lines = [f"{p.name:<{name}}  {p.version:<{version}}  {p.sha256}  {p.url}" for p in rows]
    return HEADER + "\n".join(lines) + "\n"


def manifest(data: bytes) -> dict[str, Any]:
    """The package's `package/package.json` (read up to it, not through the whole tarball)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = next((m for m in archive if m.name == "package/package.json"), None)
            handle = archive.extractfile(member) if member and member.isfile() else None
            if handle is None:
                raise LockError("not a FHIR package: no package/package.json")
            loaded = json.loads(handle.read())
    except (tarfile.TarError, OSError, ValueError) as error:
        raise LockError(f"not a FHIR package: {error}") from error
    if not isinstance(loaded, dict):
        raise LockError("not a FHIR package: package.json is not an object")
    return loaded


def dependencies(found: dict[str, Any]) -> set[str]:
    """The packages the validator loads for this one, as `name#version`."""
    declared = found.get("dependencies") or {}
    return {f"{name}#{version}" for name, version in declared.items() if name not in CORE}


def closure_problems(
    manifests: dict[str, dict[str, Any]], roots: Iterable[str] = ROOTS
) -> list[str]:
    """What keeps the locked packages (`manifests`, by `name#version`) from being exactly
    what `roots` need."""
    roots = tuple(roots)
    problems = [
        f"{root} is in ROOTS but not in the lock" for root in roots if root not in manifests
    ]
    for ref, found in sorted(manifests.items()):
        for needed in sorted(dependencies(found) - set(manifests)):
            problems.append(f"{ref} needs {needed}, which the lock does not pin")
    reached: set[str] = set()
    queue = [root for root in roots if root in manifests]
    while queue:
        ref = queue.pop()
        if ref not in reached:
            reached.add(ref)
            queue.extend(d for d in dependencies(manifests[ref]) if d in manifests)
    problems += [
        f"{ref} is in the lock but nothing in ROOTS needs it"
        for ref in sorted(set(manifests) - reached)
    ]
    return problems


def verified(package: Package, downloads: Path) -> tuple[bytes, dict[str, Any]]:
    """The package's tarball and manifest, if the tarball is the one the lock pins."""
    path = downloads / package.tarball
    if not path.is_file():
        raise LockError(f"{path}: missing (CI downloads it from {package.url})")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != package.sha256:
        raise LockError(f"{path}: sha256 {digest}, but the lock pins {package.sha256}")
    found = manifest(data)
    if (found.get("name"), found.get("version")) != (package.name, package.version):
        named = f"{found.get('name')}#{found.get('version')}"
        raise LockError(f"{path}: holds {named}, not {package.ref}")
    return data, found


def extract(data: bytes, target: Path) -> None:
    """Unpack a tarball into `target` as the validator does: every entry where it is named
    (`package/...`), regular files and folders only, nothing outside `target`."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if member.name.startswith("/") or ".." in parts or "\\" in member.name:
                    raise LockError(f"{target.name}: an entry outside the package: {member.name}")
                if member.isdir():
                    target.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise LockError(f"{target.name}: not a file or a folder: {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise LockError(f"{target.name}: unreadable entry: {member.name}")
                destination = target.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def install(
    cache: Path, downloads: Path = DOWNLOADS, lock: Path = LOCK, roots: Iterable[str] = ROOTS
) -> int:
    """Put the locked packages in `cache`, once every check has passed."""
    packages = read_lock(lock)
    # Everything is checked before anything is written; each tarball is read (and checked)
    # again as it is unpacked.
    problems = closure_problems({p.ref: verified(p, downloads)[1] for p in packages}, roots)
    if problems:
        raise LockError("\n".join(problems))
    for package in packages:
        extract(verified(package, downloads)[0], cache / package.ref)
    print(f"{len(packages)} packages in {cache}, each the tarball {lock.name} pins")
    return 0


def fetch_from_registries(name: str, version: str) -> tuple[bytes, str]:
    """The package's tarball and the URL it came from: packages.fhir.org, else packages2."""
    for pattern in REGISTRIES:
        url = pattern.format(name=name, version=version)
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                return response.read(), url
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    raise LockError(f"{name}#{version} is on no registry")


def resolve(roots: Iterable[str], fetch: Callable[[str, str], tuple[bytes, str]]) -> list[Package]:
    """Every package `roots` need, sorted by name and version. `fetch(name, version)` gives a
    package's tarball and the URL it came from."""
    found: dict[str, Package] = {}
    queue = list(roots)
    while queue:
        ref = queue.pop(0)
        if ref in found:
            continue
        name, version = ref.split("#")
        data, url = fetch(name, version)
        declared = manifest(data)
        if (declared.get("name"), declared.get("version")) != (name, version):
            raise LockError(f"{url} holds {declared.get('name')}#{declared.get('version')}")
        found[ref] = Package(name, version, hashlib.sha256(data).hexdigest(), url)
        queue.extend(sorted(dependencies(declared)))
    return sorted(found.values(), key=lambda p: (p.name, p.version))


def lock(downloads: Path = DOWNLOADS, path: Path = LOCK) -> int:
    """Write the lock for `ROOTS` from the registries."""
    pinned = {p.ref: p for p in read_lock(path)} if path.exists() else {}

    def fetch(name: str, version: str) -> tuple[bytes, str]:
        known = pinned.get(f"{name}#{version}")
        cached = downloads / f"{name}-{version}.tgz"
        if known and cached.is_file():
            data = cached.read_bytes()
            if hashlib.sha256(data).hexdigest() == known.sha256:
                return data, known.url
        data, url = fetch_from_registries(name, version)
        downloads.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data, url

    packages = resolve(ROOTS, fetch)
    path.write_text(format_lock(packages), encoding="utf-8")
    print(f"{path}: {len(packages)} packages")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    install_command = commands.add_parser("install", help="the locked packages into the cache")
    install_command.add_argument("--cache", type=Path, default=CACHE, help=f"default {CACHE}")
    commands.add_parser("lock", help="write the lock again from the registries")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            return install(args.cache, DOWNLOADS, LOCK, ROOTS)
        return lock(DOWNLOADS, LOCK)
    except LockError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
