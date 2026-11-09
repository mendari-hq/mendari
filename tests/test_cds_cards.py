# SPDX-License-Identifier: Apache-2.0
"""Cards as CDS Hooks 2.0 and Da Vinci CRD expect (LP-23f): a card's uuid is its decision's id and
its suggestion's a UUID 5 of it, a card with suggestions says its `selectionBehavior` and offers
the override reasons, its summary reads display names (the Task's text keeps the labels) and stays
under 140 characters however it is filled, its detail is the status, the notice and one plain
sentence, never a percentage or the confidence; with `MENDARI_PUBLIC_URL` the engine's reasons
are behind a link to the audit view and, for a launch-registered EHR of the caller's
organization, a `smart` link opens the pack. A CRD service declares its version and its
`coverage-info` option in discovery, and a request that turns the option off gets no coverage
information, its orders still decided."""

from __future__ import annotations

import copy
import itertools
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_pack
from mendari.core.pack import manifest_template_problems
from mendari.core.policy import threshold_reason
from mendari.core.spec import SUMMARY_CHARS, card_summary_problems
from mendari.core.templates import (
    NO_RECOMMENDATION,
    display_choices,
    expansions,
    fill,
    fill_display,
    longest,
)
from mendari.errors import PackValidationError
from mendari.evals.pack_tests import validate_pack
from mendari.server.app import create_app
from mendari.standards import cds_hooks, terminology
from tests import test_fhir_lists as lists
from tests.conftest import INBOX_DIR, encrypt_mode
from tests.test_davinci import COMPLETE, request
from tests.test_fhir import COMMUNICATION, PATIENT
from tests.test_limits import order_sign
from tests.test_prior_auth_pack import PACK_DIR as PRIOR_AUTH_DIR
from tests.test_prior_auth_pack import _order, _site_payer
from tests.test_prior_auth_pack import _order_sign as prior_auth_sign
from tests.test_referral_pack import PACK_DIR as REFERRAL_DIR
from tests.test_referral_pack import _order as referral_order
from tests.test_referral_pack import _order_sign as referral_sign
from tests.test_referral_pack import _record as referral_record
from tests.test_templates import with_wording
from tests.test_trial_pack import _record as trial_record

PUBLIC = "https://mendari.test"
REFERRAL = "/cds-services/mendari.referral-router"
PRIOR_AUTH = "/cds-services/mendari.prior-auth-preflight"
TRIAL = "/cds-services/mendari.clinical-trial-prescreener"
INBOX = "/cds-services/mendari.patient-inbox-router"
EHR_A = "https://ehr-a.example.org/fhir"
EHR_B = "https://ehr-b.example.org/fhir"
BARE_PERCENT = re.compile(r"\d\s?%")
FIGURE = re.compile(r"\d\.\d\d")  # a confidence or a threshold, as the policy writes them

