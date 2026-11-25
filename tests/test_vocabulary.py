# SPDX-License-Identifier: Apache-2.0
"""One vocabulary (ADR-0026): what Mendari governs is an agent, in every file of the repository.

**The rule** (`offences`). The old name of what Mendari governs is found in any letter case and
wherever its letters stand: a word of its own, or a part of an identifier, a key, a path, a
setting or a flag. This file writes that name only in two parts (`OLD`), so that it passes its
own rule. Its letters are let through only inside the English words that hold them (`ALLOWED`:
the words of the wave 6 inventory, section 4.13). And none of the five reference agents of the
pre-release is named by its id, however the id is written (`FORMER`, decision O7).

**Where** (`files`). Every file the repository tracks (`git ls-files`): its path, and the text of
each text file. Left out, each with its reason: whole folders (`NOT_READ`), the text of lockfiles
(`LOCKFILE`) and of binary files (`BINARY`), whose names are still read. Nothing else is left out:
the rule has no list of exceptions, and this file is read too.

A test that holds a text it makes to the same rule calls `offences` (`tests/test_ci_action.py`,
`tests/test_ci_scaffold.py`). Beside the rule, ADR-0026's other check the repository can make by
itself: a release bundles one agent, the demo (`test_a_release_bundles_one_agent_the_demo`)."""

from __future__ import annotations

import re
import subprocess
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.conftest import DEMO_AGENT_DIR, ROOT
from tests.test_spdx import NOT_OURS

# -- the rule -------------------------------------------------------------------------------------

# The old name, in two parts: written whole, this file would break its own rule.
OLD = "pa" + "ck"
# What is let through, and nothing else: the English words that hold the old name's letters
# (wave 6 inventory, section 4.13), each a pattern with why.
ALLOWED: dict[str, str] = {
    rf"{OLD}(?=ag|et)": "`package` and `packet`, and every word made from them (`packages`, "
    "`PackageBundle`, `questionnaire-package`, `packagedMedicinalProduct`, `packets`)",
    rf"(?<![a-z0-9])un{OLD}": "`unpack`, `unpacked`, `unpacks`, with `un` starting the word or "
    "after `_`: inside a longer word (a name ending in `run`, then the old name) it is not it",
    rf"(?<![a-z0-9])back{OLD}": "`backpack`",
    rf"\bstruct\.{OLD}\b": "Python's `struct.pack`, a function of the standard library",
}
_ALLOWED = re.compile("|".join(f"(?:{pattern})" for pattern in ALLOWED), re.IGNORECASE)
_OLD = re.compile(OLD, re.IGNORECASE)
# The five reference agents of the pre-release, each by the words of its id (ADR-0026, decision
# O7). Named nowhere, however the words are joined (a hyphen, an underscore, white space, or
# nothing) and in any letter case, as a name of its own and not inside a longer word.
FORMER: tuple[tuple[str, ...], ...] = (
    ("patient", "inbox", "router"),
    ("referral", "router"),
    ("prior", "auth", "preflight"),
    ("prior", "authorization", "preflight"),  # the same agent, by its name in words
    ("clinical", "trial", "prescreener"),
    ("ai", "guard"),
)
_JOINED = r"[-_\s]*"  # how an id's words may be joined
_FORMER = re.compile(
    "|".join(rf"(?<![a-z0-9]){_JOINED.join(words)}(?![a-z0-9])" for words in FORMER),
    re.IGNORECASE,
)
_IN_A_WORD = re.compile(r"[\w-]")


def located(text: str) -> list[tuple[int, str]]:
    """`(offset, what)` of each offence in `text`, in its order: each word (letters, digits, `_`
    and `-`) that holds the old name's letters outside `ALLOWED`, once, and each former agent's
    id as it is written."""
    kept = _ALLOWED.sub(lambda allowed: " " * len(allowed.group()), text)
    spans = set()
    for found in _OLD.finditer(kept):
        start, end = found.span()
        while start and _IN_A_WORD.match(text[start - 1]):
            start -= 1
        while end < len(text) and _IN_A_WORD.match(text[end]):
            end += 1
        spans.add((start, end))
    spans |= {found.span() for found in _FORMER.finditer(text)}
    return [(start, text[start:end]) for start, end in sorted(spans)]


def offences(text: str) -> list[str]:
    """What in `text` breaks the rule, as it is written there."""
    return [what for _, what in located(text)]


def test_the_old_name_is_found_however_it_is_written() -> None:
    title, upper = OLD.title(), OLD.upper()
    for said in (
        f"a {OLD}",
        f"{title}s served here",
        f"the Decision {title}",
        f"Decision{title}s",
        f"decision_{OLD}",
        f"({OLD})",
        f"`{OLD}`",
        f"{OLD}.yaml",
        f"/api/{OLD}s/x",
        f"mendari-{OLD}.json",
        f"--{OLD}-dir",
        f"MENDARI_{upper}S_PATH",
        f"{OLD}_id",
        f"{title}Id",
        f"run{title}Id",
        f"re{OLD}",
        f"re-{OLD}ed",
        f"x.oh{OLD}",
        f"struct.{OLD}_into",
        f"doUn{OLD}",
    ):
        assert offences(said), said
    # Each word once, as it is written: the whole word, not its letters alone.
    assert offences(f"the `{OLD}_id` of a decision") == [f"{OLD}_id"]
    assert offences(f"see run{title}Id, then a package") == [f"run{title}Id"]
    assert offences(f"{OLD}age_{OLD}") == [f"{OLD}age_{OLD}"]


