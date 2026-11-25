# SPDX-License-Identifier: Apache-2.0
"""`mendari probe` (`mendari.evals.probe`): it rewrites an agent's own cases in ways that keep
their meaning, finds the variants the agent answers otherwise, ranks them by harm and writes each
as a behaviour test. Every agent here is built in a temporary directory or is a copy of the
bundled demo agent: nothing is written into the repository."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import Decision, load_pack
from mendari.core import pack_store, patterns
from mendari.core.pack import pack_digest
from mendari.errors import MendariError, PackValidationError
from mendari.evals import probe, probe_report, probe_tests, probe_variants
from mendari.evals import probe_transforms as transforms
from mendari.evals.pack_tests import (
    PROBE_TESTS,
    PROBE_UNCONFIRMED,
    every_case,
    load_cases,
    probe_cases,
    run_pack_tests,
)
from mendari.evals.probe import Options, Probed
from mendari.evals.validation import problems, validate
from mendari.providers.agent import AgentProvider
from tests.conftest import DEMO_AGENT_DIR
from tests.probe_agents import CASES, MANIFEST, TESTS, build_agent, message_fields, write_rules


@pytest.fixture(autouse=True)
def _the_bundled_agent_is_as_it_was() -> Iterator[None]:
    """A probe writes into an agent (`--write`): no test may leave the repository's own agent
    changed (tasks/lessons.md, 2026-11-10)."""
    before = pack_digest(DEMO_AGENT_DIR)
    yield
    assert pack_digest(DEMO_AGENT_DIR) == before, "a test changed agents/portal-message-agent"


def found_test(number: int = 1, **changes: Any) -> dict[str, Any]:
    """A regression test as the probe writes one: a behaviour test, and where it came from."""
    case: dict[str, Any] = {
        "name": f"typos variant of cases case symptom (probe-{number:012d})",
        "input": {"message": {"body": "Bad chset pain now"}},
        "expect": {"answers": {"urgent": "yes"}, "not_outcome": "AUTO"},
        "probe": {
            "id": f"probe-{number:012d}",
            "found_by": "mendari probe",
            "transformation": "typos",
            "seed": 0,
            "original": {"source": "cases", "case": "symptom"},
        },
    }
    case.update(changes)
    return case


def write_yaml(root: Path, relative: str, value: Any) -> None:
    (root / relative).write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


# -- the tests the probe wrote are behaviour tests -------------------------------------------------


def test_the_probes_tests_run_after_the_agents_own(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    write_yaml(root, PROBE_TESTS, [found_test(1), found_test(2, name="another (probe-2)")])
    pack = load_pack(root)
    # The agent's own tests are still what its manifest names, and nothing else.
    assert [case["name"] for case in load_cases(pack)] == [case["name"] for case in TESTS]
    assert [case["probe"]["id"] for case in probe_cases(pack)] == [
        "probe-000000000001",
        "probe-000000000002",
    ]
    assert [(file, number) for file, number, _ in every_case(pack)] == [
        ("tests/cases.yaml", 1),
        ("tests/cases.yaml", 2),
        (PROBE_TESTS, 1),
        (PROBE_TESTS, 2),
    ]
    outcomes = run_pack_tests(Decision(pack, origin_cap=False))
    assert [outcome.passed for outcome in outcomes] == [True, True, False, False]
    # The weak agent misses the mistyped symptom: the test says what it expected and got.
    assert outcomes[2].failures == ("answers.urgent: expected 'yes', got 'no'",)


def test_a_fixed_agent_passes_the_probes_tests(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent", robust=True)
    write_yaml(root, PROBE_TESTS, [found_test()])
    outcomes = run_pack_tests(Decision(load_pack(root), origin_cap=False))
    assert [outcome.passed for outcome in outcomes] == [True, True, True]


def test_an_agent_with_no_tests_of_its_own_runs_the_probes(tmp_path: Path) -> None:
    manifest = {key: value for key, value in MANIFEST.items() if key != "tests"}
    root = build_agent(tmp_path / "agent")
    write_yaml(root, "agent.yaml", manifest)
    write_yaml(root, PROBE_TESTS, [found_test()])
    pack = load_pack(root)
    assert load_cases(pack) == []
    assert [outcome.passed for outcome in run_pack_tests(Decision(pack, origin_cap=False))] == [
        False
    ]


def test_without_a_probe_file_the_tests_are_the_agents_own(tmp_path: Path) -> None:
    pack = load_pack(build_agent(tmp_path / "agent"))
    assert probe_cases(pack) == []
    assert [case for _, _, case in every_case(pack)] == load_cases(pack)


def test_a_test_waiting_to_be_confirmed_is_not_run(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    write_yaml(root, PROBE_UNCONFIRMED, [found_test()])  # it would fail, if it ran
    pack = load_pack(root)
    assert probe_cases(pack) == []
    assert [o.passed for o in run_pack_tests(Decision(pack, origin_cap=False))] == [True, True]
    validate(pack)  # and it is not held to anything until a person confirms it


@pytest.mark.parametrize(
    ("written", "problem"),
    [
        ({"not": "a list"}, "tests/probe.yaml: expected a list of cases"),
        (
            [{"name": "x", "input": {}, "expect": {"outcome": "AUTO"}}],
            "tests/probe.yaml case 1 (x): no `probe`: a test in this file says where "
            "`mendari probe` found it (its `id` at least)",
        ),
        ([found_test(probe="typos")], "`probe` must be a mapping with an `id`"),
        ([found_test(probe={"id": 7})], "`probe` must be a mapping with an `id`"),
        ([found_test(probe={"transformation": "typos"})], "`probe` must be a mapping with an `id`"),
        ([found_test(), found_test()], "case 2 (typos variant of cases case symptom "),
        ([found_test(expect={})], "`expect` is empty"),
        ([found_test(expect={"answers": {"urgent": "maybe"}})], "not one of its labels (yes, no)"),
        ([found_test(note="mine")], "unknown key 'note' (a case has `name`, `input`, `expect`"),
    ],
)
def test_a_probe_file_that_is_not_one_is_refused_by_name(
    tmp_path: Path, written: Any, problem: str
) -> None:
    root = build_agent(tmp_path / "agent")
    write_yaml(root, PROBE_TESTS, written)
    with pytest.raises(PackValidationError) as refused:
        run_pack_tests(Decision(load_pack(root), origin_cap=False))
    assert problem in str(refused.value)
    assert "chset" not in str(refused.value)  # never a case's text


def test_two_tests_with_one_id_are_refused(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    write_yaml(root, PROBE_TESTS, [found_test(), found_test()])
    with pytest.raises(PackValidationError, match="its id probe-000000000001 is case 1's too"):
        probe_cases(load_pack(root))


def test_the_agents_own_tests_take_no_probe_key(tmp_path: Path) -> None:
    """The manifest's test file is as the spec says (§10): `name`, `input`, `expect`."""
    root = build_agent(tmp_path / "agent")
    write_yaml(root, "tests/cases.yaml", [found_test()])
    with pytest.raises(PackValidationError) as refused:
        load_cases(load_pack(root))
    (problem,) = refused.value.problems
    assert problem.startswith("tests/cases.yaml case 1 (typos variant of cases case symptom")
    assert problem.endswith("unknown key 'probe' (a case has `name`, `input` and `expect`)")


def test_the_store_counts_the_probes_tests_among_the_cases_an_agent_carries(
    tmp_path: Path,
) -> None:
    """An organization's store looks every case an agent carries up among the hidden evaluation
    sets (`core/pack_store.py`): the probe's tests are cases too, run or still waiting."""
    root = build_agent(tmp_path / "agent")
    waiting = found_test(2, input={"message": {"body": "No chest pain at all"}})
    write_yaml(root, PROBE_TESTS, [found_test()])
    write_yaml(root, PROBE_UNCONFIRMED, [waiting])
    carried = pack_store._cases_of(load_pack(root), {})
    assert carried == [
        *(case["input"] for case in TESTS),
        {"message": {"body": "Bad chset pain now"}},
        {"message": {"body": "No chest pain at all"}},
    ]


