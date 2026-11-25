# SPDX-License-Identifier: Apache-2.0
"""What the triage fixture (`mendari-test/triage`, tests/fixtures/reference-agents/triage)
decides, offline: the test fixture most engine tests decide with, so what they rely on is held
here. Every message is synthetic.

- Its default providers are the keyword rules, in both slots, and its policy automates only
  while its screen runs an agent (`automation_requires`): by default nothing is automated.
- With the screen's canned agent, a confident team is automated, but billing (`allow_auto:
  false`) and a front desk below its own threshold (0.95).
- Its two rules: a possible emergency goes to a nurse at priority stat (on a probability of
  0.35), and an abusive message is blocked, unless it is also a possible emergency.
- Its screen's second signals: a check of the agent by the rules caps a disagreement; canned
  answers repeated are asked once.
- Its critical label (`urgency` = `yes`): never automated by default, though its rules miss
  same-day symptoms worded as a patient might, which the screen's agent would automate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari import Decision, Outcome, Pack, load_pack
from mendari.core.spec import Priority
from mendari.evals.harness import resolve_dataset
from mendari.providers.check import CANNED_NOTE as CHECK_CANNED
from mendari.providers.llm import LlmProvider
from mendari.providers.repeat import CANNED_NOTE as REPEAT_CANNED
from mendari.sandbox.fhir_records import message_entries
from mendari.standards import cds_hooks
from tests.conftest import CALM_ANSWERS, TRIAGE_DIR, calm_agent, message

REFILL = "Please renew my prescription for metformin."
APPOINTMENT = "Can I move my appointment to Friday?"
RECORDS = "Could you send me a copy of my vaccination records?"
BILL = "Why was I charged twice this month?"
RASH = "I have had a mild rash on my arm for a few days."
UNPLACED = "I am not sure who to ask about my visit last week."
CHEST_PAIN = "I have had chest pain since last night."
SHORT_OF_BREATH = "I am short of breath even when I sit down."  # the screen's agent misses it
DIZZY = "I felt a little dizzy after my new lisinopril."  # the agent: urgent at 0.75
FEEL_OFF = "I just feel off today and wanted to ask about it."  # the agent: not urgent at 0.60
# A same-day symptom worded as a patient might, beside a refill: every screen misses it.
MISSED = "One side of my mouth will not move when I smile. Could you refill my lisinopril?"
ABUSIVE = "You people are useless. Can you send a refill of my metformin to the pharmacy?"
ABUSIVE_URGENT = "Answer me this time, you morons. I have had chest pain since last night."
NO_AUTOMATION = "automation requires screen = agent or checked (running: rules)"
AGENTS = {"router": "agent", "screen": "agent"}


def decided(decision: Decision, body: str, **extra: Any) -> tuple[Outcome, str | None, Priority]:
    result = decision(message(body, **extra))
    return result.outcome, result.recommendation, result.priority


@pytest.fixture(scope="module")
def screened(triage_agent: Pack) -> Decision:
    """The keyword router, with the screen's canned agent: the configuration that automates."""
    return Decision(triage_agent, providers={"screen": "agent"})


def test_it_is_the_fixture_the_tests_name(triage_agent: Pack, triage: Decision) -> None:
    manifest = triage_agent.manifest
    assert triage_agent.root == TRIAGE_DIR and triage.pack is triage_agent
    assert (triage_agent.id, manifest.spec_version, int(manifest.risk.level)) == (
        "mendari-test/triage",
        "0.4",
        1,
    )
    assert {slot: spec.use for slot, spec in manifest.providers.items()} == {
        "router": "rules",
        "screen": "rules",
    }
    assert sorted(manifest.providers["screen"].variants) == [
        "agent", "checked", "llm", "repeated", "rules",
    ]  # fmt: skip
    destination = manifest.questions["destination"]
    assert destination.display == "Team" and destination.name_of("front_desk") == "Front desk"
    assert destination.name_of("pharmacy") == "Pharmacy"  # no name given: the key as words


# -- the default providers: nothing is automated ---------------------------------------------------


