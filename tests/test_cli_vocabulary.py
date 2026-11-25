# SPDX-License-Identifier: Apache-2.0
"""The command line speaks one vocabulary: what Mendari governs is an agent, and "pack" is never
read or typed. No usage, help, message or error says it, and no name a command or an option had
before is read. And `mendari --help` is short: the commands to start with, under what each is
for, with every command behind `mendari help all`."""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from mendari import cli, load_pack
from mendari.cli import GROUPS, build_parser, main, overview
from mendari.sandbox import DEMO_AGENT

# What the help shows rests on how argparse writes it, which changes between Python versions;
# CI runs only the core tests on every version it supports.
pytestmark = pytest.mark.core

# "pack" and "packs" (so "Decision Pack" too), in any case, as a word of its own: also where a
# dash, a dot or an underscore joins it to another (`--pack-dir`, `pack.yaml`, `PACK_DIR`), which
# a plain word boundary would let through. `.ohpack`, the extension an archive had, is not it.
OLD_WORD = re.compile(r"(?<![a-z])packs?(?![a-z])", re.IGNORECASE)
AGENT = "my-clinic/an-agent"  # any agent: no option takes only some
# Every command with an option that names an agent, and the least it takes beside it.
AGENT_OPTIONS = {
    "audit export": [],
    "automation hold": ["--all-orgs", "--reason", "a drill"],
    "sandbox seed": ["--fhir", "http://fhir.test"],
    "sandbox worker": ["--fhir", "http://fhir.test", "--engine", "http://engine.test"],
    "sandbox cds-client": ["--fhir", "http://fhir.test", "--engine", "http://engine.test"],
    "sandbox smart-launch": ["--engine", "http://engine.test"],
    "sandbox pilot-check": ["--engine", "http://engine.test"],
    "sandbox bulk-check": ["--fhir", "http://fhir.test"],
    "bulk dataset": ["--ndjson", "export", "--labels", "labels.jsonl", "--out", "cases.jsonl"],
    "monitor": [],
    "store versions": ["--org", "clinic-a"],
    "evalset agreement": ["a-set"],
    "evalset coverage": ["a-set"],
    "evalset freeze": ["a-set", "--max-runs", "1", "--max-age-days", "1"],
}
# In the command line's source, the strings that are names, not words: keys of stored and
# signed records, and what the commands read an argument as.
NAMES = {"pack", "packs", "pack_id", "pack_digest"}


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


def listed(text: str) -> list[str]:
    """The commands a help text lists: the first word of each indented entry."""
    return re.findall(r"(?m)^  ([a-z][a-z-]*)\b", text)


