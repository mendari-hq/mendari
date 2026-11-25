# SPDX-License-Identifier: Apache-2.0
"""Malformed agent content is named, never a 500 or a traceback: behaviour tests of the wrong
shape, strings and values JSON cannot hold anywhere in an agent's YAML or JSON, unreadable or
malformed provider files, files edited after the load, malformed attestations and dataset lines
(core/agent.py, evals/behaviour_tests.py, evals/harness.py, core/card.py, evals/leaderboard.py)."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import load_agent
from mendari.cli import main
from mendari.core.decision import Decision
from mendari.errors import AgentValidationError, MendariError
from mendari.evals.behaviour_tests import load_cases
from mendari.evals.harness import load_dataset
from mendari.server import api_docs
from mendari.server.app import create_app
from tests.conftest import TRIAGE_DIR
from tests.support.engine import published
from tests.test_agent_isolation import (
    AGENTS,
    TRIAGE_ID,
    _append,
    _outsider,
    _set_key,
    _set_line,
    _shipped,
    _with_schema_property,
)

# An outsider's copy of the triage fixture (`_outsider`), and an input of its shape.
SOURCE = TRIAGE_DIR
OUTSIDER = "outsider/triage"
INPUT = "{message: {body: hi}}"


def _no_traceback(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert main(list(argv)) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    return err


def _client(tmp_path: Path, outside: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "m.db", agents_paths=[outside, AGENTS]))


# -- behaviour tests of the wrong shape (finding 1) ---------------------------------------------


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        ("- 5\n", "not a mapping"),
        (f"- name: z\n  input: {INPUT}\n  expect: [1]\n", "`expect` must be a mapping"),
        (f"- name: z\n  input: {INPUT}\n  expect: AUTO\n", "`expect` must be a mapping"),
        (f"- name: z\n  input: {INPUT}\n  expect: {{answers: 5}}\n", "`expect.answers` must"),
        (f"- name: z\n  input: {INPUT}\n  expect: {{outcome_in: 5}}\n", "`expect.outcome_in`"),
        (f"- name: 5\n  input: {INPUT}\n  expect: {{outcome: AUTO}}\n", "`name` must be text"),
        ("- name: z\n  input: [1]\n", "`input` must be a mapping"),
        ("- name: z\n", "`input` must be a mapping"),
        # Strict since LP-15: a test that cannot fail is refused, named.
        (f"- name: z\n  input: {INPUT}\n", "no `expect`: say what the agent must decide"),
        (f"- name: z\n  input: {INPUT}\n  expect: {{}}\n", "`expect` is empty"),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{outcom: AUTO}}\n",
            "unknown `expect` key 'outcom' (allowed: outcome, outcome_in",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{outcome: AUTO}}\n  providers: {{}}\n",
            "unknown key 'providers' (a case has `name`, `input` and `expect`)",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{answers: {{}}}}\n",
            "`expect.answers` is empty",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{outcome_in: []}}\n",
            "`expect.outcome_in` is empty",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{not_outcome: auto}}\n",
            "outcome 'auto' is not one of AUTO, HUMAN_REVIEW, ABSTAIN, BLOCK",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{priority: high}}\n",
            "priority 'high' is not one",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{recommendation: nowhere}}\n",
            "recommendation 'nowhere' is not a label of",
        ),
        (
            f"- name: z\n  input: {INPUT}\n  expect: {{answers: {{nosuch: x}}}}\n",
            "`expect.answers` names 'nosuch', which is not one of the agent's questions",
        ),
    ],
    ids=[
        "not-a-mapping",
        "expect-list",
        "expect-text",
        "answers-not-mapping",
        "outcome-in-not-list",
        "name-not-text",
        "input-list",
        "input-missing",
        "expect-missing",
        "expect-empty",
        "expect-key-typo",
        "case-key-unknown",
        "answers-empty",
        "outcome-in-empty",
        "not-outcome-typo",
        "priority-unknown",
        "recommendation-unknown",
        "answers-unknown-question",
    ],
)
def test_a_behaviour_test_of_the_wrong_shape_is_named_never_a_500(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, problem: str
) -> None:
    """`expect: [1]` made /openapi.json answer 500 for every agent (AttributeError while picking
    the example) and `mendari validate` print a traceback; `- 5` and `expect: {answers: 5}`
    made `mendari test` print one."""
    outside = tmp_path / "outside"
    count = len(yaml.safe_load((SOURCE / "tests" / "cases.yaml").read_text()))
    agent = _outsider(outside, _append("tests/cases.yaml", "\n" + case), SOURCE)
    with pytest.raises(AgentValidationError) as raised:
        load_cases(load_agent(agent))
    [found] = raised.value.problems
    # A case is named by its number, and by its name when it has one (text).
    named = " (z)" if "name: z" in case else ""
    assert found.startswith(f"tests/cases.yaml case {count + 1}{named}: {problem}")
    assert found in _no_traceback(capsys, "validate", str(agent))
    assert found in _no_traceback(capsys, "test", str(agent))
    client = _client(tmp_path, outside)
    reference = client.get("/openapi.json")
    assert reference.status_code == 200
    decide = reference.json()["paths"]["/api/agents/{namespace}/{name}/decide"]["post"]
    examples = decide["requestBody"]["content"]["application/json"]["examples"]
    assert examples["triage"]["summary"].startswith(f"{TRIAGE_ID}: ")  # the fixture's own
    assert client.get(f"/api/agents/{OUTSIDER}/examples").status_code == 422


def test_one_agents_tests_can_never_fail_the_api_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example is chosen per agent under a guard: whatever one agent's tests hold, the API
    reference is served with the other agents' examples."""
    real = api_docs.load_cases

    def odd(agent: object) -> object:
        if getattr(agent, "id", "") == "mendari-test/clinical-review":
            return [{"input": {}, "expect": [1]}]
        return real(agent)  # type: ignore[arg-type]

    monkeypatch.setattr(api_docs, "load_cases", odd)
    client = TestClient(create_app(db_path=tmp_path / "m.db", agents_paths=[AGENTS]))
    reference = client.get("/openapi.json")
    assert reference.status_code == 200
    decide = reference.json()["paths"]["/api/agents/{namespace}/{name}/decide"]["post"]
    examples = decide["requestBody"]["content"]["application/json"]["examples"]
    assert "triage" in examples and "clinical-review" not in examples


