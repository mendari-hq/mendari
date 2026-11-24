# SPDX-License-Identifier: Apache-2.0
"""The Prior Authorization Preflight reference pack and its synthetic data."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari import Decision
from mendari.core.policy import Outcome
from mendari.errors import FhirError
from mendari.sandbox.code_systems import procedure_codings
from mendari.standards import cds_hooks
from tests.support import fixture_data, synthetic_prior_auth
from tests.test_fhir_client import allow_ehr, fhir_json

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "tests" / "fixtures" / "reference-packs" / "prior-auth-preflight"
STRUCTURED = ("service", "payer", "diagnosis_supports", "prior_imaging")
TODAY = date.today()
ICD = "http://hl7.org/fhir/sid/icd-10-cm"
PAYERS = "https://mendari.co/fhir/sid/fictional-payers"  # the pack's synthetic payer ids
SITE_PAYERS = "https://site.example/fhir/sid/payers"  # a fictional site's own payer ids


@pytest.fixture(scope="module")
def preflight() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_prior_auth.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "calibration": 300,
        "test": 600,
    }
    assert splits == synthetic_prior_auth.public_splits()
    assert len(synthetic_prior_auth.local_site()) == 300


@pytest.mark.parametrize("dataset", ["documentation_notes", "documentation_notes_holdout"])
def test_every_dataset_follows_the_decision_table(dataset: str) -> None:
    lines = (PACK_DIR / "data" / f"{dataset}.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    records += synthetic_prior_auth.public_splits()["test"]
    for record in records:
        labels = record["labels"]
        assert labels["next_step"] == synthetic_prior_auth.next_step(labels), record["id"]
    steps = Counter(r["labels"]["next_step"] for r in synthetic_prior_auth.public_splits()["test"])
    assert set(steps) == {"submit_ready", "needs_attention", "not_required", "not_checked"}


def test_the_structured_answers_agree_with_ground_truth_on_public_data(
    preflight: Decision,
) -> None:
    provider = preflight.providers["structured"]
    for record in synthetic_prior_auth.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in STRUCTURED:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_prior_auth.local_site()
    codes = Counter(p["code"] for r in local for p in r["input"]["procedures"])
    assert codes["FX112"] > 0  # lumbar X-rays under a code the pack does not list
    assert sum(r["attributes"]["style"] == "shorthand" for r in local) > 100
    payers = Counter(r["labels"]["payer"] for r in local)
    assert payers["beacon"] > payers["acme"]


def test_the_generator_writes_the_prior_auth_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["--kind", "prior-auth", "--agent-dir", str(tmp_path / "pack")]
    assert fixture_data.main([*argv, "--local-out", str(local)]) == 0
    test = (tmp_path / "pack" / "data" / "test.jsonl").read_text().splitlines()
    assert len(test) == 600 and json.loads(test[0])["id"] == "pa-0300"
    assert len(local.read_text().splitlines()) == 300


# -- decisions ---------------------------------------------------------------------------------


def test_reasons_list_the_gaps_and_nothing_is_automated_on_rules(preflight: Decision) -> None:
    record = {
        "as_of": "2026-09-01",
        "order": {"code": "FX101"},
        "coverage": {"payer": "acme"},
        "conditions": [{"code": "M54.16"}],
        "procedures": [],
        "notes": "Back pain. Started PT 2 weeks ago.",
    }
    result = preflight.decide(record)
    assert result.recommendation == "needs_attention"
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert (
        "needs_attention: conservative_therapy is no (service is lumbar_mri, red_flag is no)"
        in result.reasons
    )
    assert (
        "needs_attention: prior_imaging is no (service is lumbar_mri, payer is acme)"
        in result.reasons
    )


# -- FHIR and CDS Hooks ------------------------------------------------------------------------


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _order(id_: str, code: str) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": procedure_codings(code)},  # fictional, then a payer's ICD-10-PCS
        "subject": {"reference": "Patient/p1"},
    }


def _bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


def _prefetch(payer: str, note: str, **payor: Any) -> dict[str, Any]:
    """An order-sign prefetch; the payer is the payor's own identifier, or `payor` as given."""

    def coded(kind: str, id_: str, codings: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
        return {"resourceType": kind, "id": id_, "code": {"coding": codings}, **extra}

    def diagnosis(id_: str, code: str) -> dict[str, Any]:
        return coded("Condition", id_, [{"system": ICD, "code": code}])

    data = base64.b64encode(note.encode()).decode()
    given = payor or {"identifier": {"value": payer}}
    return {
        "patient": {"resourceType": "Patient", "id": "p1", "birthDate": f"{TODAY.year - 50}-01-01"},
        "coverage": _bundle({"resourceType": "Coverage", "id": "c1", "payor": [given]}),
        "conditions": _bundle(diagnosis("d1", "M54.16"), diagnosis("d2", "S83.241A")),
        "procedures": _bundle(
            coded("Procedure", "x1", procedure_codings("FX111"), performedDateTime=_ago(40)),
            coded("Procedure", "x2", procedure_codings("FX211"), performedDateTime=_ago(40)),
        ),
        "notes": _bundle(
            {
                "resourceType": "DocumentReference",
                "id": "n1",
                "content": [{"attachment": {"contentType": "text/plain", "data": data}}],
            }
        ),
    }


def test_order_sign_gives_a_card_per_covered_order(preflight: Decision) -> None:
    note = "Completed 8 weeks of physical therapy. The knee locks when climbing stairs."
    request = {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {
                "resourceType": "Bundle",
                "entry": [
                    {"resource": _order("mri", "FX101")},
                    {"resource": _order("scope", "FX202")},
                    {"resource": _order("lab", "FX904")},  # a metabolic panel: not covered
                ],
            },
        },
        "prefetch": _prefetch("acme", note),
    }
    cards = cds_hooks.invoke(preflight, request)["cards"]
    assert [c["summary"] for c in cards] == [
        "Prior auth for Lumbar MRI (Acme): ready to submit",  # display names (LP-23f)
        "Prior auth for Knee arthroscopy (Acme): ready to submit",
    ]
    task = cards[0]["suggestions"][0]["actions"][0]["resource"]
    assert task["focus"] == {"reference": "ServiceRequest/mri"}
    assert task["description"] == "Prior auth for lumbar_mri (acme): submit_ready"


