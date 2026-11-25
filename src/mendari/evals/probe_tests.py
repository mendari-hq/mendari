# SPDX-License-Identifier: Apache-2.0
"""The regression tests `mendari probe` keeps in an agent's own folder.

A failure the probe found is written as a behaviour test, so `mendari test` fails on it until the
agent is fixed and guards it after. The tests live where the agent's own do, in two files
(`evals/pack_tests.py` names them and reads the first):

- `tests/probe.yaml`: the tests that run. Each is a behaviour test (`name`, `input`, `expect`)
  with a `probe` mapping saying where it came from: its `id`, the transformation, the seed, the
  original case, how bad the failure was, and where the expected answers come from.
- `tests/probe-unconfirmed.yaml`: tests whose expected answer a model changed (a denial of the
  critical finding). None runs until a person has read its text and its `expect` and confirmed it
  (`confirm`, `mendari probe --confirm ID`), which moves it to the first file.

Nothing is written twice: a test is not added when its id is already in either file, or when its
input is one the agent already tests (in its own tests, or under another id). Both files are
written whole, in the order the tests were found, so comments in them are not kept; a person may
edit or delete any case, and what they left is what stays.

The text of a variant is in these files and nowhere else: never in a report. Only an agent of
the author's own is written into (`not_writable`): never one this release bundles, a
community agent of the repository, or one pulled from a registry.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from mendari.core.pack import Pack, parse_yaml
from mendari.core.registry import origin_of
from mendari.errors import MendariError, PackValidationError
from mendari.evals.canonical import canonical_input
from mendari.evals.ledger import canonical
from mendari.evals.pack_tests import (
    PROBE_KEY,
    PROBE_TESTS,
    PROBE_UNCONFIRMED,
    load_cases,
    probe_case_problem,
    probe_cases,
)

FOUND_BY = "mendari probe"
ID_PREFIX = "probe-"
MAX_IDS_SHOWN = 10
_KEPT = (
    "# Regression tests that `mendari probe` found (docs/guides/finding-failures.md).\n"
    "# `mendari test` runs them after the agent's own. Each failed when it was written, and\n"
    "# passes once the agent is fixed. Edit or delete a case as you would one of your own;\n"
    "# comments are not kept, as the probe writes this file whole.\n"
)
_WAITING = (
    "# Tests that `mendari probe` found whose expected answer a model changed\n"
    "# (docs/guides/finding-failures.md). None of them runs. Read each one's text and its\n"
    "# `expect`, correct what is wrong, then keep it with\n"
    "#   mendari probe <agent> --confirm <id>\n"
    "# or delete it. Comments are not kept, as the probe writes this file whole.\n"
)


def variant_id(case_key: str, transformation: str, which: str) -> str:
    """The id of a variant, and of the test written from it: `probe-` and twelve hex digits,
    from the original case (`case_key`: a hash of what the agent reads of it), the
    transformation, and which variant of the two it is (the seed and the variant's number; for
    a variant a file gave without a seed, a hash of its input). The same probe gives the same
    ids, so a failure found again is the test already written."""
    named = canonical([case_key, transformation, which])
    return ID_PREFIX + hashlib.sha256(named.encode("ascii")).hexdigest()[:12]


@dataclass(frozen=True)
class Written:
    """What a write kept: the ids of the tests added to those that run, of those added to the
    ones waiting for a person, and how many were in the agent already."""

    added: tuple[str, ...]
    waiting: tuple[str, ...]
    already: int


def not_writable(pack: Pack) -> str | None:
    """Why the probe keeps no test in this agent, or None: it writes only into an agent of its
    author's own. One this release bundles (its data comes from its generator), one of the
    repository's community agents (what is written into it would be published with it), one
    in the registry's cache (a verified copy) or a version in an agent store is not changed in
    place, by where it is on disk (`core.registry.origin_of`), whatever it was loaded as."""
    where = {pack.origin, origin_of(pack.root)} - {"local"}
    if not where:
        return None
    what = {
        "maintained": "an agent this release bundles",
        "community": "a community agent of this repository",
        "hub": "an agent pulled from a registry",
        "store": "a version in an agent store",
    }[sorted(where)[0]]
    return f"{what} is not changed in place: copy it, and probe the copy"


def waiting(pack: Pack) -> list[dict[str, Any]]:
    """The tests that wait for a person to confirm what they expect, as their file holds them
    (a person may have corrected one): none of them is run, and each is held to its shape only
    when it is confirmed."""
    if not pack.path(PROBE_UNCONFIRMED).is_file():
        return []
    cases = pack.read_yaml(PROBE_UNCONFIRMED) or []
    shaped = isinstance(cases, list) and all(
        isinstance(case, dict)
        and isinstance(case.get("input"), dict)
        and isinstance(case.get(PROBE_KEY), dict)
        and isinstance(case[PROBE_KEY].get("id"), str)
        for case in cases
    )
    if not shaped:
        problem = (
            f"{PROBE_UNCONFIRMED}: expected the list of tests `mendari probe` wrote, each with "
            f"its `input` and its `{PROBE_KEY}` mapping's `id`"
        )
        raise PackValidationError(pack.id, [problem])
    found: list[dict[str, Any]] = cases
    return found


def write(
    pack: Pack,
    found: Sequence[Mapping[str, Any]],
    unconfirmed: Sequence[Mapping[str, Any]] = (),
) -> Written:
    """Add `found` to the tests that run and `unconfirmed` to those that wait, each after what
    its file holds, leaving out every test the agent has already (the module's docstring). A
    file with nothing new is not touched. The agent is read as it was loaded: one whose test
    files changed since is refused, and loaded again by the caller."""
    problem = not_writable(pack)
    if problem is not None:
        raise MendariError(problem)
    kept, held = probe_cases(pack), waiting(pack)
    ids = {case[PROBE_KEY]["id"] for case in (*kept, *held)}
    inputs = {canonical_input(case["input"]) for case in (*load_cases(pack), *kept, *held)}
    added: list[str] = []
    waits: list[str] = []
    already = 0
    for cases, into, names in ((found, kept, added), (unconfirmed, held, waits)):
        for given in cases:
            case = json.loads(json.dumps(given))  # as a file holds it: lists, not tuples
            said = canonical_input(case["input"])
            if case[PROBE_KEY]["id"] in ids or said in inputs:
                already += 1
                continue
            ids.add(case[PROBE_KEY]["id"])
            inputs.add(said)
            into.append(case)
            names.append(case[PROBE_KEY]["id"])
    if added:
        _write(pack, PROBE_TESTS, _KEPT, kept)
    if waits:
        _write(pack, PROBE_UNCONFIRMED, _WAITING, held)
    return Written(tuple(added), tuple(waits), already)


def confirm(pack: Pack, ids: Sequence[str]) -> tuple[str, ...]:
    """Move the waiting tests with these ids among those that run, as a person left them
    (`confirmed: true` is added to where each came from). Each must be a test `mendari test`
    can read, expecting what the agent can decide: one that is not is named, and nothing is
    moved. Returns the ids moved."""
    problem = not_writable(pack)
    if problem is not None:
        raise MendariError(problem)
    held = waiting(pack)
    if not held:
        raise MendariError(f"no test waits in {PROBE_UNCONFIRMED}: there is none to confirm")
    by_id = {case[PROBE_KEY]["id"]: case for case in held}
    wanted = tuple(dict.fromkeys(ids))
    for name in wanted:
        if name not in by_id:
            shown = list(by_id)[:MAX_IDS_SHOWN]
            more = f" and {len(by_id) - len(shown)} more" if len(by_id) > len(shown) else ""
            raise MendariError(
                f"{name} is not a test waiting in {PROBE_UNCONFIRMED} (it holds: "
                f"{', '.join(shown)}{more})"
            )
        refused = probe_case_problem(pack.manifest, by_id[name])
        if refused is not None:
            raise MendariError(
                f"{name} cannot be confirmed as it is in {PROBE_UNCONFIRMED}: {refused}"
            )
    kept = probe_cases(pack)
    running = {case[PROBE_KEY]["id"] for case in kept}
    kept += [
        {**by_id[name], PROBE_KEY: {**by_id[name][PROBE_KEY], "confirmed": True}}
        for name in wanted
        if name not in running
    ]
    rest = [case for case in held if case[PROBE_KEY]["id"] not in wanted]
    _write(pack, PROBE_TESTS, _KEPT, kept)
    if rest:
        _write(pack, PROBE_UNCONFIRMED, _WAITING, rest)
    else:
        pack.path(PROBE_UNCONFIRMED).unlink()
    return wanted


class _Dumper(yaml.SafeDumper):
    """YAML as the probe writes it: a text whose white space a reader would not see otherwise
    (a line break, a tab, two spaces in a row, a space at either end) is written in double
    quotes, each of those an escape or a space between the quotes. Any other text is written as
    YAML writes it."""


def _visible(dumper: yaml.SafeDumper, text: str) -> yaml.ScalarNode:
    hidden = text != " ".join(text.split())
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style='"' if hidden else None)


_Dumper.add_representer(str, _visible)
# Wide enough that a text of a few thousand characters stays on one line: a quoted text broken
# across lines is exact, and unreadable.
_WIDTH = 100_000


def _write(pack: Pack, relative: str, heading: str, cases: list[dict[str, Any]]) -> None:
    """One of the probe's files, whole: checked first to read back as the very cases it was
    given (a variant's text is white space, typography and line breaks: what is run must be
    what was found), then put in place in one step."""
    dumped = yaml.dump(cases, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=_WIDTH)
    text = heading + dumped
    if parse_yaml(text.encode("utf-8"), relative, pack.id) != cases:
        raise MendariError(
            f"{relative}: a test's text does not read back from YAML as it was found; nothing "
            "was written"
        )
    target = pack.path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.writing")  # hidden: never part of the agent
    staged.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staged, target)
