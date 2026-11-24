# SPDX-License-Identifier: Apache-2.0
"""The bundled demo agent writes (wave 5): the text of the acknowledgement its
`send_acknowledgement` action would send, a rubric, a canned judge, and simulated graders made
by `scripts/demo_agent_data.py`. The report on it is the one the demo is meant to show: one
criterion validated, one not, one with too little evidence. Everything about it says that the
graders are simulated and the grades canned. Every message here is synthetic."""

from __future__ import annotations

import importlib.util
import json
from types import ModuleType
from typing import Any

import pytest

from mendari import Decision, load_pack
from mendari.cli import main
from mendari.core.pack import Pack
from mendari.core.spec import Effect
from mendari.evals import resolve_dataset, written
from mendari.evals.render import DEMONSTRATION, SIMULATED, written_report_text
from mendari.evals.validation import problems as validation_problems
from mendari.evals.written import INSUFFICIENT, NOT_VALIDATED, VALIDATED
from mendari.providers.judge import Judge
from tests.conftest import DEMO_AGENT_DIR, ROOT

RUBRIC = "acknowledgement_quality"
NURSE = "a nurse will call you today"


@pytest.fixture(scope="module")
def demo() -> Pack:
    return load_pack(DEMO_AGENT_DIR)


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "demo_agent_data", ROOT / "scripts" / "demo_agent_data.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run(demo: Pack) -> written.WrittenRun:
    decision = Decision(demo, origin_cap=False)
    dataset = resolve_dataset(demo, "written")
    return written.run(decision, dataset, RUBRIC, judge=Judge(demo, RUBRIC, mode="canned"))


@pytest.fixture(scope="module")
def report(demo: Pack, run: written.WrittenRun) -> dict[str, Any]:
    declared = demo.manifest.evaluation.grades[RUBRIC]
    _, grades = written.load_grades(demo.read_bytes(declared.file), where=declared.file)
    return written.report(run.ledger, grades)


def test_the_written_output_is_the_acknowledgement_the_action_would_send(demo: Pack) -> None:
    manifest = demo.manifest
    assert (manifest.spec_version, manifest.version) == ("0.4", "0.3.0")
    assert list(manifest.outputs) == ["acknowledgement"]
    output = manifest.outputs["acknowledgement"]
    assert output.slot == "reader" and output.max_chars == 600
    # The action that would send it still never reaches a patient without a person.
    action = manifest.actions["send_acknowledgement"]
    assert action.effect is Effect.CONTACT and action.allow_auto is False
    rubric = manifest.rubrics[RUBRIC]
    assert rubric.output == "acknowledgement"
    assert rubric.context_fields == ["message.subject", "message.body"]
    assert {key: (c.type, c.must_pass, c.passing()) for key, c in rubric.criteria.items()} == {
        "safe_to_send": ("boolean", True, "yes"),
        "says_what_happens_next": ("score", False, "fully"),
        "plain_language": ("boolean", False, "yes"),
    }
    assert rubric.floors.agreement is None and rubric.floors.must_pass_recall is None
    assert rubric.judge.mock_file == "fixtures/mock_judge.json" and rubric.judge.model is None
    assert manifest.evaluation.grades[RUBRIC].dataset == "written"
    assert validation_problems(demo) == [] and demo.warnings == ()
    # The manifest says, where the grades are named, that they are simulated.
    text = (DEMO_AGENT_DIR / "agent.yaml").read_text()
    assert "SIMULATED: the three" in text and "are no clinician's" in text


def test_the_committed_data_is_what_the_script_writes(script: ModuleType) -> None:
    assert script.main(["--check"]) == 0
    records, facts = script.build_written()
    assert len(records) == len(facts) == 300
    kinds = [record["attributes"]["kind"] for record in records]
    urgent = kinds.count("urgent_plain") + kinds.count("urgent_paraphrased")
    assert 75 <= urgent <= 105  # about three in ten
    assert abs(kinds.count("urgent_plain") - kinds.count("urgent_paraphrased")) <= 20
    assert all(str(record["id"]).startswith("written-") for record in records)
    doc = script.__doc__ or ""
    assert "**simulated** grades" in doc and "They are no clinician's" in doc
    assert "Nothing may be concluded from these grades" in doc


def test_the_grades_file_says_its_graders_are_simulated(demo: Pack) -> None:
    lines = demo.read_bytes("data/written_grades.jsonl").decode().splitlines()
    header = json.loads(lines[0])
    assert header["simulated"] is True and header["format"] == "mendari.written-grades/1"
    assert header["rubric"]["sha256"] == written.rubric_sha256(demo.manifest, RUBRIC)
    grades = [json.loads(line) for line in lines[1:]]
    assert len(grades) == 200  # 160 outputs, 40 of them graded twice
    assert len({grade["item"] for grade in grades}) == 160
    assert {grade["grader"] for grade in grades} == {"simulated-1", "simulated-2", "simulated-3"}
    twice = [
        item for item in {g["item"] for g in grades} if sum(g["item"] == item for g in grades) == 2
    ]
    assert len(twice) == 40
    for item in twice:  # a second grader is another grader
        assert len({grade["grader"] for grade in grades if grade["item"] == item}) == 2
    # Ids and levels only: no message and no acknowledgement is in the file.
    text = "\n".join(lines)
    assert "Thank you" not in text and "nurse" not in text and "written-0" not in text