def test_by_default_a_confident_team_goes_to_a_person(triage: Decision) -> None:
    result = triage(message(REFILL, channel="portal"))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "pharmacy")
    assert result.answers["destination"].confidence > 0.99
    assert result.reasons == ("confidence 1.00 >= auto threshold 0.90", NO_AUTOMATION)
    assert {answer.provider.variant for answer in result.answers.values()} == {"rules"}


def test_by_default_each_team_and_no_suggestion(triage: Decision) -> None:
    assert decided(triage, APPOINTMENT) == (Outcome.HUMAN_REVIEW, "front_desk", Priority.ROUTINE)
    assert decided(triage, BILL) == (Outcome.HUMAN_REVIEW, "billing", Priority.ROUTINE)
    assert decided(triage, RASH) == (Outcome.HUMAN_REVIEW, "nurse", Priority.ROUTINE)
    unplaced = triage(message(UNPLACED))
    assert (unplaced.outcome, unplaced.recommendation) == (Outcome.ABSTAIN, None)
    assert unplaced.reasons == ("confidence 0.48 < review threshold 0.50",)
    assert unplaced.fallback == "The front desk reads the message."


# -- the screen's agent: what is automated, and what is not ---------------------------------------


def test_with_the_screens_agent_a_confident_team_is_automated(screened: Decision) -> None:
    assert decided(screened, REFILL) == (Outcome.AUTO, "pharmacy", Priority.ROUTINE)
    assert decided(screened, RASH) == (Outcome.AUTO, "nurse", Priority.ROUTINE)
    appointment = screened(message(APPOINTMENT))
    assert (appointment.outcome, appointment.recommendation) == (Outcome.AUTO, "front_desk")
    assert appointment.reasons == ("confidence 0.98 >= auto threshold 0.95",)


def test_billing_is_never_automated(screened: Decision) -> None:
    bill = screened(message(BILL))
    assert (bill.outcome, bill.recommendation) == (Outcome.HUMAN_REVIEW, "billing")
    assert bill.answers["destination"].confidence > 0.95
    assert bill.reasons[-1] == "policy never automates 'billing'"


def test_the_front_desk_needs_more_confidence_than_the_others(screened: Decision) -> None:
    """A records request is 0.92: above the policy's 0.90, below the front desk's own 0.95."""
    records = screened(message(RECORDS))
    assert (records.outcome, records.recommendation) == (Outcome.HUMAN_REVIEW, "front_desk")
    assert 0.90 < records.answers["destination"].confidence < 0.95
    assert records.reasons == ("confidence 0.92 is below the auto threshold 0.95 for 'front_desk'",)


def test_a_canned_answer_at_each_confidence(triage_agent: Pack) -> None:
    """The canned router: 0.95 (automated), 0.75 (a person), 0.40 (no suggestion), and its
    default, 0.55 (a person), each as the service version 2.0.0 the canned file reports."""
    canned = Decision(triage_agent, providers=AGENTS)
    refill = canned(message(REFILL))
    assert (refill.outcome, refill.answers["destination"].confidence) == (Outcome.AUTO, 0.95)
    assert refill.answers["destination"].provider.model == "mock-triage-router"
    assert decided(canned, RECORDS)[:2] == (Outcome.HUMAN_REVIEW, "front_desk")
    assert canned(message(RECORDS)).answers["destination"].confidence == 0.75
    assert decided(canned, UNPLACED)[:2] == (Outcome.ABSTAIN, None)
    assert canned(message(UNPLACED)).answers["destination"].confidence == 0.4
    unknown = canned(message("Hello again, it is me."))
    assert (unknown.outcome, unknown.recommendation) == (Outcome.HUMAN_REVIEW, "front_desk")
    assert unknown.answers["destination"].confidence == 0.55


# -- the policy's rules ---------------------------------------------------------------------------


def test_a_possible_emergency_goes_to_a_nurse_now(triage: Decision, screened: Decision) -> None:
    for decision in (triage, screened):
        result = decision(message(f"{REFILL} {CHEST_PAIN}"))
        assert (result.outcome, result.recommendation, result.priority) == (
            Outcome.HUMAN_REVIEW,
            "nurse",
            Priority.STAT,
        )
        assert result.rule == "possible-emergency"
        assert result.reasons[0].startswith("rule possible-emergency: ")