def test_validate_names_a_probe_test_by_its_file_and_number(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    refused = found_test(2, input={"message": {"body": "Bad chset pain now", "note": 1}})
    write_yaml(root, PROBE_TESTS, [found_test(1), refused])
    found = problems(load_pack(root))
    assert [(problem.kind, problem.file, problem.place) for problem in found] == [
        ("tests", PROBE_TESTS, "case 2")
    ]
    assert found[0].text.startswith(f"{PROBE_TESTS} case 2 (typos variant of cases case symptom")
    assert found[0].text.endswith("input message: has fields the schema does not allow: note")


# -- what the probe changes ------------------------------------------------------------------------

BODIES = [case["input"]["message"]["body"] for case in CASES]
OFFLINE = tuple(transforms.OFFLINE)
# An agent's canned answers that depend on what the message says: a keyword, as the bundled
# demo agent's fixture has.
CANNED_BY_KEYWORD = {
    "cases": [
        {
            "match": {"contains_any": ["chest pain"]},
            "response": {
                "answers": {
                    "request": {"value": "other", "confidence": 0.9},
                    "urgent": {"value": "yes", "confidence": 0.95},
                }
            },
        }
    ],
    "default": {
        "answers": {
            "request": {"value": "other", "confidence": 0.8},
            "urgent": {"value": "no", "confidence": 0.9},
        }
    },
}


def probed(root: Path, *names: str, **options: Any) -> Probed:
    """A probe of the agent at `root` on its dataset, with the named transformations (typing
    errors when none is named)."""
    pack = load_pack(root)
    decision = Decision(pack, origin_cap=False)
    chosen = Options(transformations=names or ("typos",), **options)
    return probe.run(probe.plan(decision, probe.source(pack), chosen))


def test_the_text_is_the_fields_the_agent_reads_and_nothing_else_changes(tmp_path: Path) -> None:
    extra = {"channel": "portal", "received": "2026-11-03"}
    cases = [
        {**case, "input": {"message": {**case["input"]["message"], **extra}}} for case in CASES
    ]
    found = probed(build_agent(tmp_path / "agent", cases=cases), *OFFLINE)
    assert found.report["fields"] == ["message.body"]  # the rules' `text_fields`
    assert len(found.variants) == len(CASES) * len(OFFLINE)
    for variant in found.variants:
        message = variant.input["message"]
        assert message["body"] != variant.case.input["message"]["body"]
        assert (message["channel"], message["received"]) == ("portal", "2026-11-03")
        assert variant.case.input["message"]["body"] in BODIES  # the original is not touched


def test_with_no_fields_named_the_text_is_the_schemas_free_strings(tmp_path: Path) -> None:
    """An `agent` variant that sends the whole input names no field: the text is then every
    string the input schema leaves free. One it holds to a list of values, a format or the shape
    a pattern gives is a code or a date, and never changed."""
    root = build_agent(tmp_path / "agent")
    message_fields(
        root,
        {"code": {"type": "string", "pattern": "^[a-z]+-[0-9]+$"}},
        subject={"type": "string"},
        sender={"$ref": "#/$defs/code"},
    )
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["providers"]["reader"] = {
        "answers": ["request", "urgent"],
        "use": "agent",
        "variants": {"agent": {"type": "agent", "mock_file": "fixtures/answers.json"}},
    }
    write_yaml(root, "agent.yaml", manifest)
    (root / "fixtures").mkdir()
    (root / "fixtures" / "answers.json").write_text(json.dumps(CANNED_BY_KEYWORD))
    message = {
        "body": "Bad chest pain now",
        "subject": "Question about tablets",
        "channel": "portal",
        "received": "2026-11-03",
        "sender": "patient-12",
    }
    case = {"id": "one", "input": {"message": message}, "labels": {"urgent": "yes"}}
    (root / "data" / "cases.jsonl").write_text(json.dumps(case) + "\n")

    found = probed(root, "typos", "case")
    assert found.report["fields"] == ["message.body", "message.subject"]
    assert len(found.variants) == 2
    for variant in found.variants:
        changed = variant.input["message"]
        assert changed["body"] != message["body"] and changed["subject"] != message["subject"]
        assert {key: changed[key] for key in ("channel", "received", "sender")} == {
            "channel": "portal",
            "received": "2026-11-03",
            "sender": "patient-12",
        }
    # What is added (a greeting, an instruction) goes to the main text, the longest: here the
    # subject, by four letters. The other field is left as it was.
    for variant in probed(root, "additions", "injection").variants:
        changed = variant.input["message"]
        assert message["subject"] in changed["subject"] != message["subject"]
        assert changed["body"] == message["body"]


def test_the_texts_of_a_list_are_each_changed_and_their_field_is_named_once(
    tmp_path: Path,
) -> None:
    """An input may hold its text in a list (notes, documents). The text of each item is
    changed, the code beside it is not, and the report names the field once, however many
    items a case has."""
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    note = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "kind": {"enum": ["nurse", "patient"]}},
    }
    message_fields(root, notes={"type": "array", "items": note})
    notes = [
        {"text": "Called the patient about tablets", "kind": "nurse"},
        {"text": "Thank you for calling me back", "kind": "patient"},
    ]
    message = {"body": "Bad chest pain now", "notes": notes}
    case = {"id": "one", "input": {"message": message}, "labels": {"urgent": "yes"}}
    (root / "data" / "cases.jsonl").write_text(json.dumps(case) + "\n")

    found = probed(root, "typos", "case")
    assert found.report["fields"] == ["message.body", "message.notes[].text"]
    assert len(found.variants) == 2
    for variant in found.variants:
        changed = variant.input["message"]["notes"]
        assert [item["kind"] for item in changed] == ["nurse", "patient"]
        assert all(new["text"] != old["text"] for new, old in zip(changed, notes, strict=True))


def test_a_pattern_that_any_text_meets_does_not_make_a_text_a_code(tmp_path: Path) -> None:
    """A schema's `pattern` may give a code its shape (`^[a-z]{2,3}$`), or only ask that a
    text hold something (`\\S`) or stay on one line. The first is never changed; the others are
    text like any other, and a variant their pattern refuses is left out when it is built."""
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    message_fields(
        root,
        body={"pattern": r"\S"},
        subject={"type": "string", "pattern": "^[^\\n]*$"},
        language={"type": "string", "pattern": "^[a-z]{2,3}(-[A-Z]{2})?$"},
    )
    message = {
        "body": "Bad chest pain now",
        "subject": "A question for the nurse",
        "language": "en",
    }
    case = {"id": "one", "input": {"message": message}, "labels": {"urgent": "yes"}}
    (root / "data" / "cases.jsonl").write_text(json.dumps(case) + "\n")

    found = probed(root, "typos", "case", "whitespace")
    assert found.report["fields"] == ["message.body", "message.subject"]
    assert [variant.transformation for variant in found.variants] == ["typos", "case"]
    for variant in found.variants:
        changed = variant.input["message"]
        assert changed["body"] != message["body"] and changed["subject"] != message["subject"]
        assert changed["language"] == "en"
    # A line break in the subject is not one line: that variant is no input of this agent.
    lines = {line["name"]: line for line in found.report["transformations"]}
    assert lines["whitespace"]["skipped"] == {"not_valid_input": 1}


