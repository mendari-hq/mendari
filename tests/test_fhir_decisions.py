# SPDX-License-Identifier: Apache-2.0
"""Today's FHIR decisions, pinned (LP-23a): every agent with a FHIR mapping (the test fixtures'
and the bundled demo agent) decides each of its synthetic FHIR cases as
tests/fixtures/fhir/decisions-golden.json says (outcome, recommendation, priority, each answer's
label and the unkeyed input hash), so a change to the FHIR path shows that it leaves readable
input as it was. And the FHIR the golden decides from conforms to R4 and US Core 6.1.0, carries
no SNOMED CT, is the same on every run, and decides as its case does."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from types import ModuleType
from typing import Any

import pytest

from mendari.core.decision import Decision
from mendari.core.pack import load_pack
from mendari.core.spec import Outcome
from mendari.sandbox.code_systems import CASE_SYSTEM, CHANNEL_SYSTEM, SANDBOX_TAG
from mendari.standards import fhir_structure, us_core
from tests.conftest import DEMO_AGENT_DIR, FIXTURE_AGENTS, FIXTURE_NAMESPACE, ROOT
from tests.support import fhir_decisions_golden as script
from tests.support import fixture_records as records


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TERMINOLOGY = "http://terminology.hl7.org/CodeSystem"
MESSAGE_SYSTEMS = {SANDBOX_TAG["system"], CASE_SYSTEM, CHANNEL_SYSTEM, "urn:ietf:bcp:47"}
# The code systems each agent's golden FHIR uses: the sandbox's own (its tag, the case id, the
# message channels), a language's BCP 47 tag, and for a chart ICD-10-CM, RxNorm, LOINC,
# ICD-10-PCS, UCUM and HL7's and US Core's own. None is SNOMED CT.
ALLOWED = {
    "mendari-test/triage": MESSAGE_SYSTEMS,
    "mendari/portal-message-agent": MESSAGE_SYSTEMS,
    "mendari-test/chart-summary": {
        SANDBOX_TAG["system"],
        CASE_SYSTEM,
        records.ICD10CM,
        records.RXNORM,
        records.LOINC,
        records.ICD10PCS,
        records.UCUM,
        f"{TERMINOLOGY}/condition-category",
        f"{TERMINOLOGY}/condition-clinical",
        "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
    },
}
URN = re.compile(r"urn:uuid:[0-9a-f-]{36}")


def bundle(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def test_every_mapped_pack_decides_its_synthetic_fhir_as_the_golden_says() -> None:
    """Every test fixture's agent with a FHIR mapping, and the bundled demo agent, is in the
    golden, with every case of its dataset, and decides each as the golden says; between them
    the cases reach every outcome. If a change is meant, rerun
    `python -m tests.support.fhir_decisions_golden` and explain each changed case in the pull
    request."""
    fixtures = [load_pack(path.parent) for path in sorted(FIXTURE_AGENTS.glob("*/agent.yaml"))]
    ours = [agent for agent in fixtures if agent.id.split("/")[0] == FIXTURE_NAMESPACE]
    mapped = {
        agent.id
        for agent in [*ours, load_pack(DEMO_AGENT_DIR)]
        if agent.manifest.inputs.fhir is not None
    }
    assert set(script.CASES) == mapped and len(mapped) == 3
    for agent_id, golden in script.CASES.items():
        assert load_pack(golden.directory).id == agent_id
    pinned = json.loads(script.GOLDEN.read_text(encoding="utf-8"))
    got = script.golden()
    assert pinned["format"] == got["format"] and pinned["today"] == got["today"]
    assert list(pinned["packs"]) == list(got["packs"]) == list(script.CASES)
    for agent_id in script.CASES:
        want, have = pinned["packs"][agent_id]["cases"], got["packs"][agent_id]["cases"]
        assert [case["id"] for case in want] == [r["id"] for r in script.records(agent_id)]
        differ = [(w, h) for w, h in zip(want, have, strict=True) if w != h]
        assert differ == [], f"{agent_id}: {len(differ)} cases differ; the first: {differ[0]}"
        assert [case["id"] for case in want if "refused" in case] == [], agent_id  # all decided
    reached = {case["outcome"] for agent in pinned["packs"].values() for case in agent["cases"]}
    assert reached == {outcome.value for outcome in Outcome}
    assert script.render(got) == script.GOLDEN.read_text(encoding="utf-8")  # byte for byte


def systems(value: Any) -> set[str]:
    """Every `system` in a FHIR value: its codings', identifiers' and tags'."""
    if isinstance(value, dict):
        own = {value["system"]} if isinstance(value.get("system"), str) else set()
        return own.union(*(systems(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(systems(item) for item in value))
    return set()


@pytest.mark.parametrize("agent_id", sorted(script.CASES))
def test_the_new_synthetic_fhir_conforms_to_us_core_and_carries_no_snomed_ct(agent_id: str) -> None:
    """The FHIR every golden case is decided from: no base R4 or US Core 6.1.0 finding, the same
    resources on every run (a message's fullUrls are fresh `urn:uuid`s, which nothing pins), and
    no SNOMED CT: only the code systems `ALLOWED` lists, and none of SNOMED CT's concept ids, its
    url, OID or a concept URI (scripts/check_snomed.py). The fullUrls' uuids hold numbers, so the
    scan for any number shaped like a concept id is the packages' only."""
    check = _script("check_snomed")
    build = script.CASES[agent_id].build
    used: set[str] = set()
    for record in script.records(agent_id):
        entries = build(record)
        again = build(record)
        assert URN.sub("urn", json.dumps(entries)) == URN.sub("urn", json.dumps(again))
        assert fhir_structure.check(bundle(entries)) == [], record["id"]
        assert us_core.validate(bundle(entries)) == [], record["id"]
        assert check.findings(f"sandbox/{record['id']}.json", json.dumps(entries)) == []
        used |= systems(entries)
    assert used == ALLOWED[agent_id]


@pytest.mark.parametrize("agent_id", sorted(script.CASES))
def test_the_new_synthetic_fhir_decides_as_its_record_does(agent_id: str) -> None:
    """Through the agent's FHIR mapping, every golden case decides as its dataset line does: the
    same input (its hash), outcome, recommendation and answers."""
    golden = script.CASES[agent_id]
    decision = Decision.from_path(golden.directory)
    with script.on_the_day():
        for record in script.records(agent_id):
            direct = decision.decide(record["input"])
            result, _ = decision.decide_fhir(bundle(golden.build(record)))
            assert (result.input_hash, result.outcome, result.recommendation) == (
                direct.input_hash,
                direct.outcome,
                direct.recommendation,
            ), record["id"]
            labels = {key: answer.label for key, answer in result.answers.items()}
            assert labels == {key: answer.label for key, answer in direct.answers.items()}