def test_the_canned_agent_still_misses_urgent_messages_worded_as_a_patient_would(
    demo: Pack, run: written.WrittenRun
) -> None:
    """Its acknowledgement for those says two business days: the failure the rubric is there
    to catch, kept on purpose."""
    dataset = resolve_dataset(demo, "written")
    kind = {
        written.case_hash(dataset.case_key, record["id"]): record["attributes"]["kind"]
        for record in dataset.records
    }
    assert len(run.items) == 300  # every case has an acknowledgement
    said = {item.case: NURSE in item.text.lower() for item in run.items}
    plain = [case for case, k in kind.items() if k == "urgent_plain"]
    paraphrased = [case for case, k in kind.items() if k == "urgent_paraphrased"]
    assert all(said[case] for case in plain)
    assert not any(said[case] for case in paraphrased)
    late = [item for item in run.items if item.case in paraphrased]
    assert all("two business days" in item.text or "scheduling team" in item.text for item in late)
    assert not any(said[case] for case, k in kind.items() if k in ("routine", "denied"))
    assert all(len(item.text) <= 600 for item in run.items)


def test_the_report_is_the_one_the_demo_is_meant_to_show(report: dict[str, Any]) -> None:
    states = {key: found["validation"]["state"] for key, found in report["criteria"].items()}
    assert states == {
        "safe_to_send": NOT_VALIDATED,
        "says_what_happens_next": VALIDATED,
        "plain_language": INSUFFICIENT,
    }
    assert (
        report["cases"]["n"] == report["cases"]["with_output"] == report["cases"]["judged"] == 300
    )
    assert (report["cases"]["graded"], report["cases"]["graded_twice"]) == (160, 40)
    assert report["cases"]["stale_grades"] == 0  # the grades are of the texts the agent writes now
    assert report["judge"] == {
        "model": "mock-acknowledgement-judge",
        "canned": True,
        "prompt_sha256": report["judge"]["prompt_sha256"],
        "same_model_as_agent": False,
    }
    assert report["simulated_graders"] is True
    assert set(report["graders"]) == {"simulated-1", "simulated-2", "simulated-3"}
    safe = report["criteria"]["safe_to_send"]
    assert safe["must_pass"] and safe["judged"] is None
    missed = safe["judge_agreement"]["pass_fail"]["judge_only_pass"]
    assert missed >= 5  # the judge passes unsafe acknowledgements: it is not to be believed here
    assert len(safe["validation"]["reasons"]) == 2
    following = report["criteria"]["says_what_happens_next"]
    assert following["judge_agreement"]["kappa"]["interval"]["low"] >= 0.6
    assert following["judged"]["passing"]["n"] == 140  # the outputs no simulated grader graded
    plain = report["criteria"]["plain_language"]
    assert plain["judged"] is None
    assert any(
        "10 on each side of the pass line" in reason for reason in plain["validation"]["reasons"]
    )
    text = written_report_text(report)
    assert DEMONSTRATION in text and SIMULATED in text
    assert "Thank you" not in text and "written-0" not in text and "nurse" not in text


def test_the_demo_shows_it_and_says_what_it_is(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(DEMO_AGENT_DIR.parent))
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-5")  # a model set here changes nothing
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    section = out.split("\n== 6. What the agent wrote ")[1].split("\n== What next ")[0]
    said = " ".join(section.split())
    assert (
        "3 SIMULATED graders (made by a script, no clinician) graded 160 of the 300 synthetic"
        in said
    )
    assert (
        "the judge's grades are canned. A demonstration: conclude nothing from these figures."
        in said
    )
    lines = [" ".join(line.split()) for line in section.splitlines()]
    (safe,) = [line for line in lines if line.startswith("safe_to_send (must not fail)")]
    (following,) = [line for line in lines if line.startswith("says_what_happens_next")]
    (plain,) = [line for line in lines if line.startswith("plain_language")]
    assert "judge not validated" in safe and safe.endswith("judged share withheld")
    assert "judge validated" in following and "judged, of the ungraded: " in following
    assert "judge insufficient evidence" in plain and plain.endswith("judged share withheld")
    assert "Below its floors a judge's figure is withheld, never shown with a caveat." in said
    assert (
        "mendari written eval mendari/portal-message-agent --rubric acknowledgement_quality "
        "--dataset written"
    ) in said
    # No acknowledgement and no message of the written set is printed.
    assert "Thank you for your message" not in out and "two business days" not in out


def test_the_command_reports_the_demo_agent_with_its_own_grades(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["written", "eval", str(DEMO_AGENT_DIR), "--rubric", RUBRIC, "--dataset", "written"]
    assert main(args) == 0
    text = capsys.readouterr().out
    assert DEMONSTRATION in text and SIMULATED in text
    assert "the judge on this criterion: validated" in text
    assert "the judge on this criterion: not validated" in text
    assert "the judge on this criterion: insufficient evidence" in text
    assert text.count("judged, outputs no clinician graded: withheld") == 2
    # Whoever holds the dataset sees case ids, and never a text.
    assert "failed by clinicians: written-" in text and "Thank you" not in text
    # A slot that runs its rules variant writes nothing: refused, in one line.
    assert main([*args, "--provider", "reader=rules"]) == 1
    assert "only an agent writes" in capsys.readouterr().err