def test_the_emergency_rule_acts_on_a_probability(screened: Decision) -> None:
    """`min_probability: 0.35`: urgent at 0.75 matches, and so does "not urgent" at 0.60, whose
    probability of urgent is 0.40."""
    dizzy = screened(message(DIZZY))
    assert (dizzy.answers["urgency"].label, dizzy.rule) == ("yes", "possible-emergency")
    off = screened(message(FEEL_OFF))
    assert off.answers["urgency"].label == "no"
    assert off.answers["urgency"].probabilities["yes"] == pytest.approx(0.40)
    assert (off.outcome, off.recommendation, off.priority, off.rule) == (
        Outcome.HUMAN_REVIEW,
        "nurse",
        Priority.STAT,
        "possible-emergency",
    )


def test_an_abusive_message_is_blocked_unless_it_may_be_an_emergency(triage: Decision) -> None:
    blocked = triage(message(ABUSIVE))
    assert (blocked.outcome, blocked.rule) == (Outcome.BLOCK, "abusive-message")
    assert blocked.answers["abusive"].confidence >= 0.9
    assert blocked.fallback == "The front desk reads the message." and not blocked.rejected
    first = triage(message(ABUSIVE_URGENT))
    assert first.answers["abusive"].label == "yes"
    assert (first.outcome, first.recommendation, first.priority, first.rule) == (
        Outcome.HUMAN_REVIEW,
        "nurse",
        Priority.STAT,
        "possible-emergency",
    )


def test_a_symptom_said_not_to_be_there_is_no_emergency(triage: Decision) -> None:
    for denied in ("I do not have chest pain.", "I have no chest pain or trouble breathing."):
        result = triage(message(f"{denied} {APPOINTMENT}"))
        assert result.answers["urgency"].label == "no", denied
        assert (result.recommendation, result.priority) == ("front_desk", Priority.ROUTINE)


@pytest.mark.parametrize(
    "given",
    [
        {"message": {"body": "   "}},
        {"message": {"body": ""}},
        {"message": {"body": REFILL, "channel": "fax"}},
        {"message": {"body": REFILL, "sender": "someone"}},
        {"note": {"text": REFILL}},
    ],
    ids=["blank", "empty", "unknown-channel", "unknown-key", "another-shape"],
)
def test_an_input_the_schema_refuses_is_blocked(triage: Decision, given: dict[str, Any]) -> None:
    refused = triage(given)
    assert (refused.outcome, refused.recommendation, refused.rejected) == (
        Outcome.BLOCK,
        None,
        True,
    )
    assert refused.answers == {}


# -- second signals and calls ---------------------------------------------------------------------


def test_a_check_by_the_rules_caps_what_the_agent_missed(triage_agent: Pack) -> None:
    checked = Decision(triage_agent, providers={"screen": "checked"})
    agreed = checked(message(REFILL))
    assert (agreed.outcome, agreed.recommendation) == (Outcome.AUTO, "pharmacy")
    assert CHECK_CANNED in agreed.answers["urgency"].evidence
    missed = checked(message(SHORT_OF_BREATH))
    assert missed.answers["urgency"].label == "no"  # the agent's answer: it missed it
    assert (missed.outcome, missed.recommendation) == (Outcome.HUMAN_REVIEW, "nurse")
    assert missed.reasons[-1] == (
        "agent and second disagree on urgency (agent: no, second: yes); a person decides"
    )
    alone = Decision(triage_agent, providers={"screen": "agent"})
    assert decided(alone, SHORT_OF_BREATH)[:2] == (Outcome.AUTO, "nurse")  # without the check


def test_canned_answers_repeated_are_asked_once(triage_agent: Pack) -> None:
    repeated = Decision(triage_agent, providers={"screen": "repeated"})
    result = repeated(message(REFILL))
    assert result.answers["urgency"].evidence[0] == REPEAT_CANNED
    # Not a variant the policy automates with: a person decides.
    assert (result.outcome, result.reasons[-1]) == (
        Outcome.HUMAN_REVIEW,
        "automation requires screen = agent or checked (running: repeated)",
    )


