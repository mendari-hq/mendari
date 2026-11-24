# SPDX-License-Identifier: Apache-2.0
"""The product names none of the five former Decision Packs (tasks/todo.md, "Plan: the
engineering to A+", wave 1(d)). They are test fixtures (`tests/fixtures/reference-agents`, with
their generators and sandbox in `tests/support`) and live on the branch `archive/decision-packs`:
nothing a release ships, builds or deploys knows one, and a script or command that needs a kind
of agent the product does not bundle is given it (`--agent`).

CI may name one in a single way, as a fixture's directory, so a job that exercises a standard
with one of them says which fixture it runs on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_fhir_structure import load_script
from tests.test_shipped import tracked

ROOT = Path(__file__).resolve().parents[1]
# The five, by the names they had: their ids' second halves, and the two names that were
# written in words. In any letter case.
FORMER = re.compile(
    r"inbox-router|ai-guard|trial-prescreener|prior-auth-preflight|referral-router"
    r"|patient inbox router|ai guard",
    re.IGNORECASE,
)
# What a release ships, builds or deploys, and the scripts that maintain it.
PRODUCT = ("src", "scripts", "deploy", "Dockerfile", "docker-compose.yml", "pyproject.toml")
FIXTURES = ROOT / "tests" / "fixtures" / "reference-agents"
# A fixture named as CI may name one: by its directory, or a file under it.
FIXTURE_PATH = re.compile(r"tests/fixtures/reference-agents/[a-z0-9-]+")


def named(paths: tuple[str, ...], *, fixture_paths: bool) -> list[str]:
    """`path:line: the name` for every former name in the tracked text files under `paths`;
    with `fixture_paths`, a fixture's directory is not one."""
    found = []
    for path in tracked(*paths):
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:  # an image, a font
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            read = FIXTURE_PATH.sub("", line) if fixture_paths else line
            found += [f"{path}:{number}: {name.group(0)}" for name in FORMER.finditer(read)]
    return found


def test_the_rule_finds_each_former_name_and_only_those() -> None:
    fixtures = sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
    assert len(fixtures) == 5 and all(FORMER.search(name) for name in fixtures)
    for said in (*(f"mendari/{name}" for name in fixtures), "the Patient Inbox Router", "AI Guard"):
        assert FORMER.search(said), said
    kept = ("mendari/portal-message-agent", "a referral", "the inbox", "a guard", "prior-auth")
    for said in kept:
        assert not FORMER.search(said), said
    # A fixture's directory is what CI may write; the same name anywhere else is found.
    path = f"--agent tests/fixtures/reference-agents/{fixtures[0]}"
    assert not FORMER.search(FIXTURE_PATH.sub("", path))
    assert FORMER.search(FIXTURE_PATH.sub("", f"{path} --approve mendari/{fixtures[0]}"))


def test_the_product_names_no_former_pack() -> None:
    assert len(tracked(*PRODUCT)) > 300  # the listing is the product, not an empty one
    assert named(PRODUCT, fixture_paths=False) == []


def test_ci_names_a_former_pack_only_as_a_fixtures_directory() -> None:
    assert named((".github",), fixture_paths=False)  # it does name fixtures, by their directory
    assert named((".github",), fixture_paths=True) == []
    # Every directory it names is a fixture that exists.
    for path in tracked(".github"):
        for found in FIXTURE_PATH.findall((ROOT / path).read_text(encoding="utf-8")):
            assert (ROOT / found).is_dir(), f"{path}: {found}"


def test_a_script_that_needs_another_kind_of_agent_asks_for_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The product bundles one agent, which reads a portal message: the Da Vinci samples need a
    prior-authorization agent, have no default for it, and say what to give
    (`tests/test_inferno_inputs.py` holds the DTR inputs to the same)."""
    script = load_script("davinci_sample")
    with pytest.raises(SystemExit):
        script.cli([str(tmp_path / "out")])
    said = capsys.readouterr().err
    assert "--agent is needed: a prior-authorization agent" in said
    assert "by id or directory" in said and "demo agent reads a portal message" in said
    assert not (tmp_path / "out").exists()
