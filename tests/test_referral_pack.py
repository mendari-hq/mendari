# SPDX-License-Identifier: Apache-2.0
"""The Referral Router reference pack and its synthetic data."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.cli import main
from mendari.core.policy import Outcome
from mendari.core.site_codes import ENV as SITE_CODES_ENV
from mendari.errors import FhirError
from mendari.sandbox import synthetic_referral
from mendari.sandbox.fhir_records import REFERRAL
from mendari.server.app import create_app
from mendari.standards import cds_hooks
from tests.test_site_codes import mapped, maps_of

ROOT = Path(__file__).parents[1]
PACK_DIR = ROOT / "packs" / "referral-router"
CHECKLIST = ("recent_visit_note", "ecg_on_file", "imaging_on_file", "recent_labs", "marked_urgent")
TODAY = date.today()
LOINC = "http://loinc.org"
# A lab order, as a US Core EHR categorizes one (SNOMED CT: laboratory procedure): never a referral.
LAB = {"system": "http://snomed.info/sct", "code": "108252007"}
# The order type a fictional EHR marks a referral with: its own code system, not the pack's.
SITE_REFERRAL = {"system": "https://ehr.example/referral-type", "code": "REF"}


@pytest.fixture(scope="module")
def router() -> Decision:
    return Decision.from_path(PACK_DIR)


# -- synthetic data ----------------------------------------------------------------------------


def test_generation_is_deterministic_and_sized() -> None:
    splits = synthetic_referral.public_splits()
    assert {name: len(records) for name, records in splits.items()} == {
        "train": 1500,
        "calibration": 300,
        "test": 600,
    }
    assert splits == synthetic_referral.public_splits()
    assert len(synthetic_referral.local_site()) == 300


def test_ground_truth_follows_the_decision_table() -> None:
    records = synthetic_referral.public_splits()["test"]
    for record in records:
        assert record["labels"]["next_step"] == synthetic_referral.next_step(record["labels"])
    steps = Counter(r["labels"]["next_step"] for r in records)
    assert set(steps) == {"schedule", "return_for_info", "urgent_review", "coordinator_review"}


def test_the_checklist_agrees_with_ground_truth_on_public_data(router: Decision) -> None:
    """Public records use only the document codes the pack lists, so the checklist must be
    exact; a disagreement is a bug in the generator or the checklist."""
    provider = router.providers["checklist"]
    for record in synthetic_referral.public_splits()["calibration"]:
        answers = provider.answer(record["input"]).answers
        for question in CHECKLIST:
            assert answers[question].label == record["labels"][question], (record["id"], question)


def test_the_local_site_differs_the_way_the_card_says() -> None:
    local = synthetic_referral.local_site()
    assert sum(r["attributes"]["style"] == "shorthand" for r in local) > 100
    codes = Counter(d["code"] for r in local for d in r["input"]["documents"])
    assert codes["34117-2"] > codes["11506-3"]  # notes filed as a history and physical
    mix = Counter(r["labels"]["specialty"] for r in local)
    assert mix.most_common(1)[0][0] == "orthopedics"


def test_sandbox_generate_writes_the_referral_data(tmp_path: Path) -> None:
    local = tmp_path / "local.jsonl"
    argv = ["sandbox", "generate", "--kind", "referral", "--pack-dir", str(tmp_path / "pack")]
    assert main([*argv, "--local-out", str(local)]) == 0
    train = (tmp_path / "pack" / "data" / "train.jsonl").read_text().splitlines()
    assert len(train) == 1500 and json.loads(train[0])["id"] == "ref-00000"
    assert len(local.read_text().splitlines()) == 300


# -- decisions ---------------------------------------------------------------------------------

BASE: dict[str, Any] = {
    "as_of": "2026-09-01",
    "patient": {"age": 45, "sex": "female"},
    "referral": {
        "reason": "Acne not improving after 8 months. Please advise.",
        "priority": "routine",
    },
    "documents": [{"code": "11506-3", "date": "2026-08-20"}],
    "reports": [
        {"code": "11524-6", "date": "2026-06-10"},
        {"code": "18748-4", "date": "2026-07-15"},
    ],
    "labs": [{"code": "58410-2", "date": "2026-08-15"}],
}


def test_without_a_model_screen_nothing_is_automated(router: Decision) -> None:
    result = router.decide(BASE)
    assert result.recommendation == "schedule" and result.outcome is Outcome.HUMAN_REVIEW
    assert (
        "automation requires triage_screen = rules_and_jev or rules_and_llm (running: rules)"
        in result.reasons
    )


def test_reasons_list_what_is_missing(router: Decision) -> None:
    reason = "Palpitations for 6 months, ECG normal. Please assess."
    record = {**BASE, "referral": {"reason": reason, "priority": "routine"}, "reports": []}
    result = router.decide(record)
    assert result.recommendation == "return_for_info"
    assert "return_for_info: ecg_on_file is no (specialty is cardiology)" in result.reasons


# -- FHIR and CDS Hooks ------------------------------------------------------------------------


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _order(
    id_: str, reason: str, category: dict[str, str] = REFERRAL, **extra: Any
) -> dict[str, Any]:
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "priority": "routine",
        "category": [{"coding": [category]}],
        "reasonCode": [{"text": reason}],
        "subject": {"reference": "Patient/p1"},
        **extra,
    }


def _record() -> dict[str, dict[str, Any]]:
    def bundle(*resources: dict[str, Any]) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r} for r in resources],
        }

    def coded(kind: str, id_: str, code: str, date_field: str, days: int) -> dict[str, Any]:
        document = kind == "DocumentReference"
        return {
            "resourceType": kind,
            "id": id_,
            "type" if document else "code": {"coding": [{"system": LOINC, "code": code}]},
            date_field: f"{_ago(days)}T09:00:00Z" if document else _ago(days),  # date: an instant
        }

    patient = {"resourceType": "Patient", "id": "p1", "gender": "male"}
    patient["birthDate"] = f"{TODAY.year - 61}-03-01"
    return {
        "patient": patient,
        "documents": bundle(coded("DocumentReference", "d1", "11506-3", "date", 20)),
        "reports": bundle(coded("DiagnosticReport", "r1", "18748-4", "effectiveDateTime", 60)),
        "labs": bundle(coded("Observation", "o1", "58410-2", "effectiveDateTime", 10)),
    }


def test_a_fhir_referral_becomes_a_task_for_the_specialty(router: Decision) -> None:
    prefetch = _record()
    order = _order("sr1", "Palpitations for 6 months, ECG normal. Please assess.")
    result, task = router.decide_fhir(prefetch, focus=order)
    assert result.answers["specialty"].label == "cardiology"
    assert result.recommendation == "return_for_info"  # no ECG on file
    assert task["performerType"][0]["coding"][0]["code"] == "cardiology"
    assert task["description"] == "Referral to cardiology: return_for_info"
    assert task["focus"] == {"reference": "ServiceRequest/sr1"}


def test_the_mappings_code_sets_hold_exactly_the_checklists_document_and_report_codes() -> None:
    """Through FHIR the checklist's documents and reports are read through the mapping's code
    sets, which copy the codes logic/checklist.yaml looks for: a code in the checklist alone
    would count on pack inputs sent directly and never through FHIR (LP-23k review). So the two
    lists stay equal, list by list (the card says a site keeps them so)."""
    checklist = yaml.safe_load((PACK_DIR / "logic" / "checklist.yaml").read_text())
    code_sets = yaml.safe_load((PACK_DIR / "fhir" / "mapping.yaml").read_text())["code_sets"]
    looked_for: dict[str, set[str]] = {"documents": set(), "reports": set()}
    for question in checklist["questions"].values():
        for check in question["checks"]:
            exists = check.get("exists") or {}
            if exists.get("in") in looked_for:
                looked_for[exists["in"]].update(exists["codes"])
    assert looked_for["documents"] == set(code_sets["checklist_documents"]["codes"])
    assert looked_for["reports"] == set(code_sets["checklist_reports"]["codes"])
    assert {code_sets[name]["system"] for name in ("checklist_documents", "checklist_reports")} == {
        LOINC
    }


def test_a_system_less_loinc_document_code_is_not_read_through_fhir(router: Decision) -> None:
    """A coding with no system is not read as LOINC by the checklist's code sets (spec 0.3
    code sets match by system), so a visit note coded {code: 11506-3} alone is not on file and
    the referral goes back to the referrer, as the card says; the 0.2 mapping read the first
    coding's code whatever its system."""
    prefetch = _record()
    order = _order("sr1", "Palpitations for 6 months, ECG normal. Please assess.")
    documents = prefetch["documents"]["entry"]
    assert any(e["resource"]["type"]["coding"][0]["code"] == "11506-3" for e in documents)
    coded, _ = router.decide_fhir(prefetch, focus=order)
    assert coded.answers["recent_visit_note"].label == "yes"
    for entry in documents:
        for coding in entry["resource"]["type"]["coding"]:
            coding.pop("system", None)
    bare, _ = router.decide_fhir(prefetch, focus=order)
    assert bare.answers["recent_visit_note"].label == "no"


