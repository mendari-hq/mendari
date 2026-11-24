# SPDX-License-Identifier: Apache-2.0
"""One vocabulary in what a reader reads (tasks/todo.md, "Plan: the engineering to A+"): the
README, every page and file of the docs site, its navigation and theme, the community files, the
changelog and the READMEs beside the code say agent, never pack.

**The rule** is `tests/test_vocabulary.py`'s `offences` (no whole word "pack" or "packs", no
"Decision Pack" however it is joined; a wire name written as code, and a canonical URL that
stored FHIR resources carry, are let through there, each with its reason), held on every line of
every file in `files()` and on each file's own path. Beside it, `FORMER_NAMES`: the names the
plan's table renamed that the rule cannot see (a setting's name is one word to it) or lets
through (a file's name written as code reads like a chain event's). A folder's former name
(`community-packs/`) needs no entry there: the rule sees the word in it.

**What is left alone** is said here and nowhere else, each entry with its reason: `NOT_READ` (a
file, whole), `PARTS` (a file from one line to another) and `ALLOWED` (a text, in the files a
pattern names). An entry that nothing needs any more fails the test."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

import pytest

from tests.conftest import ROOT
from tests.test_vocabulary import offences

# -- what is read ---------------------------------------------------------------------------------

_FILES = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "mkdocs.yml",
    "hub/README.md",
)
_TEXT = {".md", ".yml", ".yaml", ".json", ".html", ".css", ".js", ".txt"}


def files() -> list[str]:
    """Every file a reader reads, as paths from the repository's root: the files above, all of
    `docs/` and `docs_theme/`, and each README of `deploy/`, `examples/` and `agents/`."""
    found = {ROOT / name for name in _FILES}
    for folder in ("docs", "docs_theme"):
        found |= {path for path in (ROOT / folder).rglob("*") if path.is_file()}
    found |= set((ROOT / "deploy").rglob("README.md"))
    found |= set((ROOT / "examples").glob("*/README.md"))
    found |= {path for path in (ROOT / "agents").rglob("README*") if path.is_file()}
    names = sorted(path.relative_to(ROOT).as_posix() for path in found)
    names = [name for name in names if not name.rsplit("/", 1)[-1].startswith(".")]
    for name in names:
        assert (ROOT / name).suffix in _TEXT, f"{name}: not a text file this test reads"
    return names


# -- the rule, and the names it cannot see --------------------------------------------------------

# Renamed by the plan's table, and invisible to the rule: each is written nowhere in these files
# but where `PARTS` and `ALLOWED` say (the name a thing has now is in the table).
FORMER_NAMES = (
    "pack.yaml",
    "MENDARI_PACKS_PATH",
    "MENDARI_PACKS",
    "MENDARI_PACK_STORE",
    "MENDARI_COMMUNITY_PACKS",
)
_FORMER = re.compile("|".join(rf"(?<![\w.-]){re.escape(name)}(?![\w-])" for name in FORMER_NAMES))


def problems(text: str) -> list[str]:
    """What in `text` is not the one vocabulary: the rule's offences, then the former names."""
    return [*offences(text), *(found.group(0) for found in _FORMER.finditer(text))]


# -- what is left alone ---------------------------------------------------------------------------

# Files not read at all: file -> why.
NOT_READ = {
    "docs/grading/method-1.0.md": "the grading method 1.0 is pinned by the SHA-256 of its bytes "
    "(`mendari.evals.method`, which signed results name): one word changed is another method",
}

# A file from the line that starts with `first` up to the one that starts with `before` (to the
# end of the file where `before` is None): (file, first, before) -> why.
_THIS_CHANGE = "- **One vocabulary: what Mendari governs is an agent**"
_HISTORY = "- **Decision Packs:** the specification"
PARTS: dict[tuple[str, str, str | None], str] = {
    ("CHANGELOG.md", _THIS_CHANGE, _HISTORY): "the entry for this change says what was renamed "
    "and what still works under the name it had",
    ("CHANGELOG.md", _HISTORY, None): "the changelog's history is kept as it was written",
}

_PYTHON = (
    "a Python name, written as code where a page says which code enforces a rule: the plan "
    "renames no identifier or module before its wave 6"
)
_ARCHIVE = (
    "a signed archive holds an agent's files under `pack/` (`hub/archive.py`): the format's "
    "own name, which signatures cover, renamed with the stored formats (wave 6)"
)
# A text left alone in the files a pattern names (`*`: any of them): (files, text) -> why.
ALLOWED: dict[tuple[str, str], str] = {
    ("docs/spec/agent-manifest-0.2.md", "A manifest named `pack.yaml`"): "the spec's one "
    "sentence that a manifest under the name it had before still loads",
    (
        "docs/decisions/ADR-*.md",
        "Terminology updated 2026-11-23: what this record called a pack is an agent.",
    ): "the line under a decision record's status that says which word its text replaced",
    **{
        ("*", f"`{name}`"): _PYTHON
        for name in (
            "Pack.attestation",
            "Pack.bounded",
            "Pack.community",
            "Pack.path",
            "Pack.read_bytes",
            "Pack.read_text",
            "Pack.warnings",
            "core/pack.py",
        )
    },
    ("*", "`pack/`"): _ARCHIVE,
    ("docs/security/threat-model.md", "`pack//<absolute path>`"): _ARCHIVE,
    ("*", "`packs/<namespace>/<name>/<version>`"): "the folder the registry cache had until "
    "2026-11, where a version pulled then is still read (`hub/client.py` `former_cache`): an "
    "operator removes one by that path",
    ("*", "--build-arg PACK="): "the Dockerfile's build argument is still named `PACK`: the "
    "command needs its real name, and packaging renames it",
    (
        "docs/guides/connecting-an-ehr.md",
        "/smart/launch?pack=<agent id>",
    ): "the launch route's query parameter is `pack` (`server/smart_routes.py`): a name on the "
    "wire, which an EHR's registered launch address may hold, renamed with the wire names",
}