def helped(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """What a help flag prints before it stops, with exit 0."""
    with pytest.raises(SystemExit) as stopped:
        main(list(argv))
    assert stopped.value.code == 0
    return capsys.readouterr().out


@pytest.fixture(autouse=True)
def _a_terminal_80_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")


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
    for everything in (False, True):  # `mendari --help` and `mendari help all`
        assert not OLD_WORD.search(overview(root, everything=everything))


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
    assert cli.EVERY_COMMAND in strings and "no agents match" in strings  # it reads them all
    said = [text for text in strings if OLD_WORD.search(text) and text not in NAMES]
    assert said == []


def test_an_error_names_what_is_typed_now(capsys: pytest.CaptureFixture[str]) -> None:
    """A usage error, for a missing or mistyped argument, names the agent and never the pack."""
    for argv, named in (
        (["validate"], "the following arguments are required: agent"),
        (["monitor", "--agent"], "argument --agent: expected one argument"),
        (
            ["bulk", "dataset", "--ndjson", "e", "--labels", "l.jsonl", "--out", "o.jsonl"],
            "the following arguments are required: --agent",
        ),
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


# -- what is typed ------------------------------------------------------------------------------


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
        assert root.parse_args([*argv, "--agent", AGENT]).pack == AGENT, command
        assert root.parse_args([*argv, f"--agent={AGENT}"]).pack == AGENT, command


def test_a_sandbox_command_names_the_demo_agent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sandbox runs on the agent a release bundles: each of its commands names it by default
    and says so."""
    parse = build_parser().parse_args
    for command, rest in AGENT_OPTIONS.items():
        if not command.startswith("sandbox "):
            continue
        assert parse([*command.split(), *rest]).pack == DEMO_AGENT, command
        said = " ".join(helped(capsys, *command.split(), "--help").split())
        assert "(default: the bundled demo agent)" in said, command
    # No command of the sandbox, or any other, names an agent the product does not bundle.
    former = re.compile(
        r"inbox-router|ai-guard|trial-prescreener|prior-auth-preflight|referral-router"
    )
    assert not [
        command for command, parser in parsers(build_parser()) if former.search(shown(parser))
    ]


def test_new_writes_an_agent(tmp_path: Path) -> None:
    def files(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    plain = tmp_path / "plain"
    assert main(["new", str(plain), "--id", "my-clinic/my-agent"]) == 0
    reader = load_pack(plain).manifest.providers["reader"]
    assert reader.use == "agent" and reader.variants["agent"].type == "agent"
    assert "fixtures/mock_agent.json" in files(plain)  # it answers offline as written

    # The other template is chosen as it was.
    community = tmp_path / "community-agents" / "sorter"
    assert main(["new", str(community), "--community"]) == 0
    contributed = load_pack(community, origin="community")
    assert contributed.id == "community/sorter"
    assert contributed.manifest.providers["reader"].use == "rules"


def test_the_flag_new_once_needed_is_shown_nowhere() -> None:
    """`mendari new NAME` writes an agent with no flag, and takes no `--agent`: it is shown
    nowhere a person learns the command, so no one is told to type it: what the product prints
    (the demo's last line named it), the console, the site, the Agent Hub, the README, the docs,
    the examples and what GitHub shows a contributor."""
    root = Path(cli.__file__).resolve().parents[2]
    flagged = re.compile(r"mendari new\b[^\n`|;&,<]*--agent(?![\w-])")  # within one command
    assert flagged.search("uv run mendari new --agent my-agent --id my-clinic/my-agent")
    assert flagged.search("with `mendari new my-agent --agent`")
    assert not flagged.search("mendari new my-agent --id my-clinic/my-agent")
    assert not flagged.search("`mendari new x --community`, then `mendari sandbox seed --agent x`")
    read = [root / "README.md", root / "CONTRIBUTING.md"]
    for folder, patterns in {
        "src/mendari": ("*.py", "*.js", "*.html", "*.md", "*.yaml"),
        "web": ("*.html",),
        "hub/app": ("*.tsx", "*.ts"),
        "docs": ("*.md",),
        "examples": ("*.md",),
        "agents": ("*.md", "*.yaml"),
        ".github": ("*.md", "*.yml"),
    }.items():
        read += [path for pattern in patterns for path in (root / folder).rglob(pattern)]
    assert len(read) > 250 and root / "src" / "mendari" / "sandbox" / "demo.py" in read
    shown = [
        f"{path.relative_to(root)}:{number}: {line.strip()[:120]}"
        for path in sorted(read)
        if "node_modules" not in path.parts and "vendor" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if flagged.search(line)
    ]
    assert shown == [], "\n".join(shown)


# -- a short help --------------------------------------------------------------------------------


def test_every_command_is_in_exactly_one_group() -> None:
    grouped = [name for group in GROUPS for name in (*group.first, *group.more)]
    assert sorted(grouped) == sorted(commands(build_parser()))
    assert len({group.heading for group in GROUPS}) == len(GROUPS)


def test_help_is_the_commands_to_start_with(capsys: pytest.CaptureFixture[str]) -> None:
    text = helped(capsys, "--help")
    assert listed(text) == [name for group in GROUPS for name in group.first]
    assert text.startswith("usage: mendari <command> [options]\n")
    for group in GROUPS:
        assert (f"\n\n{group.heading}\n  " in text) == bool(group.first), group.heading
    lines = text.splitlines()
    assert len(lines) <= 26 and max(map(len, lines)) <= 78  # one screen, no line wrapped
    assert lines[-1] == "`mendari help all` (also `mendari --help-all`) lists every command."
    assert helped(capsys, "-h") == text
    assert main(["help"]) == 0
    assert capsys.readouterr().out == text


def test_help_all_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    text = helped(capsys, "--help-all")
    every = [name for group in GROUPS for name in (*group.first, *group.more)]
    assert listed(text) == every
    assert set(every) == set(commands(build_parser()))
    for group in GROUPS:
        assert f"\n\n{group.heading}\n  {group.first[0] if group.first else group.more[0]} " in text
    # Then the options, and where one command's own help is.
    assert all(f"\n  {option}" in text for option in ("-h, --help", "--help-all", "--version"))
    assert text.splitlines()[-1].startswith("`mendari help <command>` ")
    assert main(["help", "all"]) == 0
    assert capsys.readouterr().out == text


def test_help_shows_any_commands_own_help(capsys: pytest.CaptureFixture[str]) -> None:
    root = build_parser()
    for command, parser in parsers(root):  # every command, however deep
        assert cli._command(root, command.split()) is parser, command
    # As its `--help` prints it; a group lists its own commands.
    for words in (["eval"], ["audit"], ["audit", "verify"], ["access", "revoke", "key"]):
        assert main(["help", *words]) == 0
        assert capsys.readouterr().out == helped(capsys, *words, "--help")


@pytest.mark.parametrize(
    "command", [["no-such-command"], ["audit", "no-such-command"], ["all", "x"]]
)
def test_help_for_no_command_is_one_line(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["help", *command]) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == (
        f"error: `mendari {' '.join(command)}` is not a command: `mendari help all` lists every "
        "command\n"
    )