def test_a_referral_under_a_site_code_is_decided_once_the_site_lists_it(
    router: Decision, tmp_path: Path
) -> None:
    """The pack matches only the fictional referral code of its synthetic examples, so an EHR's
    own referral code (a real EHR's is often SNOMED CT) decides nothing until the site lists it
    in the FHIR mapping (owner-accepted, LP-14), or translates it in its site code map, which
    changes no pack file (LP-23k, the next test)."""
    order = _order(
        "sr1", "Palpitations for 6 months, ECG normal. Please assess.", category=SITE_REFERRAL
    )
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        router.decide_fhir(_record(), focus=order)
    copy = tmp_path / "referral-router"
    shutil.copytree(PACK_DIR, copy)
    mapping = copy / "fhir" / "mapping.yaml"
    listed = mapping.read_text().replace(
        "  path: category.coding\n  code_set: referral_category\n",
        '  path: category.coding.code\n  in: ["FR101", "REF"]\n',
    )
    assert listed != mapping.read_text()
    mapping.write_text(listed)
    result, _ = Decision.from_path(copy).decide_fhir(_record(), focus=order)
    assert result.answers["specialty"].label == "cardiology"


def test_a_referral_under_a_site_code_is_decided_through_the_site_code_map(
    router: Decision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 0.3: the pack finds a referral through its code set `referral_category` (FR101),
    wherever the coding sits among its categories' codings, and a site's own referral code
    through its site code map (`MENDARI_SITE_CODES`, LP-23i), with no pack file changed. A
    referral the EHR marks only with its own order type is then decided, by `decide_fhir`, the
    order-sign hook and the server alike, and never automated (the `site_code` cap): where the
    same referral coded FR101 would be AUTO, it is HUMAN_REVIEW with the cap's reason, and the
    decision records the map. A translation into another set, or another organization's map,
    finds nothing; a referral carrying FR101 itself, behind the site's code, is decided as
    without a map. A document the checklist looks for under a site's own code is read through
    the map too (`checklist_documents`)."""
    maps = maps_of(mapped({"referral_category": [{**SITE_REFERRAL, "as": "FR101"}]}))
    reason = EACH_STEP["schedule"]
    site_only = _order("sr1", reason, category=SITE_REFERRAL)
    both = _order("sr2", reason)
    both["category"] = [{"coding": [SITE_REFERRAL, REFERRAL]}]

    # Without a map: FR101 is found behind the site's coding; the site's code alone is not.
    assert router.decide_fhir(_record(), focus=both)[0].recommendation == "schedule"
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        router.decide_fhir(_record(), focus=site_only)
    unrelated = maps_of(mapped({"checklist_documents": [{**SITE_REFERRAL, "as": "11506-3"}]}))
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        Decision(router.pack, site_codes=unrelated).decide_fhir(_record(), focus=site_only)
    elsewhere = maps_of(
        mapped({"referral_category": [{**SITE_REFERRAL, "as": "FR101"}]}, org="b"), signed_in=True
    )
    with pytest.raises(FhirError, match="missing required FHIR resources: ServiceRequest"):
        Decision(router.pack, site_codes=elsewhere).decide_fhir(_record(), focus=site_only)

    # With the map: decided, recorded with the map, and capped.
    mapped_router = Decision(router.pack, site_codes=maps)
    through, task = mapped_router.decide_fhir(_record(), focus=site_only)
    assert through.recommendation == "schedule" and through.outcome is Outcome.HUMAN_REVIEW
    assert "site code translated (referral_category): a person decides" in through.reasons
    assert through.site_codes == maps.maps["default"].stamp()
    assert task["focus"] == {"reference": "ServiceRequest/sr1"}
    own, _ = mapped_router.decide_fhir(_record(), focus=both)
    assert not any("site code" in r for r in own.reasons)
    assert own.outcome is Outcome.HUMAN_REVIEW  # the rules alone: nothing automates here

    # Where the referral would automate (a copy of the pack whose policy needs no model screen,
    # as a site validating its own screen might have), the site-coded one goes to a person.
    copy = tmp_path / "referral-router"
    shutil.copytree(PACK_DIR, copy)
    policy = yaml.safe_load((copy / "policy.yaml").read_text())
    del policy["automation_requires"]
    (copy / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    automating = Decision(load_pack(copy), site_codes=maps)
    assert automating.decide_fhir(_record(), focus=both)[0].outcome is Outcome.AUTO
    capped, _ = automating.decide_fhir(_record(), focus=site_only)
    assert (capped.outcome, capped.recommendation) == (Outcome.HUMAN_REVIEW, "schedule")
    assert capped.reasons[-1] == "site code translated (referral_category): a person decides"

    # On order-sign: of a site-coded referral and a lab order, the referral alone is decided.
    with AuditStore(tmp_path / "audit.db") as store:
        hooked = Decision(router.pack, store=store, site_codes=maps)
        orders = [site_only, _order("lab1", "Annual bloods", category=LAB)]
        cds_hooks.invoke(hooked, _order_sign(orders, _record()))
        decided = store.query(pack_id=router.pack.id)
        assert [(row["outcome"], row["recommendation"]) for row in decided] == [
            ("HUMAN_REVIEW", "schedule")
        ]

    # A visit note under a site's own code: missing without the map (the referral goes back),
    # on file through it.
    record = _record()
    record["documents"]["entry"][0]["resource"]["type"] = {"coding": [SITE_REFERRAL]}
    notes = maps_of(
        mapped(
            {
                "referral_category": [{**SITE_REFERRAL, "as": "FR101"}],
                "checklist_documents": [{**SITE_REFERRAL, "as": "11506-3"}],
            }
        )
    )
    unread, _ = mapped_router.decide_fhir(record, focus=both)
    assert unread.answers["recent_visit_note"].label == "no"
    read, _ = Decision(router.pack, site_codes=notes).decide_fhir(record, focus=both)
    assert read.answers["recent_visit_note"].label == "yes"
    assert "site code translated (checklist_documents): a person decides" in read.reasons

    # Through the server, with the setting.
    monkeypatch.setenv(
        SITE_CODES_ENV,
        json.dumps(mapped({"referral_category": [{**SITE_REFERRAL, "as": "FR101"}]})),
    )
    with TestClient(create_app(db_path=tmp_path / "served.db")) as app:
        answer = app.post(
            "/api/packs/mendari/referral-router/fhir/decide",
            json={
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": r} for r in [site_only, *_entries(_record())]],
            },
        )
    assert answer.status_code == 200, answer.text
    decision = answer.json()["decision"]
    assert (decision["outcome"], decision["recommendation"]) == ("HUMAN_REVIEW", "schedule")
    assert decision["reasons"][-1] == "site code translated (referral_category): a person decides"


def _entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The resources of a `_record` prefetch, its search Bundles flattened."""
    found: list[dict[str, Any]] = []
    for value in record.values():
        if value.get("resourceType") == "Bundle":
            found += [entry["resource"] for entry in value["entry"]]
        else:
            found.append(value)
    return found


def _order_sign(orders: list[dict[str, Any]], prefetch: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook": "order-sign",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {
            "userId": "Practitioner/1",
            "patientId": "p1",
            "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": o} for o in orders]},
        },
        "prefetch": prefetch,
    }


