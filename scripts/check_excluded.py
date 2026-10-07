# SPDX-License-Identifier: Apache-2.0
"""No internal material reaches what is published: project notes and plans (notes/, tasks/), agent
instructions (CLAUDE.md, AGENTS.md, .claude/) and CI configuration (.github/) are refused at any
depth in a source distribution, a wheel, the built documentation site and a container image.

An archive (.tar.gz, .tar, .whl, .zip) or a directory is checked whole. An image is exported and
checked for agent files anywhere, and for every excluded name in the application's own paths
(the package, its metadata, its user's home and its data folder): a base image's own
directories may be called `tasks` or `notes`. Exit 1, naming each excluded path found.

    python3 scripts/check_excluded.py dist/mendari-*.tar.gz dist/*.whl site/
    python3 scripts/check_excluded.py --image mendari-engine:ci
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO

# Names refused as any component of a path.
AGENT_FILES = frozenset({"CLAUDE.md", "AGENTS.md", ".claude"})
EXCLUDED = AGENT_FILES | {"tasks", "notes", ".github"}
# An image's application paths: the installed package and its metadata, the user's home, /data.
APPLICATION = re.compile(
    r"^(opt/venv/lib/python[^/]+/site-packages/(mendari|mendari-[^/]+\.dist-info)/"
    r"|home/mendari/|data/)"
)


def excluded(names: Iterable[str], refused: frozenset[str] = EXCLUDED) -> list[str]:
    """The paths among `names` with a refused component, sorted."""
    found = set()
    for name in names:
        if refused & set(PurePosixPath(name).parts):
            found.add(name)
    return sorted(found)


def members(path: Path) -> list[str]:
    """Every path inside an archive, or under a directory, relative to it."""
    if path.is_dir():
        return [p.relative_to(path).as_posix() for p in path.rglob("*")]
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        return archive.getnames()


def image_problems(export: IO[bytes]) -> list[str]:
    """The excluded paths in an exported image filesystem (`docker export`, a tar stream)."""
    with tarfile.open(fileobj=export, mode="r|") as archive:
        names = [member.name for member in archive]
    application = [n for n in names if APPLICATION.match(PurePosixPath(n).as_posix().lstrip("/"))]
    return sorted(set(excluded(names, AGENT_FILES)) | set(excluded(application)))


def _image(image: str) -> list[str]:
    container = subprocess.run(
        ["docker", "create", image], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        with subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE) as export:
            assert export.stdout is not None
            found = image_problems(export.stdout)
        if export.returncode != 0:
            raise SystemExit(f"docker export failed for {image}")
        return found
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path, help="archives or directories")
    parser.add_argument("--image", action="append", default=[], help="a local image to export")
    args = parser.parse_args(argv)
    if not args.paths and not args.image:
        parser.error("name an archive, a directory or an image")
    failed = False
    checked = [(str(path), excluded(members(path))) for path in args.paths]
    checked += [(f"image {image}", _image(image)) for image in args.image]
    for what, found in checked:
        for name in found:
            print(f"excluded path in {what}: {name}", file=sys.stderr)
        if found:
            failed = True
        else:
            print(f"ok  {what}: no notes, plans, agent or CI files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