def test_a_model_with_no_model_set_answers_nothing(
    triage_agent: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    result = Decision(triage_agent, providers={"router": "llm"})(message(REFILL))
    assert (result.outcome, result.recommendation) == (Outcome.ABSTAIN, None)
    assert result.errors == {"destination": "not_configured"}


def test_the_two_models_share_one_call(triage_agent: Pack, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both slots have an `llm` variant: on the same model they are asked in one call."""
    monkeypatch.setenv("LLM_MODEL", "example/model-1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        schema = body["response_format"]["json_schema"]["schema"]["properties"]
        top = {"destination": "pharmacy", "urgency": "no", "abusive": "no"}
        content = {
            question: {
                label: 0.97 if label == top[question] else 0.03 / (len(spec["properties"]) - 1)
                for label in spec["properties"]
            }
            for question, spec in schema.items()
        }
        answer = {"role": "assistant", "content": json.dumps(content)}
        return httpx.Response(
            200,
            json={
                "model": "example/model-1",
                "choices": [{"index": 0, "finish_reason": "stop", "message": answer}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    original = LlmProvider.__init__

    def init(self: LlmProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(LlmProvider, "__init__", init)
    result = Decision(triage_agent, providers={"router": "llm", "screen": "llm"})(message(REFILL))
    assert len(calls) == 1
    asked = calls[0]["response_format"]["json_schema"]["schema"]["properties"]
    assert set(asked) == {"destination", "urgency", "abusive"}
    assert (result.recommendation, result.answers["urgency"].label) == ("pharmacy", "no")
    assert result.reasons[-1] == "automation requires screen = agent or checked (running: llm)"


# -- canned answers, and a live service in their place ---------------------------------------------


def test_with_sign_in_canned_answers_never_automate_and_live_ones_do(
    triage_agent: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server with sign-in never automates on canned answers (`canned_cap`); the tests that
    need AUTO there answer every `agent` variant from a live service (`calm_agent`)."""
    canned = Decision(triage_agent, providers=AGENTS, canned_cap=True)(message(REFILL))
    assert canned.outcome is Outcome.HUMAN_REVIEW
    assert canned.reasons[-1].startswith("canned answers: MENDARI_AGENT_URL is not set")
    providers = calm_agent(monkeypatch)
    assert providers == AGENTS
    live = Decision(triage_agent, providers=providers, canned_cap=True)(message(UNPLACED))
    assert (live.outcome, live.recommendation) == (Outcome.AUTO, "pharmacy")
    assert {key: answer.label for key, answer in live.answers.items()} == {
        key: answer["value"] for key, answer in CALM_ANSWERS.items()
    }
    assert live.answers["destination"].provider.model == "calm-agent"


# -- the critical label ---------------------------------------------------------------------------


def critical_cases(pack: Pack) -> list[dict[str, Any]]:
    """Every true critical case (`urgency` = `yes`) of every dataset of the fixture."""
    return [
        record
        for name in pack.manifest.evaluation.datasets
        for record in resolve_dataset(pack, name).records
        if record["labels"]["urgency"] == "yes"
    ]


def test_no_critical_case_is_automated_by_default(triage_agent: Pack, triage: Decision) -> None:
    assert [(c.question, c.label) for c in triage_agent.manifest.evaluation.critical] == [
        ("urgency", "yes")
    ]
    cases = critical_cases(triage_agent)
    assert len(cases) == 154  # 66 in `test`, 44 in `calibration`, 40 in `critical`, 4 examples
    assert not [r["id"] for r in cases if triage(r["input"]).outcome is Outcome.AUTO]


def test_the_rules_miss_emergencies_worded_as_a_patient_might(
    triage_agent: Pack, triage: Decision, screened: Decision
) -> None:
    """On purpose, as the card says: the screen's rules find every same-day symptom a keyword
    list words, and none worded as a patient might. With the screen's agent, some of those are
    automated: what its evaluation is there to show."""
    cases = critical_cases(triage_agent)
    kinds = {r["id"]: r["attributes"]["kind"] for r in cases}
    found = {r["id"] for r in cases if triage(r["input"]).answers["urgency"].label == "yes"}
    assert {kinds[case] for case in found} == {"urgent_plain", "abusive_urgent"}
    assert {kinds[case] for case in set(kinds) - found} == {"urgent_paraphrased"}
    automated = [r["id"] for r in cases if screened(r["input"]).outcome is Outcome.AUTO]
    assert automated and {kinds[case] for case in automated} <= {
        "urgent_plain",  # one the agent misses, but routed to a nurse
        "urgent_paraphrased",
    }
    assert screened(message(MISSED)).outcome is Outcome.AUTO


def test_without_its_gate_the_rules_would_automate_critical_cases(
    triage_agent: Pack, agent_copy: Callable[..., Path]
) -> None:
    """The `critical` set's purpose: with `automation_requires` gone, the rules alone automate
    some of its cases (beside a confident request), which the safety invariant's check finds."""
    ungated = Decision(load_pack(agent_copy(policy=lambda p: p.pop("automation_requires"))))
    records = resolve_dataset(triage_agent, "critical").records
    automated = [r for r in records if ungated(r["input"]).outcome is Outcome.AUTO]
    assert automated
    assert {r["attributes"]["kind"] for r in automated} == {"urgent_paraphrased"}


# -- FHIR and CDS Hooks ---------------------------------------------------------------------------


def test_a_communication_decides_as_its_message_does(triage: Decision) -> None:
    given = {"message": {"subject": "Refill", "body": REFILL, "channel": "sms"}}
    record = {"id": "fhir-1", "input": given}
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": message_entries(record)}
    result, task = triage.decide_fhir(bundle)
    direct = triage(given)
    assert (result.outcome, result.recommendation, result.input_hash) == (
        direct.outcome,
        direct.recommendation,
        direct.input_hash,
    )
    coding = task["performerType"][0]["coding"][0]
    assert coding["system"] == "https://mendari.co/fhir/CodeSystem/fixture-triage-team"
    assert coding["code"] == "pharmacy"


def test_cards_show_the_teams_a_possible_emergency_and_a_refused_input(
    triage_agent: Pack, triage: Decision
) -> None:
    hooks = triage_agent.manifest.cds_hooks
    assert hooks is not None and hooks.card is not None
    template = hooks.card
    refill = triage(message(REFILL))
    assert cds_hooks.card_key(template, refill) == "pharmacy"
    assert cds_hooks.shows_card(template, refill)
    nurse = triage(message(RASH))  # a nurse's message shows no card
    assert not cds_hooks.shows_card(template, nurse)
    emergency = triage(message(CHEST_PAIN))  # but a possible emergency does, in the rule's words
    rule_card = cds_hooks.rule_card(template, emergency)
    assert rule_card is not None and rule_card.indicator == "critical"
    refused = triage({"message": {"body": " "}})
    assert cds_hooks.card_key(template, refused) == "refused"
    assert cds_hooks.shows_card(template, refused)
    unplaced = triage(message(UNPLACED))
    assert cds_hooks.card_key(template, unplaced) == "none"
    assert not cds_hooks.shows_card(template, unplaced)


# -- copies ----------------------------------------------------------------------------------------


def test_a_copy_has_tiny_datasets_and_its_edits(agent_copy: Callable[..., Path]) -> None:
    def risk(manifest: dict[str, Any]) -> None:
        manifest["risk"]["level"] = 2

    def lower(policy: dict[str, Any]) -> None:
        policy["thresholds"]["auto"] = 0.8

    first = agent_copy(manifest=risk, policy=lower)
    second = agent_copy()
    assert first != second and first.parent == second.parent
    copied = load_pack(first)
    assert int(copied.manifest.risk.level) == 2
    assert copied.manifest.policy.thresholds.auto == 0.8
    for name, whole in (("examples", 12), ("test", 300), ("calibration", 200), ("critical", 40)):
        shipped = (TRIAGE_DIR / "data" / f"{name}.jsonl").read_text(encoding="utf-8")
        assert len(shipped.splitlines()) == whole
        assert len(resolve_dataset(copied, name).records) == min(20, whole)
    assert (second / "agent.yaml").read_bytes() == (TRIAGE_DIR / "agent.yaml").read_bytes()