def test_on_order_sign_only_a_referral_gets_a_card(router: Decision) -> None:
    """Of the orders signed together, only a referral ServiceRequest is decided and may show a
    card: the lab order and the MedicationRequest never do. (A referral gets a card only when
    the person signing can act: `test_only_referrals_that_need_action_show_a_card`.)"""
    orders = [
        _order("sr1", "New chest tightness on exertion for 3 weeks, eases with rest. Please see."),
        _order("lab1", "Annual bloods", category=LAB),
        {"resourceType": "MedicationRequest", "id": "m1", "status": "draft"},
    ]
    cards = cds_hooks.invoke(router, _order_sign(orders, _record()))["cards"]
    assert [c["summary"] for c in cards] == ["Possibly urgent Cardiology referral: review today"]
    assert cards[0]["indicator"] == "critical"


# One signed referral for each next step, on the record `_record` gives (no ECG on file).
EACH_STEP = {
    "schedule": "Itchy rash on both elbows for 4 months, not settling with emollients. "
    "Please advise on treatment.",
    "coordinator_review": "Recurrent ear infections and hearing loss. Please refer to ENT.",
    "return_for_info": "Palpitations for 6 months, ECG normal. Please assess.",
    "urgent_review": "New chest tightness on exertion for 3 weeks, eases with rest. Please see.",
}


