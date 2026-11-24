# SPDX-License-Identifier: Apache-2.0
"""The pack directories a pull request changes (workflow `packs`, job `pack-check`),
NUL-separated on stdout, so a directory name is never word-split or put into code, whatever
characters it holds.

    uv run python scripts/changed_packs.py BASE_SHA | while IFS= read -r -d '' dir; do ...; done

Under `agents/`, a changed file belongs to the nearest folder above it that holds a manifest
(`agent.yaml`, or `pack.yaml`, the name it had before: `mendari.core.pack.MANIFEST_NAMES`; an
agent can sit up to four levels deep, as the registry finds it). A file under no agent, such as
one left over from a removed agent, is reported as its top folder, `agents/<name>`, which then
has no manifest: the workflow says it was removed.

Under `community-agents/`, an agent is exactly one folder, `community-agents/<name>`, whatever its
files (a manifest deeper is the agent's mistake, which `mendari community-check` reports).
Files directly in `community-agents/` (`MAINTAINERS.yaml`, `README.md`) belong to no agent.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mendari.core.pack import has_manifest  # noqa: E402

PACKS = "agents"
COMMUNITY = "community-agents"
MAX_DEPTH = 4  # the deepest manifest the registry loads: agents/a/b/c/agent.yaml


def pack_dirs(paths: Iterable[str], root: Path) -> list[str]:
    """The pack directories, relative to `root`, that the changed `paths` belong to."""
    found: set[str] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[0] not in (PACKS, COMMUNITY):
            continue  # outside agents/ and community-agents/, or a file directly in one
        if parts[0] == COMMUNITY:
            found.add(str(PurePosixPath(*parts[:2])))
            continue
        owner = PurePosixPath(*parts[:2])
        for depth in range(min(len(parts) - 1, MAX_DEPTH), 1, -1):
            candidate = PurePosixPath(*parts[:depth])
            if has_manifest(root / candidate):
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