def test_only_the_english_words_that_hold_its_letters_are_let_through() -> None:
    for said in (
        "a package",
        "packages",
        "PackageBundle",
        "questionnaire-package",
        "packagedMedicinalProduct",
        "a packet",
        "packets",
        "unpack",
        "Unpacking",
        "unpacked",
        "unpacks",
        "_unpack",
        "struct.unpack(data)",
        "a backpack",
        "backpacks",
        "struct.pack('>I', 1)",
        "an agent",
        "agents/<name>/",
        "tests/fixtures/agents/",
    ):
        assert offences(said) == [], said
    assert all(why.strip() for why in ALLOWED.values())


def test_a_former_agent_is_found_however_its_id_is_written() -> None:
    for words in FORMER:
        for said in (
            "-".join(words),
            f"mendari/{'-'.join(words)}",
            "_".join(words),
            "_".join(words).upper(),
            " ".join(word.title() for word in words),
            "".join(word.title() for word in words),
            "\n  ".join(words),
            f"test_{'_'.join(words)}_decides",
        ):
            assert offences(said), said
    for said in (
        "mendari/portal-message-agent",
        "a referral, the inbox, a guard",
        "an agent that routes referrals",
        "prior-auth",
        "AI guardrails",
        "openai_guard",
        "clinic-a/inbox-router",  # a name that shares words with one: the ids are named, not words
    ):
        assert offences(said) == [], said


# -- where ----------------------------------------------------------------------------------------

# Folders the rule does not read, each with why: not the project's words of today.
NOT_READ: dict[str, str] = {
    "notes/": "plans, reviews and the build log: the history of the work, in the words of its day",
    "tasks/": "the plan and its lessons, which say what was done, and when",
    "licenses/": "other projects' licences and notices, word for word",
    **{folder: "vendored: another project's code, as it is published" for folder in NOT_OURS},
}
# A lockfile, whose text a package manager writes: other projects' names and versions.
LOCKFILE = re.compile(r"(?:^|/)(?:package-lock\.json|[^/]+\.lock)$")
# A binary file, by its kind: one that is not UTF-8 must be one of these, so a text file in
# another encoding fails here instead of going unread.
BINARY = (".png", ".ico", ".woff2")


@dataclass(frozen=True)
class File:
    """A tracked file the rule reads: its path, and its text (None for a lockfile or a binary
    file, whose name alone is read)."""

    path: str
    text: str | None


@cache
def tracked() -> tuple[str, ...]:
    """Every file the repository tracks, by its path from the root."""
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return tuple(path for path in listed.stdout.decode().split("\0") if path)


@cache
def files() -> tuple[File, ...]:
    """The tracked files the rule reads: all but `NOT_READ`; a lockfile's or a binary file's
    name only."""
    read = []
    for path in tracked():
        if path.startswith(tuple(NOT_READ)) or not (ROOT / path).is_file():
            continue  # a file deleted and not yet staged says nothing
        if LOCKFILE.search(path):
            read.append(File(path, None))
            continue
        data = (ROOT / path).read_bytes()
        try:
            read.append(File(path, data.decode("utf-8")))
        except UnicodeDecodeError:
            assert path.endswith(BINARY), f"{path}: not UTF-8, and not of a kind in BINARY"
            read.append(File(path, None))
    return tuple(read)


def test_every_tracked_file_is_read_but_those_left_out() -> None:
    read = {file.path: file for file in files()}
    assert len(read) > 800  # the whole repository, not a part of it
    for path in (
        "CLAUDE.md",
        "README.md",
        "CHANGELOG.md",
        "pyproject.toml",
        "Dockerfile",
        ".github/workflows/ci.yml",
        "docs/decisions/ADR-0026-one-release-baseline-agents-only.md",
        "src/mendari/cli.py",
        "src/mendari/dashboard/main.js",
        "hub/app/page.tsx",
        "web/index.html",
        "agents/portal-message-agent/agent.yaml",
        "tests/test_vocabulary.py",  # the rule's own file: it has no exception
    ):
        assert read[path].text, path
    for folder, why in NOT_READ.items():
        assert why.strip() and any(path.startswith(folder) for path in tracked()), folder
        assert not any(path.startswith(folder) for path in read), folder
    named_only = {path for path, file in read.items() if file.text is None}
    assert {"uv.lock", "hub/package-lock.json"} <= named_only
    assert any(path.endswith(".woff2") for path in named_only)
    assert all(LOCKFILE.search(path) or path.endswith(BINARY) for path in named_only)


def _lines(text: str) -> tuple[list[int], list[str]]:
    """Where each line of `text` starts, and the lines."""
    return [0, *(found.end() for found in re.finditer("\n", text))], text.split("\n")


def test_no_file_says_the_old_name_or_names_a_former_agent() -> None:
    """The rule, on every file the repository tracks: each offence by its file and line."""
    found = []
    for file in files():
        found += [f"{file.path}: {what!r} in the file's path" for _, what in located(file.path)]
        if file.text is None:
            continue
        starts, lines = _lines(file.text)
        for at, what in located(file.text):
            line = bisect_right(starts, at) - 1
            found.append(f"{file.path}:{line + 1}: {what!r} in {lines[line].strip()[:120]!r}")
    assert not found, f"{len(found)} offences:\n" + "\n".join(found)


# -- the agents a release holds -------------------------------------------------------------------


def test_a_release_bundles_one_agent_the_demo() -> None:
    """The product bundles one agent, the demo; every other agent the tests use is a fixture of
    theirs (ADR-0026, point 1)."""
    bundled = {Path(path).parts[1] for path in tracked() if path.startswith("agents/")}
    assert bundled == {DEMO_AGENT_DIR.name}
