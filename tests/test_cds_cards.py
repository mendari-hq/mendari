# SPDX-License-Identifier: Apache-2.0
"""Cards as CDS Hooks 2.0 expects (LP-23f): a card's uuid is its decision's id and its
suggestion's a UUID 5 of it, a card with suggestions says its `selectionBehavior` and offers the
override reasons, its summary reads display names (the Task's text keeps the labels) and stays
under 140 characters however it is filled, its detail is the status, the notice and one plain
sentence, never a percentage or the confidence; with `MENDARI_PUBLIC_URL` the engine's reasons
are behind a link to the audit view and, for a launch-registered EHR of the caller's
organization, a `smart` link opens the agent. The hooks are the test fixtures': the triage
fixture's message hook (`tests/support/cds_messages.py`), the chart-summary fixture's
`patient-view` and its `order-sign` twin (`tests/support/chart_fixture.py`), on synthetic
charts (`tests/support/fixture_records.py`)."""

from __future__ import annotations

import copy
import itertools
import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.core.agent import manifest_template_problems
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
from mendari.errors import AgentValidationError
from mendari.evals.behaviour_tests import validate_agent
from mendari.server.app import create_app
from mendari.standards import cds_hooks, terminology
from tests.conftest import CHART_DIR, DEMO_AGENT_DIR, FIXTURE_AGENTS, TRIAGE_DIR, encrypt_mode
from tests.support.cds_messages import HOOK as TRIAGE
from tests.support.cds_messages import message_hook
from tests.support.chart_fixture import ORDERS_SERVICE as ORDERS
from tests.support.chart_fixture import SERVICE as CHART
from tests.support.chart_fixture import chart_copy, hook, order_hook, order_sign, prefetch
from tests.support.fixture_records import (
    ICD10PCS,
    SHOWCASE,
    Case,
    Condition,
    Medication,
)
from tests.support.fixture_records import ORDERS as ORDER_CODES

PUBLIC = "https://mendari.test"
EHR_A = "https://ehr-a.example.org/fhir"
EHR_B = "https://ehr-b.example.org/fhir"
EHR_C = "https://ehr-c.example.org/fhir"
BARE_PERCENT = re.compile(r"\d\s?%")
FIGURE = re.compile(r"\d\.\d\d")  # a confidence or a threshold, as the policy writes them
# A synthetic chart with one long-term condition (type 2 diabetes) and metformin: a routine
# follow-up, automated, so its decision's reasons hold the confidence thresholds' too.
ROUTINE_CHART = Case(
    id="chart-cards-1",
    age=58,
    sex="male",
    conditions=(Condition("E11.9", "2018-05-02"),),
    medications=(Medication("6809", "2023-03-01"),),
)
SOON_SUMMARY = "Synthetic chart summary: see the patient soon; anticoagulant listed: Yes"

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


def checked(id_: str, code: str) -> dict[str, Any]:
    """A draft order the order-sign twin checks: one of its ICD-10-PCS codes."""
    title, text = ORDER_CODES[code]
    return {
        "resourceType": "ServiceRequest",
        "id": id_,
        "status": "draft",
        "intent": "order",
        "code": {"coding": [{"system": ICD10PCS, "code": code, "display": title}], "text": text},
        "subject": {"reference": f"Patient/{SHOWCASE.id}"},
    }


def orders_request() -> dict[str, Any]:
    """Two orders the twin checks, signed together on the showcase chart: two cards."""
    return order_hook(SHOWCASE, checked("o1", "0DJD8ZZ"), checked("o2", "0DJ08ZZ"))


def inbox_request() -> dict[str, Any]:
    """The message hook's request (`mendari-message-received`, a refill with its Communication
    and sender as prefetch): one card from the triage fixture's service.
    `tests/test_agent_calibration.py` sends it to a calibrated agent's hook."""
    return message_hook()


# Each fixture's hook, called with a request that gives at least one card.
HOOKS: dict[str, Callable[[], dict[str, Any]]] = {
    TRIAGE: message_hook,  # a refill: the pharmacy's card
    CHART: lambda: hook(ROUTINE_CHART),  # a routine follow-up
    ORDERS: orders_request,  # a follow-up soon, once for each order
}