def test_only_referrals_that_need_action_show_a_card(tmp_path: Path) -> None:
    """A card only where the person signing the referral can act (`show`, owner decision 4,
    confirmed 2026-11-06): one to send back with what is missing, and one to review today; and,
    since the owner's choice of 2026-11-07, one refused as invalid input
    (`test_a_referral_refused_as_invalid_input_shows_a_card_that_says_why`). A referral to
    schedule or for a coordinator is decided and audited, with no card. An ABSTAIN because part
    of the record could not be read still shows its `info` card for every referral (owner
    decision 6)."""
    template = load_pack(PACK_DIR).manifest.cds_hooks
    assert template is not None and template.card is not None
    assert template.card.show == ["return_for_info", "urgent_review", "refused"]
    orders = [_order(f"sr-{n}", reason) for n, reason in enumerate(EACH_STEP.values())]
    with AuditStore(tmp_path / "audit.db") as store:
        router = Decision.from_path(PACK_DIR, store=store)
        cards = cds_hooks.invoke(router, _order_sign(orders, _record()))["cards"]
        decided = store.query(pack_id=router.pack.id)
        assert {row["recommendation"] for row in decided} == set(EACH_STEP)  # each one decided
        for row in decided:  # and audited, card or no card
            held = store.get(row["id"])
            assert held is not None and held["events"], row["recommendation"]
        made = {row["id"]: row["recommendation"] for row in decided}
        assert [made[card["uuid"]] for card in cards] == ["return_for_info", "urgent_review"]
        assert [(card["summary"], card["indicator"]) for card in cards] == [
            ("Referral to Cardiology is missing information", "warning"),
            ("Possibly urgent Cardiology referral: review today", "critical"),
        ]
        assert all(card["suggestions"][0]["label"] == "Create a referral task" for card in cards)

        unread = _record() | {
            "labs": {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "exception"}],
            }
        }
        cards = cds_hooks.invoke(router, _order_sign(orders, unread))["cards"]
        assert len(cards) == len(EACH_STEP)  # whatever `show` says
        assert {(card["summary"], card["indicator"]) for card in cards} == {
            (cds_hooks.UNREADABLE_SUMMARY, "info")
        }
        assert len(store.query(pack_id=router.pack.id, outcome="ABSTAIN")) == len(EACH_STEP)