def _part(lines: list[str], first: str, before: str | None) -> range:
    """The lines of a part, as indexes: from the one that starts with `first`."""
    starts = [index for index, line in enumerate(lines) if line.startswith(first)]
    assert len(starts) == 1, f"one line starts with {first!r}: found {len(starts)}"
    if before is None:
        return range(starts[0], len(lines))
    ends = [index for index, line in enumerate(lines) if line.startswith(before)]
    assert len(ends) == 1 and ends[0] > starts[0], f"no line after it starts with {before!r}"
    return range(starts[0], ends[0])


def _allowed(name: str) -> list[str]:
    return [text for (pattern, text) in ALLOWED if fnmatchcase(name, pattern)]


def found_in(name: str, text: str) -> list[str]:
    """`file:line: what, in which line` for each line of `text`, the file called `name`, that
    is not the one vocabulary once what is left alone is taken out; and the same for `name`."""
    lines = text.split("\n")
    skipped: set[int] = set()
    for file, first, before in PARTS:
        if file == name:
            skipped.update(_part(lines, first, before))
    found = [f"{name}: {', '.join(problems(name))!r} in the file's name"] if problems(name) else []
    for index, line in enumerate(lines):
        if index in skipped:
            continue
        kept = line
        for allowed in _allowed(name):
            kept = kept.replace(allowed, " ")
        said = problems(kept)
        if said:
            found.append(f"{name}:{index + 1}: {', '.join(sorted(set(said)))!r} in {line[:160]!r}")
    return found


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# -- the tests ------------------------------------------------------------------------------------


def test_the_files_are_found() -> None:
    names = files()
    assert len(names) > 50
    for expected in (
        "README.md",
        "mkdocs.yml",
        "docs/index.md",
        "docs/spec/agent-manifest-0.2.md",
        "docs/decisions/ADR-0001-questions-as-the-core-abstraction.md",
        "docs_theme/main.html",
        "deploy/vercel/README.md",
        "examples/http-agent/README.md",
    ):
        assert expected in names, expected
    assert set(NOT_READ) <= set(names)


@pytest.mark.parametrize("name", [name for name in files() if name not in NOT_READ])
def test_a_file_says_agent(name: str) -> None:
    found = found_in(name, _read(name))
    assert not found, f"{len(found)} in {name}:\n" + "\n".join(found)


def test_what_is_held_and_what_is_left_alone() -> None:
    """The rule and the former names are both held, on a file's lines and on its name; an
    allowed text is left alone in its own files only, and a part only between its two lines."""
    assert problems("a pack and `pack_id`") == ["pack"]
    assert problems("`MENDARI_PACKS_PATH`, then `MENDARI_AGENTS_PATH`") == ["MENDARI_PACKS_PATH"]
    assert problems("MENDARI_PACKS=a/b") == ["MENDARI_PACKS"]
    assert problems("in `community-packs/<name>/`") == ["packs"]  # a folder's name: the rule's
    assert problems("`tests/fixtures/reference-packs`") == ["packs"]
    assert problems("the manifest, `pack.yaml`") == ["pack.yaml"]
    assert not problems("`agent.yaml`, `community-agents/`, `/api/agents`, unpack a package")
    assert len(found_in("docs/x.md", "An agent.\nA pack.\nSee `--pack`.")) == 2
    assert found_in("docs/a-pack-store.md", "An agent.") == [
        "docs/a-pack-store.md: 'pack' in the file's name"
    ]
    spec, sentence = "docs/spec/agent-manifest-0.2.md", "A manifest named `pack.yaml`, the old name"
    assert not found_in(spec, sentence)
    assert len(found_in("docs/index.md", sentence)) == 1  # the same sentence, on another page
    assert len(found_in(spec, sentence + ", and a second `pack.yaml`.")) == 1
    log = f"Intro: a pack.\n{_THIS_CHANGE} here.\n  `--pack` is read.\n{_HISTORY}\n- Packs.\n"
    assert found_in("CHANGELOG.md", log) == ["CHANGELOG.md:1: 'pack' in 'Intro: a pack.'"]


def test_every_exception_is_still_needed() -> None:
    """No list outlives its reasons: each file not read, each part and each allowed text is
    still there, and without it the rule (or a former name) would be broken."""
    names = files()
    for name, reason in NOT_READ.items():
        assert reason.strip() and any(problems(line) for line in _read(name).split("\n")), name
    for (name, first, before), reason in PARTS.items():
        lines = _read(name).split("\n")
        part = [lines[index] for index in _part(lines, first, before)]
        assert reason.strip() and any(problems(line) for line in part), (name, first)
    for (pattern, text), reason in ALLOWED.items():
        assert reason.strip() and problems(text), f"{text!r} breaks nothing: take it off the list"
        holding = [name for name in names if fnmatchcase(name, pattern) and text in _read(name)]
        assert holding, f"{pattern}: no file says {text!r} any more: take it off the list"
