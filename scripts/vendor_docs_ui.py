"""Vendor pinned npm releases: Swagger UI (`swagger-ui-dist`) for the API reference at
`/docs/reference`, and axe-core for the browser tests' accessibility checks (`PACKAGES`).

The reference page runs in Mendari's origin, where a signed-in person's session can read and
export the audit trail. So its code is served from this repository, never a CDN, at a version
pinned here and under a strict CSP like every other page (threat model I6, E5).

The pin is `src/mendari/dashboard/vendor/swagger-ui/package.json`, with `package-lock.json`
beside it: Dependabot watches that directory and proposes an update by changing both. Nothing
there is ever installed. This script downloads the tarball the lockfile names, checks it against
the lockfile's integrity value, and extracts only the files the page needs into `<version>/`:
the bundle and its stylesheet, the bundle's third-party license notices, and the Apache-2.0
LICENSE and NOTICE that must ship with it. `manifest.json` records the version, the integrity
value and each file's sha256. The server reads the version from it and serves the files under a
path that names it, cached as immutable.

axe-core (MPL-2.0) is vendored the same way into `tests/browser/vendor/axe-core`, with its
LICENSE and third-party notices: the browser tests inject it into each page they check. It is
never served by the engine.

`--check` is offline and runs in the test suite (`tests/test_vendor.py`): for every package, the
pin, the lockfile, the manifest and the files must agree. A Dependabot pull request therefore
fails CI until someone runs this script in it, reads the external hosts it reports, and commits
the new files.

Usage:

    uv run python scripts/vendor_docs_ui.py            # vendor every pinned package
    uv run python scripts/vendor_docs_ui.py axe-core   # vendor one
    uv run python scripts/vendor_docs_ui.py --check    # the files match the pins (offline)
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://registry.npmjs.org/"


@dataclass(frozen=True)
class Vendored:
    """One pinned npm release: where it is vendored, the files taken from it (the first is the
    code whose hosts are listed for review) and the license it must still carry."""

    package: str
    directory: Path
    files: tuple[str, ...]
    license: str


SWAGGER = Vendored(
    "swagger-ui-dist",
    ROOT / "src/mendari/dashboard/vendor/swagger-ui",
    (
        "swagger-ui-bundle.js",
        "swagger-ui-bundle.js.LICENSE.txt",  # the bundle's first line points here
        "swagger-ui.css",
        "LICENSE",
        "NOTICE",
    ),
    "Apache-2.0",
)
AXE = Vendored(
    "axe-core",
    ROOT / "tests/browser/vendor/axe-core",
    ("axe.min.js", "LICENSE", "LICENSE-3RD-PARTY.txt"),
    "MPL-2.0",
)
PACKAGES = (SWAGGER, AXE)
# Swagger UI's, as before there were two packages (tests/test_vendor.py, docs).
VENDOR, PACKAGE, FILES = SWAGGER.directory, SWAGGER.package, SWAGGER.files
VERSION = re.compile(r"^\d+\.\d+\.\d+$")  # an exact release: no range, no pre-release
HOST = re.compile(rb"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)


def pinned(directory: Path = VENDOR, spec: Vendored = SWAGGER) -> dict[str, str]:
    """The version `package.json` pins, and the lockfile's tarball URL and integrity for it."""
    package = json.loads((directory / "package.json").read_text())
    version = package["dependencies"][spec.package]
    if not VERSION.match(version):
        raise ValueError(f"package.json pins {spec.package} {version!r}, not an exact version")
    locked = json.loads((directory / "package-lock.json").read_text())
    entry = locked["packages"][f"node_modules/{spec.package}"]
    if entry["version"] != version:
        raise ValueError(f"package-lock.json has {entry['version']}, package.json {version}")
    if not entry["resolved"].startswith(REGISTRY):
        raise ValueError(f"package-lock.json resolves {spec.package} outside {REGISTRY}")
    if not entry["integrity"].startswith("sha512-"):
        raise ValueError("package-lock.json has no sha512 integrity value")
    return {"version": version, "tarball": entry["resolved"], "integrity": entry["integrity"]}


