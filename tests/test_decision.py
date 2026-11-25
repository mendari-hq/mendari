# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from mendari import Agent, AuditStore, Decision, Outcome
from mendari.errors import AgentValidationError
from tests.conftest import message


def test_from_hub(triage: Decision) -> None:
    decision = Decision.from_hub("mendari-test/triage")
    assert decision.agent.digest == triage.agent.digest
    assert decision.provider_info["router"].variant == "rules"


def test_result_matches_generated_output_schema(triage: Decision) -> None:
    result = triage(message("Can I get a refill on my atorvastatin?", channel="portal"))
    Draft202012Validator(triage.agent.output_schema).validate(result.to_dict())
    assert result.agent_digest == triage.agent.digest
    assert result.input_hash.startswith("sha256:")  # no store, so no input hash key


def test_invalid_input_is_blocked_without_calling_providers(triage: Decision) -> None:
    result = triage({"message": {"body": "hi"}, "unexpected": True})
    assert result.outcome is Outcome.BLOCK
    assert result.answers == {}
    assert result.fallback is not None
    assert any("unexpected" in reason for reason in result.reasons)


def test_unknown_provider_slot_is_rejected(triage_agent: Agent) -> None:
    with pytest.raises(AgentValidationError, match="unknown provider slots"):
        Decision(triage_agent, providers={"nope": "rules"})


def test_swapping_providers_keeps_the_contract(triage_agent: Agent) -> None:
    """The router's keyword rules by default; its agent variant (canned answers) in their place
    answers the same question in the same shape."""
    decision = Decision(triage_agent, providers={"router": "agent"})
    result = decision(message("I got a bill for $120 and my insurance should have covered it"))
    assert result.answers["destination"].provider.type == "agent"
    assert result.recommendation == "billing"


def test_provider_failure_falls_back_safely(
    triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router's LLM with a model and no credential (no gateway key, and the suite's Vercel
    OIDC token refused): the decision abstains and says what a person does instead."""
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    decision = Decision(triage_agent, providers={"router": "llm"})
    result = decision(message("I need a refill"))
    assert result.outcome is Outcome.ABSTAIN
    assert result.errors == {"destination": "credential_unavailable"}
    assert result.fallback and "front desk" in result.fallback


def test_decisions_are_recorded_when_a_store_is_attached(triage_agent: Agent) -> None:
    with AuditStore(":memory:") as store:
        decision = Decision(triage_agent, store=store)
        result = decision(message("My A1c came back at 7.8, what does that mean?"))
        stored = store.get(result.id)
    assert stored is not None
    assert stored["record"]["input"]["message"]["body"].startswith("My A1c")
    assert stored["events"][0]["event"] == "decision.created"


SECRET = "Synthetic secret: chest pain since noon"  # must never leave the input


@pytest.mark.parametrize(
    "bad_input",
    [
        {"message": {"body": SECRET * 300}},  # longer than maxLength
        {"message": {"body": SECRET, "channel": SECRET}},  # not in the enum
        {"message": {"body": SECRET, "subject": SECRET * 20}},  # longer than maxLength
        {"message": SECRET},  # an object the schema wants, given as text
        {"message": {"body": SECRET, SECRET: SECRET}},  # a key the schema does not allow
        {"message": {"body": 7, "subject": [SECRET]}},  # wrong types
    ],
)
def test_rejection_reasons_never_quote_the_input(
    triage: Decision, bad_input: dict[str, object]
) -> None:
    """jsonschema's messages quote the value; reasons are stored in the audit trail and copied
    into the FHIR Task, so they are built from the schema alone."""
    result = triage(bad_input)
    assert result.outcome is Outcome.BLOCK and result.reasons
    assert SECRET not in json.dumps(result.to_dict())


def test_rejection_reasons_still_say_where_and_why(triage: Decision) -> None:
    result = triage({"message": {"body": "", "channel": "fax"}, "unexpected": True})
    assert result.reasons == (
        "input rejected: (root): has fields the schema does not allow: unexpected",
        "input rejected: message/body: must have at least 1 characters",
        "input rejected: message/body: does not match the pattern the schema requires",
        "input rejected: message/channel: is not one of the values the schema allows",
    )