# -- values JSON cannot hold, anywhere in an agent's YAML or JSON (finding 2) ---------------------

SURROGATE = "a string with a lone surrogate, which is not UTF-8 text"


def _tagged(tag: str) -> str:
    return f"a !!{tag} value, which JSON cannot hold"


@pytest.mark.parametrize(
    ("relative", "line", "column", "reason"),
    [
        ("agent.yaml", 'name: "Triage \\ud800"', 7, SURROGATE),
        ("agent.yaml", '"x_\\udfff": 1', 1, SURROGATE),
        ("agent.yaml", "x_blob: !!binary /w==", 9, _tagged("binary")),
        ("agent.yaml", "x_set: !!set {a}", 8, _tagged("set")),
        ("agent.yaml", "x_omap: !!omap [a: 1]", 9, _tagged("omap")),
        ("agent.yaml", "x_pairs: !!pairs [a: 1]", 10, _tagged("pairs")),
        ("logic/router_rules.yaml", 'x_name: "\\ud800"', 9, SURROGATE),
    ],
    ids=[
        "manifest-name",
        "manifest-key",
        "binary",
        "set",
        "omap",
        "pairs",
        "rules",
    ],
)
def test_a_yaml_value_json_cannot_hold_is_refused_at_load_with_its_place(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    relative: str,
    line: str,
    column: int,
    reason: str,
) -> None:
    """A manifest name with a lone surrogate made GET /api/agents answer 500, so the list of every
    agent failed; a variant's `!!binary` extra and a rule's surrogate name made the agent's detail
    and decisions 500. `mendari validate` said ok. Refused at load, by file and place."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, _set_line(relative, line), SOURCE)
    at = (agent / relative).read_text(encoding="utf-8").splitlines().index(line) + 1
    problem = f"{relative}: {reason} (line {at}, column {column})"
    with pytest.raises(AgentValidationError) as raised:
        load_agent(agent)
    assert raised.value.problems == [problem]
    assert problem in _no_traceback(capsys, "validate", str(agent))
    client = _client(tmp_path, outside)
    listed = client.get("/api/agents")
    assert listed.status_code == 200
    assert OUTSIDER not in [p["id"] for p in listed.json()]
    assert len(listed.json()) == len(_shipped())  # every fixture's agent, not the outsider
    assert problem in client.get("/api/health").json()["invalid"][str(agent.resolve())]


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("agent.yaml", "risk:\n  level: 3\n  category: x\n  rationale: a second risk\n"),
        ("logic/router_rules.yaml", "negation_window: 40\n"),
        ("fhir/mapping.yaml", "focus: ServiceRequest\n"),
    ],
    ids=["manifest", "rules", "mapping"],
)
def test_a_key_given_twice_is_refused_at_load_with_both_places(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relative: str, text: str
) -> None:
    """The safe loader kept the last of two equal keys, silently: a manifest could show `risk:`
    near its top and be decided by a second `risk:` further down, and a reviewer reading the
    first was misled. Refused, by file and the line and column of both."""
    key = text.split(":", 1)[0]
    agent = _outsider(tmp_path, _append(relative, "\n" + text), SOURCE)
    lines = (agent / relative).read_text(encoding="utf-8").splitlines()
    first, second = [n + 1 for n, line in enumerate(lines) if line.startswith(f"{key}:")]
    problem = (
        f"{relative}: a key given twice (line {second}, column 1; first at line {first}, column 1)"
    )
    with pytest.raises(AgentValidationError) as raised:
        if relative == "fhir/mapping.yaml":  # an owned agent's mapping is read where it is used
            from mendari.standards.fhir import FhirAdapter

            FhirAdapter.for_agent(load_agent(agent))
        else:
            load_agent(agent)
    assert raised.value.problems == [problem]
    assert problem in _no_traceback(capsys, "validate", str(agent))


def test_a_merged_key_may_be_set_again_and_equal_keys_in_two_mappings_are_two_keys() -> None:
    """YAML's merge (`<<`) gives defaults the mapping may override: not a key given twice."""
    from mendari.core.agent import parse_yaml

    text = b"base: &b {level: 1, note: a}\nother:\n  <<: *b\n  level: 2\nlevel: 3\n"
    assert parse_yaml(text, "x.yaml", "x") == {
        "base": {"level": 1, "note": "a"},
        "other": {"level": 2, "note": "a"},
        "level": 3,
    }
    with pytest.raises(AgentValidationError) as raised:
        parse_yaml(b"a:\n  b: 1\n  c: 2\n  b: 3\n", "x.yaml", "x")
    assert raised.value.problems == [
        "x.yaml: a key given twice (line 4, column 3; first at line 2, column 3)"
    ]