def served(tmp_path: Path, name: str = "cards.db") -> TestClient:
    """The engine serving the test fixtures' agents and the chart fixture's order-sign twin."""
    agents = tmp_path / "agents"
    if not agents.is_dir():
        order_sign(agents)
    return TestClient(create_app(db_path=tmp_path / name, agents_paths=[FIXTURE_AGENTS, agents]))


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
    cards = cards_of(client, ORDERS)
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
    twin = Decision.from_path(tmp_path / "agents" / "chart-orders")
    body = orders_request()
    result, task = twin.decide_fhir(
        body["prefetch"], focus=body["context"]["draftOrders"]["entry"][0]["resource"]
    )
    made = [cds_hooks.card(twin.agent, result, task) for _ in range(2)]
    assert made[0]["uuid"] == made[1]["uuid"] == result.id
    assert made[0]["suggestions"][0]["uuid"] == made[1]["suggestions"][0]["uuid"]


def test_card_summaries_use_display_names_and_task_text_is_unchanged(tmp_path: Path) -> None:
    """The card reads the agent's display names (`names`, else a readable form of the label);
    the Task it suggests keeps its text and performer code, labels as they are."""
    client = served(tmp_path)
    (chart,) = cards_of(client, CHART, hook(SHOWCASE))
    assert chart["summary"] == SOON_SUMMARY  # a yes-or-no question's display: "Yes"
    task = chart["suggestions"][0]["actions"][0]["resource"]
    assert task["description"] == (
        "Synthetic chart summary: follow-up soon; anticoagulant listed: yes"
    )
    (desk,) = cards_of(client, TRIAGE, message_hook(2, "Can I book an appointment next week?"))
    assert desk["summary"] == "Front desk: a message to read (routine)"  # `names`
    assert desk["suggestions"][0]["label"] == "Create a routing task"  # the triage template's
    code = desk["suggestions"][0]["actions"][0]["resource"]["performerType"][0]["coding"][0]
    assert code["code"] == "front_desk"
    urgent = message_hook(3, "I have chest pain since this morning.")
    (emergency,) = cards_of(client, TRIAGE, urgent)
    # A team with no `names` entry: the default wording, a readable form of its label.
    assert emergency["summary"] == "Possible emergency: Nurse triage now (stat)"
    # `fill` is the Task's and unchanged; `fill_display` the card's.
    reader = Decision.from_path(CHART_DIR)
    result, _ = reader.decide_fhir(prefetch(SHOWCASE))
    template = "{follow_up} {on_anticoagulant} {recommendation} {priority}"
    assert fill(template, result, recommendation="soon") == "soon yes soon urgent"
    assert fill_display(template, result, reader.agent.manifest) == (
        "Follow-up soon Yes Follow-up soon urgent"
    )
    abstained = copy.copy(result)
    object.__setattr__(abstained, "recommendation", None)
    assert fill_display("{recommendation}", abstained, reader.agent.manifest) == (NO_RECOMMENDATION)


def with_summary(base: Path, summary: str) -> Path:
    """A copy of the chart fixture under its own id (not a bundled agent: from a registry, it is
    bounded) with its `soon` card summary replaced."""

    def worded(manifest: dict[str, Any]) -> None:
        manifest["cds_hooks"]["card"]["summaries"]["soon"] = summary

    return chart_copy(base, manifest=worded)


