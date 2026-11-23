# SPDX-License-Identifier: Apache-2.0
"""The command line speaks one vocabulary: what Mendari governs is an agent, and "pack" is never
read or typed. No usage, help, message or error says it. The names people typed before
(`--pack`, `--pack-dir`, `mendari pack`, `mendari new --agent`) are still read, and shown
nowhere."""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from mendari import cli, load_pack
from mendari.cli import build_parser, main

# The old names rest on how argparse reads a command line, which changes between Python
# versions; CI runs only the core tests on every version it supports.
pytestmark = pytest.mark.core

# "pack" and "packs" (so "Decision Pack" too), in any case, as a word of its own: also where a
# dash, a dot or an underscore joins it to another (`--pack-dir`, `pack.yaml`, `PACK_DIR`), which
# a plain word boundary would let through. `.ohpack`, the archive's extension, is not that word.
OLD_WORD = re.compile(r"(?<![a-z])packs?(?![a-z])", re.IGNORECASE)
AGENT = "mendari/referral-router"  # a choice wherever an option takes only some agents
# Every command with an option that names an agent, and the least it takes beside it.
AGENT_OPTIONS = {
    "audit export": [],
    "automation hold": ["--all-orgs", "--reason", "a drill"],
    "sandbox seed": ["--fhir", "http://fhir.test"],
    "sandbox worker": ["--fhir", "http://fhir.test", "--engine", "http://engine.test"],
    "sandbox smart-launch": ["--engine", "http://engine.test"],
    "sandbox omop-check": ["--dataset", "cases.jsonl"],
    "sandbox bulk-check": ["--fhir", "http://fhir.test"],
    "omop dataset": ["--db", "cdm.db", "--out", "cases.jsonl"],
    "omop run": ["--db", "cdm.db"],
    "bulk dataset": ["--ndjson", "export", "--labels", "labels.jsonl", "--out", "cases.jsonl"],
    "monitor": [],
    "store versions": ["--org", "clinic-a"],
    "evalset agreement": ["a-set"],
    "evalset coverage": ["a-set"],
    "evalset freeze": ["a-set", "--max-runs", "1", "--max-age-days", "1"],
}
# In the command line's source, the strings that are names, not words: keys of stored and
# signed records, what the commands read an argument as, the options' names before, folders.
NAMES = {
    *("pack", "packs", "pack_id", "pack_digest", "pack_dir"),
    *("--pack", "--pack-dir"),
    *("new_pack", "community_pack", "agent_pack"),
}
FIXTURE_AGENTS = "tests/fixtures/reference-packs/"  # `mendari sandbox generate`'s defaults


def parsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    """Every parser of the command line, by the command that reaches it ("" is `mendari`)."""
    yield " ".join(path), parser
    for name, child in commands(parser).items():
        yield from parsers(child, (*path, name))


