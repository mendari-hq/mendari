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
from tests.support.platform import CHART, TRIAGE

DEAD = "http://127.0.0.1:9"  # nothing listens: refused at once
DIGEST = "sha256:" + "0" * 64
# The bundled demo agent on the search path, as in a release (here the test fixtures stand
# where the bundled agents are): a sandbox command that names no agent runs on it.
DEMO = {"MENDARI_AGENTS_PATH": str(Path(__file__).resolve().parents[1] / "agents")}


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


SET_ID = "00000000-0000-4000-8000-000000000000"  # a grading round's id, of no round
# command -> (argv after the command, extra environment). Paths are `places` keys in braces.
BROKEN: dict[str, tuple[list[str], dict[str, str]]] = {
    "list": (["--no-such-option"], {}),
    "usage": (["--db", "{missing_db}"], {}),
    "validate": (["no/such-agent"], {}),
    "test": ([TRIAGE, "--provider", "routr=llm"], {}),
    "probe": (["no/such-agent"], {}),
    "decide": ([TRIAGE, "--input", "{missing}"], {}),
    "eval": ([TRIAGE, "--dataset", "{missing}"], {}),
    "data coverage": ([TRIAGE, "--dataset", "{missing}"], {}),
    "data check": ([TRIAGE, "{missing}"], {}),
    "calibrate": ([TRIAGE, "--slot", "routr", "--variant", "x"], {}),
    "leaderboard": ([TRIAGE, "--report", "{missing}"], {}),
    "compare": (["{missing}", "{missing}", "--dataset", "test"], {}),
    "card": (["no/such-agent"], {}),
    "schema": (["no/such-agent"], {}),
    "serve": (["--db", "{not_db}"], {}),
    "audit verify": (["--db", "{missing_db}"], {}),
    "audit checkpoint": (["--db", "{not_db}"], {}),
    "audit export": (["--db", "{missing_db}"], {}),
    "automation hold": (["--all-orgs", "--reason", "drill", "--db", "{not_db}"], {}),
    "automation lift": (["h-1", "--db", "{not_db}"], {}),
    "automation list": (["--db", "{not_db}"], {}),
    "access org create": (["clinic-a", "--name", "Clinic A", "--db", "{not_db}"], {}),
    "access org list": (["--db", "{missing_db}"], {}),
    "access bootstrap": (["--email", "owner@example.org", "--db", "{not_db}"], {}),
    "access invite": (
        ["--org", "a", "--email", "x@example.org", "--role", "viewer", "--db", "{not_db}"],
        {},
    ),
    "access members": (["--org", "a", "--db", "{missing_db}"], {}),
    "access requests": (["--db", "{not_db}"], {}),
    "access check": (["--db", "{missing_db}"], {}),
    "access keys": (["--db", "{missing_db}"], {}),
    "access clients": (["--org", "a", "--db", "{not_db}"], {}),
    "access revoke key": (["k-1", "--reason", "leak", "--db", "{not_db}"], {}),
    "access revoke client": (["c-1", "--reason", "leak", "--db", "{missing_db}"], {}),
    "access revoke all": (["--org", "a", "--reason", "leak", "--db", "{not_db}"], {}),
    "access end-sessions": (["--kind", "people", "--reason", "drill", "--db", "{not_db}"], {}),
    "feedback export": ([TRIAGE, "--out", "{out}", "--db", "{missing_db}"], {}),
    "sandbox seed": (["--fhir", DEAD, "--wait", "1"], DEMO),
    "sandbox worker": (["--fhir", DEAD, "--engine", DEAD, "--once"], {}),
    "sandbox cds-client": (["--engine", DEAD, "--fhir", DEAD, "--listen", "127.0.0.1:0"], DEMO),
    "sandbox smart-launch": (["--engine", DEAD, "--launcher", DEAD, "--fhir", DEAD], DEMO),
    "sandbox pilot-check": (["--engine", DEAD], DEMO),
    "sandbox bulk-check": (["--fhir", DEAD], DEMO),
    "bulk export": (["--fhir", DEAD, "--out", "{out}"], {}),
    "bulk dataset": (
        [
            *("--agent", CHART, "--ndjson", "{missing}"),
            *("--labels", "{missing}", "--out", "{out}"),
        ],
        {},
    ),
    "keys generate": (["--out", "{file}"], {}),
    "keys register": (["--namespace", "x", "--public-key", "{missing}", "--registry", DEAD], {}),
    "login": (["--registry", DEAD], {}),
    "push": ([TRIAGE, "--key", "{missing}", "--registry", DEAD], {}),
    "pull": (["x/y", "--registry", DEAD], {}),
    "search": (["x", "--registry", DEAD], {}),
    "archive build": ([TRIAGE, "--key", "{missing}"], {}),
    "archive verify": (["{missing}", "--public-key", "{missing}"], {}),
    "registry serve": (["--db", "{not_db}"], {}),
    "registry verify": (["--db", "{missing_db}"], {}),
    "registry token": (
        ["--namespace", "a", "--org", "a", "--reason", "x", "--db", "{missing_db}"],
        {},
    ),
    "registry tokens": (["--db", "{missing_db}"], {}),
    "registry revoke-token": (["0" * 16, "--reason", "x", "--db", "{not_db}"], {}),
    "registry claim": (
        ["--namespace", "a", "--org", "a", "--reason", "x", "--db", "{missing_db}"],
        {},
    ),
    "registry approve": (["--namespace", "a", "--reason", "x", "--db", "{missing_db}"], {}),
    "registry refuse": (["--namespace", "a", "--reason", "x", "--db", "{not_db}"], {}),
    "registry claims": (["--db", "{missing_db}"], {}),
    # Sites (LP-22d): a database that is not there, or not a database.
    "registry site": (["--org", "a", "--name", "a", "--reason", "x", "--db", "{missing_db}"], {}),
    "registry site-verify": (["--site", "0" * 16, "--reason", "x", "--db", "{not_db}"], {}),
    "registry site-key": (
        ["--site", "0" * 16, "--key", "{missing}", "--reason", "x", "--db", "{missing_db}"],
        {},
    ),
    "registry site-revoke-key": (["ed25519:" + "0" * 16, "--reason", "x", "--db", "{not_db}"], {}),
    "registry site-close": (["--site", "0" * 16, "--reason", "x", "--db", "{missing_db}"], {}),
    "registry sites": (["--db", "{missing_db}"], {}),
    # This machine as a site (LP-22e): a site id that is not one, a site not made yet.
    "site init": (["--site", "not-a-site-id", "--out", "{out}"], {}),
    "site key": (["--new", "--out", "{out}"], {}),
    "site ticket": ([TRIAGE, "--dataset", "{missing}"], {}),
    # Signed reports (LP-22f): a submission that is not there.
    "site submit": (["{missing}"], {}),
    "registry accept-report": (["{missing}", "--db", "{missing_db}"], {}),
    "registry publisher-type": (
        ["--namespace", "a", "--type", "verified", "--reason", "x", "--db", "{missing_db}"],
        {},
    ),
    "registry review": (
        ["a/b@1.0.0", "--risk-level", "1", "--reason", "x", "--db", "{missing_db}"],
        {},
    ),
    "registry recall": (
        ["a/b@1.0.0", "--severity", "low", "--reason", "x", "--db", "{not_db}"],
        {},
    ),
    "registry delete": (["a/b", "--reason", "x", "--db", "{missing_db}"], {}),
    "registry revoke-key": (["a", "ed25519:0", "--reason", "x", "--db", "{missing_db}"], {}),
    "registry countersign": (["--db", "{missing_db}"], {}),
    "registry checkpoint": (["--db", "{missing_db}"], {}),
    "yank": (["site-a/inbox", "--reason", "x", "--severity", "low", "--key", "{missing}"], {}),
    "monitor": (["--db", "{missing_db}"], {}),
    # The committee packet reads the server's database and makes none.
    "packet": ([TRIAGE, "--db", "{missing_db}"], {}),
    # No webhook (the tests clear MENDARI_ALERT_WEBHOOK); in CI, refused before that.
    "alerts test": ([], {}),
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
    # Written output (wave 5): an agent with no rubric has none to grade against.
    "written eval": ([TRIAGE, "--rubric", "tone", "--dataset", "test"], {}),
    # Its grading rounds: each reads a missing database or one that is not one.
    "written round create": (
        [
            *(TRIAGE, "--rubric", "tone", "--dataset", "test"),
            *("--org", "a", "--sample", "5", "--db", "{not_db}"),
        ],
        {},
    ),
    "written round assign": ([SET_ID, "--grader", "user:0123456789abcdef", "--db", "{not_db}"], {}),
    "written round progress": ([SET_ID, "--db", "{missing_db}"], {}),
    "written round close": ([SET_ID, "--db", "{not_db}"], {}),
    "written round list": (["--db", "{missing_db}"], {}),
    # The agent store (LP-20e, LP-20f): each reads a missing database or one that is not one.
    "store versions": (["--org", "a", "--db", "{missing_db}"], {}),
    "store verify": (["--db", "{not_db}"], {}),
    "store withdraw": ([DIGEST, "--org", "a", "--reason", "x", "--db", "{missing_db}"], {}),
    "store purge": (["--db", "{not_db}"], {}),
    "new": (["{file}"], {}),
    "demo": (["--no-such-option"], {}),
    "mcp": (["--db", "{not_db}"], {}),
    "help": (["no-such-command"], {}),
}