# CDS Hooks 2.0's Card, its parts closed (a field the specification does not define fails).
_CODING = {
    "type": "object",
    "required": ["code"],
    "properties": {
        "system": {"type": "string"},
        "code": {"type": "string"},
        "display": {"type": "string"},
    },
    "additionalProperties": False,
}
_ABSOLUTE = {"type": "string", "pattern": r"^https?://[^/\s]+"}
CARD = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["summary", "indicator", "source"],
    "properties": {
        "uuid": {"type": "string"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 139},  # under 140
        "detail": {"type": "string"},
        "indicator": {"enum": ["info", "warning", "critical"]},
        "source": {
            "type": "object",
            "required": ["label"],
            "properties": {
                "label": {"type": "string"},
                "url": _ABSOLUTE,
                "icon": _ABSOLUTE,
                "topic": _CODING,
            },
            "additionalProperties": False,
        },
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label"],
                "properties": {
                    "label": {"type": "string"},
                    "uuid": {"type": "string"},
                    "isRecommended": {"type": "boolean"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["type", "description"],
                            "properties": {
                                "type": {"enum": ["create", "update", "delete"]},
                                "description": {"type": "string"},
                                "resource": {"type": "object"},
                                "resourceId": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            },
        },
        "selectionBehavior": {"enum": ["at-most-one", "any"]},
        "overrideReasons": {"type": "array", "items": _CODING},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label", "url", "type"],
                "properties": {
                    "label": {"type": "string"},
                    "url": _ABSOLUTE,
                    "type": {"enum": ["absolute", "smart"]},
                    "appContext": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "extension": {"type": "object"},
    },
    "additionalProperties": False,
    "dependentRequired": {"suggestions": ["selectionBehavior"]},
}


def inbox_request() -> dict[str, Any]:
    return {
        "hook": "mendari-message-received",
        "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
        "context": {"communicationId": "msg-1", "patientId": "pat-1"},
        "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
    }


def trial_request() -> dict[str, Any]:
    record = trial_record("Seen for diabetes follow-up.")

    def searchset(kind: str) -> dict[str, Any]:
        found = [{"resource": r} for r in record if r["resourceType"] == kind]
        return {"resourceType": "Bundle", "type": "searchset", "entry": found}

    return {
        "hook": "patient-view",
        "hookInstance": "4b4c1b2e-0000-4000-8000-000000000002",
        "context": {"patientId": "p1", "userId": "Practitioner/1"},
        "prefetch": {
            "patient": record[0],
            "conditions": searchset("Condition"),
            "observations": searchset("Observation"),
            "medications": searchset("MedicationStatement"),
            "medication_requests": searchset("MedicationRequest"),
            "notes": searchset("DocumentReference"),
        },
    }


def prior_auth_request() -> dict[str, Any]:
    return request("acme", COMPLETE, _order("mri", "FX101"), _order("scope", "FX202"))


# Each reference pack's hook, called with a request that gives at least one card.
HOOKS = {
    INBOX: inbox_request,
    REFERRAL: lambda: order_sign(2),
    PRIOR_AUTH: prior_auth_request,
    TRIAL: trial_request,
}


def served(tmp_path: Path, name: str = "cards.db") -> TestClient:
    return TestClient(create_app(db_path=tmp_path / name))


def cards_of(client: TestClient, path: str, body: dict[str, Any] | None = None) -> list[Any]:
    answer = client.post(path, json=body if body is not None else HOOKS[path]())
    assert answer.status_code == 200, answer.text
    cards: list[Any] = answer.json()["cards"]
    return cards


def test_a_card_with_suggestions_says_its_selection_behavior(tmp_path: Path) -> None:
    client = served(tmp_path)
    for path in HOOKS:
        cards = cards_of(client, path)
        assert cards, path
        for card in cards:
            assert card["suggestions"], path
            assert card["selectionBehavior"] == "at-most-one", path
            assert card["overrideReasons"] == terminology.override_reasons(), path


def test_the_card_uuid_is_its_decision_id_and_suggestion_ids_are_stable(tmp_path: Path) -> None:
    """The card's uuid is the decision it shows (as its extension already said), recorded in the
    trail; its suggestion's is a UUID 5 of it, the same each time the card is made, so feedback
    is checked without anything stored. Two decisions' cards share neither."""
    client = served(tmp_path)
    cards = cards_of(client, REFERRAL)
    assert len(cards) == 2
    for card in cards:
        decision = card["extension"]["mendari.decision_id"]
        assert card["uuid"] == decision
        assert client.get(f"/api/audit/{decision}").status_code == 200
        suggestion = card["suggestions"][0]["uuid"]
        assert suggestion == str(uuid.uuid5(uuid.UUID(decision), "suggestion"))
        assert suggestion == cds_hooks.suggestion_id(decision)
        assert uuid.UUID(suggestion).version == 5
    assert len({c["uuid"] for c in cards} | {c["suggestions"][0]["uuid"] for c in cards}) == 4
    router = Decision.from_hub("mendari/referral-router")
    result, task = router.decide_fhir(
        order_sign(1)["prefetch"],
        focus=order_sign(1)["context"]["draftOrders"]["entry"][0]["resource"],
    )
    made = [cds_hooks.card(router.pack, result, task) for _ in range(2)]
    assert made[0]["uuid"] == made[1]["uuid"] == result.id
    assert made[0]["suggestions"][0]["uuid"] == made[1]["suggestions"][0]["uuid"]


def test_card_summaries_use_display_names_and_task_text_is_unchanged(tmp_path: Path) -> None:
    """The card reads the pack's display names (`names`, else a readable form of the label);
    the Task it suggests keeps its text and performer code, labels as they are."""
    client = served(tmp_path)
    mri, scope = cards_of(client, PRIOR_AUTH)
    assert mri["summary"] == "Prior auth for Lumbar MRI (Acme): ready to submit"  # `names`
    assert scope["summary"] == "Prior auth for Knee arthroscopy (Acme): ready to submit"
    task = mri["suggestions"][0]["actions"][0]["resource"]
    assert task["description"] == "Prior auth for lumbar_mri (acme): submit_ready"
    (inbox,) = cards_of(client, INBOX)
    assert inbox["summary"] == "Review: route to Nurse (stat)"  # a team's: the default wording
    assert inbox["suggestions"][0]["label"] == "Create a routing task"  # the inbox template's
    code = inbox["suggestions"][0]["actions"][0]["resource"]["performerType"][0]["coding"][0]
    assert code["code"] == "nurse"
    # `fill` is the Task's and unchanged; `fill_display` the card's.
    preflight = Decision.from_hub("mendari/prior-auth-preflight")
    body = prior_auth_request()
    order = body["context"]["draftOrders"]["entry"][0]["resource"]
    result, _ = preflight.decide_fhir(body["prefetch"], focus=order)
    template = "{service} {payer} {recommendation} {priority} {red_flag}"
    assert fill(template, result, recommendation="submit_ready") == (
        "lumbar_mri acme submit_ready routine no"
    )
    assert fill_display(template, result, preflight.pack.manifest) == (
        "Lumbar MRI Acme Ready to submit routine No"
    )
    abstained = copy.copy(result)
    object.__setattr__(abstained, "recommendation", None)
    assert fill_display("{recommendation}", abstained, preflight.pack.manifest) == (
        NO_RECOMMENDATION
    )


def test_every_summary_expansion_fits_140_characters(tmp_path: Path) -> None:
    """Every text every reference pack's summaries can give, with display names, is under a
    card's 140 characters (CDS Hooks 2.0: "<140"; Inferno's CRD kit fails a summary of 140):
    each expansion counted here, and `longest` is the longest of them. A summary that can read
    140 or longer is refused from a registry, warned when owned, and refused by `mendari
    validate`; an owned pack's card cuts it to 139."""
    roots = [p for p in sorted(INBOX_DIR.parent.iterdir()) if (p / "pack.yaml").is_file()]
    counted = 0
    for root in roots:
        pack = load_pack(root)
        assert card_summary_problems(pack.manifest) == [], root
        card = pack.manifest.cds_hooks.card if pack.manifest.cds_hooks else None
        if card is None:
            continue
        choices = display_choices(pack.manifest)
        for summary in card.summary_templates().values():  # also_show's summaries too
            texts = [text for text, _ in expansions(summary, choices)]
            assert max(len(text) for text in texts) <= SUMMARY_CHARS, summary
            assert len(longest(summary, choices)) == max(len(text) for text in texts)
            counted += len(texts)
    assert counted > 40
    # `longest` needs one expansion, however many a template has.
    many = {name: [f"{name}-{n}" * n for n in range(1, 6)] for name in "abcdef"}
    template = "{a}{b}{c}{d}{e}{f}{a}"
    brute = max(len(text) for text, _ in expansions(template, many))
    assert len(longest(template, many)) == brute
    assert sum(1 for _ in itertools.product(*many.values())) == 5**6

    # The longest display of `{t2d_diagnosis}` is "Unknown": 139 characters at most loads from a
    # registry as it is, and 140 is refused.
    assert SUMMARY_CHARS == 139
    fits = with_wording(tmp_path / "fits", summary="Candidate: " + "x" * 118 + " ({t2d_diagnosis})")
    loaded = load_pack(fits, origin="hub")
    assert loaded.warnings == () and card_summary_problems(loaded.manifest) == []
    long = with_wording(tmp_path / "long", summary="Candidate: " + "x" * 119 + " ({t2d_diagnosis})")
    problem = (
        "cds_hooks.card.summaries.candidate can read 140 characters with display names; "
        "a card's summary is at most 139 (CDS Hooks: under 140)"
    )
    with pytest.raises(PackValidationError) as refused:
        load_pack(long, origin="hub")
    assert refused.value.problems == [problem]
    owned = load_pack(long)
    assert owned.warnings == (problem,)
    assert manifest_template_problems(owned.manifest) == [problem]
    with pytest.raises(PackValidationError, match=re.escape(problem)):
        validate_pack(owned)
    # An owned pack's summary that reads longer is cut under 140 on its card.
    cut = with_wording(tmp_path / "cut", summary="Candidate: " + "x" * 140 + " ({t2d_diagnosis})")
    prefetch = {"patient": lists.PATIENT_9} | {
        key: lists.searchset([r for r in lists.RECORD if r["resourceType"] == kind])
        for key, kind in (
            ("conditions", "Condition"),
            ("observations", "Observation"),
            ("notes", "DocumentReference"),
        )
    }
    (made,) = cds_hooks.invoke(Decision.from_path(cut), lists.cds_request(prefetch))["cards"]
    assert made["summary"] == ("Candidate: " + "x" * 140)[:139]


@pytest.mark.parametrize("public", [None, PUBLIC])
def test_no_card_shows_a_bare_percentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public: str | None
) -> None:
    """The confidence left the card (it showed `confidence: 73%`): no card of any pack, linked
    or not, holds a figure as a percentage, in any of its words, nor the confidence or a
    threshold as a bare figure (the confidence thresholds' reason, which the trail keeps)."""
    if public is not None:
        monkeypatch.setenv("MENDARI_PUBLIC_URL", public)
    client = served(tmp_path)
    thresholds = 0
    for path in HOOKS:
        for card in cards_of(client, path):
            decision = client.get(f"/api/audit/{card['uuid']}").json()["record"]
            assert decision["confidence"] is None or decision["confidence"] > 0
            thresholds += sum(threshold_reason(reason) for reason in decision["reasons"])
            words = [card["summary"], card["detail"], card["source"]["label"]]
            words += [s["label"] for s in card["suggestions"]]
            words += [link["label"] for link in card.get("links", [])]
            assert [w for w in words if BARE_PERCENT.search(w)] == [], path
            assert "**confidence:**" not in card["detail"], path  # the old line, gone
            shown = [card["summary"], card["detail"]]  # the source label has the pack version
            assert [w for w in shown if FIGURE.search(w)] == [], path
            assert "confidence" not in card["detail"], path
    assert thresholds >= 3  # the referral, prior auth and trial decisions' reasons give them


