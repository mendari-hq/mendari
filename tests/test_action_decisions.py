# SPDX-License-Identifier: Apache-2.0
"""Decisions that carry proposed actions (spec 0.4 sections 4 and 5): the first way in.

The fixture agent `tests/fixtures/mini-agents/actions-mini` declares one action of each effect,
and its canned agent proposes them. Here each proposal is followed to its disposition, under
every outcome; a proposal that cannot be released lowers the decision and never raises it; a
response that is not of the contract's shape fails closed; an agent the deployment does not
vouch for gets nothing released without a person; and an agent that declares no action
decides as an answer alone, until its agent proposes one."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import AuditStore, Decision, GovernanceOverride, load_pack
from mendari.cli import main
from mendari.core import actions
from mendari.core.decision import DecisionResult
from mendari.core.spec import Outcome
from mendari.errors import PackValidationError
from mendari.providers.agent import ENDPOINT_ENV, validate_response
from tests.conftest import DEMO_AGENT_DIR, ROOT, message

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
URL = "https://actions-agent.test/decide"
SENTINELS = ("SENTINEL-ARGUMENT-TEXT", "delete_chart_sentinel", "SENTINEL-EXTRA-KEY")
Edit = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def agent() -> Decision:
    return Decision.from_path(MINI)


def copy_of(
    tmp_path: Path, *, manifest: Edit | None = None, mock: Edit | None = None, name: str = "copy"
) -> Path:
    """The fixture agent, copied, with its manifest and its canned answers edited."""
    target = tmp_path / name
    shutil.copytree(MINI, target)
    if manifest is not None:
        raw = yaml.safe_load((target / "agent.yaml").read_text())
        manifest(raw)
        (target / "agent.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    if mock is not None:
        path = target / "fixtures" / "mock_agent.json"
        data = json.loads(path.read_text())
        mock(data)
        path.write_text(json.dumps(data))
    return target


def shown(result: DecisionResult) -> list[tuple[str, str | None, str, str]]:
    assert result.actions is not None
    return [
        (a.name, None if a.effect is None else a.effect.value, a.disposition, a.reason)
        for a in result.actions
    ]


def live(monkeypatch: pytest.MonkeyPatch, respond: Callable[[dict[str, Any]], Any]) -> list[Any]:
    """Point `MENDARI_AGENT_URL` at `respond`, which answers each request's JSON body."""
    monkeypatch.setenv(ENDPOINT_ENV, URL)
    seen: list[dict[str, Any]] = []

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json=respond(body))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return seen


def answers(request: str = "refill", confidence: float = 0.95, **more: Any) -> dict[str, Any]:
    return {
        "answers": {
            "request": {"value": request, "confidence": confidence},
            "abusive": {"value": "no", "confidence": 0.97},
        },
        **more,
    }


# -- each outcome, and what it does to each effect ------------------------------------------------


def test_an_automated_decision_releases_what_reads_drafts_writes_and_contacts(
    agent: Decision,
) -> None:
    result = agent(message("Could you refill my lisinopril?"))
    assert (result.outcome, result.recommendation) == (Outcome.AUTO, "refill")
    assert shown(result) == [  # two proposals with the same name and arguments are one
        ("draft_reply", "draft", "released", "with_decision"),
        ("send_reply", "contact", "released", "automated"),
    ]
    assert result.reasons == ("confidence 0.95 >= auto threshold 0.90",)  # nothing stopped it
    first, second = result.actions or ()
    assert first.arguments == {"message": "Your refill request is with the pharmacy."}
    assert first.id != second.id and first.slot == second.slot == "reader"
    assert first.arguments_hash == actions.plain_hash(first.arguments)
    assert (first.declared, first.source, first.problems) == (True, "answer", ())


def test_an_order_is_never_released_without_a_person(agent: Decision) -> None:
    result = agent(message("Please renew my prescription."))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert result.reasons == (
        "confidence 0.95 >= auto threshold 0.90",
        "action 'renew_prescription' (order) is never released without a person; a person decides",
    )
    assert shown(result) == [
        ("look_up_medications", "read", "released", "with_decision"),
        ("renew_prescription", "order", "held", "awaiting_review"),
    ]
    assert result.priority.value == "routine" and result.rule is None  # nothing else changed