@pytest.mark.parametrize(
    ("edit", "relative", "what"),
    [
        (_with_schema_property({"x-d": "\ud800"}), "schemas/input.schema.json", "lone surrogate"),
        (
            _with_schema_property({"\udfff": 1}),
            "schemas/input.schema.json",
            "lone surrogate",
        ),
        (
            _with_schema_property({"type": "number", "maximum": float("nan")}),
            "schemas/input.schema.json",
            "a number that is not finite",
        ),
    ],
    ids=["schema-value", "schema-key", "schema-nan"],
)
def test_a_json_value_json_cannot_send_is_refused_at_load(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[Path], None],
    relative: str,
    what: str,
) -> None:
    """`"x-d": "\\ud800"` in the input schema (json.load accepts the escape) made the agent's
    detail answer 500."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, edit, SOURCE)
    with pytest.raises(AgentValidationError) as raised:
        load_agent(agent)
    [problem] = raised.value.problems
    assert problem.startswith(f"{relative}: not JSON, it holds ") and what in problem
    assert problem in _no_traceback(capsys, "validate", str(agent))
    client = _client(tmp_path, outside)
    assert client.get("/api/agents").status_code == 200
    assert client.get(f"/api/agents/{OUTSIDER}").status_code == 404


# -- provider files: malformed, of the wrong shape, unreadable (finding 3) ----------------------

# The canned answers of the triage fixture's router `agent` variant, which these copies make the
# default, so every caller builds it.
MODEL = "fixtures/router.json"


def _router_agent(agent: Path) -> None:
    manifest = yaml.safe_load((agent / "agent.yaml").read_text())
    manifest["providers"]["router"]["use"] = "agent"
    (agent / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def _model(change: Callable[[dict[str, object]], object]) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        _router_agent(agent)
        path = agent / MODEL
        model = json.loads(path.read_text())
        path.write_text(json.dumps(change(model)))

    return edit


def _raw_model(text: str) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        _router_agent(agent)
        (agent / MODEL).write_text(text)

    return edit


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (_raw_model("{"), f"{MODEL}: not a JSON document (JSONDecodeError)"),
        (_raw_model("9" * 5000), f"{MODEL}: not a JSON document (ValueError)"),
        (_raw_model("[" * 100_000), f"{MODEL}: not a JSON document (RecursionError)"),
        (_raw_model('"\\ud800"'), f"{MODEL}: not JSON, it holds a string with a lone surrogate"),
        (
            _model(lambda m: {**m, "x_count": 10**400}),
            f"{MODEL}: not JSON, it holds an integer too large to be a number",
        ),
        (_raw_model("[]"), f"{MODEL}: mock file must be a mapping"),
        (_model(lambda m: {**m, "x_extra": 1}), f"{MODEL}: unexpected mock file keys"),
        (_model(lambda m: {"model": m["model"]}), f"{MODEL}: mock file needs `cases` or `default`"),
        (_model(lambda m: {**m, "cases": 5}), f"{MODEL}: `cases` must be a list"),
    ],
    ids=[
        "not-json",
        "long-integer",
        "too-deep",
        "surrogate",
        "huge-integer",
        "list",
        "extra-key",
        "no-answers",
        "cases-not-list",
    ],
)
def test_a_malformed_model_file_is_named_never_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[Path], None],
    problem: str,
) -> None:
    """An agent variant's canned answers (its mock file) as `{`, `[]`, a 5,000-digit integer or
    100,000 nested `[`: named by `mendari validate`, `mendari test` and the agent's detail, never
    a traceback."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, edit, SOURCE)
    assert problem in _no_traceback(capsys, "validate", str(agent))
    assert problem in _no_traceback(capsys, "test", str(agent))
    client = _client(tmp_path, outside)
    detail = client.get(f"/api/agents/{OUTSIDER}")
    assert detail.status_code == 422 and problem in detail.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_while_hashing_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mode-000 file in the agent made `mendari validate` print a PermissionError traceback
    from the digest."""
    outside = tmp_path / "outside"
    agent = _outsider(outside, lambda p: None, SOURCE)
    locked = agent / "logic" / "notes.txt"
    locked.write_text("x")
    locked.chmod(0)
    try:
        with pytest.raises(AgentValidationError) as raised:
            load_agent(agent)
        assert raised.value.problems == ["logic/notes.txt: cannot be read (PermissionError)"]
        assert "logic/notes.txt: cannot be read" in _no_traceback(capsys, "validate", str(agent))
        assert _client(tmp_path, outside).get("/api/agents").status_code == 200
    finally:
        locked.chmod(0o644)


# -- a file edited in place after the load (finding 4) -------------------------------------------


def _edit_in_place(path: Path, old: str, new: str) -> None:
    before = path.stat().st_ino
    with path.open("r+", encoding="utf-8") as handle:
        text = handle.read()
        assert old in text
        handle.seek(0)
        handle.write(text.replace(old, new))
        handle.truncate()
    assert path.stat().st_ino == before  # the same file on disk: only its content changed


CHANGED = "logic/screen_rules.yaml: changed since the agent's digest was computed"
# The screen's chest pain pattern, and the same misspelled, so it no longer matches.
CHEST, CHEST_MISSPELLED = "chest (pain|pressure|tightness)", "chest (pian|pressure|tightness)"


def test_a_file_edited_in_place_after_the_load_is_refused_not_used(tmp_path: Path) -> None:
    """Rewriting a rules file in place (same inode) between load and first use changed
    decisions while they carried the digest computed at load. Every read of an agent file checks
    its content against the digest's hash of that file, so an agent edited after its load is
    refused when it is built; the server builds every agent at start-up (LP-16d), so a file
    edited after that is never read, and its decisions stay the digested content's."""
    outside = tmp_path / "outside"
    agent_dir = _outsider(outside, lambda p: None, SOURCE)
    agent = load_agent(agent_dir)
    _edit_in_place(agent_dir / "logic" / "screen_rules.yaml", CHEST, CHEST_MISSPELLED)
    with pytest.raises(AgentValidationError) as raised:
        Decision(agent)
    assert raised.value.problems[0].startswith(CHANGED)
    # The server: built at start, edited before the next request. The edit would change this
    # case's decision for an agent built from it; the server keeps deciding as it loaded it.
    client = _client(tmp_path, outside)
    case = yaml.safe_load((SOURCE / "tests" / "cases.yaml").read_text())[0]

    def decided() -> Any:
        response = client.post(f"/api/agents/{OUTSIDER}/decide", json=case["input"])
        assert response.status_code == 200, response.text
        body = response.json()
        return body["outcome"], body["answers"], body["agent"]["digest"]

    before = decided()
    _edit_in_place(agent_dir / "logic" / "screen_rules.yaml", CHEST_MISSPELLED, CHEST)
    rebuilt = Decision(load_agent(agent_dir)).decide(case["input"]).to_dict()
    assert (rebuilt["outcome"], rebuilt["answers"]) != before[:2]  # the edit would matter
    assert decided() == before  # and is not used
    unedited = client.post(f"/api/agents/{TRIAGE_ID}/decide", json=case["input"])
    assert unedited.status_code == 200