def test_the_patterns_of_an_agent_from_a_registry_are_asked_on_the_engine_it_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether a string is text is asked of its schema's pattern. An agent the deployment does
    not own has its patterns run on the engine that never backtracks (`Pack.bounded`): the probe
    asks them there too, and never on `re`, which a pattern can stall."""
    root = build_agent(tmp_path / "agent")
    message_fields(root, body={"pattern": r"\S"})
    pack = load_pack(root, origin="hub")
    assert pack.bounded
    asked: list[bool] = []
    search = patterns.search

    def seen(pattern: str, text: str, *, community: bool = False) -> bool:
        asked.append(community)
        return search(pattern, text, community=community)

    monkeypatch.setattr(patterns, "search", seen)
    planned = probe.plan(Decision(pack), probe.source(pack), Options(transformations=("typos",)))
    assert planned.summary()["fields"] == ["message.body"]
    assert asked and all(asked)


def test_a_variant_the_input_schema_refuses_is_never_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema takes a body of thirty characters at most: every case fits, and none does
    with an instruction added to it. Those variants are counted, with why, and never sent."""
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    message_fields(root, body={"maxLength": 30})
    sent = live_agent(monkeypatch, by_keyword)
    pack = load_pack(root)
    planned = probe.plan(
        Decision(pack, origin_cap=False),
        probe.source(pack),
        Options(transformations=("injection", "case")),
    )
    # The plan already leaves them out: a dry run counts only what would be sent.
    assert planned.summary()["calls"] == {"decisions": 10, "agent": 10, "generator": 0}
    assert planned.summary()["transformations"][0] == {
        "name": "injection",
        "variants": 0,
        "skipped": {"not_valid_input": 5},
    }
    found = probe.run(planned)
    lines = {line["name"]: line for line in found.report["transformations"]}
    assert lines["injection"]["tried"] == 0
    assert lines["injection"]["skipped"] == {"not_valid_input": 5}
    assert lines["case"]["tried"] == 5 and lines["case"]["skipped"] == {}
    assert len(sent) == 10  # five cases as written, and their five case variants
    assert all(len(body["input"]["message"]["body"]) <= 30 for body in sent)
    assert "Not tried: injection 5 (not valid input)." in probe_report.text(found.report)


def test_an_injected_instruction_asks_for_the_answers_that_would_do_most_harm(
    tmp_path: Path,
) -> None:
    """For a critical answer, its other label; for the primary question, a label the policy
    may automate that is not the case's own (`other` is never automated here)."""
    manifest = load_pack(build_agent(tmp_path / "agent")).manifest
    assert probe_variants.asks(manifest, {"request": "booking", "urgent": "yes"}) == {
        "urgent": "no",
        "request": "medication",
    }
    assert probe_variants.asks(manifest, {"request": "medication"})["request"] == "booking"
    assert probe_variants.asks(manifest, {}) == {"urgent": "no", "request": "medication"}
    found = probed(tmp_path / "agent", "injection")
    for variant in found.variants:
        added = variant.input["message"]["body"][len(variant.case.input["message"]["body"]) :]
        asked = probe_variants.asks(manifest, variant.case.labels)
        assert f"urgent: no, request: {asked['request']}" in added


def test_a_dataset_the_agent_cannot_evaluate_is_refused_as_an_evaluation_refuses_it(
    tmp_path: Path,
) -> None:
    mislabelled = [{**CASES[0], "labels": {"urgent": "perhaps"}}]
    pack = load_pack(build_agent(tmp_path / "agent", cases=mislabelled))
    with pytest.raises(MendariError) as refused:
        probe.source(pack)
    assert str(refused.value) == (
        "cases case symptom: labels.urgent is not one of its labels (yes, no)"
    )
    manifest = {key: value for key, value in MANIFEST.items() if key not in ("tests", "evaluation")}
    bare = build_agent(tmp_path / "bare")
    write_yaml(bare, "agent.yaml", manifest)
    with pytest.raises(MendariError) as nothing:
        probe.source(load_pack(bare))
    assert str(nothing.value) == (
        "clinic/probe-triage@0.1.0 has no case to probe: name a labelled dataset with --dataset, "
        "or give the agent behaviour tests"
    )