def test_an_action_the_manifest_tightens_is_never_released_without_a_person(
    agent: Decision,
) -> None:
    result = agent(message("Put it in the queue, please."))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert result.reasons[-1] == (
        "action 'queue_refill' is never released without a person (allow_auto: false); "
        "a person decides"
    )
    assert shown(result) == [
        ("draft_reply", "draft", "released", "with_decision"),
        ("queue_refill", "write", "held", "awaiting_review"),
    ]


def test_a_decision_that_awaits_review_holds_what_writes_and_contacts(agent: Decision) -> None:
    result = agent(message("I need to move my appointment."))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "appointment")
    assert shown(result) == [
        ("look_up_medications", "read", "released", "with_decision"),
        ("file_note", "write", "held", "awaiting_review"),
        ("send_reply", "contact", "held", "awaiting_review"),
    ]
    # Nothing proposed here stops automation: the confidence alone sent it to a person.
    assert result.reasons == ("confidence 0.70 is below the auto threshold 0.90 for 'appointment'",)


def test_an_abstention_blocks_side_effects_at_once_and_still_reads(agent: Decision) -> None:
    result = agent(message("It is unclear what I want."))
    assert (result.outcome, result.recommendation) == (Outcome.ABSTAIN, None)
    assert shown(result) == [
        ("look_up_medications", "read", "released", "with_decision"),
        ("file_note", "write", "blocked", "no_recommendation"),
        ("renew_prescription", "order", "blocked", "no_recommendation"),
    ]
    assert not [a for a in result.actions or () if a.disposition == "held"]  # nobody confirms it


def test_a_rules_block_blocks_every_action(agent: Decision) -> None:
    result = agent(message("This message is abusive."))
    assert result.outcome is Outcome.BLOCK and result.rule == "abusive-message"
    assert shown(result) == [
        ("look_up_medications", "read", "blocked", "decision_blocked"),
        ("draft_reply", "draft", "blocked", "decision_blocked"),
    ]
    assert result.reasons == ("rule abusive-message: The message is abusive and is not routed.",)


def test_an_input_the_schema_refuses_proposes_nothing(agent: Decision) -> None:
    refused = agent({"message": {"body": "refill", "extra": True}})
    assert refused.outcome is Outcome.BLOCK and refused.rejected
    assert refused.actions == ()  # declared, none proposed: no provider ran
    assert refused.to_dict()["actions"] == []


def test_an_agent_that_proposes_nothing_has_an_empty_list(agent: Decision) -> None:
    result = agent(message("Hello there."))
    assert (result.outcome, result.actions) == (Outcome.AUTO, ())
    assert result.to_dict()["actions"] == []


# -- what is refused lowers the decision ----------------------------------------------------------


def test_an_undeclared_action_is_blocked_and_the_decision_goes_to_a_person(
    agent: Decision,
) -> None:
    result = agent(message("Try an undeclared thing."))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert shown(result) == [
        ("draft_reply", "draft", "released", "with_decision"),
        ("delete_chart_sentinel", None, "blocked", "undeclared"),
    ]
    assert result.reasons[-1] == (
        "the agent proposed an action its manifest does not declare; a person decides"
    )
    undeclared = (result.actions or ())[1]
    assert (undeclared.declared, undeclared.effect) == (False, None)
    # The caller's own answer names it, as proposed; nothing that is kept does.
    assert undeclared.to_dict()["name"] == "delete_chart_sentinel"
    assert undeclared.chained()["name"] is None


def test_arguments_that_fail_their_schema_block_the_action_and_lower_the_decision(
    agent: Decision,
) -> None:
    result = agent(message("Send malformed arguments."))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "refill")
    assert shown(result) == [("send_reply", "contact", "blocked", "invalid_arguments")]
    assert result.reasons[-1] == (
        "action 'send_reply': its arguments do not match its schema; a person decides"
    )
    (blocked,) = result.actions or ()
    assert blocked.problems == (
        "(root): has a field the schema does not allow",
        "message: must be of type string",
    )
    assert blocked.to_dict()["problems"] == list(blocked.problems)


