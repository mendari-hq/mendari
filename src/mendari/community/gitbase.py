# SPDX-License-Identifier: Apache-2.0
"""The base of a pull request, read from git: the files a change touches and what a file held
before it. Paths come NUL-separated (`-z`) and travel only through argv, so a name is never
split or run, whatever characters it holds."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from mendari.errors import MendariError

COMMUNITY_DIR = "community-packs"


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", *args],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise MendariError("git is not installed; it is needed to compare with --base") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MendariError(
            f"git {args[0]} failed: {detail[-1] if detail else 'no output'}"
        ) from None
    return result.stdout


def _paths(output: bytes) -> list[str]:
    return [p for p in output.decode("utf-8", "surrogateescape").split("\0") if p]


def toplevel(path: Path) -> Path | None:
    """The root of the git repository holding `path`, or None outside one."""
    try:
        out = _git(path, "rev-parse", "--show-toplevel")
    except MendariError:
        return None
    return Path(out.decode().strip())


@dataclass(frozen=True)
class Base:
    """The commit a change is compared with (`ref`), in the repository at `repo`."""

    repo: Path
    ref: str

    def __post_init__(self) -> None:
        if self.ref.startswith("-"):  # never an option to git
            raise MendariError(f"--base {self.ref!r} is not a commit")
        _git(self.repo, "rev-parse", "--verify", "--quiet", f"{self.ref}^{{commit}}")

    def changed(self) -> list[str]:
        """Every file the change touches: committed since the base (`base...HEAD`), changed in
        the working tree, and new files under community-packs/ not yet added."""
        diff = ("diff", "-z", "--name-only", "--no-renames")
        found = set(_paths(_git(self.repo, *diff, f"{self.ref}...HEAD")))
        found |= set(_paths(_git(self.repo, *diff, "HEAD")))
        new = ("ls-files", "-z", "--others", "--exclude-standard", "--", COMMUNITY_DIR)
        found |= set(_paths(_git(self.repo, *new)))
        return sorted(found)

    def read(self, path: str) -> bytes | None:
        """The file at `path` in the base, or None if the base has none there."""
        listed = _paths(_git(self.repo, "ls-tree", "-z", "--name-only", self.ref, "--", path))
        if path not in listed:
            return None
        return _git(self.repo, "cat-file", "blob", f"{self.ref}:{path}")

    def is_file(self, path: str) -> bool:
        """`path` in the base is a regular file: not a symbolic link (whose target `read` would
        give as content), a folder or a submodule."""
        entry = _git(self.repo, "ls-tree", "-z", self.ref, "--", path).split(b"\0")[0]
        mode, _, rest = entry.partition(b" ")
        named = rest.partition(b"\t")[2].decode("utf-8", "surrogateescape")
        return named == path and mode in (b"100644", b"100755")

    def files(self, folder: str) -> dict[str, bytes]:
        """Every file under `folder` in the base, by path relative to it (empty: none there).
        A symbolic link is listed with its target as content, as git stores it."""
        listed = _paths(
            _git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.ref, "--", folder)
        )
        prefix = folder.rstrip("/") + "/"
        return {
            path.removeprefix(prefix): _git(self.repo, "cat-file", "blob", f"{self.ref}:{path}")
            for path in listed
            if path.startswith(prefix)
        }


GITLINK = "160000"  # the mode git gives a submodule: a commit of another repository


def gitlinks(repo: Path, path: str) -> list[str]:
    """The git submodules (gitlinks) under `path`, staged or committed: another repository's
    commit, whose files are not in this one and that no check here can read."""
    found: set[str] = set()
    for entry in _git(repo, "ls-files", "-s", "-z", "--", path).split(b"\0"):
        mode, _, rest = entry.partition(b" ")
        if mode.decode() == GITLINK:
            found.add(rest.partition(b"\t")[2].decode("utf-8", "surrogateescape"))
    tree = _git(repo, "ls-tree", "-r", "-z", "HEAD", "--", path)
    for entry in tree.split(b"\0"):
        mode, _, rest = entry.partition(b" ")
        if mode.decode() == GITLINK:
            found.add(rest.partition(b"\t")[2].decode("utf-8", "surrogateescape"))
    return sorted(found)
