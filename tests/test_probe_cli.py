# SPDX-License-Identifier: Apache-2.0
"""`mendari probe AGENT` on the command line: what it prints, what it writes, and the exit code
a CI job reads. Every agent is one built in a temporary directory."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, cli, load_pack
from mendari.cli import GROUPS, build_parser, main
from mendari.core.pack import pack_digest
from mendari.evals import probe, probe_report
from mendari.evals.pack_tests import PROBE_TESTS, PROBE_UNCONFIRMED
from mendari.evals.probe import Options
from tests.conftest import DEMO_AGENT_DIR
from tests.probe_agents import CASES, build_agent, write_rules
from tests.test_probe import CANNED_BY_KEYWORD, GIVEN, agent_variant


@pytest.fixture(autouse=True)
def _the_bundled_agent_is_as_it_was() -> Iterator[None]:
    before = pack_digest(DEMO_AGENT_DIR)
    yield
    assert pack_digest(DEMO_AGENT_DIR) == before, "a test changed agents/portal-message-agent"


@pytest.fixture
def agent(tmp_path: Path) -> Path:
    return build_agent(tmp_path / "agent")


def ran(capsys: pytest.CaptureFixture[str], *argv: Any) -> tuple[int, str, str]:
    """`mendari probe ...`: its exit code, what it printed, and what it said on stderr."""
    code = main(["probe", *map(str, argv)])
    printed = capsys.readouterr()
    return code, printed.out, printed.err


def expected(root: Path, *names: str, **options: Any) -> dict[str, Any]:
    """The report the library gives for the same probe."""
    pack = load_pack(root)
    chosen = Options(transformations=names, **options) if names else Options(**options)
    planned = probe.plan(Decision(pack, origin_cap=False), probe.source(pack), chosen)
    return probe.run(planned).report


# -- the report and the exit code ------------------------------------------------------------------


def test_probe_prints_the_report_and_changes_nothing(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = pack_digest(agent)
    code, out, err = ran(capsys, agent)
    assert (code, err) == (0, "")  # a report only: findings do not fail it unless asked
    assert out == probe_report.text(expected(agent)) + "\n"
    assert out.splitlines()[-1].startswith("Verdict: not ready. ")
    assert pack_digest(agent) == before and not (agent / PROBE_TESTS).exists()


@pytest.mark.parametrize(
    ("robust", "fail_on", "code"),
    [
        (False, "critical", 2),
        (False, "any", 2),
        (True, "critical", 0),
        (True, "any", 0),
    ],
)
def test_fail_on_gives_a_ci_job_its_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], robust: bool, fail_on: str, code: int
) -> None:
    root = build_agent(tmp_path / "agent", robust=robust)
    assert ran(capsys, root, "--fail-on", fail_on)[0] == code


def test_json_is_the_report_alone_and_out_writes_it(
    agent: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "reports" / "probe.json"
    code, out, err = ran(capsys, agent, "--transform", "typos", "--json", "--out", out_file)
    report = json.loads(out)
    assert code == 0 and err == ""
    assert report == expected(agent, "typos") and report["format"] == "mendari.probe/1"
    assert json.loads(out_file.read_text(encoding="utf-8")) == report
    for body in (case["input"]["message"]["body"] for case in CASES):
        assert body not in out and body not in out_file.read_text(encoding="utf-8")


def test_the_options_choose_the_transformations_the_cases_and_the_seed(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = ran(
        capsys, agent, "--transform", "typos,case", "--transform", "whitespace", "--limit", "3",
        "--variants", "2", "--seed", "7", "--json",
    )  # fmt: skip
    report = json.loads(out)
    assert report == expected(agent, "typos", "case", "whitespace", limit=3, variants=2, seed=7)
    assert [line["name"] for line in report["transformations"]] == ["typos", "case", "whitespace"]
    assert report["cases"]["probed"] == 3 and report["seed"] == 7
    _, out, _ = ran(capsys, agent, "--limit", "all", "--field", "message.body", "--json")
    assert json.loads(out)["cases"]["probed"] == 5


def test_the_agents_own_tests_can_be_the_cases(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = ran(capsys, agent, "--from-tests", "--transform", "typos", "--json")
    report = json.loads(out)
    assert report["source"] == {"kind": "tests", "name": "tests/cases.yaml"}
    # Its two tests: the symptom's variant loses it; ids are the tests' numbers.
    assert [(found["case"], found["harm"]) for found in report["findings"]] == [
        ("1", "critical_lost")
    ]
    code, _, err = ran(capsys, agent, "--from-tests", "--dataset", "cases")
    assert code == 1 and err == "error: --from-tests and --dataset: choose one\n"


def test_a_variant_of_the_slot_is_chosen_as_for_the_other_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["rules"] = {
        "type": "rules",
        "config": "logic/rules.yaml",
    }
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    _, out, _ = ran(capsys, root, "--transform", "typos", "--json")
    assert json.loads(out)["providers"] == {"reader": "agent"}
    _, out, _ = ran(capsys, root, "--transform", "typos", "--provider", "reader=rules", "--json")
    assert json.loads(out)["providers"] == {"reader": "rules"}


# -- a dry run -------------------------------------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_decides_nothing(
    agent: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a dry run decides nothing")

    monkeypatch.setattr(Decision, "decide", never)
    code, out, err = ran(capsys, agent, "--dry-run", "--write", "--fail-on", "any")
    assert (code, err) == (0, "")
    assert (
        out.splitlines()[1] == "A dry run: nothing was decided, and no agent or model was called."
    )
    assert out.splitlines()[-1] == (
        "35 decisions (5 cases as written and 30 variants), none of them a call to an agent."
    )
    assert not (agent / PROBE_TESTS).exists()
    _, out, _ = ran(capsys, agent, "--dry-run", "--json")
    summary = json.loads(out)
    assert summary["format"] == "mendari.probe-plan/1"
    assert summary["calls"] == {"decisions": 35, "agent": 0, "generator": 0}


# -- writing the failures as tests -----------------------------------------------------------------


def test_write_keeps_each_failure_as_a_test_that_mendari_test_runs(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["test", str(agent)]) == 0
    capsys.readouterr()
    code, out, err = ran(capsys, agent, "--transform", "typos", "--write")
    assert (code, err) == (0, "")
    assert out.splitlines()[-2:] == [
        f"Wrote 3 tests to {PROBE_TESTS}: `mendari test` fails on them until the agent is fixed.",
        "The agent's files changed, and its digest with them: it is another version now.",
    ]
    assert len(yaml.safe_load((agent / PROBE_TESTS).read_text(encoding="utf-8"))) == 3

    assert main(["test", str(agent)]) == 1  # the agent is not fixed
    shown = capsys.readouterr().out
    assert shown.count("FAIL  typos variant of cases case ") == 3
    assert shown.splitlines()[-1] == "2 passed, 3 failed"
    assert main(["validate", str(agent)]) == 0  # and it is still a valid agent
    capsys.readouterr()

    write_rules(agent, robust=True)  # the fix
    assert main(["test", str(agent)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "5 passed, 0 failed"
    # Probing the fixed agent finds nothing, and writes nothing.
    held = (agent / PROBE_TESTS).read_bytes()
    code, out, _ = ran(capsys, agent, "--write", "--fail-on", "any")
    assert code == 0 and out.splitlines()[-1] == "Nothing new to write."
    assert (agent / PROBE_TESTS).read_bytes() == held


def test_writing_twice_writes_nothing_twice(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran(capsys, agent, "--transform", "typos", "--write")
    held = (agent / PROBE_TESTS).read_bytes()
    code, out, _ = ran(capsys, agent, "--transform", "typos", "--write")
    assert code == 0 and (agent / PROBE_TESTS).read_bytes() == held
    assert out.splitlines()[-1] == "Nothing new to write: 3 failures are tests already."
    # One failure is said as one.
    assert probe_report.kept_lines((), (), 1, {}, "my-agent") == [
        "Nothing new to write: 1 failure is a test already."
    ]
    assert probe_report.kept_lines(("probe-a",), ("probe-b", "probe-c"), 1, {"site": 2}, "x") == [
        f"Wrote 1 test to {PROBE_TESTS}: `mendari test` fails on it until the agent is fixed.",
        f"2 tests wait in {PROBE_UNCONFIRMED}: a model changed what they expect, so they do not "
        "run. Read each one's text and its `expect` there, then `mendari probe x --confirm "
        "probe-b --confirm probe-c`.",
        "1 failure more is a test already.",
        "Not written: 2 failures of a site's own cases, whose text stays where it is marked as "
        "a site's.",
        "The agent's files changed, and its digest with them: it is another version now.",
    ]


def test_with_json_what_was_written_is_said_on_stderr(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = ran(capsys, agent, "--transform", "typos", "--write", "--json")
    assert code == 0 and json.loads(out)["totals"]["changed"] == 3
    assert err.startswith(f"Wrote 3 tests to {PROBE_TESTS}: ")


def test_write_is_refused_before_anything_runs_where_the_agent_is_not_the_authors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled demo agent is this release's: probed where it is, written into never."""
    from mendari.core import registry

    monkeypatch.setattr(registry, "_BUNDLED_DIRS", (DEMO_AGENT_DIR.parent,))
    monkeypatch.setattr(Decision, "decide", lambda *args, **kwargs: pytest.fail("it ran"))
    code, out, err = ran(capsys, DEMO_AGENT_DIR, "--write")
    assert (code, out) == (1, "")
    assert err == (
        "error: --write: an agent this release bundles is not changed in place: copy it, and "
        "probe the copy\n"
    )


