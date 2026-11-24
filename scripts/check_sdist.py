# SPDX-License-Identifier: Apache-2.0
"""The source distribution holds only what a source build needs (pyproject.toml,
[tool.hatch.build.targets.sdist]): the package, the agent it bundles (`agents/`), the README,
the licence, its NOTICE and the build file. Exit 1, naming each stray entry, if it holds anything
else (the tests, which read repository files the sdist leaves out and so run from a clone;
project notes, agent or CI configuration, deploy templates, the websites), or if it lacks a file
git tracks under the included folders.

With `--wheel`, the wheel built from it is checked too: it holds every file git tracks in the
folders it is built from ([tool.hatch.build.targets.wheel]: the package with the console's files
and the templates `mendari new` writes, and the bundled agent), and nothing else but the build's
own metadata. An installed Mendari has no clone beside it, so a file the wheel lacks is missing
for everyone who installs it.

    uv build --out-dir dist && uv run python scripts/check_sdist.py dist/mendari-*.tar.gz
    python3 scripts/check_sdist.py dist/mendari-*.tar.gz --wheel dist/mendari-*.whl
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What pyproject.toml includes, as top-level names inside the archive's single root folder.
INCLUDED = frozenset({"src", "agents", "README.md", "LICENSE", "NOTICE", "pyproject.toml"})
# Written by the build backend itself.
GENERATED = frozenset({"PKG-INFO", ".gitignore"})


def problems(names: Iterable[str], tracked: Iterable[str] | None = None) -> list[str]:
    """What is wrong with an sdist whose members are `names` (each `<root>/<path>`).
    `tracked`, when given, lists the repository's files: each one under an included folder must
    be in the archive."""
    found: list[str] = []
    paths: set[str] = set()
    for name in names:
        _, _, path = name.partition("/")
        if not path:
            continue  # the root folder itself
        paths.add(path)
        top = path.split("/", 1)[0]
        if top not in INCLUDED | GENERATED:
            found.append(f"not part of a source build: {path}")
    for path in tracked or ():
        top = path.split("/", 1)[0]
        if top in INCLUDED and path not in paths:
            found.append(f"tracked but missing: {path}")
    return sorted(set(found))


def wheel_sources() -> dict[str, str]:
    """The repository folders a wheel is built from, each with the folder it becomes in the
    wheel: pyproject.toml's `packages` (a package keeps its own name) and `force-include`."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    return {
        **{package: package.rsplit("/", 1)[-1] for package in wheel["packages"]},
        **wheel["force-include"],
    }


def wheel_problems(
    names: Iterable[str], tracked: Iterable[str], sources: Mapping[str, str]
) -> list[str]:
    """What is wrong with a wheel whose members are `names`: a file git tracks in a folder the
    wheel is built from (`sources`) that it lacks, or a file that is neither one of those nor
    the build's own metadata (`<name>-<version>.dist-info/`)."""
    expected: dict[str, str] = {}
    for path in tracked:
        for source, target in sources.items():
            if path.startswith(f"{source}/"):
                expected[target + path[len(source) :]] = path
    held = set(names)
    found = [
        f"tracked but missing from the wheel: {path}"
        for name, path in expected.items()
        if name not in held
    ]
    found += [
        f"in the wheel but not tracked: {name}"
        for name in held
        if name not in expected and not name.split("/", 1)[0].endswith(".dist-info")
    ]
    return sorted(found)


def _tracked() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in result.stdout.decode().split("\0") if p]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sdist", type=Path, help="the .tar.gz that `uv build` wrote")
    parser.add_argument("--wheel", type=Path, help="also check the .whl built from it")
    args = parser.parse_args()
    tracked = _tracked()
    with tarfile.open(args.sdist) as archive:
        names = [m.name for m in archive.getmembers() if not m.isdir()]
    found = problems(names, tracked)
    if not found:
        print(f"ok  {args.sdist.name}: {len(names)} files, only what a source build needs")
    if args.wheel is not None:
        with zipfile.ZipFile(args.wheel) as wheel:
            held = [name for name in wheel.namelist() if not name.endswith("/")]
        missing = wheel_problems(held, tracked, wheel_sources())
        if not missing:
            print(f"ok  {args.wheel.name}: {len(held)} files, every tracked one and no other")
        found += missing
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