def _order_sign(prefetch: dict[str, Any], *orders: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ec",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": o} for o in orders]},
        },
        "prefetch": prefetch,
    }


def test_long_notes_with_a_red_flag_abstain_with_a_card_at_the_order(preflight: Decision) -> None:
    """A patient's notes, joined, may be longer than the input schema lets `notes` be (20,000
    characters). The order then abstains (`could not read notes: too_large`) and shows its
    `info` card, with CRD's `indeterminate` coverage information, where the schema would have
    refused the whole input (a BLOCK, which shows no card) and the red flag in its first note gone
    unseen by the clinician signing it."""
    prefetch = _prefetch("acme", "New saddle anesthesia and urinary retention.")
    filler = base64.b64encode((" Completed 8 weeks of physical therapy." * 300).encode()).decode()
    prefetch["notes"]["entry"] += [
        {
            "resource": {
                "resourceType": "DocumentReference",
                "id": f"n{n}",
                "content": [{"attachment": {"contentType": "text/plain", "data": filler}}],
            }
        }
        for n in (2, 3)
    ]  # 11,700 characters each
    result, _ = preflight.decide_fhir(prefetch, focus=_order("mri", "FX101"))
    assert result.outcome is Outcome.ABSTAIN
    assert result.reasons == ("could not read notes: too_large",)
    answer = cds_hooks.invoke(preflight, _order_sign(prefetch, _order("mri", "FX101")))
    assert [(c["summary"], c["indicator"]) for c in answer["cards"]] == [
        (cds_hooks.UNREADABLE_SUMMARY, "info")
    ]
    assert "- notes: too_large" in answer["cards"][0]["detail"]
    assert "indeterminate" in json.dumps(answer["systemActions"])