def test_the_engine_trace_is_behind_the_audit_link_only_when_one_can_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `MENDARI_PUBLIC_URL` (or with one that is no web address) a card has no link and
    its reasons stay in its detail, but for the confidence thresholds' (`threshold_reason`),
    whose figures have no interval; with it, the detail is the status, the notice and one plain
    sentence, the reasons are behind an absolute link to the decision in the audit view, and
    `source.url` is the pack's card."""
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "ftp://mendari.test")
    for public in (None, "ftp://mendari.test"):
        if public is None:
            monkeypatch.delenv("MENDARI_PUBLIC_URL")
        client = served(tmp_path, f"plain-{public is None}.db")
        for card in cards_of(client, REFERRAL):
            assert "links" not in card and "url" not in card["source"]
            reasons = client.get(f"/api/audit/{card['uuid']}").json()["record"]["reasons"]
            kept = [reason for reason in reasons if not threshold_reason(reason)]
            assert kept and len(kept) < len(reasons)  # one of each, to show both ways
            assert all(f"- {reason}" in card["detail"] for reason in kept)
            assert not any(reason in card["detail"] for reason in reasons if reason not in kept)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", f"{PUBLIC}/")
    client = served(tmp_path, "linked.db")
    for card in cards_of(client, REFERRAL):
        decision = card["uuid"]
        assert card["links"] == [
            {
                "label": cds_hooks.AUDIT_LINK,
                "url": f"{PUBLIC}/?pack=mendari/referral-router#audit/{decision}",
                "type": "absolute",
            }
        ]
        assert card["source"]["url"] == f"{PUBLIC}/?pack=mendari/referral-router#card"
        record = client.get(f"/api/audit/{decision}").json()["record"]
        assert record["reasons"] and not any(r in card["detail"] for r in record["reasons"])
        status, sentence = card["detail"].split("\n\n")[:2]
        outcome = next(o for o in cds_hooks.OUTCOME_SENTENCES if o.value == record["outcome"])
        assert status.startswith("*") and sentence == cds_hooks.OUTCOME_SENTENCES[outcome]