@pytest.mark.parametrize(
    "body",
    [
        "Could you refill my lisinopril?",
        "Please renew my prescription.",
        "Try an undeclared thing.",
        "Send malformed arguments.",
        "It is unclear what I want.",
        "This message is abusive.",
    ],
)
def test_no_reason_record_or_problem_holds_an_argument_or_an_undeclared_name(
    agent: Decision, body: str
) -> None:
    result = agent(message(body))
    record = result.to_record()
    assert "actions" not in record  # the stored record has the shape it always had
    kept = json.dumps([record, list(result.reasons)])
    for action in result.actions or ():
        kept += json.dumps([action.chained(), list(action.problems)])
        for value in action.arguments.values():
            if isinstance(value, str):
                assert value not in kept, value
    for sentinel in SENTINELS:
        assert sentinel not in kept, sentinel
    # A reason names an action only when the manifest declares it.
    declared = set(agent.pack.manifest.actions)
    for reason in result.reasons:
        if reason.startswith("action '"):
            assert reason.split("'")[1] in declared


def test_a_provider_failure_proposes_nothing_and_nothing_automates(tmp_path: Path) -> None:
    def no_default(mock: dict[str, Any]) -> None:
        del mock["default"]

    decision = Decision.from_path(copy_of(tmp_path, mock=no_default))
    result = decision(message("Hello there."))  # no case matches: the slot fails
    assert result.outcome is Outcome.ABSTAIN and result.actions == ()
    assert result.errors == {"request": "invalid_response", "abusive": "invalid_response"}


# -- a response that is not of the contract's shape fails closed ----------------------------------


def nine_deep() -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(8):
        value = {"in": value}
    return value


MALFORMED: dict[str, Any] = {
    "not a list": {"name": "send_reply"},
    "a text": "send_reply",
    "seventeen": [{"name": "look_up_medications", "arguments": {"n": n}} for n in range(17)],
    "a name of the wrong shape": [{"name": "Send Reply"}],
    "no name": [{"arguments": {}}],
    "an extra key": [{"name": "send_reply", "arguments": {}, "effect": "read"}],
    "arguments that are no object": [{"name": "send_reply", "arguments": ["x"]}],
    "arguments too long": [{"name": "send_reply", "arguments": {"message": "x" * 16_000}}],
    "arguments too deep": [{"name": "send_reply", "arguments": nine_deep()}],
    "an entry that is no object": ["send_reply"],
}


