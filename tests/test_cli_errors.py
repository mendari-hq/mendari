# SPDX-License-Identifier: Apache-2.0
"""Every `mendari` command answers a broken input with one line per problem (`error: ...`) and
exit 1, never a traceback; `--debug` (or MENDARI_DEBUG=1) lets the error through with its
traceback (LP-15). Every command has a case here: a command added without one fails
`test_every_command_has_a_broken_input`."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari import __version__
from mendari.cli import build_parser, main

DEAD = "http://127.0.0.1:9"  # nothing listens: refused at once


def commands(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> Iterator[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from commands(child, (*path, name))
            return
    yield " ".join(path)


@pytest.fixture
def places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Paths for broken inputs, and a home the commands cannot write outside of."""
    for name in ("HOME", "MENDARI_CONFIG_DIR", "MENDARI_CACHE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name in ("DATABASE_URL", "MENDARI_PROVIDERS", "MENDARI_DEBUG", "MENDARI_REGISTRY_URL"):
        monkeypatch.delenv(name, raising=False)
    not_a_database = tmp_path / "not-a-database"
    not_a_database.write_text("hello")
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    return {
        "missing": str(tmp_path / "missing.json"),
        "missing_db": str(tmp_path / "missing.db"),
        "not_db": str(not_a_database),
        "file": str(a_file),
        "out": str(tmp_path / "out.jsonl"),
    }


# command -> (argv after the command, extra environment). Paths are `places` keys in braces.
BROKEN: dict[str, tuple[list[str], dict[str, str]]] = {
    "list": ([], {"MENDARI_COMMUNITY_PACKS": "maybe"}),
    "validate": (["no/such-pack"], {}),
    "test": (["mendari/patient-inbox-router", "--provider", "routr=llm"], {}),
    "decide": (["mendari/ai-guard", "--input", "{missing}"], {}),
    "eval": (["mendari/ai-guard", "--dataset", "{missing}"], {}),
    "train": (["no/such-pack"], {}),
    "calibrate": (["mendari/patient-inbox-router", "--slot", "routr", "--variant", "x"], {}),
    "agreement": (["mendari/patient-inbox-router", "--slot", "routr", "--variants", "a,b"], {}),
    "leaderboard": (["mendari/ai-guard", "--report", "{missing}"], {}),
    "card": (["no/such-pack"], {}),
    "schema": (["no/such-pack"], {}),
    "serve": (["--db", "{not_db}"], {}),
    "audit verify": (["--db", "{missing_db}"], {}),
    "audit export": (["--db", "{missing_db}"], {}),
    "automation hold": (["--all-orgs", "--reason", "drill", "--db", "{not_db}"], {}),
    "automation lift": (["h-1", "--db", "{not_db}"], {}),
    "automation list": (["--db", "{not_db}"], {}),
    "feedback export": (["mendari/ai-guard", "--out", "{out}", "--db", "{missing_db}"], {}),
    "sandbox generate": (["--kind", "inbox", "--local-out", "{file}/x.jsonl"], {}),
    "sandbox seed": (["--fhir", DEAD, "--wait", "1"], {}),
    "sandbox worker": (["--fhir", DEAD, "--engine", DEAD, "--once"], {}),
    "sandbox smart-launch": (["--engine", DEAD, "--launcher", DEAD, "--fhir", DEAD], {}),
    "sandbox pilot-check": (["--engine", DEAD], {}),
    "sandbox payer": (["--port", "0"], {}),
    "sandbox omop": (["--dataset", "{missing}", "--db", "{out}"], {}),
    "sandbox omop-check": (["--dataset", "{missing}"], {}),
    "sandbox bulk-check": (["--fhir", DEAD], {}),
    "pas build": (
        ["--decision", "d-1", "--db", "{missing_db}", "--out", "{out}"]
        + [
            f"--{name}"
            for kind in ("patient", "coverage", "insurer", "requester", "order")
            for name in (kind, "{missing}")
        ],
        {},
    ),
    "omop dataset": (
        ["--db", "{not_db}", "--pack", "mendari/clinical-trial-prescreener", "--out", "{out}"],
        {},
    ),
    "omop run": (["--db", "{not_db}", "--pack", "mendari/clinical-trial-prescreener"], {}),
    "bulk export": (["--fhir", DEAD, "--out", "{out}"], {}),
    "bulk dataset": (
        [
            *("--pack", "mendari/referral-router", "--ndjson", "{missing}"),
            *("--labels", "{missing}", "--out", "{out}"),
        ],
        {},
    ),
    "keys generate": (["--out", "{file}"], {}),
    "keys register": (["--namespace", "x", "--public-key", "{missing}", "--registry", DEAD], {}),
    "login": (["--registry", DEAD], {}),
    "push": (["mendari/ai-guard", "--key", "{missing}", "--registry", DEAD], {}),
    "pull": (["x/y", "--registry", DEAD], {}),
    "search": (["x", "--registry", DEAD], {}),
    "pack build": (["mendari/ai-guard", "--key", "{missing}"], {}),
    "pack verify": (["{missing}", "--public-key", "{missing}"], {}),
    "registry serve": (["--db", "{not_db}"], {}),
    "monitor": (["--db", "{missing_db}"], {}),
    "smart check": ([DEAD], {}),
    "phi purge": (["--db", "{missing_db}"], {}),
    "phi rotate": (["--db", "{missing_db}", "--check"], {}),
    "phi erase": (
        ["--db", "{missing_db}", "--decision", "d-1", "--reason", "request", "--by", "x"],
        {},
    ),
    "notes erase": (
        ["--db", "{missing_db}", "--id", "n-1", "--reason", "request", "--by", "x"],
        {},
    ),
    "new": (["{file}"], {}),
    "community-check": (["{missing}"], {}),
    "demo": ([], {"MENDARI_COMMUNITY_PACKS": "maybe"}),
}


# Commands whose answer to a broken input is their check report, one problem a line on stdout,
# and exit 1, as for any failed check: the report is the result, not an error.
REPORTS = {"community-check", "smart check"}


def argv_for(command: str, places: dict[str, str]) -> list[str]:
    arguments, _ = BROKEN[command]
    return [*command.split(), *(argument.format(**places) for argument in arguments)]


def test_every_command_has_a_broken_input() -> None:
    assert sorted(commands(build_parser())) == sorted(BROKEN)


@pytest.mark.parametrize("command", sorted(BROKEN))
def test_a_broken_input_is_one_line_and_exit_1(
    command: str,
    places: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in BROKEN[command][1].items():
        monkeypatch.setenv(name, value)
    code = main(argv_for(command, places))
    out, err = capsys.readouterr()
    lines = [line for line in err.splitlines() if line.strip()]
    assert code == 1, (command, err)
    assert "Traceback" not in out + err
    if command in REPORTS:
        assert ("error" in out or "fail" in out) and not err
        return
    assert lines and all(line.startswith(("error: ", "warning: ")) for line in lines), err
    assert any(line.startswith("error: ") for line in lines), err
    assert "Traceback" not in err


@pytest.mark.parametrize("command", sorted(BROKEN))
def test_debug_lets_the_error_through(
    command: str, places: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in BROKEN[command][1].items():
        monkeypatch.setenv(name, value)
    if command in REPORTS:  # a report, not an error: --debug changes nothing
        assert main([*argv_for(command, places), "--debug"]) == 1
        return
    with pytest.raises(Exception) as raised:
        main([*argv_for(command, places), "--debug"])
    assert not isinstance(raised.value, SystemExit)


def test_debug_also_comes_from_the_environment(
    places: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_DEBUG", "1")
    with pytest.raises(FileNotFoundError):
        main(["decide", "mendari/ai-guard", "--input", places["missing"]])


def test_debug_goes_before_or_after_the_command(
    places: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["--debug", "audit", "verify"], ["audit", "verify", "--debug"]):
        with pytest.raises(Exception, match="no database at"):
            main([*argv, "--db", places["missing_db"]])
    assert main(["audit", "verify", "--db", places["missing_db"]]) == 1
    assert capsys.readouterr().err.startswith("error: no database at")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == f"mendari {__version__}\n"


def test_a_usage_error_is_one_line_naming_where_help_is(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "mendari/ai-guard"]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith("error: mendari eval: the following arguments are required: --dataset")
    assert line.endswith("(see mendari eval --help)")


def test_ctrl_c_is_one_line_and_exit_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("mendari.cli.cmd_list", interrupted)
    assert main(["list"]) == 130
    assert capsys.readouterr().err == "interrupted\n"


def test_an_unexpected_error_says_only_its_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A library's own message can carry case text: only Mendari's messages are printed."""

    def leaky(_: Any) -> int:
        raise ValueError("patient Jane Doe, 1980-01-01")

    monkeypatch.setattr("mendari.cli.cmd_list", leaky)
    assert main(["list"]) == 1
    err = capsys.readouterr().err
    assert err == "error: unexpected ValueError; run again with --debug for the traceback\n"