def _launch_setup(monkeypatch: pytest.MonkeyPatch, *, smart: bool = True) -> None:
    monkeypatch.setenv("MENDARI_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv(
        "MENDARI_FHIR_ALLOWED_SERVERS", "https://ehr-a.example.org,https://ehr-b.example.org"
    )
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR_A}=default:decide,{EHR_B}=clinic-b:decide")
    if smart:
        monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")


def _launch_links(client: TestClient, fhir_server: str | None, **headers: str) -> list[Any]:
    body = order_sign(1)
    if fhir_server is not None:
        body["fhirServer"] = fhir_server
    answer = client.post(REFERRAL, json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    (card,) = answer.json()["cards"]
    return [link for link in card["links"] if link["type"] == "smart"]


def test_a_card_links_to_the_launch_view_only_for_a_registered_ehr_of_the_same_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `smart` link to the pack's launch, only when SMART is on and the request's `fhirServer`
    is registered for launches (`MENDARI_SMART_EHRS`) to the caller's own organization: open
    mode's is `default`."""
    _launch_setup(monkeypatch, smart=False)
    assert _launch_links(served(tmp_path, "off.db"), EHR_A) == []  # SMART off
    _launch_setup(monkeypatch)
    client = served(tmp_path, "open.db")
    assert _launch_links(client, EHR_A) == [
        {
            "label": cds_hooks.LAUNCH_LINK,
            "url": f"{PUBLIC}/smart/launch?pack=mendari/referral-router",
            "type": "smart",
        }
    ]
    assert _launch_links(client, f"{EHR_A}/") != []  # the base as registrations normalize it
    assert _launch_links(client, EHR_B) == []  # another organization's EHR
    assert _launch_links(client, "https://ehr-c.example.org/fhir") == []  # not registered
    assert _launch_links(client, None) == []

    # Signed in: a key of `clinic-b` gets the link for its own EHR only.
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)
    db = tmp_path / "signed.db"
    signed = TestClient(create_app(db_path=db))
    with AuditStore(db) as store:
        made = store.create_api_key(name="ehr", role="service", org="clinic-b", created_by="t")
    key = {"authorization": f"Bearer {made['key']}"}
    assert [link["type"] for link in _launch_links(signed, EHR_B, **key)] == ["smart"]
    assert _launch_links(signed, EHR_A, **key) == []