@pytest.mark.parametrize("what", list(MALFORMED))
def test_a_malformed_actions_key_is_an_invalid_response(
    what: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole response is refused, as for any malformed answer: the slot's questions fail,
    so nothing is recommended, nothing is released and nothing automates."""
    live(monkeypatch, lambda _: answers(actions=MALFORMED[what]))
    result = Decision.from_path(MINI)(message("Could you refill my lisinopril?"))
    assert result.outcome is Outcome.ABSTAIN and result.recommendation is None
    assert result.errors == {"request": "invalid_response", "abusive": "invalid_response"}
    assert result.actions == () and result.answers == {}


def test_a_well_formed_live_response_is_gated_as_a_canned_one_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposed = [
        {"name": "file_note", "arguments": {"note": "Asked for a refill."}},
        {"name": "look_up_medications"},
    ]
    seen = live(monkeypatch, lambda _: answers(actions=proposed))
    result = Decision.from_path(MINI)(message("Could you refill my lisinopril?"))
    assert result.outcome is Outcome.AUTO
    assert shown(result) == [
        ("file_note", "write", "released", "automated"),
        ("look_up_medications", "read", "released", "with_decision"),
    ]
    # The request carried the declared actions, in the manifest's order, each as declared.
    (request,) = seen
    assert [a["name"] for a in request["actions"]] == list(
        Decision.from_path(MINI).pack.manifest.actions
    )
    assert request["actions"][0] == {
        "name": "look_up_medications",
        "description": "Read the patient's active medication list.",
        "effect": "read",
    }
    assert "allow_auto" not in json.dumps(request)


def test_a_mock_file_whose_response_proposes_what_the_contract_does_not_allow_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def malformed(mock: dict[str, Any]) -> None:
        mock["default"]["actions"] = [{"name": "Send Reply", "arguments": {}}]

    folder = copy_of(tmp_path, mock=malformed)
    with pytest.raises(PackValidationError, match="default: an action's name must match"):
        Decision.from_path(folder)
    assert main(["validate", str(folder)]) != 0
    assert "an action's name must match" in capsys.readouterr().err


# -- an agent the deployment does not vouch for ---------------------------------------------------


def unblocked(raw: dict[str, Any]) -> None:
    raw["policy"]["rules"] = []  # no rule BLOCKs: what is held is held by the gate alone
    raw["questions"].pop("abusive")
    raw["providers"]["reader"]["answers"] = ["request"]


def one_question(mock: dict[str, Any]) -> None:
    for response in (*(case["response"] for case in mock["cases"]), mock["default"]):
        response["answers"].pop("abusive")


def test_a_registry_agent_is_vouched_for_once_its_organization_approves_its_digest(
    tmp_path: Path,
) -> None:
    folder = copy_of(tmp_path, manifest=unblocked, mock=one_question)
    pulled = load_pack(folder, origin="hub")
    unapproved = Decision(pulled)
    approved = Decision(pulled, origin_approval=lambda pack: True)
    assert not unapproved.vouched() and approved.vouched()
    assert unapproved.automation_stopped() is not None and approved.automation_stopped() is None
    body = message("Could you refill my lisinopril?")
    held = unapproved(body)
    assert held.outcome is Outcome.HUMAN_REVIEW
    assert {disposition for _, _, disposition, _ in shown(held)} == {"held"}
    released = approved(body)
    assert released.outcome is Outcome.AUTO
    assert {disposition for _, _, disposition, _ in shown(released)} == {"released"}
    # Recalled by its registry: held again, whatever the approval says.
    recalled = Decision(
        pulled, origin_approval=lambda pack: True, recall_source=lambda pack: "recalled"
    )
    assert not recalled.vouched()
    assert {disposition for _, _, disposition, _ in shown(recalled(body))} == {"held"}


# -- the risk level -------------------------------------------------------------------------------


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_write_and_contact_are_released_only_where_the_risk_level_allows_auto(
    tmp_path: Path, level: int
) -> None:
    def risky(raw: dict[str, Any]) -> None:
        raw["risk"]["level"] = level

    pack = load_pack(copy_of(tmp_path, manifest=risky))
    approval = GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-11")
    body = message("Could you refill my lisinopril?")
    plain, governed = Decision(pack)(body), Decision(pack, governance=approval)(body)
    automated = [
        ("draft_reply", "draft", "released", "with_decision"),
        ("send_reply", "contact", "released", "automated"),
    ]
    held = [
        ("draft_reply", "draft", "released", "with_decision"),
        ("send_reply", "contact", "held", "awaiting_review"),
    ]
    assert shown(plain) == (automated if level <= 1 else held)
    # A recorded approval that lets a level 2 or 3 agent automate releases its actions too;
    # nothing lets a level 4 agent.
    assert shown(governed) == (automated if level <= 3 else held)
    assert (governed.outcome is Outcome.AUTO) is (level <= 3)
    assert Decision(pack).automation_stopped() == (
        None if level <= 1 else f"risk level {level} requires human review"
    )
    assert (Decision(pack, governance=approval).automation_stopped() is None) is (level <= 3)
    # An order waits for a person at every level, approval or not.
    for decision in (Decision(pack), Decision(pack, governance=approval)):
        order = decision(message("Please renew my prescription."))
        assert order.outcome is Outcome.HUMAN_REVIEW
        assert shown(order)[1] == ("renew_prescription", "order", "held", "awaiting_review")


def test_a_hold_stops_releases_as_it_stops_automation(agent: Decision) -> None:
    held = Decision(agent.pack, hold_source=lambda pack: "an operator's hold")
    result = held(message("Could you refill my lisinopril?"))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert shown(result) == [
        ("draft_reply", "draft", "released", "with_decision"),
        ("send_reply", "contact", "held", "awaiting_review"),
    ]
    assert held.automation_stopped() == "automation is on hold: an operator's hold"
    shadow = agent.decide(message("Could you refill my lisinopril?"), shadow=True)
    assert shadow.outcome is Outcome.HUMAN_REVIEW
    assert shown(shadow)[1] == ("send_reply", "contact", "held", "awaiting_review")


# -- an agent that declares no action ----------------------------------------------------------


def test_a_response_proposes_actions_only_where_they_are_read() -> None:
    """`validate_response` reads a response's `actions` only for a variant that may propose
    them (an `agent`'s): without, the key is not read, whatever it holds."""
    questions = Decision.from_path(MINI).pack.manifest.questions
    malformed = answers(actions="anything at all, even this")
    assert "actions" not in validate_response(malformed, questions)
    assert validate_response(answers(), questions, actions=True)["actions"] == []


def test_a_0_4_agent_that_declares_nothing_is_as_it_was_until_its_agent_proposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def undeclaring(raw: dict[str, Any]) -> None:
        raw.pop("actions")

    def silent(mock: dict[str, Any]) -> None:
        for response in (*(case["response"] for case in mock["cases"]), mock["default"]):
            response.pop("actions", None)

    folder = copy_of(tmp_path, manifest=undeclaring, mock=silent)
    quiet = Decision.from_path(folder)(message("Could you refill my lisinopril?"))
    assert quiet.outcome is Outcome.AUTO and quiet.actions is None
    assert "actions" not in quiet.to_dict()
    # Whatever it proposes is undeclared: blocked, and the decision goes to a person.
    seen = live(monkeypatch, lambda _: answers(actions=[{"name": "send_reply"}]))
    proposing = Decision.from_path(folder)(message("Could you refill my lisinopril?"))
    assert "actions" not in seen[0]  # nothing declared: the request is what it was
    assert proposing.outcome is Outcome.HUMAN_REVIEW
    assert shown(proposing) == [("send_reply", None, "blocked", "undeclared")]


# -- the bundled agent ----------------------------------------------------------------------------


def test_the_bundled_agent_proposes_what_the_demo_shows() -> None:
    demo = Decision(load_pack(DEMO_AGENT_DIR))
    refill = demo(message("Can you send a refill of my metformin to the pharmacy?"))
    assert refill.outcome is Outcome.AUTO
    assert shown(refill) == [("assign_queue", "write", "released", "automated")]
    assert (refill.actions or ())[0].arguments == {"queue": "refill"}
    question = demo(message("Did my lab results come back yet?"))
    assert (question.outcome, question.recommendation) == (Outcome.HUMAN_REVIEW, "other")
    assert shown(question) == [
        ("assign_queue", "write", "held", "awaiting_review"),
        ("send_acknowledgement", "contact", "held", "awaiting_review"),
    ]
    assert question.reasons == (
        "confidence 0.82 is below the auto threshold 0.90 for 'other'",
        "action 'send_acknowledgement' is never released without a person (allow_auto: false); "
        "a person decides",
    ) or question.reasons[-1].startswith("action 'send_acknowledgement' is never released")
    urgent = demo(message("Please refill my inhaler. Also I have had chest pain since last night."))
    assert (urgent.outcome, urgent.priority.value) == (Outcome.HUMAN_REVIEW, "urgent")
    assert shown(urgent) == [("assign_queue", "write", "held", "awaiting_review")]
    # Its rules variant proposes nothing: only an agent proposes.
    rules = Decision(load_pack(DEMO_AGENT_DIR), providers={"reader": "rules"})
    assert rules(message("Can you send a refill of my metformin to the pharmacy?")).actions == ()


def test_decide_on_the_command_line_shows_the_dispositions_with_no_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["decide", str(MINI), "--text", "Please renew my prescription."]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"] == "HUMAN_REVIEW"
    assert [(a["name"], a["disposition"]) for a in printed["actions"]] == [
        ("look_up_medications", "released"),
        ("renew_prescription", "held"),
    ]


def test_a_store_keys_the_arguments_hash_and_the_record_holds_no_actions() -> None:
    with AuditStore(":memory:") as store:
        decision = Decision.from_path(MINI, store=store)
        result = decision(message("Could you refill my lisinopril?"))
        first = (result.actions or ())[0]
        assert first.arguments_hash == store.phi.arguments_hash(first.arguments)
        kept = store.get(result.id)
        assert kept is not None and "actions" not in kept["record"]
        assert [a["name"] for a in kept["actions"]] == ["draft_reply", "send_reply"]