# Commands whose broken input is refused by the command line itself, before the command runs:
# those that take no input of their own.
USAGE = {"list", "demo"}
# Commands whose answer to a broken input is their check report, one problem a line on stdout,
# and exit 1, as for any failed check: the report is the result, not an error.
REPORTS = {"smart check"}
# The commands wave 6 removes, until their code goes (a name, or the first word of a group's):
# `train`, `improve` and `agreement` (G3); Da Vinci's PAS and OMOP, with the sandbox's payer and
# OMOP commands (G4, G8); the contribution path's check (G10). Set aside, so `BROKEN` holds every
# command that stays.
REMOVED = {
    *("train", "improve", "agreement"),
    *("pas", "omop", "sandbox payer", "sandbox omop", "sandbox omop-check"),
    "community-check",
}


def staying(command: str) -> bool:
    return command not in REMOVED and command.split()[0] not in REMOVED


def argv_for(command: str, places: dict[str, str]) -> list[str]:
    arguments, _ = BROKEN[command]
    return [*command.split(), *(argument.format(**places) for argument in arguments)]


def test_every_command_has_a_broken_input() -> None:
    assert sorted(filter(staying, commands(build_parser()))) == sorted(BROKEN)


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
    # Each broken input reaches its command (not a usage error, unless it is one on purpose)
    # and is named, never "unexpected".
    assert not any("unexpected" in line for line in lines), err
    if command not in USAGE:
        assert "--help)" not in err, err
    assert not Path(places["missing_db"]).exists()  # a missing database is never created


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
        main(["decide", TRIAGE, "--input", places["missing"]])


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
    assert main(["eval", TRIAGE]) == 1
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