def test_discovery_declares_the_crd_version_and_configuration_options(tmp_path: Path) -> None:
    """A CRD service (a pack with `coverage_information`) declares CRD's version, its first two
    numbers, and one configuration option for its one card type, with CRD's four mandatory
    elements and a default; no other service declares either."""
    services = {s["id"]: s for s in served(tmp_path).get("/cds-services").json()["services"]}
    crd = services["mendari.prior-auth-preflight"]["extension"]
    assert crd["davinci-crd.version"] == ["2.2"]
    (option,) = crd["davinci-crd.configuration-options"]
    assert {key for key in option} == {"code", "type", "name", "description", "default"}
    assert (option["code"], option["type"], option["default"]) == ("coverage-info", "boolean", True)
    others = [s for name, s in services.items() if name != "mendari.prior-auth-preflight"]
    assert others and all("extension" not in s for s in others)


def test_coverage_info_false_sends_no_coverage_information(tmp_path: Path) -> None:
    """`davinci-crd.configuration` with `coverage-info: false`: neither the CRD service's cards
    (each says it is `coverage-info`) nor its system actions, and each order still decided and
    recorded. True, another option, or a value of another type changes nothing."""
    client = served(tmp_path)
    plain = client.post(PRIOR_AUTH, json=prior_auth_request()).json()
    assert len(plain["cards"]) == 2 and len(plain["systemActions"]) == 2
    assert {c["source"]["topic"]["code"] for c in plain["cards"]} == {"coverage-info"}
    off = prior_auth_request()
    off["extension"] = {"davinci-crd.configuration": {"coverage-info": False}}
    with AuditStore(tmp_path / "cards.db") as store:
        before = len(store.events())
    assert client.post(PRIOR_AUTH, json=off).json() == {"cards": []}
    with AuditStore(tmp_path / "cards.db") as store:
        made = [e for e in store.events()[before:] if e["event"] == "decision.created"]
    assert len(made) == 2
    for value in (True, "false", 0, None):
        on = prior_auth_request()
        on["extension"] = {"davinci-crd.configuration": {"coverage-info": value, "max-cards": 1}}
        answer = client.post(PRIOR_AUTH, json=on).json()
        assert len(answer["cards"]) == 2 and len(answer["systemActions"]) == 2, value
    for shape in ({"davinci-crd.configuration": "off"}, "off", ["coverage-info"]):
        odd = {**prior_auth_request(), "extension": shape}
        assert len(client.post(PRIOR_AUTH, json=odd).json()["systemActions"]) == 2
    # Only a CRD service reads it: the referral router's cards are no coverage information.
    referral = {**order_sign(1), "extension": off["extension"]}
    assert len(cards_of(client, REFERRAL, referral)) == 1


