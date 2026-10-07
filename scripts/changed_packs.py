# SPDX-License-Identifier: Apache-2.0
"""The pack directories a pull request changes (workflow `packs`, job `pack-check`),
NUL-separated on stdout, so a directory name is never word-split or put into code, whatever
characters it holds.

    uv run python scripts/changed_packs.py BASE_SHA | while IFS= read -r -d '' dir; do ...; done

Under `packs/`, a changed file belongs to the nearest folder above it that holds a `pack.yaml`
(a pack can sit up to four levels deep, as the registry finds it). A file under no pack, such as
one left over from a removed pack, is reported as its top folder, `packs/<name>`, which then has
no `pack.yaml`: the workflow says it was removed.

Under `community-packs/`, a pack is exactly one folder, `community-packs/<name>`, whatever its
files (a `pack.yaml` deeper is the pack's mistake, which `mendari community-check` reports).
Files directly in `community-packs/` (`MAINTAINERS.yaml`, `README.md`) belong to no pack.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PACKS = "packs"
COMMUNITY = "community-packs"
MANIFEST = "pack.yaml"
MAX_DEPTH = 4  # the deepest pack.yaml the registry loads: packs/a/b/c/pack.yaml


def pack_dirs(paths: Iterable[str], root: Path) -> list[str]:
    """The pack directories, relative to `root`, that the changed `paths` belong to."""
    found: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] not in (PACKS, COMMUNITY):
            continue  # outside packs/ and community-packs/, or a file directly in one
        if parts[0] == COMMUNITY:
            found.add(str(PurePosixPath(*parts[:2])))
            continue
        owner = PurePosixPath(*parts[:2])
        for depth in range(min(len(parts) - 1, MAX_DEPTH), 1, -1):
            candidate = PurePosixPath(*parts[:depth])
            if (root / candidate / MANIFEST).is_file():
                owner = candidate
                break
        found.add(str(owner))
    return sorted(found)


def _changed(base: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "-z",
            "--name-only",
            "--no-renames",
            f"{base}...HEAD",
            "--",
            PACKS,
            COMMUNITY,
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", help="the commit the pull request is compared with")
    args = parser.parse_args()
    for directory in pack_dirs(_changed(args.base), ROOT):
        sys.stdout.buffer.write(directory.encode("utf-8", "surrogateescape") + b"\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
