# SPDX-License-Identifier: Apache-2.0
"""The source distribution holds only what a source build needs (pyproject.toml,
[tool.hatch.build.targets.sdist]): the package, the agent it bundles (`agents/`), the README,
the licence, its NOTICE and the build file. Exit 1, naming each stray entry, if it holds anything
else (the tests, which read repository files the sdist leaves out and so run from a clone;
project notes, agent or CI configuration, deploy templates, the websites), or if it lacks a file
git tracks under the included folders.

    uv build --out-dir dist && uv run python scripts/check_sdist.py dist/mendari-*.tar.gz
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
from collections.abc import Iterable
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


def _tracked() -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in result.stdout.decode().split("\0") if p]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sdist", type=Path, help="the .tar.gz that `uv build` wrote")
    args = parser.parse_args()
    with tarfile.open(args.sdist) as archive:
        names = [m.name for m in archive.getmembers() if not m.isdir()]
    found = problems(names, _tracked())
    for line in found:
        print(line)
    if not found:
        print(f"ok  {args.sdist.name}: {len(names)} files, only what a source build needs")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