@pytest.mark.parametrize("public", [None, PUBLIC])
def test_every_card_matches_the_cds_hooks_2_0_card_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public: str | None
) -> None:
    """Every card of every reference pack's hook, an unreadable ABSTAIN's among them, linked and
    not, matches CDS Hooks 2.0's Card with nothing it does not define; a CRD service's card says
    its type (`source.topic`, which CRD 2.2.1 requires)."""
    if public is not None:
        _launch_setup(monkeypatch)
    client = served(tmp_path)
    validator = jsonschema.Draft202012Validator(CARD)
    seen = 0
    unreadable = order_sign(1)
    unreadable["prefetch"]["labs"] = {"resourceType": "OperationOutcome", "issue": []}
    # A refused referral (`refused`) and a red flag on an order `show` leaves out (`also_show`).
    refused = referral_sign(
        [referral_order("sr-long", "Palpitations. " + "x" * 5_000)], referral_record()
    )
    note = "New saddle anesthesia and urinary retention."
    red_flag = prior_auth_sign(_site_payer(note), _order("mri", "FX101"))
    added = [(REFERRAL, unreadable), (REFERRAL, refused), (PRIOR_AUTH, red_flag)]
    for path, body in [*((p, make()) for p, make in HOOKS.items()), *added]:
        if public is not None:
            body["fhirServer"] = EHR_A
        for card in cards_of(client, path, body):
            errors = [e.message for e in validator.iter_errors(card)]
            assert errors == [], (path, errors)
            assert ("topic" in card["source"]) is (path == PRIOR_AUTH)
            assert ("links" in card) is (public is not None)
            seen += 1
    assert seen >= 9
    assert cards_of(client, REFERRAL, unreadable)[0]["summary"] == cds_hooks.UNREADABLE_SUMMARY