def test_through_fhir_an_order_and_its_payer_reference_are_read_as_the_changelog_says(
    preflight: Decision,
) -> None:
    """Two FHIR-path changes of 0.3.0 its changelog states: an order whose fictional code has no
    system is not the pack's (the 0.2 mapping read the first coding whatever its system), so it
    gets no decision; and a payor reference that could not be read abstains even when the payor
    also carries an identifier (the 0.2 mapping read that identifier)."""
    note = "Completed 8 weeks of physical therapy."
    bare = _order("mri", "FX101")
    bare["code"] = {"coding": [{"code": "FX101"}]}
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        preflight.decide_fhir(_prefetch("acme", note), focus=bare)
    both = _prefetch("", note, reference="Organization/o9", identifier={"value": "acme"})
    result, _ = preflight.decide_fhir(both, focus=_order("mri", "FX101"))
    assert result.outcome is Outcome.ABSTAIN
    assert result.reasons == ("could not read coverage.payer: not_fetched",)


# The red-flag rule's card (`cds_hooks.card.also_show`), for an order `show` leaves out.
RED_FLAG_SUMMARY = (
    "Possible red flag in the notes: consider urgent imaging and an expedited review; prior auth "
    "not checked"
)


def _site_payer(note: str) -> dict[str, Any]:
    """An order-sign prefetch whose payer is a site's own payer id, which reads as `other`."""
    site = _prefetch("", note, reference="Organization/o2")
    site["payers"] = _bundle(_payer("o2", SITE_PAYERS, "SP100"))
    return site


def test_a_red_flag_under_a_site_payer_id_shows_a_card_at_signing(preflight: Decision) -> None:
    """The owner's choice (2026-11-07, "Red flag shows a card"): a site's own payer id reads as
    `other`, so the order is `not_checked`, which `show` leaves out, but a possible red flag in
    its notes shows the clinician signing it a card in the red-flag rule's words
    (`cds_hooks.card.also_show`), `critical`, and the order is still in the review queue at
    urgent priority. The card says nothing of the payer's requirements and the order carries no
    CRD coverage information, as for any `not_checked` order. With a payer the pack knows, the
    same note shows the order's own card, worded as before."""
    note = "New saddle anesthesia and urinary retention."
    order = _order("mri", "FX101")
    result, _ = preflight.decide_fhir(_site_payer(note), focus=order)
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "not_checked")
    assert (result.priority.value, result.rule) == ("urgent", "possible-red-flag")
    assert result.answers["red_flag"].label == "yes"
    answer = cds_hooks.invoke(preflight, _order_sign(_site_payer(note), order))
    assert [(c["summary"], c["indicator"]) for c in answer["cards"]] == [
        (RED_FLAG_SUMMARY, "critical")
    ]
    assert "systemActions" not in answer
    (card,) = answer["cards"]
    assert cds_hooks.OUTCOME_SENTENCES[Outcome.HUMAN_REVIEW] in card["detail"]
    assert any("possible-red-flag" in line for line in card["detail"].splitlines())
    assert note not in card["detail"]
    known = cds_hooks.invoke(preflight, _order_sign(_prefetch("acme", note), order))
    assert [(c["summary"], c["indicator"]) for c in known["cards"]] == [
        ("Prior auth for Lumbar MRI (Acme): ready to submit", "info")
    ]
    assert len(known["systemActions"]) == 1


@pytest.mark.parametrize(
    "note",
    [
        "Completed 8 weeks of physical therapy.",
        "No red flags. Denies fever, weight loss and saddle numbness.",
        "Mother had breast cancer.",
    ],
)
def test_a_not_checked_order_with_no_red_flag_still_shows_no_card(
    preflight: Decision, note: str
) -> None:
    """Every other `not_checked` order stays as before (the owner's choice keeps them quiet):
    decided and in the review queue at routine priority, with no card and no coverage
    information at signing. Red flags documented as absent, or in a family member, are not red
    flags (the criteria), so they stay quiet too."""
    order = _order("mri", "FX101")
    result, _ = preflight.decide_fhir(_site_payer(note), focus=order)
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "not_checked")
    assert (result.priority.value, result.rule) == ("routine", None)
    assert cds_hooks.invoke(preflight, _order_sign(_site_payer(note), order)) == {"cards": []}