def test_every_summary_expansion_fits_140_characters(tmp_path: Path) -> None:
    """Every text every bundled and test fixture agent's summaries can give, with display names,
    is under a card's 140 characters (CDS Hooks 2.0: "<140"; Inferno's CRD kit fails a summary
    of 140): each expansion counted here, and `longest` is the longest of them. A summary that
    can read 140 or longer is refused from a registry, warned when owned, and refused by
    `mendari validate`; an owned agent's card cuts it to 139."""
    folders = [p for p in sorted(FIXTURE_AGENTS.iterdir()) if (p / "agent.yaml").is_file()]
    roots = [DEMO_AGENT_DIR, *folders]
    assert TRIAGE_DIR in roots and CHART_DIR in roots
    counted = 0
    for root in roots:
        agent = load_agent(root)
        assert card_summary_problems(agent.manifest) == [], root
        card = agent.manifest.cds_hooks.card if agent.manifest.cds_hooks else None
        if card is None:
            continue
        choices = display_choices(agent.manifest)
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

    # The longest display of `{on_anticoagulant}` is "unknown": 139 characters at most loads
    # from a registry as it is, and 140 is refused.
    assert SUMMARY_CHARS == 139
    fits = with_summary(tmp_path / "fits", "Soon: " + "x" * 123 + " ({on_anticoagulant})")
    loaded = load_agent(fits, origin="hub")
    assert loaded.warnings == () and card_summary_problems(loaded.manifest) == []
    long = with_summary(tmp_path / "long", "Soon: " + "x" * 124 + " ({on_anticoagulant})")
    problem = (
        "cds_hooks.card.summaries.soon can read 140 characters with display names; "
        "a card's summary is at most 139 (CDS Hooks: under 140)"
    )
    with pytest.raises(AgentValidationError) as refused:
        load_agent(long, origin="hub")
    assert refused.value.problems == [problem]
    owned = load_agent(long)
    assert owned.warnings == (problem,)
    assert manifest_template_problems(owned.manifest) == [problem]
    with pytest.raises(AgentValidationError, match=re.escape(problem)):
        validate_agent(owned)
    # An owned agent's summary that reads longer is cut under 140 on its card.
    cut = with_summary(tmp_path / "cut", "Soon: " + "x" * 140 + " ({on_anticoagulant})")
    (made,) = cds_hooks.invoke(Decision.from_path(cut), hook(SHOWCASE))["cards"]
    assert made["summary"] == ("Soon: " + "x" * 140)[:139]


@pytest.mark.parametrize("public", [None, PUBLIC])
def test_no_card_shows_a_bare_percentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public: str | None
) -> None:
    """The confidence left the card (it showed `confidence: 73%`): no card of any agent, linked
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
            shown = [card["summary"], card["detail"]]  # the source label has the agent version
            assert [w for w in shown if FIGURE.search(w)] == [], path
            assert "confidence" not in card["detail"], path
    assert thresholds >= 2  # the triage and the routine chart decisions' reasons give them


def test_the_engine_trace_is_behind_the_audit_link_only_when_one_can_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `MENDARI_PUBLIC_URL` (or with one that is no web address) a card has no link and
    its reasons stay in its detail, but for the confidence thresholds' (`threshold_reason`),
    whose figures have no interval; with it, the detail is the status, the notice and one plain
    sentence, the reasons are behind an absolute link to the decision in the audit view, and
    `source.url` is the agent's card. Both links name the agent as the console's address does
    (`?agent=`). On the triage fixture's card, whose decision has a reason of each kind."""
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "ftp://mendari.test")
    for public in (None, "ftp://mendari.test"):
        if public is None:
            monkeypatch.delenv("MENDARI_PUBLIC_URL")
        client = served(tmp_path, f"plain-{public is None}.db")
        for card in cards_of(client, TRIAGE):
            assert "links" not in card and "url" not in card["source"]
            reasons = client.get(f"/api/audit/{card['uuid']}").json()["record"]["reasons"]
            kept = [reason for reason in reasons if not threshold_reason(reason)]
            assert kept and len(kept) < len(reasons)  # one of each, to show both ways
            assert all(f"- {reason}" in card["detail"] for reason in kept)
            assert not any(reason in card["detail"] for reason in reasons if reason not in kept)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", f"{PUBLIC}/")
    client = served(tmp_path, "linked.db")
    for card in cards_of(client, TRIAGE):
        decision = card["uuid"]
        assert card["links"] == [
            {
                "label": cds_hooks.AUDIT_LINK,
                "url": f"{PUBLIC}/?agent=mendari-test/triage#audit/{decision}",
                "type": "absolute",
            }
        ]
        assert card["source"]["url"] == f"{PUBLIC}/?agent=mendari-test/triage#card"
        record = client.get(f"/api/audit/{decision}").json()["record"]
        assert record["reasons"] and not any(r in card["detail"] for r in record["reasons"])
        status, sentence = card["detail"].split("\n\n")[:2]
        outcome = next(o for o in cds_hooks.OUTCOME_SENTENCES if o.value == record["outcome"])
        assert status.startswith("*") and sentence == cds_hooks.OUTCOME_SENTENCES[outcome]


