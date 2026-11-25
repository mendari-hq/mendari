# SPDX-License-Identifier: Apache-2.0
"""Fixture F3, `mendari-test/chart-summary` (wave 6): a synthetic agent that reads a patient's
FHIR record, which the tests of record reading move onto. Synthetic test fixture, not
clinically reviewed.

Its files are what its generator writes (`tests/support/fixture_records.py`); it validates,
passes its behaviour tests with each variant and evaluates; every case's record is R4 and US
Core 6.1.0, carries no SNOMED CT, and reads back as the case; and each record-reading feature
works on it: a coding found by its system wherever it sits, `paths`, `follow` (inside the
record, contained, and as a linked read through a vendor-shaped server), the note text
(`attachment_text`), `medication_period`, a list of two resource types, the site code map, the
EHR declaration, the patient-view and order-sign cards, an unreadable prefetch abstaining, and a
patient-level Bulk export read as the dataset."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.cli import main
from mendari.core import clock, site_codes
from mendari.core.spec import Outcome
from mendari.errors import FhirError
from mendari.sandbox.code_systems import CASE_SYSTEM
from mendari.server.app import create_app
from mendari.standards import bulk, cds_hooks, fhir_structure, us_core
from mendari.standards.fhir import FhirAdapter
from mendari.standards.linked import Linker
from tests.support import chart_ehr, chart_fixture
from tests.support import fixture_records as records
from tests.support.fixture_records import (
    BASE,
    Case,
    Condition,
    Form,
    Note,
)

ROOT = Path(__file__).resolve().parents[1]
CHART = records.AGENT
NOT_FETCHED = "could not read medications.code: not_fetched"
SITE_REASON = f"site code translated ({records.CODE_SET}): a person decides"


@pytest.fixture(autouse=True)
def _on_the_day() -> Iterator[None]:
    """Decide on the day the records were built for: an age read from a birth date is the
    case's age."""
    with mock.patch.object(clock, "today", lambda: records.TODAY):
        yield


@pytest.fixture(scope="module")
def chart() -> Decision:
    return Decision.from_path(CHART)


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_for_chart", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def every_case() -> list[Case]:
    return [*records.cases("examples"), *records.cases("test")]


def case_where(test: Any) -> Case:
    """The first test case `test` holds for."""
    return next(case for case in records.cases("test") if test(case))


def codes(case: Case) -> set[str]:
    return {medication.code for medication in case.medications}


def bundle_of(resources: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [records.bundle_entry(resource) for resource in resources],
    }


def plainly_none(case: Case) -> bool:
    """A chart with nothing to follow up that the canned agent is sure of."""
    special = {records.UNSURE, records.CANNOT_TELL}
    return case.labels["follow_up"] == "none" and not special & {c.code for c in case.conditions}


# -- the generator and the agent -----------------------------------------------------------------


def test_the_committed_files_are_what_the_generator_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert records.differing() == []
    assert records.main(["--check"]) == 0
    assert capsys.readouterr().err == ""
    records._cases.cache_clear()  # made again from the seed: the same bytes
    assert records.written() == records.written()
    assert {path.relative_to(ROOT).as_posix() for path in records.written()} == {
        "tests/fixtures/agents/chart-summary/data/examples.jsonl",
        "tests/fixtures/agents/chart-summary/data/test.jsonl",
        "tests/fixtures/us-core/chart.json",
        "tests/fixtures/us-core/invalid/chart.json",
    }


def test_a_changed_file_fails_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = tmp_path / "chart.json"
    changed.write_text("{}\n")
    written = {changed: "[]\n", tmp_path / "missing.json": "[]\n"}
    monkeypatch.setattr(records, "written", lambda: written)
    monkeypatch.setattr(records, "ROOT", tmp_path)
    assert records.differing() == [changed, tmp_path / "missing.json"]
    assert records.main(["--check"]) == 1