def _edited(tmp_path: Path, source: Path, edit: Any) -> Path:
    """A copy of a reference pack whose manifest `edit` changed (in place, as a mapping)."""
    root = tmp_path / source.name
    shutil.copytree(source, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    edit(manifest)
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


def _card_of(manifest: dict[str, Any]) -> dict[str, Any]:
    card: dict[str, Any] = manifest["cds_hooks"]["card"]
    return card


def test_the_spec_0_3_card_keys_are_checked_with_the_manifest(tmp_path: Path) -> None:
    """`also_show` names policy rules, needs a `show` list and has summaries held to what
    `summaries` are (bare, known placeholders, under 140 characters); `refused` is a card key
    beside `none`, and may not also be a label of the primary question. Both are spec 0.3: a
    0.1 or 0.2 pack that uses either is refused at load, wherever it comes from."""

    def refused_by(source: Path, edit: Any, problem: str, name: str) -> None:
        root = _edited(tmp_path / name, source, edit)
        with pytest.raises(PackValidationError, match=re.escape(problem)):
            load_pack(root)

    def unknown_rule(m: dict[str, Any]) -> None:
        _card_of(m)["also_show"] = {"no-such-rule": {"summary": "Something to see"}}

    refused_by(
        PRIOR_AUTH_DIR,
        unknown_rule,
        "cds_hooks.card.also_show names unknown policy rules ['no-such-rule']",
        "rule",
    )

    def without_show(m: dict[str, Any]) -> None:
        del _card_of(m)["show"]

    refused_by(
        PRIOR_AUTH_DIR,
        without_show,
        "cds_hooks.card.also_show needs a `show` list (every card shows)",
        "show",
    )

    def placeholder(m: dict[str, Any]) -> None:
        _card_of(m)["also_show"]["possible-red-flag"]["summary"] = "Red flag for {no_such}"

    refused_by(
        PRIOR_AUTH_DIR,
        placeholder,
        "cds_hooks.card.also_show.possible-red-flag.summary uses unknown placeholders",
        "placeholder",
    )

    def older(m: dict[str, Any]) -> None:
        m["spec_version"] = "0.2"

    for source, named in (
        (PRIOR_AUTH_DIR, "['cds_hooks.card.also_show']"),
        (REFERRAL_DIR, "[\"cds_hooks.card key 'refused'\"]"),
    ):
        problem = f"{named} use spec 0.3 card features; set spec_version: '0.3'"
        refused_by(source, older, problem, source.name)
    # A summary that can read 140 characters or longer is a problem, as for `summaries`.
    root = _edited(
        tmp_path / "long",
        PRIOR_AUTH_DIR,
        lambda m: _card_of(m)["also_show"]["possible-red-flag"].update(summary="x" * 140),
    )
    owned = load_pack(root)
    assert card_summary_problems(owned.manifest) == [
        "cds_hooks.card.also_show.possible-red-flag.summary can read 140 characters with "
        "display names; a card's summary is at most 139 (CDS Hooks: under 140)"
    ]


def test_a_template_that_does_not_name_refused_keys_refused_input_as_none(
    tmp_path: Path,
) -> None:
    """`refused` is opt-in: a card template that names it in none of `summaries`, `indicators`
    and `show` words a decision that refused its input as `none`, as before spec 0.3's card
    keys, so packs that do not use it are unchanged (the inbox's `none` summary for its BLOCK,
    `tests/test_inbox_pack.py`). The Referral Router without its `refused` entries is quiet for
    a refused referral again, its `none` key not in `show`."""

    def drop_refused(m: dict[str, Any]) -> None:
        card = _card_of(m)
        del card["summaries"]["refused"], card["indicators"]["refused"]
        card["show"].remove("refused")

    root = _edited(tmp_path, REFERRAL_DIR, drop_refused)
    router = Decision.from_path(root)
    body = referral_sign([referral_order("sr-long", "x" * 5_001)], referral_record())
    assert cds_hooks.invoke(router, body) == {"cards": []}
    template = router.pack.manifest.cds_hooks
    assert template is not None and template.card is not None
    assert not template.card.names_refused()
    result, _ = router.decide_fhir(
        body["prefetch"], focus=body["context"]["draftOrders"]["entry"][0]["resource"]
    )
    assert (result.outcome.value, result.rejected) == ("BLOCK", True)
    assert cds_hooks.card_key(template.card, result) == "none"
    assert not cds_hooks.shows_card(template.card, result)
    shown = Decision.from_path(REFERRAL_DIR)
    template = shown.pack.manifest.cds_hooks
    assert template is not None and template.card is not None
    assert cds_hooks.card_key(template.card, result) == "refused"