def _launch_setup(monkeypatch: pytest.MonkeyPatch, *, smart: bool = True) -> None:
    """SMART on (unless not `smart`), EHRs A and B registered for launches, and C allowed but not
    registered: the chart fixture's hook opens a client for a `fhirServer` it is sent (its
    mapping follows references), so an EHR off the allow-list is refused before any card."""
    monkeypatch.setenv("MENDARI_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv(
        "MENDARI_FHIR_ALLOWED_SERVERS",
        "https://ehr-a.example.org,https://ehr-b.example.org,https://ehr-c.example.org",
    )
    monkeypatch.setenv("MENDARI_SMART_EHRS", f"{EHR_A}=default:decide,{EHR_B}=clinic-b:decide")
    if smart:
        monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", "mendari-app")


def _launch_links(client: TestClient, fhir_server: str | None, **headers: str) -> list[Any]:
    body = hook(SHOWCASE)
    if fhir_server is not None:
        body["fhirServer"] = fhir_server
    answer = client.post(CHART, json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    (card,) = answer.json()["cards"]
    return [link for link in card["links"] if link["type"] == "smart"]


def test_a_card_links_to_the_launch_view_only_for_a_registered_ehr_of_the_same_organization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `smart` link to the agent's launch, only when SMART is on and the request's `fhirServer`
    is registered for launches (`MENDARI_SMART_EHRS`) to the caller's own organization: open
    mode's is `default`. It names the agent by the launch route's own parameter, `pack`, which
    stays (a name on the wire: `GET /smart/launch?pack=`)."""
    _launch_setup(monkeypatch, smart=False)
    assert _launch_links(served(tmp_path, "off.db"), EHR_A) == []  # SMART off
    _launch_setup(monkeypatch)
    client = served(tmp_path, "open.db")
    assert _launch_links(client, EHR_A) == [
        {
            "label": cds_hooks.LAUNCH_LINK,
            "url": f"{PUBLIC}/smart/launch?agent=mendari-test/chart-summary",
            "type": "smart",
        }
    ]
    assert _launch_links(client, f"{EHR_A}/") != []  # the base as registrations normalize it
    assert _launch_links(client, EHR_B) == []  # another organization's EHR
    assert _launch_links(client, EHR_C) == []  # allowed, not registered for launches
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


@pytest.mark.parametrize("public", [None, PUBLIC])
def test_every_card_matches_the_cds_hooks_2_0_card_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, public: str | None
) -> None:
    """Every card of every fixture's hook, an unreadable ABSTAIN's among them, linked and not,
    matches CDS Hooks 2.0's Card with nothing it does not define; none says a card type
    (`source.topic`), which no hook here has."""
    if public is not None:
        _launch_setup(monkeypatch)
    client = served(tmp_path)
    validator = jsonschema.Draft202012Validator(CARD)
    seen = 0
    unreadable = hook(SHOWCASE)
    unreadable["prefetch"]["conditions"] = {"resourceType": "OperationOutcome", "issue": []}
    # A chart refused as invalid input (`refused`: a child's, as the agent is for adults) and a
    # possible emergency in a message for a team `show` leaves out (`also_show`).
    refused = hook(replace(SHOWCASE, id="chart-cards-child", age=12))
    emergency = message_hook(4, "I have chest pain since this morning.")
    added = [(CHART, unreadable), (CHART, refused), (TRIAGE, emergency)]
    for path, body in [*((p, make()) for p, make in HOOKS.items()), *added]:
        if public is not None:
            body["fhirServer"] = EHR_A
        for card in cards_of(client, path, body):
            errors = [e.message for e in validator.iter_errors(card)]
            assert errors == [], (path, errors)
            assert "topic" not in card["source"]
            assert ("links" in card) is (public is not None)
            seen += 1
    assert seen >= 7
    assert cards_of(client, CHART, unreadable)[0]["summary"] == cds_hooks.UNREADABLE_SUMMARY
    assert cards_of(client, CHART, refused)[0]["summary"] == (
        "Chart not summarized: Mendari refused the record as invalid input"
    )


def _card_of(manifest: dict[str, Any]) -> dict[str, Any]:
    card: dict[str, Any] = manifest["cds_hooks"]["card"]
    return card


def test_the_spec_0_3_card_keys_are_checked_with_the_manifest(
    agent_copy: Callable[..., Path],
) -> None:
    """`also_show` names policy rules, needs a `show` list and has summaries held to what
    `summaries` are (bare, known placeholders, under 140 characters), on copies of the triage
    fixture, whose `possible-emergency` rule has a card of its own."""

    def refused_by(edit: Any, problem: str) -> None:
        root = agent_copy(manifest=edit)
        with pytest.raises(AgentValidationError, match=re.escape(problem)):
            load_agent(root)

    def unknown_rule(m: dict[str, Any]) -> None:
        _card_of(m)["also_show"] = {"no-such-rule": {"summary": "Something to see"}}

    refused_by(unknown_rule, "cds_hooks.card.also_show names unknown policy rules ['no-such-rule']")

    def without_show(m: dict[str, Any]) -> None:
        del _card_of(m)["show"]

    refused_by(without_show, "cds_hooks.card.also_show needs a `show` list (every card shows)")

    def placeholder(m: dict[str, Any]) -> None:
        _card_of(m)["also_show"]["possible-emergency"]["summary"] = "Emergency for {no_such}"

    refused_by(
        placeholder,
        "cds_hooks.card.also_show.possible-emergency.summary uses unknown placeholders",
    )
    # A summary that can read 140 characters or longer is a problem, as for `summaries`.
    root = agent_copy(
        manifest=lambda m: _card_of(m)["also_show"]["possible-emergency"].update(summary="x" * 140)
    )
    owned = load_agent(root)
    assert card_summary_problems(owned.manifest) == [
        "cds_hooks.card.also_show.possible-emergency.summary can read 140 characters with "
        "display names; a card's summary is at most 139 (CDS Hooks: under 140)"
    ]


def test_a_template_that_does_not_name_refused_keys_refused_input_as_none(
    tmp_path: Path,
) -> None:
    """`refused` is opt-in: a card template that names it in none of `summaries`, `indicators`
    and `show` words a decision that refused its input as `none`, so an agent that does not use
    it words its BLOCK as before. The chart fixture without its `refused` entries is quiet for a
    refused chart (a child's), its `none` key not in `show`."""

    def drop_refused(m: dict[str, Any]) -> None:
        card = _card_of(m)
        del card["summaries"]["refused"], card["indicators"]["refused"]
        card["show"].remove("refused")

    child = replace(SHOWCASE, id="chart-cards-child", age=12)
    quiet = Decision.from_path(chart_copy(tmp_path, manifest=drop_refused))
    assert cds_hooks.invoke(quiet, hook(child)) == {"cards": []}
    template = quiet.agent.manifest.cds_hooks
    assert template is not None and template.card is not None
    assert not template.card.names_refused()
    result, _ = quiet.decide_fhir(prefetch(child))
    assert (result.outcome.value, result.rejected) == ("BLOCK", True)
    assert cds_hooks.card_key(template.card, result) == "none"
    assert not cds_hooks.shows_card(template.card, result)
    shown = Decision.from_path(CHART_DIR)
    template = shown.agent.manifest.cds_hooks
    assert template is not None and template.card is not None
    assert cds_hooks.card_key(template.card, result) == "refused"
