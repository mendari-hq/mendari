# SPDX-License-Identifier: Apache-2.0
"""`mendari probe` (`mendari.evals.probe`): it rewrites an agent's own cases in ways that keep
their meaning, finds the variants the agent answers otherwise, ranks them by harm and writes each
as a behaviour test. Every agent here is built in a temporary directory or is a copy of the
bundled demo agent: nothing is written into the repository."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari import Decision, load_pack
from mendari.core import pack_store
from mendari.core.pack import pack_digest
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import (
    PROBE_TESTS,
    PROBE_UNCONFIRMED,
    every_case,
    load_cases,
    probe_cases,
    run_pack_tests,
)
from mendari.evals.validation import problems, validate
from tests.conftest import DEMO_AGENT_DIR
from tests.probe_agents import MANIFEST, TESTS, build_agent


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