def test_the_agent_validates_passes_its_tests_with_each_variant_and_evaluates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate", str(CHART)]) == 0
    assert main(["test", str(CHART)]) == 0
    assert main(["test", records.AGENT_ID, "--provider", "reader=rules"]) == 0
    assert "11 passed, 0 failed" in capsys.readouterr().out
    for variant in ("agent", "rules"):
        assert (
            main(["eval", str(CHART), "--dataset", "test", "--provider", f"reader={variant}"]) == 0
        )
        shown = capsys.readouterr().out
        assert "Dataset     test (n=120)" in shown
        assert "Critical follow_up=soon" in shown and "0 automated" in shown
    agent = load_agent(CHART)
    assert (agent.id, agent.manifest.spec_version, agent.manifest.risk.level) == (
        "mendari-test/chart-summary",
        "0.4",
        1,
    )
    assert agent.warnings == ()


def test_every_file_says_it_is_a_synthetic_test_fixture() -> None:
    manifest = load_agent(CHART).manifest
    for text in (manifest.summary, manifest.card.intended_use):
        assert "synthetic test fixture" in text.lower(), text
        assert "not clinically reviewed" in text.lower(), text
    assert manifest.card.provenance == "synthetic"
    for path in sorted(CHART.rglob("*")):
        if not path.is_file() or path.suffix == ".jsonl":
            continue  # the datasets are data: data/README.md says what they are
        text = path.read_text(encoding="utf-8")
        said = (
            "synthetic test fixture" in text.lower() and "not clinically reviewed" in text.lower()
        )
        assert said, path.relative_to(CHART)


def test_each_label_comes_from_the_facts_and_every_outcome_is_reached(chart: Decision) -> None:
    """The labels are the generator's rule on its facts; the canned agent automates, reviews and
    abstains on the test set, the input schema refuses a child's chart, and no chart on the watch
    list is ever automated, by either variant."""
    anticoagulants = set(records.ANTICOAGULANTS)
    for split, plan in records.PLANS.items():
        found: Counter[tuple[str, str]] = Counter()
        for case in records.cases(split):
            labels = case.labels
            found[(labels["follow_up"], labels["on_anticoagulant"])] += 1
            kinds = {records.CONDITIONS[c.code][1] for c in case.conditions}
            assert (labels["follow_up"] == "soon") == ("soon" in kinds)
            assert (labels["on_anticoagulant"] == "yes") == bool(codes(case) & anticoagulants)
            assert case.conditions  # every chart codes a condition
        assert dict(found) == plan, split
    rules = Decision(chart.agent, providers={"reader": "rules"})
    outcomes = set()
    for case in records.cases("test"):
        canned, read = chart.decide(case.input()), rules.decide(case.input())
        outcomes.add(canned.outcome)
        if case.labels["follow_up"] == "soon":
            assert Outcome.AUTO not in (canned.outcome, read.outcome), case.id
    assert outcomes == {Outcome.AUTO, Outcome.HUMAN_REVIEW, Outcome.ABSTAIN}
    child = chart.decide({"patient": {"age": 12}, "conditions": [], "medications": []})
    assert child.outcome is Outcome.BLOCK


def test_a_copy_is_another_agent_with_the_mapping_a_test_gives_it(tmp_path: Path) -> None:
    """`chart_copy` is the agent under its own id, so a copy served beside it, or loaded as from
    a registry, never stands in its place, and a test's changes merge into the mapping (None
    takes a key out)."""
    site = "https://site.example/fhir/CodeSystem/conditions"
    root = chart_fixture.chart_copy(tmp_path / "copy", systems=(site,))
    for origin in ("local", "hub"):
        agent = load_agent(root, origin=origin)
        assert (agent.id, agent.manifest.spec_version, agent.warnings) == (
            chart_fixture.COPY_ID,
            "0.4",
            (),
        )
    assert site in {use.url for use in load_agent(root).manifest.code_systems}
    changed = chart_fixture.chart_copy(
        tmp_path / "changed",
        {
            "resources": {"Medication": None},
            "fields": {"patient.age": None},
            "lists": {
                "conditions": {"resource": "Condition", "fields": {"text": {"path": "code.text"}}}
            },
        },
        use="rules",
    )
    agent = load_agent(changed)
    mapping = FhirAdapter.for_agent(agent).mapping
    assert set(mapping.resources) == {"Patient", "DocumentReference"}
    assert set(mapping.fields) == {"patient.sex", "problems", "notes"}
    assert set(mapping.lists["conditions"].sources()["Condition"].fields) == {"text"}
    assert set(mapping.lists) == {"conditions", "medications"}
    assert agent.manifest.providers["reader"].use == "rules"
    twin = load_agent(chart_fixture.order_sign(tmp_path / "twin"))
    assert (twin.id, twin.manifest.cds_hooks.hook if twin.manifest.cds_hooks else None) == (
        chart_fixture.ORDERS_ID,
        "order-sign",
    )
    assert FhirAdapter.for_agent(twin).mapping.focus == "ServiceRequest"


