# SPDX-License-Identifier: Apache-2.0
"""Spec 0.4: the manifest's `outputs`, `rubrics` and `evaluation.grades` (written output,
docs/spec/agent-manifest-0.4.md sections 16 to 19). Each validation rule, the names a refusal
gives, and the rule that a manifest may raise a floor and never lower it. That a 0.4 agent keeps
every 0.3 feature is tests/test_spec_04.py's."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from mendari.core import spec
from mendari.core.spec import (
    AGREEMENT_FLOOR,
    MAX_CRITERIA,
    MAX_LEVELS,
    MAX_OUTPUT_CHARS,
    MAX_OUTPUTS,
    MUST_PASS_RECALL_FLOOR,
    PackManifest,
    Rubric,
    RubricCriterion,
    WrittenOutput,
)
from mendari.evals import written
from tests.conftest import ROOT

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"

SAFE = {
    "type": "boolean",
    "display": "Safe to send",
    "instructions": "Is the reply safe to send as it is written?",
    "criteria": {"yes": "Nothing in it could harm.", "no": "It tells an urgent patient to wait."},
    "must_pass": True,
}
ADDRESSES = {
    "type": "score",
    "instructions": "How fully does the reply say what happens next?",
    "levels": {
        "fully": {"score": 2, "criterion": "It says who acts and when."},
        "not_addressed": {"score": 0, "criterion": "It says nothing about what was asked."},
        "partly": {"score": 1, "criterion": "It acknowledges, and leaves out what happens next."},
    },
    "pass_from": "fully",
}
WRITTEN: dict[str, Any] = {
    "outputs": {
        "reply_draft": {
            "description": "A short reply to the patient that says what happens next.",
            "slot": "reader",
            "max_chars": 1200,
        }
    },
    "rubrics": {
        "reply_quality": {
            "output": "reply_draft",
            "context_fields": ["message.body"],
            "criteria": {"safe_to_send": SAFE, "addresses_request": ADDRESSES},
        }
    },
}


def raw(**changes: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = yaml.safe_load((MINI / "agent.yaml").read_text())
    manifest.pop("actions")
    manifest.update(copy.deepcopy(WRITTEN))
    manifest["evaluation"] = {"datasets": {"written": "data/written.jsonl"}}
    manifest.update(changes)
    return manifest


def rubric(**changes: Any) -> dict[str, Any]:
    manifest = raw()
    manifest["rubrics"]["reply_quality"].update(changes)
    return manifest


def criterion(**changes: Any) -> dict[str, Any]:
    manifest = raw()
    manifest["rubrics"]["reply_quality"]["criteria"]["extra"] = {**copy.deepcopy(SAFE), **changes}
    return manifest


def refused(manifest: dict[str, Any], said: str) -> None:
    with pytest.raises(ValidationError) as error:
        PackManifest.model_validate(manifest)
    assert said in str(error.value), str(error.value)


def test_a_0_4_manifest_declares_outputs_and_rubrics() -> None:
    manifest = PackManifest.model_validate(raw())
    output = manifest.outputs["reply_draft"]
    assert (output.slot, output.input, output.max_chars) == ("reader", None, 1200)
    assert output.source == "slot:reader"
    assert output.request("reply_draft") == {
        "name": "reply_draft",
        "description": "A short reply to the patient that says what happens next.",
        "max_chars": 1200,
    }
    found = manifest.rubrics["reply_quality"]
    assert found.output == "reply_draft" and found.context_fields == ["message.body"]
    safe, addresses = found.criteria["safe_to_send"], found.criteria["addresses_request"]
    assert safe.scale() == [("no", 0.0), ("yes", 1.0)] and safe.passing() == "yes"
    assert safe.must_pass and not addresses.must_pass
    assert safe.passes("yes") and not safe.passes("no") and not safe.passes("maybe")
    # Levels in score order, whatever order the manifest writes them in.
    assert addresses.scale() == [("not_addressed", 0.0), ("partly", 1.0), ("fully", 2.0)]
    assert addresses.passing() == "fully"
    assert [addresses.passes(level) for level, _ in addresses.scale()] == [False, False, True]
    # The engine's floors unless the rubric raises one; the judge's settings are an llm's.
    assert found.agreement_floor() == AGREEMENT_FLOOR == written.AGREEMENT_FLOOR == 0.6
    assert found.must_pass_recall_floor() == MUST_PASS_RECALL_FLOOR == 0.8
    assert found.judge.model is None and found.judge.mock_file is None
    assert found.judge.timeout_ms == 30_000


def test_defaults_and_the_other_source() -> None:
    manifest = raw()
    manifest["outputs"]["given"] = {
        "description": "A reply the input already holds.",
        "input": "draft",
    }
    loaded = PackManifest.model_validate(manifest)
    assert loaded.outputs["given"].max_chars == spec.DEFAULT_OUTPUT_CHARS == 4000
    assert loaded.outputs["given"].source == "input:draft"
    assert Rubric.model_validate({"output": "x", "criteria": {"c": SAFE}}).context_fields == []
    score = RubricCriterion.model_validate({**ADDRESSES, "pass_from": "partly"})
    assert [score.passes(level) for level, _ in score.scale()] == [False, True, True]


@pytest.mark.parametrize(
    ("output", "said"),
    [
        ({"description": "x"}, "exactly one of `slot` and `input`"),
        ({"description": "x", "slot": "reader", "input": "draft"}, "exactly one of"),
        ({"description": "", "slot": "reader"}, "description"),
        ({"description": "   ", "slot": "reader"}, "a description a person can read"),
        ({"description": "x" * 501, "slot": "reader"}, "description"),
        ({"description": "x", "slot": "reader", "max_chars": 0}, "max_chars"),
        ({"description": "x", "slot": "reader", "max_chars": MAX_OUTPUT_CHARS + 1}, "max_chars"),
        ({"description": "x", "input": "a..b"}, "dotted field"),
        ({"description": "x", "input": "a b"}, "dotted field"),
        ({"description": "x", "slot": "reader", "gate": True}, "Extra inputs are not permitted"),
        ({"description": "x", "slot": "nobody"}, "names unknown slot 'nobody'"),
    ],
)
def test_an_output_that_is_not_of_the_sections_shape_is_refused(
    output: dict[str, Any], said: str
) -> None:
    manifest = raw()
    manifest["outputs"]["other"] = output
    refused(manifest, said)


def test_an_output_needs_an_agent_behind_its_slot() -> None:
    """Only an `agent` variant writes: a slot of rules, or one whose agent is a composite's
    member, writes nothing."""
    manifest = raw()
    manifest["providers"]["reader"]["variants"] = {"agent": {"type": "rules", "config": "r.yaml"}}
    refused(manifest, "has no variant of type `agent`")
    manifest = raw()
    member = manifest["providers"]["reader"]["variants"]["agent"]
    manifest["providers"]["reader"]["variants"] = {
        "agent": {"type": "check", "members": {"first": member, "second": dict(member)}}
    }
    refused(manifest, "a composite's member does not count")


def test_names_and_counts() -> None:
    manifest = raw()
    manifest["outputs"]["Reply-Draft"] = {"description": "x", "slot": "reader"}
    refused(manifest, "output name 'Reply-Draft' must match")
    manifest = raw()
    manifest["rubrics"]["Bad Name"] = manifest["rubrics"]["reply_quality"]
    refused(manifest, "rubric name 'Bad Name' must match")
    manifest = raw()
    manifest["rubrics"]["reply_quality"]["criteria"]["Not-A-Key"] = SAFE
    refused(manifest, "criterion key 'Not-A-Key' must match")
    manifest = raw()
    manifest["outputs"] = {
        f"output_{n}": {"description": "x", "slot": "reader"} for n in range(MAX_OUTPUTS + 1)
    } | manifest["outputs"]
    refused(manifest, f"at most {MAX_OUTPUTS} outputs")
    manifest = raw()
    manifest["rubrics"]["reply_quality"]["criteria"] = {
        f"c{n}": SAFE for n in range(MAX_CRITERIA + 1)
    }
    refused(manifest, "criteria")
    most = raw()
    most["rubrics"]["reply_quality"]["criteria"] = {f"c{n}": SAFE for n in range(MAX_CRITERIA)}
    assert len(PackManifest.model_validate(most).rubrics["reply_quality"].criteria) == 16
    refused(rubric(criteria={}), "criteria")
    refused(rubric(output="summary"), "rubrics.reply_quality.output 'summary' is not a declared")
    refused(rubric(context_fields=["message.body", "message.body"]), "more than once")
    refused(rubric(context_fields=["message body"]), "dotted fields of the input")


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"type": "choice", "criteria": {"a": "A.", "b": "B."}}, "never `choice`"),
        ({"pass_from": "yes"}, "takes no `pass_from`: `yes` passes"),
        ({"criteria": {"yes": "Y.", "no": "N.", "maybe": "M."}}, "only has labels yes/no"),
        ({"must_pass": "sometimes"}, "must_pass"),
        ({"weight": 2}, "Extra inputs are not permitted"),
        (
            {"derive": {"from": ["safe_to_send"], "table": [{"else": "yes"}]}},
            "graded, never derived",
        ),
    ],
)
def test_a_yes_no_criterion(changes: dict[str, Any], said: str) -> None:
    refused(criterion(**changes), said)


def levels(*scores: float) -> dict[str, Any]:
    return {f"level_{n}": {"score": score, "criterion": "x"} for n, score in enumerate(scores)}


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"pass_from": None}, "names `pass_from`"),
        ({"pass_from": "excellent"}, "names `pass_from`"),
        ({"levels": levels(0, 1, 1), "pass_from": "level_1"}, "distinct scores"),
        ({"levels": levels(0), "pass_from": "level_0"}, "between 2 and 255 labels"),
        (
            {"levels": levels(*range(MAX_LEVELS + 1)), "pass_from": "level_1"},
            f"2 to {MAX_LEVELS} levels",
        ),
        ({"levels": levels(0, float("inf")), "pass_from": "level_1"}, "finite"),
    ],
)
def test_a_score_criterion(changes: dict[str, Any], said: str) -> None:
    base = {key: value for key, value in ADDRESSES.items() if key != "criteria"}
    manifest = raw()
    manifest["rubrics"]["reply_quality"]["criteria"]["extra"] = {**base, **changes}
    refused(manifest, said)
    most = raw()
    most["rubrics"]["reply_quality"]["criteria"]["extra"] = {
        **base,
        "levels": levels(*range(MAX_LEVELS)),
        "pass_from": "level_3",
    }
    assert (
        len(PackManifest.model_validate(most).rubrics["reply_quality"].criteria["extra"].scale())
        == 7
    )


@pytest.mark.parametrize("floor", ["agreement", "must_pass_recall"])
def test_a_manifest_may_raise_a_floor_and_never_lower_it(floor: str) -> None:
    lowest = {"agreement": AGREEMENT_FLOOR, "must_pass_recall": MUST_PASS_RECALL_FLOOR}[floor]
    for lowered in (0.0, 0.1, lowest - 0.01):
        refused(
            rubric(floors={floor: lowered}),
            f"rubrics.reply_quality.floors.{floor} is below the engine's {lowest}: a manifest "
            "may raise a floor, never lower it",
        )
    for kept in (lowest, 0.9, 0.99):
        loaded = PackManifest.model_validate(rubric(floors={floor: kept})).rubrics["reply_quality"]
        own = loaded.agreement_floor() if floor == "agreement" else loaded.must_pass_recall_floor()
        assert own == kept
    for outside in (1.0, 1.5, -0.2):
        refused(rubric(floors={floor: outside}), floor)
    refused(rubric(floors={"min_paired": 5}), "Extra inputs are not permitted")


def test_the_judge_takes_an_llms_model_settings_and_nothing_of_the_deployments() -> None:
    judge = {"model": "anthropic/claude-sonnet-5", "reasoning_effort": "low", "max_tokens": 512}
    loaded = PackManifest.model_validate(rubric(judge=judge)).rubrics["reply_quality"].judge
    assert (loaded.model, loaded.reasoning_effort, loaded.max_tokens) == (
        "anthropic/claude-sonnet-5",
        "low",
        512,
    )
    for bad, said in (
        ({"model": "no spaces allowed"}, "model"),
        ({"timeout_ms": 5}, "timeout_ms"),
        ({"temperature": 9}, "temperature"),
        ({"base_url_env": "ANYTHING"}, "Extra inputs are not permitted"),
        ({"api_key_env": "ANYTHING"}, "Extra inputs are not permitted"),
        ({"endpoint": "https://example.org"}, "Extra inputs are not permitted"),
    ):
        refused(rubric(judge=bad), said)


def test_grades_name_a_rubric_and_one_of_the_datasets() -> None:
    grades = {"reply_quality": {"dataset": "written", "file": "data/written_grades.jsonl"}}
    manifest = raw()
    manifest["evaluation"]["grades"] = grades
    loaded = PackManifest.model_validate(manifest).evaluation.grades["reply_quality"]
    assert (loaded.dataset, loaded.file) == ("written", "data/written_grades.jsonl")
    manifest = raw()
    manifest["evaluation"]["grades"] = {"other": grades["reply_quality"]}
    refused(manifest, "evaluation.grades.other is not a declared rubric")
    manifest = raw()
    manifest["evaluation"]["grades"] = {"reply_quality": {"dataset": "test", "file": "g.jsonl"}}
    refused(
        manifest, "evaluation.grades.reply_quality.dataset must name one of evaluation.datasets"
    )


@pytest.mark.parametrize("version", ["0.2", "0.3"])
@pytest.mark.parametrize("section", ["outputs", "rubrics", "evaluation.grades"])
def test_an_older_manifest_is_refused_naming_the_key(version: str, section: str) -> None:
    manifest = raw(spec_version=version)
    manifest["evaluation"]["grades"] = {
        "reply_quality": {"dataset": "written", "file": "data/written_grades.jsonl"}
    }
    with pytest.raises(ValidationError) as error:
        PackManifest.model_validate(manifest)
    assert "['outputs', 'rubrics', 'evaluation.grades'] use spec 0.4 features" in str(error.value)
    assert "set spec_version: '0.4'" in str(error.value)
    # Only that section used: it alone is named.
    only = raw(spec_version=version)
    if section == "outputs":
        only.pop("rubrics")
    elif section == "rubrics":
        only.pop("outputs")
    else:
        only.pop("rubrics")
        only.pop("outputs")
        only["evaluation"]["grades"] = {"q": {"dataset": "written", "file": "g.jsonl"}}
    with pytest.raises(ValidationError) as error:
        PackManifest.model_validate(only)
    assert f"['{section}'] use spec 0.4 features" in str(error.value)
    # Empty mappings are what a dumped manifest carries: they count as left out.
    empty = raw(spec_version=version, outputs={}, rubrics={})
    empty["evaluation"]["grades"] = {}
    older = PackManifest.model_validate(empty)
    assert older.outputs == {} and older.rubrics == {} and older.evaluation.grades == {}


def test_a_manifest_loads_again_from_its_own_dump() -> None:
    manifest = raw()
    manifest["rubrics"]["reply_quality"]["floors"] = {"agreement": 0.7}
    manifest["rubrics"]["reply_quality"]["judge"] = {"mock_file": "fixtures/mock_judge.json"}
    loaded = PackManifest.model_validate(manifest)
    dumped = loaded.model_dump(mode="json", by_alias=True)
    assert PackManifest.model_validate(dumped) == loaded
    assert dumped["outputs"]["reply_draft"]["input"] is None
    assert (
        WrittenOutput.model_validate(dumped["outputs"]["reply_draft"])
        == loaded.outputs["reply_draft"]
    )


def test_a_manifest_may_lower_the_ceiling_on_judge_errors_and_never_raise_it() -> None:
    """The share of outputs a judge may fail to grade is a ceiling: tightening it is lowering
    it, the other way round from the two floors."""
    assert spec.MAX_JUDGE_ERRORS == written.MAX_JUDGE_ERRORS == 0.05
    default = PackManifest.model_validate(raw()).rubrics["reply_quality"]
    assert default.max_judge_errors() == 0.05
    for kept in (0.0, 0.01, 0.05):
        loaded = PackManifest.model_validate(rubric(floors={"max_judge_errors": kept}))
        assert loaded.rubrics["reply_quality"].max_judge_errors() == kept
    for raised in (0.051, 0.2, 1.0):
        refused(
            rubric(floors={"max_judge_errors": raised}),
            "rubrics.reply_quality.floors.max_judge_errors is above the engine's 0.05: a "
            "manifest may lower this ceiling, never raise it",
        )
    for outside in (-0.1, 1.5):
        refused(rubric(floors={"max_judge_errors": outside}), "max_judge_errors")