def test_a_rules_file_edited_in_place_is_refused_though_its_parse_is_kept(tmp_path: Path) -> None:
    """A rules file is parsed at load; its fingerprint is read later. Both must be the digested
    bytes, or the provider's recorded model would describe other rules."""
    agent_dir = tmp_path / "agent"
    shutil.copytree(SOURCE, agent_dir)
    agent = load_agent(agent_dir)
    _edit_in_place(agent_dir / "logic" / "router_rules.yaml", "# Keyword", "# keyword")
    with pytest.raises(AgentValidationError, match=r"router_rules\.yaml: changed since"):
        Decision(agent)


def test_a_file_edited_while_the_agent_loads_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between hashing and parsing: the parse reads the bytes it checks, never the file again."""
    from mendari.core import agent as agent_module

    agent_dir = tmp_path / "agent"
    shutil.copytree(SOURCE, agent_dir)
    real = agent_module._digest

    def digest_then_edit(root: Path, label: str = "") -> object:
        found = real(root, label)
        _edit_in_place(agent_dir / "agent.yaml", "primary:", "primary: ")
        return found

    monkeypatch.setattr(agent_module, "_digest", digest_then_edit)
    with pytest.raises(AgentValidationError, match=r"agent\.yaml: changed since"):
        load_agent(agent_dir)


# -- attestations: the benchmark, the leaderboard and the changelog (finding 5) -----------------


def test_the_benchmark_must_be_in_evals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`evaluation.benchmark: DECISION_CARD.md` loaded and validated, then `mendari card` printed
    a JSONDecodeError traceback and the agent's detail answered 500."""
    agent = _outsider(
        tmp_path,
        _set_key("agent.yaml", "evaluation", {"benchmark": "DECISION_CARD.md"}),
        SOURCE,
    )
    problem = "evaluation.benchmark: path 'DECISION_CARD.md' is not in evals/"
    with pytest.raises(AgentValidationError) as raised:
        load_agent(agent)
    assert raised.value.problems == [problem]
    assert problem in _no_traceback(capsys, "card", str(agent))