def test_the_fields_can_be_named(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed(root, "case", fields=("message.body",))
    assert found.report["fields"] == ["message.body"]
    with pytest.raises(MendariError) as refused:
        probed(root, "case", fields=("message.subject",))
    assert str(refused.value) == (
        "no case has text in message.subject: name the input's text fields with --field"
    )


# -- which cases, and the same ones for a seed -----------------------------------------------------


def test_the_same_seed_gives_the_same_cases_variants_and_report(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    first, again, other = (
        probed(root, *OFFLINE, seed=seed, limit=3, variants=2) for seed in (4, 4, 5)
    )
    assert [(v.case.id, v.transformation, v.index, v.input) for v in first.variants] == [
        (v.case.id, v.transformation, v.index, v.input) for v in again.variants
    ]
    assert first.report == again.report
    assert [v.input for v in other.variants] != [v.input for v in first.variants]
    assert first.report["seed"] == 4 and first.report["cases"]["probed"] == 3


def test_a_limit_takes_cases_with_a_critical_label_and_without_in_turn(tmp_path: Path) -> None:
    routine = [
        {
            "id": f"routine-{number}",
            "input": {"message": {"body": f"Is the clinic open on day {number}"}},
            "labels": {"request": "other", "urgent": "no"},
        }
        for number in range(40)
    ]
    root = build_agent(tmp_path / "agent", cases=[*routine, *CASES])
    found = probed(root, "case", limit=4)
    chosen = [variant.case.id for variant in found.variants]
    # Both of the dataset's two cases with a critical label, among forty-three without.
    assert len(chosen) == 4 and {"symptom", "symptom-and-medicine"} < set(chosen)
    assert found.report["cases"] == {
        "in_source": 45,
        "distinct": 45,
        "probed": 4,
        "labelled": 4,
        "critical": 2,
        "refused": 0,
        "unanswered": 0,
    }
    everything = probed(root, "case", limit=None)
    assert everything.report["cases"]["probed"] == 45


def test_a_message_given_twice_is_probed_once(tmp_path: Path) -> None:
    """A case is what the agent reads of it: the same message received again, on another day or
    another channel, is the same case, and costs no second call."""
    again = {
        "id": "symptom-again",
        "input": {"message": {"body": "Bad chest pain now", "channel": "phone"}},
        "labels": {"request": "other", "urgent": "yes"},
    }
    found = probed(build_agent(tmp_path / "agent", cases=[*CASES, again]), "typos")
    assert found.report["cases"]["in_source"] == 6 and found.report["cases"]["distinct"] == 5
    assert sorted(variant.case.id for variant in found.variants) == sorted(
        case["id"] for case in CASES
    )
    # What a case is known by is a hash of that text, never the text.
    keys = {variant.case.key for variant in found.variants}
    assert len(keys) == 5 and all(re.fullmatch(r"[0-9a-f]{64}", key) for key in keys)


def test_naming_the_text_to_change_does_not_change_what_a_case_is(tmp_path: Path) -> None:
    """`fields` names the text to change. A case is still what the agent reads of it: two
    messages under one subject are two cases, each known by the hash it has in any probe."""
    cases = [
        {**case, "input": {"message": {**case["input"]["message"], "subject": "A question"}}}
        for case in CASES
    ]
    root = build_agent(tmp_path / "agent", cases=cases)
    message_fields(root, subject={"type": "string"})

    as_read = probed(root, "case")
    by_subject = probed(root, "case", fields=("message.subject",))
    assert by_subject.report["fields"] == ["message.subject"]
    assert by_subject.report["cases"]["distinct"] == len(CASES)
    assert {variant.case.key for variant in by_subject.variants} == {
        variant.case.key for variant in as_read.variants
    }


# -- what counts as a failure, and how bad ---------------------------------------------------------


def test_a_planted_weakness_is_found_and_the_worst_comes_first(tmp_path: Path) -> None:
    """The weak agent finds a symptom by one phrase typed exactly. A typing error in it loses
    the symptom: where the message also asks for a medicine the policy then automates it (the
    worst), elsewhere a person still reads it; and a mistyped `Appointment` goes to the wrong
    queue, which is no critical answer."""
    for seed in range(4):
        found = probed(build_agent(tmp_path / f"agent-{seed}"), "typos", seed=seed)
        assert [(f.variant.case.id, f.harm) for f in found.findings] == [
            ("symptom-and-medicine", "automated"),
            ("symptom", "critical_lost"),
            ("visit", "wrong"),
        ]
        worst, lost, wrong = found.findings
        assert worst.answers == {"urgent": {"original": "yes", "variant": "no", "expected": "yes"}}
        assert worst.outcome == ("HUMAN_REVIEW", "AUTO")
        assert lost.outcome == ("HUMAN_REVIEW", "HUMAN_REVIEW")
        assert wrong.answers == {
            "request": {"original": "booking", "variant": "other", "expected": "booking"}
        }
        assert wrong.outcome == ("AUTO", "HUMAN_REVIEW")


def test_among_automated_variants_one_that_lost_a_critical_answer_comes_first(
    tmp_path: Path,
) -> None:
    """Both would go out with no person reading them: one to the wrong queue, the other
    without the symptom it reports. The second is the worse, whatever the cases are called."""
    steered = {
        "case": "opening-hours",
        "transformation": "added-word",
        "input": {"message": {"body": "Is the clinic open on Friday, RX"}},
    }
    root = build_agent(tmp_path / "agent")
    variants = tmp_path / "variants.jsonl"
    variants.write_text(json.dumps(steered) + "\n", encoding="utf-8")
    pack = load_pack(root)
    planned = probe.plan(
        Decision(pack, origin_cap=False),
        probe.source(pack),
        Options(transformations=("typos",)),
        given=probe.load_variants(variants, pack.manifest),
    )
    found = probe.run(planned)
    assert [(f.variant.case.id, f.harm, sorted(f.answers)) for f in found.findings] == [
        ("symptom-and-medicine", "automated", ["urgent"]),
        ("opening-hours", "automated", ["request"]),
        ("symptom", "critical_lost", ["urgent"]),
        ("visit", "wrong", ["request"]),
    ]


def test_a_robust_agent_has_no_finding(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent", robust=True)
    for seed in range(4):
        found = probed(root, *OFFLINE, seed=seed, variants=2)
        assert found.findings == []
        tried = found.report["totals"]["tried"]
        assert found.report["totals"] == {
            "tried": tried,
            "changed": 0,
            "critical_lost": 0,
            "automated": 0,
        }
        assert tried >= 40  # six transformations, two variants of each where two differ
        assert found.report["verdict"] == (
            f"Verdict: no answer changed on {tried} variants of 5 cases."
        )


def test_without_labels_the_agents_answers_on_the_original_are_expected(tmp_path: Path) -> None:
    """Metamorphic testing: a change that keeps the meaning must keep the answers, whatever the
    right ones are."""
    found = probed(build_agent(tmp_path / "agent", labelled=False), "typos")
    assert [(f.variant.case.id, f.harm, f.expected_from) for f in found.findings] == [
        ("symptom-and-medicine", "automated", "original"),
        ("symptom", "critical_lost", "original"),
        ("visit", "wrong", "original"),
    ]
    labelled = probed(build_agent(tmp_path / "labelled"), "typos")
    assert {f.expected_from for f in labelled.findings} == {"labels"}
    # The report says what the variants were held to, where it would say how many are critical.
    assert (found.report["cases"]["labelled"], labelled.report["cases"]["labelled"]) == (0, 5)
    assert probe_report.text(found.report).splitlines()[1] == (
        "5 of the 5 distinct cases of cases (no labels: each variant is held to the agent's own "
        "answers as written), seed 0. Text changed: message.body."
    )


def test_a_variant_the_agent_does_not_answer_is_no_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that fails on a variant (here, on any message that is all capitals) fails
    closed: the policy holds the decision for a person. That is counted, by transformation, and
    is no changed answer: a call that timed out once would otherwise read as a failure."""
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)

    def answer(text: str) -> dict[str, Any]:
        return {"request": {"value": "nonsense"}} if text.isupper() else by_keyword(text)

    live_agent(monkeypatch, answer)
    found = probed(root, "case", "punctuation", seed=1)
    lines = {line["name"]: line for line in found.report["transformations"]}
    capitals = sum(variant.detail == "capitals" for variant in found.variants)
    assert capitals > 0 and lines["case"]["unanswered"] == capitals
    assert lines["case"]["tried"] == 5 and lines["case"]["changed"] == 0
    assert lines["punctuation"]["unanswered"] == 0 and found.findings == []
    shown = probe_report.text(found.report)
    assert (
        "No answer (a person decides those; probe again to see whether it repeats): "
        f"case {capitals}."
    ) in shown


def test_a_case_the_agent_already_gets_wrong_is_not_the_probes_finding(tmp_path: Path) -> None:
    """The evaluation reports a case the agent gets wrong as written. The probe reports what a
    change made worse: here the original is missed and its variants are missed alike."""
    missed = {
        "id": "missed-as-written",
        "input": {"message": {"body": "Crushing weight behind my ribs"}},
        "labels": {"request": "other", "urgent": "yes"},
    }
    found = probed(build_agent(tmp_path / "agent", cases=[missed]), *OFFLINE)
    assert found.findings == [] and found.report["totals"]["changed"] == 0
    assert found.report["cases"]["critical"] == 1


def test_a_critical_case_automated_as_written_is_not_counted_again(tmp_path: Path) -> None:
    """`RX` and a symptom the rules do not know: automated as written, wrongly, and so is each
    variant. That is the evaluation's finding, once, not the probe's six times."""
    missed = {
        "id": "automated-as-written",
        "input": {"message": {"body": "RX due, crushing weight behind my ribs"}},
        "labels": {"request": "medication", "urgent": "yes"},
    }
    found = probed(build_agent(tmp_path / "agent", cases=[missed]), *OFFLINE)
    assert found.findings == [] and found.report["totals"]["automated"] == 0


# -- the report ------------------------------------------------------------------------------------


def test_the_report_counts_by_transformation_worst_first(tmp_path: Path) -> None:
    found = probed(build_agent(tmp_path / "agent"), "typos", "case")
    report = found.report
    assert report["format"] == "mendari.probe/1"
    assert report["agent"] == {
        "id": "clinic/probe-triage",
        "name": "Probe triage",
        "version": "0.1.0",
        "digest": load_pack(tmp_path / "agent").digest,
    }
    assert report["providers"] == {"reader": "rules"} and report["canned"] is None
    assert report["source"] == {"kind": "dataset", "name": "cases"}
    counts = {"critical_lost": 0, "automated": 0, "unanswered": 0, "skipped": {}}
    assert report["transformations"] == [
        {
            **counts,
            "name": "typos",
            "keeps_meaning": True,
            "tried": 5,
            "changed": 3,
            "critical_lost": 2,
            "automated": 1,
        },
        {**counts, "name": "case", "keeps_meaning": True, "tried": 5, "changed": 0},
    ]
    assert report["totals"] == {"tried": 10, "changed": 3, "critical_lost": 2, "automated": 1}
    assert report["calls"] == {"decisions": 15, "agent": 0, "generator": 0}
    assert [(f["case"], f["transformation"], f["harm"]) for f in report["findings"]] == [
        ("symptom-and-medicine", "typos", "automated"),
        ("symptom", "typos", "critical_lost"),
        ("visit", "typos", "wrong"),
    ]
    detail = found.findings[0].variant.detail
    assert detail in ("1 typing error", "2 typing errors")  # one, or one in each of its two words
    assert report["findings"][0] == {
        "id": found.findings[0].variant.id,
        "case": "symptom-and-medicine",
        "transformation": "typos",
        "detail": detail,
        "harm": "automated",
        "answers": {"urgent": {"original": "yes", "variant": "no", "expected": "yes"}},
        "outcome": {"original": "HUMAN_REVIEW", "variant": "AUTO"},
        "expected_from": "labels",
    }
    assert report["verdict"] == (
        "Verdict: not ready. 1 variant would have been automated while wrong or critical, and "
        "2 critical answers were lost. Fix the agent, then probe again."
    )


def test_the_report_holds_ids_names_and_counts_and_never_a_cases_text(tmp_path: Path) -> None:
    found = probed(build_agent(tmp_path / "agent"), *OFFLINE)
    shown = probe_report.text(found.report)
    dumped = json.dumps(found.report)
    assert found.findings
    texts = set(BODIES) | {variant.input["message"]["body"] for variant in found.variants}
    for held in (shown, dumped):
        assert not [text for text in texts if text in held]
        # Nor a word of one: none of these is a name the agent's manifest gives anything.
        assert not re.search(r"chest|pain|tablets|appointment|friday|ribs", held, re.IGNORECASE)
    for said in ("symptom-and-medicine", "typos", "automated", "urgent yes -> no"):
        assert said in shown
    assert shown.splitlines()[-1] == found.report["verdict"]


def test_the_text_report_is_a_table_then_the_worst_findings_then_the_verdict(
    tmp_path: Path,
) -> None:
    found = probed(build_agent(tmp_path / "agent"), "typos", "case")
    assert probe_report.text(found.report).splitlines() == [
        "Probe triage 0.1.0 (clinic/probe-triage), reader=rules",
        "5 of the 5 distinct cases of cases (2 with a critical label), seed 0. Text changed: "
        "message.body.",
        "15 decisions, none of them a call to an agent.",
        "",
        "transformation  tried  changed  critical lost  automated",
        "typos               5        3              2          1",
        "case                5        0              0          0",
        "total              10        3              2          1",
        "",
        "Findings, worst first (3)",
        "  automated      symptom-and-medicine  typos  urgent yes -> no; HUMAN_REVIEW -> AUTO",
        "  critical lost  symptom               typos  urgent yes -> no",
        "  wrong          visit                 typos  request booking -> other; AUTO -> "
        "HUMAN_REVIEW",
        "",
        "Verdict: not ready. 1 variant would have been automated while wrong or critical, and 2 "
        "critical answers were lost. Fix the agent, then probe again.",
    ]


def test_the_verdict_says_the_worst_that_was_found(tmp_path: Path) -> None:
    def verdict(*ids: str) -> str:
        cases = [case for case in CASES if case["id"] in ids]
        root = build_agent(tmp_path / "-".join(ids), cases=cases)
        return str(probed(root, "typos").report["verdict"])

    assert verdict("symptom") == (
        "Verdict: 1 critical answer was lost on a variant; a person would still have read the "
        "case. Fix the agent, then probe again."
    )
    assert verdict("visit", "medicine") == (
        "Verdict: the answers changed on 1 of 2 variants, none of them a critical answer."
    )
    assert verdict("medicine", "opening-hours") == (
        "Verdict: no answer changed on 2 variants of 2 cases."
    )
    assert verdict("medicine") == "Verdict: no answer changed on 1 variant of 1 case."


@pytest.mark.parametrize(
    ("ids", "fail_on", "code"),
    [
        (("symptom", "visit"), None, 0),  # a report only
        (("symptom", "visit"), "critical", 2),
        (("symptom", "visit"), "any", 2),
        (("visit",), "critical", 0),  # a wrong queue is no critical finding
        (("visit",), "any", 2),
        (("medicine",), "any", 0),
        (("medicine",), "critical", 0),
    ],
)
def test_a_ci_job_fails_on_critical_findings_or_on_any(
    tmp_path: Path, ids: tuple[str, ...], fail_on: str | None, code: int
) -> None:
    cases = [case for case in CASES if case["id"] in ids]
    found = probed(build_agent(tmp_path / "agent", cases=cases), "typos")
    assert probe_report.exit_code(found.report, fail_on) == code
    assert probe_report.EXIT_FOUND == 2


# -- each failure becomes a behaviour test ---------------------------------------------------------


def kept_tests(root: Path, relative: str = PROBE_TESTS) -> list[dict[str, Any]]:
    held = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
    assert isinstance(held, list)
    return held


def test_each_failure_is_written_as_a_test_that_fails_until_the_agent_is_fixed(
    tmp_path: Path,
) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed(root, "typos")
    kept = probe.keep(load_pack(root), found.findings)
    worst = found.findings[0].variant
    assert kept.added == tuple(finding.variant.id for finding in found.findings)
    assert (kept.waiting, kept.already, kept.left_out) == ((), 0, {})
    assert not (root / PROBE_UNCONFIRMED).exists()
    # A behaviour test like any other, and where it came from.
    assert kept_tests(root)[0] == {
        "name": f"typos variant of cases case symptom-and-medicine ({worst.id})",
        "input": worst.input,
        "expect": {"answers": {"urgent": "yes"}, "not_outcome": "AUTO"},
        "probe": {
            "id": worst.id,
            "found_by": "mendari probe",
            "transformation": "typos",
            "detail": worst.detail,  # how many typing errors: one or two
            "seed": 0,
            "variant": 0,
            "original": {"source": "cases", "case": "symptom-and-medicine"},
            "harm": "automated",
            "expected_from": "labels",
        },
    }
    assert [case["expect"] for case in kept_tests(root)[1:]] == [
        {"answers": {"urgent": "yes"}, "not_outcome": "AUTO"},
        {"answers": {"request": "booking"}},
    ]

    def passed() -> list[bool]:
        pack = load_pack(root)
        validate(pack)
        return [outcome.passed for outcome in run_pack_tests(Decision(pack, origin_cap=False))]

    assert passed() == [True, True, False, False, False]  # `mendari test` fails on the three
    write_rules(root, robust=True)  # the agent is fixed
    assert passed() == [True] * 5  # and the three stay, as its guard


def test_probing_again_writes_nothing_twice(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    first = probe.keep(load_pack(root), probed(root, "typos").findings)
    written = (root / PROBE_TESTS).read_bytes()
    again = probe.keep(load_pack(root), probed(root, "typos").findings)
    assert len(first.added) == 3 and (again.added, again.already) == ((), 3)
    assert (root / PROBE_TESTS).read_bytes() == written
    # Another seed makes other variants: what fails is added after what is there, and a
    # variant that says the same as one already kept is not kept again.
    other = probe.keep(load_pack(root), probed(root, "typos", seed=1).findings)
    assert len(other.added) + other.already == 3
    tests = kept_tests(root)
    assert [case["probe"]["id"] for case in tests] == [*first.added, *other.added]
    inputs = [json.dumps(case["input"], sort_keys=True) for case in tests]
    assert len(set(inputs)) == len(inputs)
    assert written in (root / PROBE_TESTS).read_bytes() or other.added == ()


def test_a_tests_id_comes_from_the_case_the_transformation_and_the_seed(tmp_path: Path) -> None:
    def ids(root: Path, *names: str, **options: Any) -> dict[tuple[str, str], str]:
        found = probed(root, *names, **options)
        return {(v.case.input["message"]["body"], v.transformation): v.id for v in found.variants}

    root = build_agent(tmp_path / "agent")
    first = ids(root, "typos", "case")
    assert ids(root, "typos", "case") == first
    assert all(re.fullmatch(r"probe-[0-9a-f]{12}", found) for found in first.values())
    assert len(set(first.values())) == len(first)  # one id per case and transformation
    assert set(ids(root, "typos", "case", seed=1).values()).isdisjoint(first.values())
    # The case is what the agent reads of it, whatever the dataset calls it.
    renamed = [{**case, "id": f"renamed-{number}"} for number, case in enumerate(CASES)]
    assert ids(build_agent(tmp_path / "renamed", cases=renamed), "typos", "case") == first


def test_a_variants_white_space_is_written_where_a_person_can_see_it(tmp_path: Path) -> None:
    """A variant's text is its white space, too: line breaks, a tab, two spaces, a space at
    the end. The file shows each as an escape on one line, where a folded YAML text would hide
    them; and what is read back is the text exactly."""
    root = build_agent(tmp_path / "agent")
    wrapped = "Bad chest\npain  since last\nnight\n"  # as the white-space transformation leaves it
    edged = "  Bad chest pain\tnow "
    quoted = "Bad \N{LEFT DOUBLE QUOTATION MARK}chest\N{RIGHT DOUBLE QUOTATION MARK} pain"
    bodies = [wrapped, edged, "Bad chset pain now", quoted, "yes"]
    cases = [
        found_test(number, input={"message": {"body": body}})
        for number, body in enumerate(bodies, start=1)
    ]
    kept = probe_tests.write(load_pack(root), cases)
    assert len(kept.added) == 5
    written = (root / PROBE_TESTS).read_text(encoding="utf-8")
    assert '      body: "Bad chest\\npain  since last\\nnight\\n"\n' in written
    assert '      body: "  Bad chest pain\\tnow "\n' in written
    assert "      body: Bad chset pain now\n" in written  # nothing hidden: written plainly
    assert f"      body: {quoted}\n" in written
    assert "      body: 'yes'\n" in written  # the text, not a YAML true
    assert [case["input"]["message"]["body"] for case in kept_tests(root)] == bodies
    assert written.startswith("# Regression tests that `mendari probe` found")
    assert len(probe_cases(load_pack(root))) == 5


def test_an_agent_that_is_not_the_authors_own_is_not_written_into(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed(root, "typos")
    bundled = load_pack(root, origin="maintained")
    with pytest.raises(MendariError) as refused:
        probe.keep(bundled, found.findings)
    assert str(refused.value) == (
        "an agent this release bundles is not changed in place: copy it, and probe the copy"
    )
    assert not (root / PROBE_TESTS).exists()


def test_a_sites_own_case_is_reported_and_never_written(tmp_path: Path) -> None:
    """A case `mendari feedback export` wrote is a site's own, and may be a patient's words:
    its variant is probed here, and never copied into the agent's tests, where nothing would
    mark it."""
    marked = [
        {**case, "attributes": {"exported_by": "mendari feedback export"}}
        if case["id"] == "symptom"
        else case
        for case in CASES
    ]
    root = build_agent(tmp_path / "agent", cases=marked)
    found = probed(root, "typos")
    assert [finding.variant.case.id for finding in found.findings] == [
        "symptom-and-medicine",
        "symptom",
        "visit",
    ]
    kept = probe.keep(load_pack(root), found.findings)
    assert len(kept.added) == 2 and kept.left_out == {"site": 1}
    assert [case["probe"]["original"]["case"] for case in kept_tests(root)] == [
        "symptom-and-medicine",
        "visit",
    ]


# -- variants from a file --------------------------------------------------------------------------

GIVEN = [
    {
        "case": "symptom",
        "transformation": "paraphrase",
        "input": {"message": {"body": "An elephant is sitting on my ribcage"}},
    },
    {
        "case": "visit",
        "transformation": "translate:es",
        "input": {"message": {"body": "Quiero una cita en mayo"}},
        "model": "a-lab/a-model",
    },
    {
        "case": "symptom",
        "transformation": "negation",
        "input": {"message": {"body": "No chest pain now, only a question"}},
        "labels": {"urgent": "no"},
        "model": "a-lab/a-model",
    },
]


def given(tmp_path: Path, *records: dict[str, Any]) -> Path:
    path = tmp_path / "variants.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def probed_with(root: Path, variants: Path, *names: str, **options: Any) -> Probed:
    """A probe with the variants of a file, and the named transformations (none by default)."""
    pack = load_pack(root)
    decision = Decision(pack, origin_cap=False)
    extra = probe.load_variants(variants, pack.manifest)
    planned = probe.plan(
        decision, probe.source(pack), Options(transformations=names, **options), given=extra
    )
    return probe.run(planned)


def test_variants_someone_wrote_are_tried_as_the_probes_own(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed_with(root, given(tmp_path, *GIVEN))
    assert [(f.variant.case.id, f.variant.transformation, f.harm) for f in found.findings] == [
        ("symptom", "paraphrase", "critical_lost"),
        ("symptom", "negation", "wrong"),
        ("visit", "translate:es", "wrong"),
    ]
    lines = found.report["transformations"]
    assert [(line["name"], line["keeps_meaning"]) for line in lines] == [
        ("paraphrase", True),
        ("translate:es", True),
        ("negation", False),  # its variant changes what is right
    ]
    assert [(line["tried"], line["changed"]) for line in lines] == [(1, 1)] * 3
    # Only the cases the file names are decided: two originals and three variants.
    assert found.report["cases"]["probed"] == 2 and found.report["calls"]["decisions"] == 5
    denied = found.findings[1]
    assert denied.answers == {"urgent": {"original": "yes", "variant": "yes", "expected": "no"}}
    assert denied.expect == {"answers": {"urgent": "no"}} and denied.expected_from == "model"
    assert "urgent yes -> yes (expected no)" in probe_report.text(found.report)
    assert {variant.model for variant in found.variants} == {None, "a-lab/a-model"}


def test_a_files_variants_and_the_probes_own_run_together(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed_with(root, given(tmp_path, GIVEN[0]), "typos")
    assert [line["name"] for line in found.report["transformations"]] == ["typos", "paraphrase"]
    assert found.report["cases"]["probed"] == 5  # every case, since a transformation is named
    assert found.report["totals"]["tried"] == 6


def test_a_variant_that_answers_better_than_the_original_changes_but_is_not_a_test(
    tmp_path: Path,
) -> None:
    """The original is mistyped and the agent misses it; the variant is typed right and the
    agent gets it. The answers changed, so it is counted, but nothing is worse: there is no
    failure to hold the agent to."""
    mistyped = {
        "id": "mistyped",
        "input": {"message": {"body": "Apointmnet for May"}},
        "labels": {"request": "booking", "urgent": "no"},
    }
    right = {
        "case": "mistyped",
        "transformation": "retyped",
        "input": {"message": {"body": "Appointment for May"}},
    }
    root = build_agent(tmp_path / "agent", cases=[mistyped])
    found = probed_with(root, given(tmp_path, right))
    (finding,) = found.findings
    assert (finding.harm, finding.expect) == ("changed", None)
    assert finding.answers == {
        "request": {"original": "other", "variant": "booking", "expected": "booking"}
    }
    assert found.report["totals"] == {"tried": 1, "changed": 1, "critical_lost": 0, "automated": 0}
    assert probe_report.exit_code(found.report, "any") == 2
    kept = probe.keep(load_pack(root), found.findings)
    assert (kept.added, kept.left_out) == ((), {"no_expectation": 1})
    assert not (root / PROBE_TESTS).exists()


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ("not json", "variants.jsonl:1: not JSON"),
        ("[1]", "variants.jsonl:1: a variant is an object with `case`, `transformation`"),
        ('{"case": "symptom"}', "variants.jsonl:1: no `transformation`"),
        (
            '{"case": "symptom", "transformation": "Para Phrase", "input": {}}',
            "variants.jsonl:1: `transformation` is a short name in lower case",
        ),
        (
            '{"case": "symptom", "transformation": "paraphrase", "input": "text"}',
            "variants.jsonl:1: `input` must be an object, the agent's whole input",
        ),
        (
            '{"case": "symptom", "transformation": "p", "input": {}, "labels": {"urgent": "so"}}',
            "variants.jsonl:1: `labels.urgent` is not one of its labels (yes, no)",
        ),
        (
            '{"case": "symptom", "transformation": "p", "input": {}, "labels": {"mood": "no"}}',
            "variants.jsonl:1: `labels` names a question the agent does not have",
        ),
        (
            '{"case": "symptom", "transformation": "p", "input": {}, "text": "x"}',
            "variants.jsonl:1: unknown key `text`",
        ),
        (
            '{"case": "symptom", "transformation": "p", "input": {}, "seed": -1}',
            "variants.jsonl:1: `seed` and `variant` are whole numbers, 0 or more",
        ),
        (
            '{"case": "symptom", "transformation": "p", "input": {}, "model": "a model"}',
            "variants.jsonl:1: `model` is a model's id",
        ),
    ],
)
def test_a_variants_file_that_is_not_one_is_refused_by_line(
    tmp_path: Path, line: str, problem: str
) -> None:
    path = tmp_path / "variants.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    manifest = load_pack(build_agent(tmp_path / "agent")).manifest
    with pytest.raises(MendariError) as refused:
        probe.load_variants(path, manifest)
    assert str(refused.value).startswith(problem)
    assert "Para Phrase" not in str(refused.value)  # what the file says is never repeated


def test_a_variant_of_a_case_the_source_does_not_have_is_refused(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    unknown = {**GIVEN[0], "case": "no-such-case"}
    with pytest.raises(MendariError) as refused:
        probed_with(root, given(tmp_path, GIVEN[0], unknown))
    assert str(refused.value) == (
        "variants.jsonl: 1 variant names a case that cases does not have (the first: no-such-case)"
    )


def test_the_variants_tried_can_be_written_out_and_tried_again_from_the_file(
    tmp_path: Path,
) -> None:
    """What a probe built (or a model wrote) is kept as a file, so the same variants run again
    offline, and give the same findings under the same ids."""
    root = build_agent(tmp_path / "agent")
    first = probed(root, "typos", "whitespace", seed=3)
    out = tmp_path / "variants.jsonl"
    out.write_text(probe.dump_variants(first.variants), encoding="utf-8")
    record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert set(record) == {"case", "transformation", "input", "detail", "seed", "variant"}
    again = probed_with(root, out)
    assert [variant.id for variant in again.variants] == [v.id for v in first.variants]
    assert [(f.variant.id, f.harm) for f in again.findings] == [
        (f.variant.id, f.harm) for f in first.findings
    ]
    assert again.report["totals"] == first.report["totals"]


# -- a test a model changed the expected answer of waits for a person ------------------------------


def test_a_denial_a_model_wrote_waits_to_be_confirmed_and_is_not_run(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    found = probed_with(root, given(tmp_path, *GIVEN))
    kept = probe.keep(load_pack(root), found.findings)
    paraphrase, denial, translation = (finding.variant.id for finding in found.findings)
    assert kept.added == (paraphrase, translation) and kept.waiting == (denial,)
    (waiting,) = kept_tests(root, PROBE_UNCONFIRMED)
    assert waiting["expect"] == {"answers": {"urgent": "no"}}
    assert waiting["probe"]["expected_from"] == "model"
    assert waiting["probe"]["model"] == "a-lab/a-model"
    assert kept_tests(root)[1]["probe"]["model"] == "a-lab/a-model"  # the translation's writer

    def ran() -> list[str]:
        pack = load_pack(root)
        return [outcome.name for outcome in run_pack_tests(Decision(pack, origin_cap=False))]

    assert len(ran()) == 4 and not [name for name in ran() if denial in name]
    # Writing again keeps it waiting once.
    again = probe.keep(load_pack(root), found.findings)
    assert (again.added, again.waiting, again.already) == ((), (), 3)

    assert probe_tests.confirm(load_pack(root), [denial]) == (denial,)
    assert not (root / PROBE_UNCONFIRMED).exists()  # none is left waiting
    confirmed = kept_tests(root)[-1]
    assert confirmed["probe"]["id"] == denial and confirmed["probe"]["confirmed"] is True
    assert len(ran()) == 5 and denial in ran()[-1]
    pack = load_pack(root)
    last = run_pack_tests(Decision(pack, origin_cap=False))[-1]
    assert last.failures == ("answers.urgent: expected 'no', got 'yes'",)


def test_a_test_is_confirmed_as_a_person_left_it(tmp_path: Path) -> None:
    """Before confirming, a person reads the text and may correct what is expected: what they
    wrote is what runs. A test that expects what the agent can never answer is not confirmed."""
    root = build_agent(tmp_path / "agent")
    found = probed_with(root, given(tmp_path, GIVEN[2]))
    (denial,) = probe.keep(load_pack(root), found.findings).waiting
    held = kept_tests(root, PROBE_UNCONFIRMED)
    held[0]["expect"] = {"answers": {"urgent": "perhaps"}}
    write_yaml(root, PROBE_UNCONFIRMED, held)
    with pytest.raises(MendariError) as refused:
        probe_tests.confirm(load_pack(root), [denial])
    assert str(refused.value) == (
        f"{denial} cannot be confirmed as it is in {PROBE_UNCONFIRMED}: `expect.answers.urgent` "
        "is 'perhaps', not one of its labels (yes, no)"
    )
    held[0]["expect"] = {"outcome_in": ["HUMAN_REVIEW", "ABSTAIN"]}
    write_yaml(root, PROBE_UNCONFIRMED, held)
    probe_tests.confirm(load_pack(root), [denial])
    assert kept_tests(root)[-1]["expect"] == {"outcome_in": ["HUMAN_REVIEW", "ABSTAIN"]}


def test_only_a_test_that_waits_can_be_confirmed(tmp_path: Path) -> None:
    root = build_agent(tmp_path / "agent")
    with pytest.raises(MendariError) as nothing:
        probe_tests.confirm(load_pack(root), ["probe-000000000000"])
    assert str(nothing.value) == f"no test waits in {PROBE_UNCONFIRMED}: there is none to confirm"
    found = probed_with(root, given(tmp_path, GIVEN[2]))
    (denial,) = probe.keep(load_pack(root), found.findings).waiting
    with pytest.raises(MendariError) as unknown:
        probe_tests.confirm(load_pack(root), [denial, "probe-000000000000"])
    assert str(unknown.value) == (
        f"probe-000000000000 is not a test waiting in {PROBE_UNCONFIRMED} (it holds: {denial})"
    )
    assert len(kept_tests(root, PROBE_UNCONFIRMED)) == 1  # nothing was moved


# -- what a probe costs: canned answers, a live agent, a dry run -----------------------------------

AGENT_URL = "https://agent.test/decide"


def agent_variant(root: Path, canned: dict[str, Any], **config: Any) -> Path:
    """The agent at `root` with an `agent` variant in place of its rules, answering from
    `canned` while no live agent is set."""
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    variant = {"type": "agent", "mock_file": "fixtures/answers.json", **config}
    manifest["providers"]["reader"] = {
        "answers": ["request", "urgent"],
        "use": "agent",
        "variants": {"agent": variant},
    }
    write_yaml(root, "agent.yaml", manifest)
    (root / "fixtures").mkdir(exist_ok=True)
    (root / "fixtures" / "answers.json").write_text(json.dumps(canned))
    return root


def live_agent(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    """A live agent in place of the network: `answer(text)` gives its answers to each request.
    Returns the requests it was sent."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"answers": answer(body["input"]["message"]["body"])})

    original = AgentProvider.__init__

    def init(self: AgentProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProvider, "__init__", init)
    monkeypatch.setenv("MENDARI_AGENT_URL", AGENT_URL)
    return sent


def by_keyword(text: str) -> dict[str, Any]:
    """An agent that finds a symptom by a phrase typed exactly, as the weak rules do."""
    urgent = "chest pain" in text.lower()
    return {
        "request": {"value": "other", "confidence": 0.8},
        "urgent": {"value": "yes" if urgent else "no", "confidence": 0.95},
    }


def test_canned_answers_are_probed_on_this_machine_and_the_report_says_so(tmp_path: Path) -> None:
    """The bundled demo agent answers from a fixture that matches keywords while no agent is
    set. The probe reads those answers here and calls nothing; what it finds is what the
    fixture misses, and it says so before anything else."""
    root = tmp_path / "portal-message-agent"
    shutil.copytree(DEMO_AGENT_DIR, root)
    found = probed(root, *OFFLINE, limit=40)
    report = found.report
    assert report["canned"] == "canned answers: MENDARI_AGENT_URL is not set"
    assert report["calls"]["agent"] == 0 and report["calls"]["decisions"] > 200
    assert report["providers"] == {"reader": "agent"} and report["fields"] == ["message.body"]
    lines = {line["name"]: line for line in report["transformations"]}
    # A keyword matched in lower case does not see capitals, and misses a mistyped symptom.
    assert lines["case"]["changed"] == 0
    assert lines["typos"]["critical_lost"] > 0 and lines["typos"]["automated"] > 0
    # An instruction that names a label is a keyword too: the fixture is steered by it.
    assert lines["injection"]["automated"] > 0
    shown = probe_report.text(report).splitlines()
    assert shown[2] == (
        f"{report['calls']['decisions']} decisions, none of them a call to an agent: its answers "
        "are canned (MENDARI_AGENT_URL is not set), read on this machine. What follows is what "
        "the canned answers miss, not what your agent does: set MENDARI_AGENT_URL and probe again."
    )
    assert report["verdict"].startswith("Verdict (canned answers): not ready. ")
    # Nobody is told to fix a fixture: the verdict ends with what to probe next.
    assert report["verdict"].endswith(
        " These are canned answers: set MENDARI_AGENT_URL and probe your agent."
    )
    assert shown[-1] == report["verdict"]


def test_canned_answers_that_cannot_vary_are_not_probed_at_all(tmp_path: Path) -> None:
    """A fixture with a `default` and no `cases` answers every input alike: no variant can
    change its answer, so nothing is decided."""
    always = {"default": CANNED_BY_KEYWORD["default"]}
    root = agent_variant(build_agent(tmp_path / "agent"), always)
    pack = load_pack(root)
    planned = probe.plan(Decision(pack, origin_cap=False), probe.source(pack), Options())
    assert planned.decisions == 0 and planned.variants == ()
    assert planned.nothing == (
        "the agent's answers are canned and the same whatever a case says "
        "(fixtures/answers.json has no `cases`): set MENDARI_AGENT_URL and probe your agent"
    )
    found = probe.run(planned)
    assert found.findings == [] and found.variants == []
    assert found.report["calls"] == {"decisions": 0, "agent": 0, "generator": 0}
    assert found.report["totals"]["tried"] == 0
    assert found.report["verdict"] == f"Verdict: nothing was probed: {planned.nothing}."
    assert probe_report.exit_code(found.report, None) == 0
    # A CI job that asked to fail on findings is not told all is well.
    with pytest.raises(MendariError, match="nothing was probed, so --fail-on has nothing to pass"):
        probe_report.exit_code(found.report, "critical")


def test_a_fixture_that_matches_the_text_can_vary_and_is_probed(tmp_path: Path) -> None:
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    found = probed(root, "typos")
    assert found.report["canned"] == "canned answers: MENDARI_AGENT_URL is not set"
    assert [(f.variant.case.id, f.harm) for f in found.findings] == [
        ("symptom", "critical_lost"),
        ("symptom-and-medicine", "critical_lost"),
    ]
    assert found.report["calls"]["agent"] == 0


def test_a_live_agent_is_called_once_for_each_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = agent_variant(
        build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD, state_fields=["message.body"]
    )
    sent = live_agent(monkeypatch, by_keyword)
    found = probed(root, "typos", "case")
    assert found.report["canned"] is None
    assert found.report["calls"] == {
        "decisions": 15,
        "agent": 15,
        "generator": 0,
        "cost_usd": None,  # the agent reported no cost: unknown, never zero
    }
    assert len(sent) == 15
    assert probe_report.text(found.report).splitlines()[2] == (
        "15 decisions: 15 calls to the agent, at a cost it did not report."
    )
    assert [finding.harm for finding in found.findings] == ["critical_lost", "critical_lost"]


def test_a_live_agents_reported_cost_is_added_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    original = AgentProvider.__init__

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["input"]["message"]["body"]
        usage = {"input_tokens": 40, "output_tokens": 10, "cost_usd": 0.002}
        return httpx.Response(200, json={"answers": by_keyword(text), "usage": usage})

    def init(self: AgentProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(AgentProvider, "__init__", init)
    monkeypatch.setenv("MENDARI_AGENT_URL", AGENT_URL)
    found = probed(root, "case")
    assert found.report["calls"]["cost_usd"] == pytest.approx(0.02)
    assert probe_report.text(found.report).splitlines()[2] == (
        "10 decisions: 10 calls to the agent, $0.02."
    )


def test_an_agent_that_answers_nothing_as_written_is_not_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    sent = live_agent(monkeypatch, lambda text: {"request": {"value": "nonsense"}})
    with pytest.raises(MendariError) as refused:
        probed(root, "typos")
    assert str(refused.value) == (
        "the agent answered none of the 5 cases as written (invalid_response): there is nothing "
        "to compare a variant with"
    )
    assert len(sent) == 5  # the originals only: no variant was sent after them


def test_a_dry_run_says_how_many_calls_and_makes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = agent_variant(build_agent(tmp_path / "agent"), CANNED_BY_KEYWORD)
    sent = live_agent(monkeypatch, by_keyword)
    pack = load_pack(root)
    options = Options(transformations=("typos", "case", "punctuation"), limit=3, variants=2)
    planned = probe.plan(Decision(pack, origin_cap=False), probe.source(pack), options)
    assert sent == []
    summary = planned.summary()
    typos, case, punctuation = summary["transformations"]
    # Two variants of each of three cases by each transformation, where two differ. A text
    # has one punctuation variant whatever the seed, and one or two case variants.
    assert punctuation == {"name": "punctuation", "variants": 3, "skipped": {"same_as_another": 3}}
    assert typos["variants"] + sum(typos["skipped"].values()) == 6 and typos["variants"] >= 3
    assert case["variants"] + case["skipped"].get("same_as_another", 0) == 6
    variants = typos["variants"] + case["variants"] + 3
    decisions = 3 + variants
    assert summary["calls"] == {"decisions": decisions, "agent": decisions, "generator": 0}
    assert planned.decisions == decisions and len(planned.variants) == variants
    shown = probe_report.planned(summary).splitlines()
    assert shown[:4] == [
        "Probe triage 0.1.0 (clinic/probe-triage), reader=agent",
        "A dry run: nothing was decided, and no agent or model was called.",
        "3 of the 5 distinct cases of cases (2 with a critical label), seed 0. Text to change: "
        "message.body.",
        f"Variants: typos {typos['variants']}, case {case['variants']}, punctuation 3.",
    ]
    assert shown[4].startswith("Not built: ")
    assert shown[4].endswith("punctuation 3 (same as another).")
    assert shown[5:] == [
        f"{decisions} decisions (3 cases as written and {variants} variants): at most "
        f"{decisions} calls to the agent."
    ]
    assert sent == []


def test_a_dry_run_of_canned_or_local_answers_says_there_is_no_call(tmp_path: Path) -> None:
    def last_line(root: Path) -> str:
        pack = load_pack(root)
        planned = probe.plan(
            Decision(pack, origin_cap=False), probe.source(pack), Options(transformations=("case",))
        )
        return probe_report.planned(planned.summary()).splitlines()[-1]

    assert last_line(build_agent(tmp_path / "rules")) == (
        "10 decisions (5 cases as written and 5 variants), none of them a call to an agent."
    )
    canned = agent_variant(build_agent(tmp_path / "canned"), CANNED_BY_KEYWORD)
    assert last_line(canned) == (
        "10 decisions (5 cases as written and 5 variants), none of them a call to an agent: its "
        "answers are canned (MENDARI_AGENT_URL is not set), read on this machine."
    )
    always = agent_variant(
        build_agent(tmp_path / "always"), {"default": CANNED_BY_KEYWORD["default"]}
    )
    assert last_line(always) == (
        "Nothing to probe: the agent's answers are canned and the same whatever a case says "
        "(fixtures/answers.json has no `cases`): set MENDARI_AGENT_URL and probe your agent."
    )