REFUSED_SUMMARY = (
    "Referral not checked: Mendari refused its data as invalid, so no urgency or completeness "
    "check was made"
)
REASON_TOO_LONG = "referral/reason: must have at most 5000 characters"


@pytest.mark.parametrize("public", [None, "https://mendari.test"])
def test_a_referral_refused_as_invalid_input_shows_a_card_that_says_why(
    tmp_path: Path, public: str | None
) -> None:
    """The owner's choice (2026-11-07): a referral the engine refuses as invalid input (a reason
    over 5,000 characters) is a BLOCK with no recommendation, the card key `refused`, which
    `show` names. The person signing it sees a `warning` card that says the referral was not
    checked (no urgency or completeness check) and why, each reason as the audit record keeps
    it (where in the input and which rule of the schema, never the text), whether or not the
    engine's reasons are behind the audit link, and never the outcome sentence that says the
    action must not happen. Its suggestion is not recommended. It is decided and audited as
    before, and still not in the review queue. A record that could not be read does not change
    that: the refused input makes it a BLOCK, not the ABSTAIN whose `info` card always shows,
    and the card lists both reasons."""
    template = load_pack(PACK_DIR).manifest.cds_hooks
    assert template is not None and template.card is not None and template.card.show is not None
    assert "refused" in template.card.show and "none" not in template.card.show
    unread = _record() | {
        "labs": {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "exception"}],
        }
    }
    links = cds_hooks.CardLinks(public_url=public)
    text = "Palpitations for 6 months. " + "x" * 5_000
    with AuditStore(tmp_path / "audit.db") as store:
        router = Decision.from_path(PACK_DIR, store=store)
        details = []
        for prefetch in (_record(), unread):
            refused = [_order("sr-long", text)]
            answer = cds_hooks.invoke(router, _order_sign(refused, prefetch), links=links)
            (card,) = answer["cards"]
            assert (card["summary"], card["indicator"]) == (REFUSED_SUMMARY, "warning")
            assert card["suggestions"][0]["isRecommended"] is False
            assert ("links" in card) is (public is not None)
            assert "Palpitations" not in json.dumps(card) and "xxxxx" not in json.dumps(card)
            assert cds_hooks.REFUSED_DETAIL in card["detail"]
            assert cds_hooks.OUTCOME_SENTENCES[Outcome.BLOCK] not in card["detail"]
            assert f"- {REASON_TOO_LONG}" in card["detail"].splitlines()
            details.append(card["detail"])
        assert "- could not read labs: operation_outcome" not in details[0].splitlines()
        assert "- could not read labs: operation_outcome" in details[1].splitlines()
        decided = store.query(pack_id=router.pack.id)
        assert [
            (row["outcome"], row["recommendation"], row["review_status"]) for row in decided
        ] == [("BLOCK", None, "not_required")] * 2
        for row in decided:
            held = store.get(row["id"])
            assert held is not None and held["events"]
            assert f"input rejected: {REASON_TOO_LONG}" in held["record"]["reasons"]
        assert store.query(pack_id=router.pack.id, review_status="pending") == []


def test_a_referral_to_schedule_or_for_a_coordinator_still_shows_no_card(router: Decision) -> None:
    """The owner's choice keeps 'schedule' referrals without a card, as before (and a referral
    for a coordinator), each decided; only the refused one beside them shows a card."""
    orders = [
        _order("sr-schedule", EACH_STEP["schedule"]),
        _order("sr-coordinator", EACH_STEP["coordinator_review"]),
        _order("sr-long", "Palpitations for 6 months. " + "x" * 5_000),
    ]
    cards = cds_hooks.invoke(router, _order_sign(orders, _record()))["cards"]
    assert [card["summary"] for card in cards] == [REFUSED_SUMMARY]