# -- variants from a file, and to one --------------------------------------------------------------


def test_variants_from_a_file_are_tried_alone_or_with_the_named_transformations(
    agent: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    variants = tmp_path / "variants.jsonl"
    variants.write_text("".join(json.dumps(record) + "\n" for record in GIVEN), encoding="utf-8")
    _, out, _ = ran(capsys, agent, "--variants-from", variants, "--json")
    report = json.loads(out)
    assert [line["name"] for line in report["transformations"]] == [
        "paraphrase",
        "translate:es",
        "negation",
    ]
    assert report["totals"]["tried"] == 3 and report["calls"]["generator"] == 0
    _, out, _ = ran(capsys, agent, "--variants-from", variants, "--transform", "case", "--json")
    assert [line["name"] for line in json.loads(out)["transformations"]] == [
        "case",
        "paraphrase",
        "translate:es",
        "negation",
    ]

    # What a model changed the expected answer of waits; a person confirms it by its id.
    code, out, _ = ran(capsys, agent, "--variants-from", variants, "--write")
    (waiting,) = yaml.safe_load((agent / PROBE_UNCONFIRMED).read_text(encoding="utf-8"))
    denial = waiting["probe"]["id"]
    assert code == 0 and out.splitlines()[-3:] == [
        f"Wrote 2 tests to {PROBE_TESTS}: `mendari test` fails on them until the agent is fixed.",
        f"1 test waits in {PROBE_UNCONFIRMED}: a model changed what it expects, so it does not "
        f"run. Read its text and its `expect` there, then `mendari probe {agent} --confirm "
        f"{denial}`.",
        "The agent's files changed, and its digest with them: it is another version now.",
    ]
    assert main(["test", str(agent)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "2 passed, 2 failed"

    code, out, err = ran(capsys, agent, "--confirm", denial)
    assert (code, err) == (0, "")
    assert out == f"Confirmed {denial}: `mendari test` runs it now ({PROBE_TESTS}).\n"
    assert not (agent / PROBE_UNCONFIRMED).exists()
    assert main(["test", str(agent)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "2 passed, 3 failed"
    code, _, err = ran(capsys, agent, "--confirm", denial)
    assert code == 1
    assert err == f"error: no test waits in {PROBE_UNCONFIRMED}: there is none to confirm\n"


def test_the_variants_tried_are_written_out_outside_the_agent(
    agent: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "kept" / "variants.jsonl"
    code, out, _ = ran(capsys, agent, "--transform", "typos", "--variants-out", out_file, "--json")
    first = json.loads(out)
    records = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert code == 0 and len(records) == 5
    assert {record["transformation"] for record in records} == {"typos"}
    _, out, _ = ran(capsys, agent, "--variants-from", out_file, "--json")
    assert [found["id"] for found in json.loads(out)["findings"]] == [
        found["id"] for found in first["findings"]
    ]
    inside = agent / "data" / "variants.jsonl"
    code, out, err = ran(capsys, agent, "--variants-out", inside)
    assert (code, out) == (1, "") and not inside.exists()
    assert err == (
        "error: --variants-out: that file would be in the agent's own folder, where every file "
        "is part of the agent: keep the variants outside it\n"
    )


# -- variants a model writes -----------------------------------------------------------------------


def test_a_model_is_asked_only_when_a_transformation_needs_one(
    agent: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = ran(capsys, agent, "--transform", "paraphrase")
    assert (code, out) == (1, "")
    assert err == (
        "error: no model to write variants with: pass --model, or set LLM_MODEL (there is no "
        "default model)\n"
    )
    # A language is a translation, by a model too.
    code, _, err = ran(capsys, agent, "--language", "Haitian Creole", "--model", "a model")
    assert code == 1 and err == "error: --model: not a model's id\n"
    code, _, err = ran(capsys, agent, "--language", "es", "--model", "a-lab/a-model")
    assert code == 1
    assert err.startswith("error: the model that writes variants cannot be asked (")
    code, _, err = ran(capsys, agent, "--language", "fran\N{LATIN SMALL LETTER C WITH CEDILLA}ais")
    assert code == 1 and err == (
        "error: --language: a language's name or code in plain letters, as `es` or `haitian "
        "creole`\n"
    )
    code, _, err = ran(capsys, agent, "--model", "a-lab/a-model")
    assert code == 1 and err == (
        "error: --model writes paraphrase, negation and translate variants: name one with "
        "--transform or --language\n"
    )


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--transform", "shuffle"], "error: no transformation 'shuffle': choose from typos, "),
        (["--limit", "0"], "error: mendari probe: argument --limit: 1 case or more, or `all`"),
        (["--limit", "some"], "error: mendari probe: argument --limit: 1 case or more, or `all`"),
        (["--variants", "11"], "error: 1 to 10 variants of a case by a transformation"),
        (["--seed", "-1"], "error: the seed is 0 or more"),
        (["--fail-on", "nothing"], "error: mendari probe: argument --fail-on: invalid choice"),
        (["--dataset", "no-such-set"], "error: dataset 'no-such-set' is neither a file nor "),
        (["--field", "message.subject"], "error: no case has text in message.subject: "),
        (["--confirm", "probe-000000000000"], "error: no test waits in tests/probe-unconfirmed"),
        (["--variants-from", "{missing}"], "error: {missing}: No such file or directory"),
    ],
)
def test_what_cannot_be_probed_is_one_line_and_exit_1(
    agent: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    missing = str(tmp_path / "missing.jsonl")
    code, out, err = ran(capsys, agent, *(argument.format(missing=missing) for argument in argv))
    assert (code, out) == (1, "")
    assert err.startswith(error.format(missing=missing)) and err.count("\n") == 1


# -- the bundled demo agent, and the help ----------------------------------------------------------


def test_the_bundled_demo_agent_is_probed_as_canned_answers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = ran(capsys, DEMO_AGENT_DIR, "--limit", "20", "--fail-on", "critical")
    assert (code, err) == (2, "")
    lines = out.splitlines()
    assert lines[0].startswith("Portal Message Agent ")
    assert lines[0].endswith(" (mendari/portal-message-agent), reader=agent")
    assert "none of them a call to an agent: its answers are canned" in lines[2]
    assert lines[-1].startswith("Verdict (canned answers): not ready. ")


@pytest.mark.usefixtures("release_agents")
def test_the_demo_probes_its_agent_and_says_whose_answers_those_are(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mendari demo` ends by probing the demo agent, and is plain about what that shows: its
    answers are a fixture's, read on this machine, and so are the failures."""
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    before, _, rest = out.partition("\n== 5. Find the failures nobody wrote down ")
    assert "== 4. Before going live: evaluate the agent " in before and rest
    section, _, closing = rest.partition("\n== What next ")
    lines = section.splitlines()[1:]
    # What a variant is, in a sentence; then the probe's own report, from its second line.
    assert " ".join(lines[:2]) == (
        "Each case is rewritten six ways that keep its meaning (a typing error, a line break, an "
        "instruction slipped in): an answer that then changes is a failure."
    )
    lines = lines[2:]
    assert lines[0].startswith("40 of the ") and "seed 0. Text changed: message.body." in lines[0]
    assert lines[1].startswith("280 decisions, none of them a call to an agent: its answers are ")
    assert lines[1].endswith("set MENDARI_AGENT_URL and probe again.")
    assert "transformation  tried  changed  critical lost  automated" in lines
    (verdict,) = [line for line in lines if line.startswith("Verdict")]
    assert verdict.startswith("Verdict (canned answers): not ready. ")
    assert verdict.endswith("These are canned answers: set MENDARI_AGENT_URL and probe your agent.")
    assert lines[-1] == verdict  # the section ends as the probe's report does
    assert "Findings, worst first (3 of " in section
    # How each failure becomes a test is among the demo's last words.
    assert "  mendari probe my-agent " in closing and "kept as tests (--write)" in closing
    # The section names cases by id only: none of the messages section 1 printed is in it.
    assert "chest" not in section and "refill my" not in section


def test_probe_is_on_the_first_help_screen_under_build_and_check(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    (group,) = [group for group in GROUPS if group.heading == "Build and check"]
    assert group.first == ("validate", "test", "eval", "probe", "compare")
    with pytest.raises(SystemExit):
        main(["--help"])
    shown = capsys.readouterr().out
    assert "\n  probe     find failures by rewriting its cases; keep them as tests\n" in shown
    with pytest.raises(SystemExit):
        main(["probe", "--help"])
    helped = capsys.readouterr().out
    for option in (
        *("--dataset", "--from-tests", "--transform", "--language", "--model", "--field"),
        *("--variants-from", "--variants-out", "--limit", "--variants", "--seed", "--dry-run"),
        *("--write", "--confirm", "--fail-on", "--json", "--out", "--provider", "--workers"),
    ):
        assert f"  {option}" in helped, option
    # Each transformation is named where a person looks for it.
    for name in ("typos", "case", "punctuation", "whitespace", "additions", "injection"):
        assert name in helped
    assert "paraphrase" in helped and "negation" in helped and "translate:<language>" in helped
    # The parser is built without importing the probe: its two numbers are the probe's own.
    assert build_parser().parse_args(["probe", "an-agent"]).limit == probe.DEFAULT_LIMIT == 50
    assert cli.EXIT_FOUND == probe_report.EXIT_FOUND == 2 and "exit 2 on" in helped
