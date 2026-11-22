# SPDX-License-Identifier: Apache-2.0
"""The Clinical Trial Prescreener reference pack and its synthetic data."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from mendari import Decision
from mendari.cli import main
from mendari.core import clock
from mendari.core.policy import Outcome
from mendari.sandbox import synthetic_trial
from mendari.standards import cds_hooks
from mendari.standards.fhir import FhirAdapter

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "tests" / "fixtures" / "reference-packs" / "clinical-trial-prescreener"
TODAY = clock.today()  # the engine's day (the display time zone), not the system's


@pytest.fixture(autouse=True)
def _one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine decides on the day these records were dated from, even when the run crosses
    midnight: the 365-day and 90-day boundaries below are exact to the day."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)


ICD = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SITE = "https://site.example/fhir/CodeSystem/conditions"  # a fictional site's own codes


@pytest.fixture(scope="module")
def trial() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_trial.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "calibration": 200,
        "test": 300,
    }
    assert splits == synthetic_trial.public_splits()
    assert len(synthetic_trial.local_site()) == 150


def test_ground_truth_follows_the_studys_table() -> None:
    for record in synthetic_trial.public_splits()["test"]:
        labels = record["labels"]
        assert labels["eligibility"] == synthetic_trial.eligibility(labels)
    outcomes = Counter(r["labels"]["eligibility"] for r in synthetic_trial.public_splits()["test"])
    assert set(outcomes) == {"candidate", "not_eligible", "needs_review"}


def test_the_criteria_agree_with_ground_truth_on_public_data(trial: Decision) -> None:
    """Public records use only the codes and units the pack lists, so the structured criteria
    must be exact; any disagreement is a bug in the generator or the criteria."""
    provider = trial.providers["structured"]
    for record in synthetic_trial.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in synthetic_trial.STRUCTURED:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_notes_never_contradict_their_own_labels() -> None:
    records = [*synthetic_trial.public_splits()["test"], *synthetic_trial.local_site()]
    for record in records:
        notes, labels = record["input"]["notes"], record["labels"]
        if labels["note_pancreatitis"] == "yes":
            assert "No history of pancreatitis" not in notes
            assert "Sin antecedentes de pancreatitis" not in notes
        if labels["note_pregnancy"] == "yes":
            assert "Pregnancy test negative" not in notes
            assert record["attributes"]["sex"] == "female"


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_trial.local_site()
    text = json.dumps([r["input"] for r in local], ensure_ascii=False)
    for marker in ("mmol/mol", "FC101", "17856-6", "Glucophage"):
        assert marker in text
    spanish = sum(r["attributes"]["language"] == "es" for r in local) / len(local)
    assert 0.2 < spanish < 0.4


def test_sandbox_generate_writes_the_trial_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "trial", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    test = (tmp_path / "pack" / "data" / "test.jsonl").read_text().splitlines()
    assert len(test) == 300 and json.loads(test[0])["id"] == "trial-0200"
    assert len(local.read_text().splitlines()) == 150


# -- decisions ---------------------------------------------------------------------------------


def test_every_decision_is_reviewed_at_risk_level_2(trial: Decision) -> None:
    for record in synthetic_trial.public_splits()["test"][:60]:
        assert trial.decide(record["input"]).outcome is not Outcome.AUTO


def test_reasons_name_the_deciding_criteria(trial: Decision) -> None:
    record = {
        "as_of": "2026-09-01",
        "patient": {"age": 54},
        "conditions": [{"code": "E11.9"}],
        "observations": [{"code": "4548-4", "value": 11.2, "unit": "%", "date": "2026-08-01"}],
        "medications": [{"name": "metformin", "status": "active", "start": "2025-01-01"}],
        "notes": "History of pancreatitis in 2019.",
    }
    result = trial.decide(record)
    assert result.recommendation == "not_eligible"
    assert "not_eligible: hba1c_in_range is no" in result.reasons
    assert "not_eligible: note_pancreatitis is yes" in result.reasons
    assert any("egfr_30_or_more" in reason for reason in result.reasons)  # no eGFR: unknown


def _note(text: str) -> dict[str, Any]:
    data = base64.b64encode(text.encode()).decode()
    return {
        "resourceType": "DocumentReference",
        "id": "n1",
        "content": [{"attachment": {"contentType": "text/plain", "data": data}}],
    }


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _record(notes: str = "Seen for diabetes follow-up.") -> list[dict[str, Any]]:
    def coded(resource: str, id_: str, system: str, code: str, **extra: Any) -> dict[str, Any]:
        coding = [{"system": system, "code": code}]
        return {"resourceType": resource, "id": id_, "code": {"coding": coding}, **extra}

    def lab(id_: str, code: str, value: float, unit: str, days: int) -> dict[str, Any]:
        quantity = {"valueQuantity": {"value": value, "unit": unit}}
        return coded("Observation", id_, LOINC, code, effectiveDateTime=_ago(days), **quantity)

    def medication(id_: str, text: str, status: str, **period: str) -> dict[str, Any]:
        return {
            "resourceType": "MedicationStatement",
            "id": id_,
            "status": status,
            "medicationCodeableConcept": {"text": text},
            "effectivePeriod": period,
        }

    status = {"clinicalStatus": {"coding": [{"code": "active"}]}}
    return [
        {"resourceType": "Patient", "id": "p1", "birthDate": f"{TODAY.year - 58}-01-01"},
        coded("Condition", "c1", ICD, "E11.9", **status),
        coded("Condition", "c2", ICD, "I10", **status),
        coded("Condition", "c3", ICD, "I21.4", onsetDateTime=_ago(900)),  # remote MI
        lab("o1", "4548-4", 64, "mmol/mol", 20),  # 8.0 %
        lab("o2", "4548-4", 12.5, "%", 300),  # older than 90 days: ignored
        lab("o3", "33914-3", 58, "mL/min/1.73m2", 100),
        lab("o4", "8480-6", 132, "mm[Hg]", 20),
        medication("m1", "metformin 1000 mg tablet", "active", start=_ago(200)),
        medication("m2", "insulin glargine", "completed", start=_ago(900), end=_ago(400)),
        _note(notes),
    ]


def test_a_fhir_record_is_prescreened(trial: Decision) -> None:
    entries = [{"resource": r} for r in _record()]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": entries}
    result, task = trial.decide_fhir(bundle)
    assert result.recommendation == "candidate" and result.outcome is Outcome.HUMAN_REVIEW
    assert {q: result.answers[q].label for q in synthetic_trial.STRUCTURED} == dict.fromkeys(
        synthetic_trial.STRUCTURED, "yes"
    )
    assert result.answers["no_insulin"].label == "yes"  # insulin ended 400 days ago
    assert task["for"] == {"reference": "Patient/p1"}
    assert task["description"].startswith("GLYCEMIA-2 prescreen: candidate.")


def test_through_fhir_a_condition_is_read_by_its_icd10_coding_wherever_it_is(
    trial: Decision,
) -> None:
    """The FHIR mapping (spec 0.3) reads a Condition by its ICD-10-CM coding wherever it sits
    among its codings, so a site's own coding before it no longer hides it: type 2 diabetes is
    found and a heart attack 40 days ago rules the patient out, in either order (the 0.2 mapping
    read the first coding only, a miss the owner accepted LP-23's selector would fix, on
    2026-10-08). With no ICD-10-CM or fictional coding at all, the first coding is read as the
    EHR sends it, as before: a site's own code, which the criteria match only once the site
    lists it (type 2 diabetes is `unknown`, the heart attack not found)."""

    def decide(order: str) -> tuple[dict[str, str], list[str]]:
        record = _record()
        for resource, local, code in ((record[1], "T2D", "E11.9"), (record[3], "MI", "I21.4")):
            site, icd = {"system": SITE, "code": local}, {"system": ICD, "code": code}
            codings = {"site first": [site, icd], "icd first": [icd, site], "site only": [site]}
            resource["code"] = {"coding": codings[order]}
        record[3]["onsetDateTime"] = _ago(40)
        entries = [{"resource": r} for r in record]
        bundle: dict[str, Any] = {"resourceType": "Bundle", "type": "collection", "entry": entries}
        result, _ = trial.decide_fhir(bundle)
        assert result.outcome is Outcome.HUMAN_REVIEW
        answers = {q: result.answers[q].label for q in ("t2d_diagnosis", "no_recent_cv_event")}
        mapped = FhirAdapter.for_pack(trial.pack)
        read = mapped.to_input(mapped.index(bundle))
        return answers, [condition["code"] for condition in read["conditions"]]

    found = {"t2d_diagnosis": "yes", "no_recent_cv_event": "no"}
    assert decide("site first") == decide("icd first") == (found, ["E11.9", "I10", "I21.4"])
    missed = {"t2d_diagnosis": "unknown", "no_recent_cv_event": "yes"}
    assert decide("site only") == (missed, ["T2D", "I10", "MI"])


def _medication_request(
    id_: str, status: str, *, bounds: dict[str, str] | None = None, **more: Any
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": id_,
        "status": status,
        "intent": "order",
        "subject": {"reference": "Patient/p1"},
        **more,
    }
    if bounds is not None:
        request["dosageInstruction"] = [{"timing": {"repeat": {"boundsPeriod": bounds}}}]
    return request


def test_a_medication_request_is_read_with_its_stated_start_and_stop(trial: Decision) -> None:
    """A MedicationRequest is a medication of the trial's record, by spec 0.3's stated rule
    (`medication_period`; notes/clinician-review-medication-period.md, the owner's changes in
    `test_the_owners_medication_rules_hold_through_fhir`): it starts at its first dosage
    instruction's `boundsPeriod.start`, else on `authoredOn`, and stops at that `boundsPeriod.end`
    (here none has a `boundsDuration`), else its stop is unknown, never inferred from the status;
    the dispensing window is not read; one entered in error is left out. Its drug is the RxNorm
    coded Medication it names (in the record, or contained), else its own coded concept. So the
    criteria read metformin started 200 days ago from the bounds although the order was written
    30 days ago, insulin as current until a bounds end 400 days ago, a `completed` insulin with no
    bounds as not current (its stop unknown), and an insulin entered in error not at all."""
    metformin = {"coding": [{"system": RXNORM, "code": "6809"}], "text": "metformin 1000 mg"}
    medication = {"resourceType": "Medication", "id": "med1", "code": metformin}
    insulin = {"coding": [{"system": RXNORM, "code": "274783"}], "text": "insulin glargine"}
    validity = {"validityPeriod": {"start": _ago(30), "end": _ago(-60)}}
    record = [r for r in _record() if r["resourceType"] != "MedicationStatement"]
    requests = [
        _medication_request(
            "mr1",
            "active",
            bounds={"start": _ago(200)},
            authoredOn=_ago(30),
            medicationReference={"reference": "Medication/med1", "display": "metformin"},
            dispenseRequest=validity,
        ),
        _medication_request(
            "mr2",
            "stopped",
            bounds={"start": _ago(900), "end": _ago(400)},
            authoredOn=_ago(905),
            medicationCodeableConcept=insulin,
        ),
        _medication_request(
            "mr3", "completed", authoredOn=_ago(20), medicationCodeableConcept=insulin
        ),
        _medication_request(
            "mr4", "entered-in-error", bounds={"start": _ago(10)}, medicationCodeableConcept=insulin
        ),
    ]
    mapped = FhirAdapter.for_pack(trial.pack)

    def decided(*resources: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
        entries = [{"resource": r} for r in [*record, *resources]]
        bundle: dict[str, Any] = {"resourceType": "Bundle", "type": "collection", "entry": entries}
        result, _ = trial.decide_fhir(bundle)
        return result, mapped.to_input(mapped.index(bundle))["medications"]

    result, medications = decided(*requests, medication)
    assert medications == [
        {"code": "6809", "name": "metformin 1000 mg", "status": "active", "start": _ago(200)},
        {
            "code": "274783",
            "name": "insulin glargine",
            "status": "stopped",
            "start": _ago(900),
            "end": _ago(400),
        },
        {"code": "274783", "name": "insulin glargine", "status": "completed", "start": _ago(20)},
    ]
    assert result.answers["stable_metformin"].label == "yes"  # from the bounds, not authoredOn
    assert result.answers["no_insulin"].label == "yes"  # ended 400 days ago; the other not active
    assert result.recommendation == "candidate" and result.outcome is Outcome.HUMAN_REVIEW

    # With no bounds, the start is the day it was written: a renewal 30 days ago reads as metformin
    # started 30 days ago (a known failure mode the card states).
    renewed = _medication_request(
        "mr1",
        "active",
        authoredOn=_ago(30),
        medicationReference={"reference": "Medication/med1"},
    )
    result, medications = decided(renewed, medication)
    assert medications == [
        {"code": "6809", "name": "metformin 1000 mg", "status": "active", "start": _ago(30)}
    ]
    assert result.answers["stable_metformin"].label == "no"

    # A drug named by a reference the record does not hold: not fetched here, so the decision
    # abstains rather than read the patient as not on it.
    linked = _medication_request(
        "mr1",
        "active",
        bounds={"start": _ago(500)},
        medicationReference={"reference": "Medication/x"},
    )
    result, _ = decided(linked)
    assert result.outcome is Outcome.ABSTAIN
    assert set(result.reasons) == {
        "could not read medications.code: not_fetched",
        "could not read medications.name: not_fetched",
    }


def test_the_owners_medication_rules_hold_through_fhir(trial: Decision) -> None:
    """The changes the owner, a physician, chose on 2026-11-07 in answer to the clinician brief
    (notes/clinician-review-medication-period.md), through FHIR. An active request or statement
    with no end counts as current only when it started in the 365 days before screening (the
    365th day included); older, it reads `unknown` and the chart needs review. A proposal, a plan,
    an option and an order not to give the drug are left out. A `boundsDuration` gives the stop,
    so a `completed` insulin written 60 days ago for 30 days reads as ended in the last 90 days."""
    metformin = {"coding": [{"system": RXNORM, "code": "6809"}], "text": "metformin 1000 mg"}
    insulin = {"coding": [{"system": RXNORM, "code": "274783"}], "text": "insulin glargine"}
    record = [r for r in _record() if r["resourceType"] != "MedicationStatement"]
    mapped = FhirAdapter.for_pack(trial.pack)

    def decided(*resources: dict[str, Any]) -> tuple[str | None, dict[str, str], list[Any]]:
        entries = [{"resource": r} for r in [*record, *resources]]
        bundle: dict[str, Any] = {"resourceType": "Bundle", "type": "collection", "entry": entries}
        result, _ = trial.decide_fhir(bundle)
        assert result.outcome is Outcome.HUMAN_REVIEW
        answers = {q: result.answers[q].label for q in ("stable_metformin", "no_insulin")}
        return result.recommendation, answers, mapped.to_input(mapped.index(bundle))["medications"]

    def ordered(days: int, **more: Any) -> dict[str, Any]:
        more.setdefault("medicationCodeableConcept", metformin)
        return _medication_request(
            "mr1", more.pop("status", "active"), authoredOn=_ago(days), **more
        )

    def stated(days: int) -> dict[str, Any]:
        return {
            "resourceType": "MedicationStatement",
            "id": "ms1",
            "status": "active",
            "medicationCodeableConcept": {"text": "metformin 1000 mg tablet"},
            "effectivePeriod": {"start": _ago(days)},
        }

    current = {"stable_metformin": "yes", "no_insulin": "yes"}
    stale = {"stable_metformin": "unknown", "no_insulin": "yes"}
    for resource, want in (
        (ordered(200), ("candidate", current)),
        (ordered(365), ("candidate", current)),
        (ordered(366), ("needs_review", stale)),
        (stated(200), ("candidate", current)),
        (stated(365), ("candidate", current)),
        (stated(800), ("needs_review", stale)),
    ):
        assert decided(resource)[:2] == want, resource

    absent = ("not_eligible", {"stable_metformin": "no", "no_insulin": "yes"})
    for left_out in (
        ordered(200, intent="proposal"),
        ordered(200, intent="plan"),
        ordered(200, intent="option"),
        ordered(200, doNotPerform=True),
    ):
        assert decided(left_out) == (*absent, []), left_out
    for kept in ("order", "original-order", "reflex-order", "filler-order", "instance-order"):
        assert decided(ordered(200, intent=kept))[:2] == ("candidate", current), kept

    def course(days: int, value: float, code: str) -> dict[str, Any]:
        bounds = {
            "boundsDuration": {"value": value, "code": code, "system": "http://unitsofmeasure.org"}
        }
        return _medication_request(
            "mr2",
            "completed",
            authoredOn=_ago(days),
            medicationCodeableConcept=insulin,
            dosageInstruction=[{"timing": {"repeat": bounds}}],
        )

    base = ordered(200)
    for resource, ended, no_insulin in (
        (course(60, 30, "d"), _ago(30), "no"),  # stopped 30 days ago: in the 90-day window
        (course(60, 4, "wk"), _ago(32), "no"),
        (course(200, 3, "mo"), _ago(108), "yes"),  # 92 days from the start: 108 days ago
        (course(500, 1, "a"), _ago(134), "yes"),  # 366 days from the start
        (course(91, 24, "h"), _ago(90), "no"),
        (course(60, 30, "min"), None, "yes"),  # minutes are not read: stop unknown, not taken
    ):
        recommendation, answers, items = decided(base, resource)
        assert answers["no_insulin"] == no_insulin, resource
        assert items[1].get("end") == ended, resource
        assert recommendation == ("not_eligible" if no_insulin == "no" else "candidate")


def _patient_view(record: list[dict[str, Any]]) -> dict[str, Any]:
    """A patient-view request whose prefetch holds `record` (its Patient first)."""

    def searchset(resources: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    by_type: dict[str, list[dict[str, Any]]] = {}
    for resource in record[1:]:
        by_type.setdefault(resource["resourceType"], []).append(resource)
    return {
        "hook": "patient-view",
        "hookInstance": "4b4c1b2e-0000-4000-8000-000000000002",
        "context": {"patientId": "p1", "userId": "Practitioner/1"},
        "prefetch": {
            "patient": record[0],
            "conditions": searchset(by_type["Condition"]),
            "observations": searchset(by_type["Observation"]),
            "medications": searchset(by_type["MedicationStatement"]),
            "medication_requests": searchset([]),
            "notes": searchset(by_type["DocumentReference"]),
        },
    }


def test_notes_longer_than_the_input_allows_abstain_and_show_a_card(trial: Decision) -> None:
    """A patient's notes, joined, may be longer than the input schema lets `notes` be (20,000
    characters). The decision then abstains with `could not read notes: too_large` and the hook
    shows its `info` card, where the schema would have refused the whole input (a BLOCK, which
    shows no card) and the pregnancy in the first note gone unseen at the chart. The text is never
    cut, and notes as long as the schema allows are read."""
    filler = " Seen for diabetes follow-up; the glucose log was reviewed." * 200  # 11,800
    record = _record("She is 12 weeks pregnant." + filler)
    record.append({**_note(filler), "id": "n2"})
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": r} for r in record],
    }
    result, _ = trial.decide_fhir(bundle)
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.reasons == ("could not read notes: too_large",)
    assert result.unreadable == (("notes", "too_large"),)
    assert result.data_coverage is not None
    assert result.data_coverage["fields"]["notes"]["unreadable"] == {"too_large": 1}
    assert result.data_coverage["unreadable"] == {"too_large": 1}
    cards = cds_hooks.invoke(trial, _patient_view(record))["cards"]
    assert [(c["summary"], c["indicator"]) for c in cards] == [
        (cds_hooks.UNREADABLE_SUMMARY, "info")
    ]
    assert "- notes: too_large" in cards[0]["detail"]

    head = "She is 12 weeks pregnant."
    at_most = _record(head + "x" * (20_000 - len(head)))  # exactly the schema's maxLength
    bundle["entry"] = [{"resource": r} for r in at_most]
    result, _ = trial.decide_fhir(bundle)
    assert result.outcome is Outcome.HUMAN_REVIEW and not result.unreadable
    assert result.answers["note_pregnancy"].label == "yes"


def test_patient_view_card(trial: Decision) -> None:
    def request(notes: str) -> dict[str, Any]:
        return _patient_view(_record(notes))

    cards = cds_hooks.invoke(trial, request("Seen for diabetes follow-up."))["cards"]
    assert [c["summary"] for c in cards] == ["Possible GLYCEMIA-2 candidate: confirm eligibility"]
    assert cards[0]["suggestions"][0]["label"] == "Create a prescreening review task"
    excluded = request("Currently receiving chemotherapy for colon cancer.")
    assert cds_hooks.invoke(trial, excluded) == {"cards": []}  # clearly not eligible: silent
