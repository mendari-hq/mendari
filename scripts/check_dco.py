# SPDX-License-Identifier: Apache-2.0
"""Every commit a pull request brings is signed off under the Developer Certificate of Origin 1.1
(CONTRIBUTING.md): its message has a `Signed-off-by:` line naming the commit's author, by e-mail
(`git commit -s` writes one).

Checked: the non-merge commits in `--base..--head` (the pull request's own range), except those
already in history when the rule began (`EFFECTIVE_AFTER`: every commit it can reach is exempt,
so earlier history needs no rewriting) and those of named bots, whose commits are signed by the
platform. A merge commit adds no work of its own and is not checked.

Usage (CI passes the pull request's base and head):

    python3 scripts/check_dco.py --base origin/main --head HEAD
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass

# main when the rule began (LP-13): commits it can reach are exempt.
EFFECTIVE_AFTER = "d1b352c3697d10ed793da0900f1696b80776a726"
# Bots whose commits carry the platform's own sign-off; matched on the author's e-mail.
BOTS = re.compile(r"^\d+\+(dependabot|github-actions)\[bot\]@users\.noreply\.github\.com$")
SIGNED = re.compile(r"^Signed-off-by: (?P<name>.+) <(?P<email>[^<>]+)>\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    email: str
    subject: str
    message: str


def commits(
    base: str, head: str, *, effective: str = EFFECTIVE_AFTER, cwd: str | None = None
) -> list[Commit]:
    """The non-merge commits `head` adds to `base` that `effective` cannot reach, oldest first."""
    excluded = [f"^{base}"] + ([f"^{effective}"] if _exists(effective, cwd) else [])
    out = subprocess.run(
        [
            "git",
            "log",
            "--no-merges",
            "--reverse",
            "--format=%H%x00%an%x00%ae%x00%s%x00%B%x01",
            head,
            *excluded,
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    ).stdout
    found = []
    for entry in filter(None, (chunk.strip("\n") for chunk in out.split("\x01"))):
        sha, author, email, subject, message = entry.split("\x00", 4)
        found.append(Commit(sha, author, email, subject, message))
    return found


def problems(found: Iterable[Commit]) -> list[str]:
    """One line per commit not signed off by its author (bots aside)."""
    missing = []
    for commit in found:
        if BOTS.match(commit.email):
            continue
        signers = {m["email"].strip().lower() for m in SIGNED.finditer(commit.message)}
        if commit.email.strip().lower() not in signers:
            missing.append(
                f"{commit.sha[:8]} {commit.subject!r} ({commit.author}): no "
                f"'Signed-off-by: {commit.author} <{commit.email}>'"
            )
    return missing


def _exists(ref: str, cwd: str | None) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, cwd=cwd
        ).returncode
        == 0
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    missing = problems(commits(args.base, args.head))
    for line in missing:
        print(line, file=sys.stderr)
    if missing:
        print(
            "Sign off each commit (git commit -s); for commits already made: "
            "git rebase --signoff <base>, then push with --force-with-lease (CONTRIBUTING.md).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