def test_beacon_sleep_study_needs_no_prior_auth(preflight: Decision) -> None:
    result, _ = preflight.decide_fhir(
        _prefetch("beacon", "Loud snoring."), focus=_order("psg", "FX301")
    )
    assert result.recommendation == "not_required"
    assert result.answers["payer"].label == "beacon"


def _payer(id_: str, system: str, value: str) -> dict[str, Any]:
    return {
        "resourceType": "Organization",
        "id": id_,
        "identifier": [{"system": system, "value": value}],
    }


def test_the_payer_is_read_by_coverage_reference(
    preflight: Decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The FHIR mapping (spec 0.3) reads the payer through the Coverage's first payor: the
    Organization it names, by its identifier in the synthetic payers' system, whether contained,
    in the record or read from the EHR's FHIR server on a CDS hook; else the payor's own
    identifier as the Coverage writes it, which is how the synthetic records name their payers
    (the 0.2 mapping read only that, so a payer named by reference read as `other`). A site's
    own payer identifier is neither, and no site code map translates an identifier: the payer
    reads as `other` and the order as `not_checked` (the owner's open question). A reference the
    decision could not read abstains, never reads as `other`."""
    note = "Completed 8 weeks of physical therapy."
    order = _order("mri", "FX101")

    def decided(prefetch: dict[str, Any]) -> tuple[str | None, str | None]:
        result, _ = preflight.decide_fhir(prefetch, focus=order)
        return result.answers["payer"].label if result.answers else None, result.recommendation

    in_record = _prefetch("", note, reference="Organization/o1")
    in_record["payers"] = _bundle(_payer("o1", PAYERS, "acme"))
    contained = _prefetch("", note, reference="#o1")
    contained["coverage"]["entry"][0]["resource"]["contained"] = [_payer("o1", PAYERS, "beacon")]
    assert decided(in_record) == ("acme", "submit_ready")
    assert decided(contained) == ("beacon", "submit_ready")
    assert decided(_prefetch("acme", note)) == ("acme", "submit_ready")  # the payor's own id
    both = _prefetch("", note, reference="Organization/o1", identifier={"value": "beacon"})
    both["payers"] = _bundle(_payer("o1", PAYERS, "acme"))
    assert decided(both) == ("acme", "submit_ready")  # the Organization's wins
    site = _prefetch("", note, reference="Organization/o2")
    site["payers"] = _bundle(_payer("o2", SITE_PAYERS, "SP100"))
    assert decided(site) == ("other", "not_checked")

    unread, _ = preflight.decide_fhir(_prefetch("", note, reference="Organization/o9"), focus=order)
    assert unread.outcome is Outcome.ABSTAIN
    assert unread.reasons == ("could not read coverage.payer: not_fetched",)

    # On a CDS hook, the Organization the record does not hold is read from the EHR's FHIR
    # server (allow-listed, under its base), once, and decides as if it had been sent.
    seen: list[httpx.Request] = []

    def ehr(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/fhir/Organization/o9":
            return fhir_json(_payer("o9", PAYERS, "acme"))
        return fhir_json({"resourceType": "OperationOutcome", "issue": []}, 404)

    allow_ehr(monkeypatch, ehr)
    hook = {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2eb",
        "fhirServer": "https://ehr.example.org/fhir",
        "fhirAuthorization": {
            "access_token": "synthetic-token",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": "user/Organization.rs",
            "subject": "mendari",
        },
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": order}]},
        },
        "prefetch": _prefetch("", note, reference="Organization/o9"),
    }
    cards = cds_hooks.invoke(preflight, hook)["cards"]
    assert [c["summary"] for c in cards] == ["Prior auth for Lumbar MRI (Acme): ready to submit"]
    assert [str(r.url) for r in seen] == ["https://ehr.example.org/fhir/Organization/o9"]