def _write_eval(name: str, text: str) -> Callable[[Path], None]:
    def edit(agent: Path) -> None:
        (agent / "evals" / name).write_text(text)

    return edit


def _huge_figure(name: str) -> Callable[[Path], None]:
    """The shipped attestation with one accuracy that is too large to be a float: valid in
    shape, so only the number check refuses it (it made render's percentage overflow)."""

    def edit(agent: Path) -> None:
        path = agent / "evals" / name
        data = json.loads(path.read_text())
        if name == "benchmark.json":
            data["questions"][data["primary"]]["accuracy"] = 10**400
        else:
            data["rows"][0]["accuracy"] = 10**400
        path.write_text(json.dumps(data))

    return edit


def _agent_block(value: Any) -> Callable[[Path], None]:
    """The shipped benchmark with its `agent` block replaced (`...`: removed). The card reads it
    for the version it was measured on, after the shape check that left it out."""

    def edit(agent: Path) -> None:
        path = agent / "evals" / "benchmark.json"
        data = json.loads(path.read_text())
        if value is ...:
            del data["agent"]
        else:
            data["agent"] = value
        path.write_text(json.dumps(data))

    return edit


def _run_lifted(agent: Path) -> None:
    """The shipped benchmark with its run's id, time and latency at its top level and no `run`
    block: a shape no release writes, so not a report the card reads."""
    path = agent / "evals" / "benchmark.json"
    data = json.loads(path.read_text())
    run = data.pop("run")
    data |= {key: run[key] for key in ("id", "created_at", "latency_ms")}
    path.write_text(json.dumps(data))