def check(directory: Path = VENDOR, spec: Vendored = SWAGGER) -> list[str]:
    """What disagrees between the pin, the lockfile, the manifest and the files: nothing, or
    one line per problem."""
    try:
        pin = pinned(directory, spec)
        manifest = json.loads((directory / "manifest.json").read_text())
    except (OSError, KeyError, ValueError) as error:
        return [f"{type(error).__name__}: {error}"]
    problems = [
        f"manifest.json has {key} {manifest.get(key)!r}, the lockfile {pin[key]!r}: "
        "run scripts/vendor_docs_ui.py"
        for key in ("version", "tarball", "integrity")
        if manifest.get(key) != pin[key]
    ]
    if manifest.get("package") != spec.package or tuple(manifest.get("files", {})) != spec.files:
        problems.append("manifest.json names another package or other files")
    version = str(manifest.get("version"))
    versions = sorted(p.name for p in directory.iterdir() if p.is_dir())
    if versions != [version]:
        problems.append(f"version directories {versions}, expected [{version!r}]")
    files = directory / version
    present = sorted(p.name for p in files.iterdir()) if files.is_dir() else []
    if present != sorted(spec.files):
        problems.append(f"{version}/ holds {present}, expected {sorted(spec.files)}")
    for name, digest in manifest.get("files", {}).items():
        path = files / name
        if path.is_file() and _sha256(path.read_bytes()) != digest:
            problems.append(f"{version}/{name} does not match its sha256 in manifest.json")
    return problems


def vendor(directory: Path = VENDOR, spec: Vendored = SWAGGER) -> None:
    """Download the pinned tarball, verify it, and replace the vendored files and manifest."""
    pin = pinned(directory, spec)
    with urllib.request.urlopen(pin["tarball"], timeout=120) as response:
        tarball = response.read()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    if integrity != pin["integrity"]:
        sys.exit(f"{pin['tarball']}: integrity {integrity}, the lockfile says {pin['integrity']}")
    contents = _extract(tarball, spec.files)
    meta = json.loads(contents.pop("package.json"))
    if (meta["name"], meta["version"]) != (spec.package, pin["version"]):
        sys.exit(f"the tarball is {meta['name']} {meta['version']}")
    if meta["license"] != spec.license:
        sys.exit(f"the license changed to {meta['license']}: read it before vendoring")
    before = _hosts(directory, spec)
    for old in (p for p in directory.iterdir() if p.is_dir()):
        shutil.rmtree(old)
    target = directory / pin["version"]
    target.mkdir()
    for name in spec.files:
        (target / name).write_bytes(contents[name])
    manifest: dict[str, Any] = {
        "package": spec.package,
        **pin,
        "license": meta["license"],
        "files": {name: _sha256(contents[name]) for name in spec.files},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    after = _hosts(directory, spec)
    print(f"vendored {spec.package} {pin['version']} into {target.relative_to(ROOT)}")
    print(f"hosts {spec.files[0]} names, for review (+ new, - gone):")
    for host in sorted(before | after):
        mark = "+" if host not in before else "-" if host not in after else " "
        print(f"  {mark} {host}")


def _extract(tarball: bytes, files: tuple[str, ...] = FILES) -> dict[str, bytes]:
    """The listed files and package.json, as regular files only (no links, nothing else)."""
    wanted = {f"package/{name}": name for name in (*files, "package.json")}
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member) if member.isfile() else None
            if member.name in wanted and extracted is not None:
                found[wanted[member.name]] = extracted.read()
    missing = set(wanted.values()) - set(found)
    if missing:
        sys.exit(f"the tarball lacks {sorted(missing)}")
    return found


def _hosts(directory: Path, spec: Vendored) -> set[str]:
    bundles = directory.glob(f"*/{spec.files[0]}")
    return {m.decode().lower() for p in bundles for m in HOST.findall(p.read_bytes())}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        problems = [
            f"{spec.package}: {problem}"
            for spec in PACKAGES
            for problem in check(spec.directory, spec)
        ]
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    named = {spec.package: spec for spec in PACKAGES}
    if any(name not in named for name in argv):
        print(__doc__, file=sys.stderr)
        return 2
    for spec in [named[name] for name in argv] or list(PACKAGES):
        vendor(spec.directory, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
