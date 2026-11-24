# SPDX-License-Identifier: Apache-2.0
"""Today's FHIR decisions, pinned (LP-23a): every pack with a FHIR mapping decides each of its
synthetic FHIR cases as tests/fixtures/fhir/decisions-golden.json says (outcome,
recommendation, priority, each answer's label and the unkeyed input hash), so the LP-23 steps
that change the FHIR path show that they leave readable input as it was. And the synthetic FHIR
the trial and prior-authorization packs gained (`trial_entries`, `prior_auth_entries`) conforms
to US Core 6.1.0, carries no SNOMED CT, is the same on every run, and decides as its record."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from mendari.core.decision import Decision
from mendari.core.pack import load_pack
from mendari.core.spec import Outcome
from mendari.errors import FhirError
from mendari.evals.harness import load_dataset
from mendari.standards import fhir_structure, us_core
from mendari.standards.fhir import FhirAdapter
from tests.support import fhir_decisions_golden as script
from tests.support.fhir_records import prior_auth_entries, trial_entries

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TODAY = script.TODAY
BUILT = {
    "mendari/clinical-trial-prescreener": ("trial-local-site-example.jsonl", trial_entries),
    "mendari/prior-auth-preflight": ("prior-auth-local-site-example.jsonl", prior_auth_entries),
}


def bundle(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def records(dataset: str) -> list[dict[str, Any]]:
    return load_dataset(ROOT / "tests" / "fixtures" / "datasets" / dataset).records


def test_every_mapped_pack_decides_its_synthetic_fhir_as_the_golden_says() -> None:
    """Every reference pack with a FHIR mapping is in the golden, with every case of its
    local-site example, and decides each as the golden says. If a change is meant (LP-23k, a
    pack adopting mapping syntax 0.3), rerun `python -m tests.support.fhir_decisions_golden` and
    explain each changed case in the pull request."""
    mapped = {
        pack.id
        for pack in (
            load_pack(path.parent)
            for path in sorted(ROOT.glob("tests/fixtures/reference-agents/*/agent.yaml"))
        )
        if pack.manifest.inputs.fhir is not None
    }
    assert set(script.CASES) == mapped and len(mapped) == 4
    pinned = json.loads(script.GOLDEN.read_text(encoding="utf-8"))
    got = script.golden()
    assert pinned["format"] == got["format"] and pinned["today"] == got["today"]
    assert list(pinned["packs"]) == list(got["packs"]) == list(script.CASES)
    for pack_id, (_, dataset, _) in script.CASES.items():
        want, have = pinned["packs"][pack_id]["cases"], got["packs"][pack_id]["cases"]
        assert [case["id"] for case in want] == [record["id"] for record in records(dataset)]
        differ = [(w, h) for w, h in zip(want, have, strict=True) if w != h]
        assert differ == [], f"{pack_id}: {len(differ)} cases differ; the first: {differ[0]}"
        assert sum("refused" not in case for case in want) >= 100, pack_id  # decided, mostly
    assert script.render(got) == script.GOLDEN.read_text(encoding="utf-8")  # byte for byte


def systems(value: Any) -> set[str]:
    """Every `system` in a FHIR value: its codings', identifiers' and tags'."""
    if isinstance(value, dict):
        own = {value["system"]} if isinstance(value.get("system"), str) else set()
        return own.union(*(systems(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(systems(item) for item in value))
    return set()


@pytest.mark.parametrize("pack_id", sorted(BUILT))
def test_the_new_synthetic_fhir_conforms_to_us_core_and_carries_no_snomed_ct(pack_id: str) -> None:
    """No base R4 or US Core 6.1.0 finding on any case, the same entries on every run (fullUrls
    from the case id), and no SNOMED CT: only the code systems listed here (ICD-10-CM, LOINC,
    ICD-10-PCS, HL7's own, the fictional ones and the sandbox's), and none of SNOMED CT's
    concept ids, its url, OID or a concept URI (scripts/check_snomed.py). The fullUrls' uuids
    hold numbers, so the scan for any number shaped like a concept id is the packages' only."""
    check = _script("check_snomed")
    dataset, build = BUILT[pack_id]
    hl7 = "http://terminology.hl7.org/CodeSystem/"
    allowed = {
        "http://hl7.org/fhir/sid/icd-10-cm",
        "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
        "http://loinc.org",
        f"{hl7}condition-category",
        "https://mendari.co/fhir/sandbox",
        "https://mendari.co/fhir/synthetic-case",
    }
    allowed |= {
        "mendari/clinical-trial-prescreener": {
            f"{hl7}condition-clinical",
            f"{hl7}observation-category",
            "https://mendari.co/fhir/CodeSystem/fictional-conditions",
        },
        "mendari/prior-auth-preflight": {
            f"{hl7}subscriber-relationship",
            "http://www.cms.gov/Medicare/Coding/ICD10",  # ICD-10-PCS, what a payer reads
            "https://mendari.co/fhir/CodeSystem/fictional-procedures",
        },
    }[pack_id]
    used: set[str] = set()
    for record in records(dataset):
        entries = build(record, today=TODAY)
        assert entries == build(record, today=TODAY), record["id"]
        assert fhir_structure.check(bundle(entries)) == [], record["id"]
        assert us_core.validate(bundle(entries)) == [], record["id"]
        assert check.findings(f"sandbox/{record['id']}.json", json.dumps(entries)) == []
        used |= systems(entries)
    assert used == allowed


@pytest.mark.parametrize("pack_id", sorted(BUILT))
def test_the_new_synthetic_fhir_decides_as_its_record_does(pack_id: str) -> None:
    """Through the pack's FHIR mapping, a case decides as its record does (the same outcome,
    recommendation and answers), but for an order the pack has no requirements for: the FHIR
    path finds no order to decide (its `focus_where`), where the record is decided as `other`,
    at HUMAN_REVIEW."""
    dataset, build = BUILT[pack_id]
    decision = Decision.from_path(
        ROOT / "tests" / "fixtures" / "reference-agents" / pack_id.split("/")[1]
    )
    adapter = FhirAdapter.for_pack(decision.pack)
    focus = adapter.mapping.focus_where
    refused = 0
    with script.on_the_day():
        for record in records(dataset):
            direct = decision.decide(record["input"])
            try:
                result, _ = decision.decide_fhir(bundle(build(record, today=TODAY)))
            except FhirError as error:
                assert focus is not None and focus.values is not None
                assert record["input"]["order"]["code"] not in focus.values
                assert str(error) == "missing required FHIR resources: ServiceRequest"
                assert direct.answers["service"].label == "other", record["id"]
                assert direct.outcome is Outcome.HUMAN_REVIEW, record["id"]
                refused += 1
                continue
            assert (result.outcome, result.recommendation) == (
                direct.outcome,
                direct.recommendation,
            ), record["id"]
            labels = {key: answer.label for key, answer in result.answers.items()}
            assert labels == {key: answer.label for key, answer in direct.answers.items()}
    assert refused == (41 if pack_id == "mendari/prior-auth-preflight" else 0)