# -- the records ---------------------------------------------------------------------------------

ALLOWED_SYSTEMS = {
    records.ICD10CM,
    records.RXNORM,
    records.LOINC,
    records.ICD10PCS,
    records.UCUM,
    f"{records.TERMINOLOGY}/condition-category",
    f"{records.TERMINOLOGY}/condition-clinical",
    f"{records.TERMINOLOGY}/observation-category",  # the showcase's lab result
    "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
    "https://mendari.co/fhir/sandbox",
    CASE_SYSTEM,
}


def systems(value: Any) -> set[str]:
    """Every `system` in a FHIR value: its codings', identifiers', quantities' and tags'."""
    if isinstance(value, dict):
        own = {value["system"]} if isinstance(value.get("system"), str) else set()
        return own.union(*(systems(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(systems(item) for item in value))
    return set()


def test_every_record_is_r4_and_us_core_the_same_every_run_and_has_no_snomed_ct() -> None:
    check = _script("check_snomed")
    used: set[str] = set()
    for case in [*every_case(), records.SHOWCASE]:
        bundle = case.bundle()
        assert bundle == case.bundle(), case.id
        assert fhir_structure.check(bundle) == [], case.id
        assert us_core.validate(bundle) == [], case.id
        assert check.findings(f"records/{case.id}.json", json.dumps(bundle)) == [], case.id
        used |= systems(bundle)
    assert used == ALLOWED_SYSTEMS
    site = records.SHOWCASE.bundle(site="beside")
    assert fhir_structure.check(site) == [] and us_core.validate(site) == []
    assert systems(site) - used == {records.SITE_DIAGNOSES, records.SITE_DRUGS, records.SITE_ORDERS}


def test_the_us_core_fixtures_are_valid_and_invalid_as_they_say() -> None:
    check = _script("check_snomed")
    valid = json.loads((records.US_CORE_DIR / "chart.json").read_text())
    assert valid == records.us_core_chart()
    assert fhir_structure.check(valid) == [] and us_core.validate(valid) == []
    kinds = {entry["resource"]["resourceType"] for entry in valid["entry"]}
    assert kinds >= {
        "Patient", "Practitioner", "Condition", "MedicationStatement", "Medication",
        "MedicationRequest", "DocumentReference", "Observation", "ServiceRequest",
    }  # fmt: skip
    invalid = json.loads((records.US_CORE_DIR / "invalid" / "chart.json").read_text())
    assert fhir_structure.blocking(fhir_structure.check(invalid)) == []
    issues = us_core.validate(invalid)
    flagged = {issue.resource for issue in issues}
    resources = [f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in invalid["entry"]]
    assert flagged == set(resources[1:])  # every resource but the Patient, the record's focus
    invariants = {issue.diagnostics.split(":")[0] for issue in issues if issue.code == "invariant"}
    assert invariants == {
        "us-core-1",
        "us-core-3",
        "us-core-6",
        "us-core-7",
        "us-core-10",
        "us-core-15",
        "us-core-21",
    }
    for name, bundle in (("chart.json", valid), ("invalid/chart.json", invalid)):
        assert check.findings(f"us-core/{name}", json.dumps(bundle)) == [], name


def test_every_record_reads_back_as_its_case_and_decides_as_it(chart: Decision) -> None:
    """Through the agent's mapping, each case's record gives exactly the case's input (so a
    dataset line and its record decide alike), with the site's own codes beside the standard
    ones too: each coding is found by its system wherever it sits."""
    adapter = FhirAdapter.for_agent(chart.agent)
    for case in [*every_case(), records.SHOWCASE]:
        for site in (None, "beside"):
            assert adapter.read(adapter.index(case.bundle(site=site))).value == case.input(), (
                case.id
            )
    for case in records.cases("examples"):
        result, task = chart.decide_fhir(case.bundle())
        direct = chart.decide(case.input())
        assert (result.outcome, result.recommendation) == (direct.outcome, direct.recommendation)
        assert result.input_hash == direct.input_hash and result.reasons == direct.reasons
        assert task["for"] == {"reference": f"Patient/{case.id}"}


# -- each record-reading feature -----------------------------------------------------------------


def test_a_coding_is_read_by_its_system_and_a_site_code_alone_by_the_first_coding(
    chart: Decision,
) -> None:
    """`[system=...]` finds the ICD-10-CM coding wherever it sits; a condition coded only in the
    site's own codes falls back to the first coding (`paths`), and its code set gives nothing."""
    adapter = FhirAdapter.for_agent(chart.agent)
    case = Case("chart-reading-1", 70, "female", conditions=(Condition("I50.9", "2024-02-10"),))
    own = adapter.read(adapter.index(case.bundle())).value["conditions"]
    beside = adapter.read(adapter.index(case.bundle(site="beside"))).value["conditions"]
    only = adapter.read(adapter.index(case.bundle(site="only"))).value["conditions"]
    assert own == beside and own[0]["code"] == "I50.9" and own[0]["flag"] == records.FLAG_SOON
    assert only == [
        {
            "code": "DX-4471",
            "text": "Heart failure, unspecified",
            "status": "active",
            "onset": "2024-02-10",
        }
    ]


def test_a_requests_drug_is_followed_inside_the_record_and_its_period_derived(
    chart: Decision,
) -> None:
    """A MedicationRequest's drug through a Medication beside it, one it contains, or its own
    concept; its period from its dosage bounds, a duration, or when it was written; and a
    statement's from its own period. The list reads both types, statements first."""
    adapter = FhirAdapter.for_agent(chart.agent)
    medications = adapter.read(adapter.index(records.SHOWCASE.bundle())).value["medications"]
    assert [(m["code"], m["name"], m.get("start"), m.get("end")) for m in medications] == [
        ("6809", "metformin 500 mg tablet", "2021-02-01", None),  # a statement, ongoing
        ("11289", "warfarin 5 mg tablet", "2025-11-03", "2026-11-02"),  # by reference, bounds
        ("83367", "atorvastatin 20 mg tablet", "2024-05-20", None),  # contained, only written
        ("29046", "lisinopril 10 mg tablet", "2026-08-01", "2026-10-30"),  # its concept, 90 days
    ]
    assert {m["status"] for m in medications} == {"active"}
    # Without the Medication beside it, the reference is a link the record does not hold: with
    # no server to read it from, `not_fetched`, and the decision abstains.
    entries = records.SHOWCASE.bundle()["entry"]
    away = [e for e in entries if e["resource"]["resourceType"] != "Medication"]
    assert len(away) == len(entries) - 1
    result, _ = chart.decide_fhir(bundle_of(e["resource"] for e in away))
    assert result.outcome is Outcome.ABSTAIN and result.reasons == (NOT_FETCHED,)


def test_a_linked_read_goes_to_the_vendor_shaped_server(chart: Decision) -> None:
    """The EHR keeps every drug as a request naming a Medication and every note in a Binary (an
    HTML rendition and its RTF twin): the record a hook holds has neither, so each is read once,
    by its link, from the server; the RTF twin of a note read as HTML is never read."""
    case = case_where(lambda c: c.notes and codes(c) & set(records.ANTICOAGULANTS))
    ehr = chart_ehr.ChartEhr.of(case, rendition="twin", token=chart_ehr.TOKEN)
    held = [r for r in ehr.held.values() if r["resourceType"] not in ("Medication", "Binary")]
    bundle = bundle_of(held)
    without, _ = chart.decide_fhir(bundle)
    assert without.outcome is Outcome.ABSTAIN
    assert set(without.reasons) == {NOT_FETCHED, "could not read notes: not_fetched"}
    result, _ = chart.decide_fhir(bundle, links=Linker.of(ehr.client()))
    assert (
        result.outcome is not Outcome.ABSTAIN and result.answers["on_anticoagulant"].label == "yes"
    )
    binaries = [path for path in ehr.reads() if path.startswith("Binary/")]
    drugs = [path for path in ehr.reads() if path.startswith("Medication/")]
    assert len(binaries) == len(case.notes) and all(path.endswith("-1") for path in binaries)
    assert len(drugs) == len(case.medications) == len(set(drugs))
    assert result.data_coverage is not None
    assert result.data_coverage["links"]["read"] == {"follows": len(drugs), "notes": len(binaries)}
    assert all(r.headers["authorization"] == f"Bearer {chart_ehr.TOKEN}" for r in ehr.requests)


def test_the_note_is_read_as_text_and_an_rtf_note_abstains(chart: Decision) -> None:
    """A note is read as plain text or HTML (a line a block), inline or in the Binary its url
    names; the rules read its words. A note the EHR keeps only as RTF cannot be read: the
    decision abstains, and says so by reason and format, never by its text."""
    adapter = FhirAdapter.for_agent(chart.agent)
    sentences = ("Seen today.", "Follow-up within a week.", "Takes warfarin every day.")
    hypertension = (Condition("I10", "2016-07-07"),)
    forms: tuple[tuple[Form, str], ...] = (
        ("plain", " ".join(sentences)),
        ("html", "\n".join(sentences)),
        ("binary", " ".join(sentences)),  # a Binary beside it in the record, by a relative url
    )
    for form, text in forms:
        case = Case("chart-note-1", 60, "male", hypertension, notes=(Note(sentences, form),))
        read = adapter.read(adapter.index(case.bundle())).value["notes"]
        assert read == text == case.input()["notes"], form
    rules = Decision(chart.agent, providers={"reader": "rules"})
    case = Case("chart-note-2", 60, "male", hypertension, notes=(Note(sentences, "html"),))
    read = rules.decide_fhir(case.bundle())[0]
    assert (read.recommendation, read.answers["on_anticoagulant"].label) == ("soon", "yes")
    ehr = chart_ehr.ChartEhr.of(case, rendition="rtf")
    result, _ = chart.decide_fhir(bundle_of(ehr.held.values()))
    assert result.outcome is Outcome.ABSTAIN
    assert result.reasons == ("could not read notes: unsupported_type",)
    assert result.data_coverage is not None
    assert "Follow-up" not in json.dumps(result.data_coverage)


def test_a_site_code_is_translated_by_the_site_code_map_and_never_automated(
    chart: Decision,
) -> None:
    """A long-term condition coded only in the site's own code: read through the site's map it
    is the agent's code (`follow_up_conditions`), and the decision that would be automated goes
    to a person; without the map it is not found. Coded the agent's way it is automated with the
    map as without, and the decision records the map's version and digest."""
    case = Case("chart-site-1", 58, "male", (Condition("E11.9", "2018-05-02"),))
    maps = site_codes.parse(json.dumps(records.site_code_map()), signed_in=False)
    mapped = Decision(chart.agent, site_codes=maps)
    stamp = {"version": "chart-site-1", "sha256": maps.maps["default"].sha256}
    own, _ = mapped.decide_fhir(case.bundle())
    assert (own.outcome, own.recommendation, own.site_codes) == (Outcome.AUTO, "routine", stamp)
    translated, _ = mapped.decide_fhir(case.bundle(site="only"))
    assert (translated.outcome, translated.recommendation) == (Outcome.HUMAN_REVIEW, "routine")
    assert translated.reasons == ("confidence 0.95 >= auto threshold 0.90", SITE_REASON)
    adapter = FhirAdapter.for_agent(chart.agent)
    indexed = adapter.index(case.bundle(site="only"))
    read = adapter.read(indexed, site_codes=maps.maps["default"].sets)
    assert read.translated == (records.CODE_SET,)
    assert read.value["conditions"] == [
        {
            "code": "DX-1001",  # the first coding, the site's own
            "flag": records.FLAG_ROUTINE,  # the agent's code for it, through the map
            "text": "Type 2 diabetes mellitus without complications",
            "status": "active",
            "onset": "2018-05-02",
        }
    ]
    untranslated, _ = chart.decide_fhir(case.bundle(site="only"))
    assert (untranslated.recommendation, untranslated.site_codes) == ("none", None)
    assert "flag" not in adapter.read(indexed).value["conditions"][0]


def test_the_ehr_declaration_leaves_medication_statements_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the CDS service, the EHR refuses MedicationStatement searches (as Epic, which
    has no R4 MedicationStatement API). Declared not offered for its FHIR base, the hook never
    searches them, reads the medications from MedicationRequests, records the declaration and
    never automates; undeclared, the refusal abstains. The setting is checked at startup
    against this agent: MedicationStatement is one of two sources of its medications."""
    case = case_where(lambda c: c.labels["follow_up"] == "routine" and c.medications and c.notes)
    ehr = chart_ehr.ChartEhr.of(case)
    ehr.install(monkeypatch)
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{BASE}=default:decide")
    request = chart_fixture.hook(case, prefetched=False, fhir_server=BASE, token=chart_ehr.TOKEN)

    def called(db: Path) -> dict[str, Any]:
        with TestClient(create_app(db_path=db)) as app:
            response = app.post(chart_fixture.SERVICE, json=request)
        assert response.status_code == 200, response.text
        with AuditStore(db) as store:
            found = [store.get(item["id"]) for item in store.query(limit=10)]
        assert len(found) == 1 and found[0] is not None
        return dict(found[0]["record"])

    refused = called(tmp_path / "undeclared.db")
    assert refused["outcome"] == "ABSTAIN"
    assert refused["reasons"] == ["could not read medications: unauthorized"]
    monkeypatch.setenv("MENDARI_EHR_DECLARATIONS", json.dumps(records.ehr_declaration()))
    ehr.requests.clear()
    record = called(tmp_path / "declared.db")
    assert record["ehr_declaration"] is not None
    assert record["ehr_declaration"]["attested_by"] == "A. Fictional (synthetic test)"
    assert record["outcome"] == "HUMAN_REVIEW"
    assert any(reason.startswith("medications may be incomplete") for reason in record["reasons"])
    assert record["data_coverage"]["prefetch"]["medications"] == {
        "resource": "MedicationStatement",
        "not_offered": True,
    }
    assert not [search for search in ehr.searches() if search.startswith("MedicationStatement")]
    assert len(record["input"]["medications"]) == len(case.medications)


def test_the_patient_view_cards_follow_the_agents_wording(chart: Decision) -> None:
    """With the record as search prefetch: a card for a chart to see soon or for a routine
    follow-up, in the agent's words with display names; none for a chart with nothing to follow
    up, which `show` leaves out."""
    soon = case_where(lambda c: c.labels["follow_up"] == "soon")
    routine = case_where(lambda c: c.labels == {"follow_up": "routine", "on_anticoagulant": "yes"})
    quiet = case_where(plainly_none)
    [card] = cds_hooks.invoke(chart, chart_fixture.hook(soon))["cards"]
    assert card["summary"].startswith("Synthetic chart summary: see the patient soon")
    assert card["indicator"] == "warning"
    [card] = cds_hooks.invoke(chart, chart_fixture.hook(routine))["cards"]
    assert (
        card["summary"] == "Synthetic chart summary: routine follow-up; anticoagulant listed: Yes"
    )
    assert cds_hooks.invoke(chart, chart_fixture.hook(quiet)) == {"cards": []}


def test_an_unreadable_prefetch_abstains_with_a_card(chart: Decision) -> None:
    case = case_where(lambda c: c.labels["follow_up"] == "routine")
    request = chart_fixture.hook(case)
    request["prefetch"]["conditions"] = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "forbidden"}],
    }
    [card] = cds_hooks.invoke(chart, request)["cards"]
    assert card["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert "- conditions: operation_outcome" in card["detail"]
    result, _ = chart.decide_fhir(
        chart_fixture.prefetch(case), unreadable={"conditions": "operation_outcome"}
    )
    assert result.outcome is Outcome.ABSTAIN and result.answers == {}
    assert result.reasons == ("could not read conditions: operation_outcome",)


def test_the_order_sign_twin_decides_each_checked_order(tmp_path: Path) -> None:
    """The twin decides each draft order whose code is one of its own (ICD-10-PCS), wherever
    the code sits among the order's codings, with the patient's chart, and gives each a card;
    another order (a lab test) gets none. An order coded only in the site's own order code
    counts through the site's map alone, and its decision is never automated."""
    twin = Decision(load_agent(chart_fixture.order_sign(tmp_path)))
    case = case_where(
        lambda c: (
            c.labels["follow_up"] == "routine"
            and records.UNSURE not in {x.code for x in c.conditions}
        )
    )
    lab = {
        **chart_fixture.draft(case),
        "id": "lab-1",
        "code": {"coding": [{"system": records.LOINC, "code": "6301-6"}]},
    }
    second = {**chart_fixture.draft(case), "id": "order-2"}
    answer = cds_hooks.invoke(
        twin, chart_fixture.order_hook(case, chart_fixture.draft(case), lab, second)
    )
    focuses = [
        c["suggestions"][0]["actions"][0]["resource"]["focus"]["reference"] for c in answer["cards"]
    ]
    assert focuses == [f"ServiceRequest/{case.id}-order", "ServiceRequest/order-2"]
    assert cds_hooks.invoke(twin, chart_fixture.order_hook(case, lab)) == {"cards": []}
    result, task = twin.decide_fhir(case.bundle(site="beside"))
    assert result.outcome is Outcome.AUTO and task["focus"] == {
        "reference": f"ServiceRequest/{case.id}-order"
    }
    adapter = FhirAdapter.for_agent(twin.agent)
    read = adapter.read(adapter.index(case.bundle(site="beside"))).value
    assert read["order"] == {"code": case.order, "text": records.ORDERS[case.order][1]}
    assert {key: read[key] for key in case.input()} == case.input()  # and the chart, as it is
    maps = site_codes.parse(json.dumps(records.site_code_map(orders=True)), signed_in=False)
    only, _ = Decision(twin.agent, site_codes=maps).decide_fhir(case.bundle(site="only"))
    assert only.outcome is Outcome.HUMAN_REVIEW
    assert only.reasons[-1] == (
        f"site code translated ({records.ORDER_CODE_SET}, {records.CODE_SET}): a person decides"
    )
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        twin.decide_fhir(case.bundle(site="only"))  # without the map, no order is its to decide


def test_a_bulk_export_reads_as_the_dataset() -> None:
    """A patient-level Bulk export of the test cases' records (each resource by type, as NDJSON
    gives them), labelled by the dataset: each case's input and labels are the dataset's."""
    adapter = FhirAdapter.for_agent(load_agent(CHART))
    cases = records.cases("test")[:12]
    resources = [r for case in cases for r in case.resources()]
    labels = bulk.load_labels(records.AGENT / "data" / "test.jsonl")
    made, skipped = bulk.dataset(adapter, resources, labels, key_system=CASE_SYSTEM)
    assert skipped == {"unlabelled": 0, "no_key": 0}
    assert made == [case.record() for case in cases]