@pytest.mark.parametrize("value", ["0", "off", "false", "no", ""])
def test_mendari_debug_off_keeps_one_line(
    value: str, places: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_DEBUG", value)
    assert main(["decide", TRIAGE, "--input", places["missing"]]) == 1


def test_mendari_debug_takes_on_or_off_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MENDARI_DEBUG", "maybe")
    assert main(["list"]) == 1
    assert capsys.readouterr().err == "error: MENDARI_DEBUG must be on or off (1 or 0)\n"


@pytest.mark.parametrize(
    "given",
    [
        "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIA==\n-----END PRIVATE KEY-----",
        '{"message": {"body": "Jane Example, born 1950"}}',
    ],
    ids=["a-key", "a-case"],
)
def test_a_files_content_given_as_its_path_is_never_printed(
    given: str, capsys: pytest.CaptureFixture[str]
) -> None:
    flag = "--key" if "PRIVATE" in given else "--input"
    argv = ["push", TRIAGE, "--key", given] if flag == "--key" else None
    argv = argv or ["decide", TRIAGE, "--input", given]
    assert main(argv) == 1
    err = capsys.readouterr().err
    (line,) = err.splitlines()
    assert "MC4CAQ" not in err and "Jane" not in err
    assert "looks like a file's content, not its path" in line


def test_a_json_file_that_cannot_be_read_is_named_by_its_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"message": ')
    assert main(["decide", TRIAGE, "--input", str(bad)]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert line == f"error: --input {bad}: not valid JSON (line 1, column 13: Expecting value)"
    report = tmp_path / "report.json"
    report.write_text("[]")
    assert main(["leaderboard", TRIAGE, "--report", str(report)]) == 1
    assert f"--report {report}: expected a JSON object" in capsys.readouterr().err


def test_a_closed_pipe_is_not_an_error(tmp_path: Path) -> None:
    """`mendari list | head -1`: the reader closing the pipe is no error line, no traceback."""
    import subprocess
    import sys

    shell = f"{sys.executable} -c 'from mendari.cli import main; main([\"list\"])' | head -c 1"
    ran = subprocess.run(["sh", "-c", shell], capture_output=True, text=True)
    assert "Traceback" not in ran.stderr and "BrokenPipeError" not in ran.stderr
    assert "error:" not in ran.stderr


def test_a_server_that_cannot_listen_is_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    def cannot_bind(*_: Any, **__: Any) -> None:
        raise SystemExit(3)  # what uvicorn does when the port is in use

    monkeypatch.setattr(uvicorn, "run", cannot_bind)
    assert main(["serve", "--db", str(tmp_path / "a.db"), "--port", "8765"]) == 1
    (line,) = capsys.readouterr().err.splitlines()
    assert line.startswith("error: could not listen on 127.0.0.1:8765")


def test_serve_refuses_a_typed_provider_setting_no_agent_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *_, **__: None)
    db = str(tmp_path / "a.db")
    for setting in ("router=lmm", "routr=llm"):  # a variant typo, a slot typo
        assert main(["serve", "--db", db, "--provider", setting]) == 1
        assert f"--provider settings no served agent takes: {setting}" in capsys.readouterr().err
    assert main(["serve", "--db", db, "--provider", "router=llm"]) == 0


@pytest.mark.parametrize("resamples", ["0", "100001"])
def test_compare_caps_its_resamples_like_every_bootstrap(
    resamples: str, places: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Checked before either run is read: a typo of 10**9 resamples is never hours of work.
    args = [places["missing"], places["missing"], "--dataset", "test", "--resamples", resamples]
    assert main(["compare", *args]) == 1
    assert capsys.readouterr().err.strip() == "error: --resamples is 1 to 100000"