def _sweep_uncounted(agent: Path) -> None:
    """The shipped benchmark with its threshold sweep's rows without the cases each is of
    (`scored`, and `chosen` at or above its threshold), which every row a release writes holds."""
    path = agent / "evals" / "benchmark.json"
    data = json.loads(path.read_text())
    for row in data["threshold_sweep"]:
        del row["chosen"], row["scored"]
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("edit", "absent"),
    [
        (_write_eval("benchmark.json", "{"), "benchmark"),
        (_agent_block(...), "benchmark"),
        (_agent_block(5), "benchmark"),
        (_run_lifted, "benchmark"),
        (_sweep_uncounted, "benchmark"),
        (_write_eval("benchmark.json", '{"primary": NaN}'), "benchmark"),
        (_write_eval("benchmark.json", "[]"), "benchmark"),
        (_write_eval("benchmark.json", '{"primary": "specialty"}'), "benchmark"),
        (_write_eval("benchmark.json", '{"created_at": "\\ud800"}'), "benchmark"),
        (_huge_figure("benchmark.json"), "benchmark"),
        (_write_eval("leaderboard.json", "{"), "leaderboard"),
        (_huge_figure("leaderboard.json"), "leaderboard"),
        (_write_eval("leaderboard.json", '{"rows": [5]}'), "leaderboard"),
        (_write_eval("leaderboard.json", '{"rows": [{"dataset": "x"}]}'), "leaderboard"),
    ],
    ids=[
        "benchmark-not-json",
        "benchmark-without-agent",
        "benchmark-agent-not-object",
        "benchmark-without-run",
        "benchmark-sweep-without-counts",
        "benchmark-nan",
        "benchmark-list",
        "benchmark-shape",
        "benchmark-surrogate",
        "benchmark-huge-integer",
        "leaderboard-not-json",
        "leaderboard-huge-integer",
        "leaderboard-rows",
        "leaderboard-row-shape",
    ],
)
def test_a_malformed_attestation_is_absent_with_its_problem_noted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: Callable[[Path], None], absent: str
) -> None:
    """A benchmark or leaderboard that is not JSON, holds NaN or has the wrong shape made the
    agent's detail answer 500 and `mendari card` print a traceback. Like the registry, Mendari
    now treats it as absent, and the card says why. The outsider is a copy of the triage fixture
    published with its benchmark and a board (`published`)."""
    outside = tmp_path / "outside"
    source = published(TRIAGE_DIR, tmp_path / "published", runs=[{"router": "agent"}]).root
    agent = _outsider(outside, edit, source)
    client = _client(tmp_path, outside)
    detail = client.get(f"/api/agents/{OUTSIDER}")
    assert detail.status_code == 200, detail.text
    assert detail.json()[absent] is None
    assert f"evals/{absent}.json is not shown: " in detail.json()["card_markdown"]
    assert main(["card", str(agent)]) == 0
    assert f"evals/{absent}.json is not shown" in capsys.readouterr().out