def commands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """A parser's commands by name, as argparse lists them."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def shown(parser: argparse.ArgumentParser) -> str:
    return parser.format_usage() + parser.format_help()


# -- one vocabulary ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["a pack", "Packs", "Decision Pack", "--pack-dir", "PACK_DIR", "pack.yaml", "community-packs/"],
)
def test_the_old_word_is_found_however_it_is_joined(text: str) -> None:
    assert OLD_WORD.search(text)


@pytest.mark.parametrize("text", ["a signed .ohpack archive", "the package", "unpacked"])
def test_another_word_is_not_taken_for_it(text: str) -> None:
    assert not OLD_WORD.search(text)


def test_no_usage_or_help_says_pack() -> None:
    root = build_parser()
    every = list(parsers(root))
    assert len(every) > 150  # the whole tree: every command and subcommand
    said = [
        (command, found.group())
        for command, parser in every
        for found in OLD_WORD.finditer(shown(parser))
    ]
    assert said == []


def test_no_message_says_pack() -> None:
    """What a command prints or raises: every string of the source but its docstrings and the
    names of things (`NAMES`), which stay until the records they are in change."""
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, documented) and node.body and isinstance(node.body[0], ast.Expr)
    }
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert "no agents match" in strings  # it reads them all
    said = [
        text
        for text in strings
        if OLD_WORD.search(text) and text not in NAMES and not text.startswith(FIXTURE_AGENTS)
    ]
    assert said == []


def test_an_error_names_what_is_typed_now(capsys: pytest.CaptureFixture[str]) -> None:
    """A usage error, for a missing or mistyped argument, names the agent and never the pack:
    also for an option typed under the name it had."""
    for argv, named in (
        (["validate"], "the following arguments are required: agent"),
        (["monitor", "--pack"], "argument --agent: expected one argument"),
        (["omop", "run", "--db", "cdm.db"], "the following arguments are required: --agent"),
        (["archive"], "the following arguments are required: archive_command"),
        (["no-such-command"], "invalid choice: 'no-such-command'"),
    ):
        assert main(argv) == 1
        (line,) = capsys.readouterr().err.splitlines()
        assert named in line, line
        assert not OLD_WORD.search(line), line


def test_list_names_the_setting_that_finds_agents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "discover", lambda: SimpleNamespace(packs=[], problems={}))
    assert main(["list"]) == 0
    assert capsys.readouterr().err == (
        "No agents found. Set MENDARI_AGENTS_PATH or run from a checkout.\n"
    )


# -- the names typed before ----------------------------------------------------------------------


def test_every_option_that_names_an_agent_is_still_read_as_pack() -> None:
    root = build_parser()
    named = {
        command
        for command, parser in parsers(root)
        for action in parser._actions
        if action.option_strings and action.dest == "pack"
    }
    assert named == set(AGENT_OPTIONS)  # a new one is added there, and so checked below
    for command, rest in AGENT_OPTIONS.items():
        argv = [*command.split(), *rest]
        now = root.parse_args([*argv, "--agent", AGENT])
        assert now.pack == AGENT
        assert root.parse_args([*argv, "--pack", AGENT]) == now, command
        assert root.parse_args([*argv, f"--pack={AGENT}"]) == now, command


def test_the_agents_folder_is_still_read_as_pack_dir() -> None:
    parse = build_parser().parse_args
    now = parse(["sandbox", "generate", "--agent-dir", "an-agent"])
    assert now.pack_dir == "an-agent"
    assert parse(["sandbox", "generate", "--pack-dir", "an-agent"]) == now


def test_a_name_typed_before_is_read_whole_and_shown_nowhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An abbreviation stands for an option the help shows, never for a name it had: `--pa` is
    `--patient`, and `--pac` is nothing."""
    root = build_parser()
    launch = ["sandbox", "smart-launch", "--engine", "http://engine.test"]
    assert root.parse_args([*launch, "--pa", "p-1"]).patient == "p-1"
    assert main(["monitor", "--pac", AGENT]) == 1
    assert "unrecognized arguments: --pac" in capsys.readouterr().err
    for command, parser in parsers(root):  # argparse never learns a name: it can show none
        assert not ({"--pack", "--pack-dir"} & set(parser._option_string_actions)), command
        assert "pack" not in commands(parser), command
    assert "--agent" not in shown(commands(root)["new"])  # what `mendari new` writes anyway


def test_mendari_pack_is_still_read_as_mendari_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = build_parser()
    for rest in (["build", "a/b", "--key", "k"], ["verify", "an-archive", "--public-key", "k"]):
        now = root.parse_args(["archive", *rest])
        assert now.command == "archive"
        assert root.parse_args(["pack", *rest]) == now
    # A directory named `pack` is an agent's directory, not the command.
    assert root.parse_args(["validate", "pack"]).pack == "pack"

    agent = tmp_path / "an-agent"
    assert main(["new", str(agent), "--id", "my-clinic/an-agent"]) == 0
    assert main(["keys", "generate", "--out", str(tmp_path)]) == 0
    key, public = str(tmp_path / "publisher.key"), str(tmp_path / "publisher.pub")
    built = {}
    for command in ("archive", "pack"):
        out = tmp_path / command
        assert main([command, "build", str(agent), "--key", key, "--out", str(out)]) == 0
        (built[command],) = out.iterdir()
        assert main([command, "verify", str(built[command]), "--public-key", public]) == 0
    assert built["pack"].read_bytes() == built["archive"].read_bytes()
    capsys.readouterr()
    assert main(["pack"]) == 1  # its own errors name the command as it is now
    assert "error: mendari archive: " in capsys.readouterr().err


def test_new_writes_an_agent_with_or_without_the_flag_that_asked_for_one(tmp_path: Path) -> None:
    def files(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    plain, flagged, flag_first = (tmp_path / name for name in ("plain", "flagged", "flag-first"))
    assert main(["new", str(plain), "--id", "my-clinic/my-agent"]) == 0
    # As the README and the guides gave it, and as mendari.co did, with the flag first.
    assert main(["new", str(flagged), "--agent", "--id", "my-clinic/my-agent"]) == 0
    assert main(["new", "--agent", str(flag_first), "--id", "my-clinic/my-agent"]) == 0
    assert files(plain) == files(flagged) == files(flag_first)
    reader = load_pack(plain).manifest.providers["reader"]
    assert reader.use == "agent" and reader.variants["agent"].type == "agent"
    assert "fixtures/mock_agent.json" in files(plain)  # it answers offline as written

    # The other template is chosen as it was.
    community = tmp_path / "community-agents" / "sorter"
    assert main(["new", str(community), "--community"]) == 0
    contributed = load_pack(community, origin="community")
    assert contributed.id == "community/sorter"
    assert contributed.manifest.providers["reader"].use == "rules"
    assert main(["new", str(tmp_path / "both"), "--community", "--agent"]) == 1
    assert not (tmp_path / "both").exists()
