# SPDX-License-Identifier: Apache-2.0
"""The Patient Inbox Router's automation gate (policy `automation_requires`): with the keyword
rules alone in the urgency screen, nothing is automated; with a model beside them, routine
messages are. On hand-written held-out messages the rules alone caught 4 of 20 emergencies and
automated 7 of the ones they missed."""

from __future__ import annotations

from mendari import Decision, Outcome, Pack, Priority
from tests.conftest import message

REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"
EMERGENCY = "I've had crushing chest pain for 30 minutes and it's going down my left arm."
GATE = "automation requires safety_screen = rules_and_jev or rules_and_llm (running: rules)"


def test_without_a_model_screen_nothing_is_automated(router: Decision) -> None:
    result = router.decide(message(REFILL))
    assert result.outcome is Outcome.HUMAN_REVIEW
    assert result.recommendation == "medical_assistant" and result.priority is Priority.ROUTINE
    assert GATE in result.reasons and result.errors == {}


def test_a_model_screen_allows_automation(inbox_pack: Pack, screened: dict[str, str]) -> None:
    result = Decision(inbox_pack, providers=screened).decide(message(REFILL))
    assert result.outcome is Outcome.AUTO and result.recommendation == "medical_assistant"
    assert result.errors == {} and GATE not in result.reasons


def test_the_gate_never_hides_an_emergency(router: Decision) -> None:
    result = router.decide(message(EMERGENCY))
    assert (result.outcome, result.recommendation) == (Outcome.HUMAN_REVIEW, "nurse")
    assert result.priority is Priority.STAT and result.rule == "possible-emergency"
    assert result.reasons[0].startswith("rule possible-emergency")