def test_a_changelog_that_is_not_utf8_is_a_422_never_a_500(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CHANGELOG.md that is not UTF-8 made the agent's detail answer 500 (as the registry
    refuses a card that is not UTF-8 text, it is the agent's problem)."""
    outside = tmp_path / "outside"

    def edit(agent: Path) -> None:
        (agent / "CHANGELOG.md").write_bytes(b"# Changes\n\xff\n")

    agent = _outsider(outside, edit, SOURCE)
    client = _client(tmp_path, outside)
    detail = client.get(f"/api/agents/{OUTSIDER}")
    assert detail.status_code == 422 and "CHANGELOG.md: not UTF-8 text" in detail.text
    assert "CHANGELOG.md: not UTF-8 text" in _no_traceback(capsys, "card", str(agent))


# -- dataset lines (finding 7) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        (b'{"input": {"a": ' + b"9" * 5000 + b"}}", "invalid JSON (ValueError)"),
        (b'{"input": {"a": "\xff"}}', "not UTF-8 text"),
        (b"[" * 100_000 + b"]" * 100_000, "invalid JSON (RecursionError)"),
        (b'{"input": {"a": "\\ud800"}}', "not JSON, it holds a string with a lone surrogate"),
        (b'{"input": {"a": NaN}}', "not JSON, it holds a number that is not finite"),
    ],
    ids=["long-integer", "not-utf8", "too-deep", "surrogate", "nan"],
)
def test_a_bad_dataset_line_is_named_by_file_and_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: bytes, problem: str
) -> None:
    """One bad line in data/test.jsonl made GET .../datasets answer 500 and `mendari eval`
    raise uncaught (ValueError, UnicodeDecodeError, RecursionError)."""
    outside = tmp_path / "outside"

    def edit(agent: Path) -> None:
        path = agent / "data" / "test.jsonl"
        path.write_bytes(path.read_bytes() + line + b"\n")

    agent = _outsider(outside, edit, SOURCE)
    number = (agent / "data" / "test.jsonl").read_bytes().count(b"\n")
    where = f"data/test.jsonl:{number}: {problem}"
    with pytest.raises(MendariError, match=f"{number}: "):
        load_dataset(agent / "data" / "test.jsonl")
    client = _client(tmp_path, outside)
    datasets = client.get(f"/api/agents/{OUTSIDER}/datasets")
    assert datasets.status_code in (400, 422) and where in datasets.text
    err = _no_traceback(capsys, "eval", str(agent), "--dataset", "test", "--baseline", "none")
    assert where in err
