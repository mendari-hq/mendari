# SPDX-License-Identifier: Apache-2.0
"""The Patient Inbox Router's automation gate (policy `automation_requires`): with the keyword
rules alone in the urgency screen, nothing is automated; with a model beside them, routine
messages are. On hand-written held-out messages the rules alone caught 4 of 20 emergencies and
automated 7 of the ones they missed."""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from mendari import Decision, Outcome, Pack, Priority
from mendari.standards import cds_hooks
from tests.conftest import message
from tests.test_fhir import COMMUNICATION, PATIENT

REFILL = "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"
EMERGENCY = "I've had crushing chest pain for 30 minutes and it's going down my left arm."
GATE = "automation requires safety_screen = rules_and_jev or rules_and_llm (running: rules)"
# Each team's display name and card indicator: the administrative teams' cards are `info`, the
# clinical teams' `warning` (a possible emergency is `critical`, whatever the team).
TEAMS = {
    "front_desk": ("Front desk", "info"),
    "medical_assistant": ("Medical assistant", "info"),
    "billing": ("Billing", "info"),
    "nurse": ("Nurse", "warning"),
    "physician": ("Physician", "warning"),
}
NO_TEAM = "Message: no suggested team; it stays in the shared inbox for manual triage"


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


def _hook(body: str) -> dict[str, Any]:
    """The inbox's own hook for one message, its Communication and Patient prefetched."""
    communication = copy.deepcopy(COMMUNICATION)
    communication["payload"] = [{"contentString": body}]
    return {
        "hook": "mendari-message-received",
        "prefetch": {"communication": communication, "patient": PATIENT},
    }


def test_the_inbox_card_has_its_own_template(
    router: Decision, inbox_pack: Pack, screened: dict[str, str]
) -> None:
    """The inbox's card template (LP-23j, plan conflict 45): an indicator for each team, a
    summary for a message with no suggested team, and its own suggestion label. A team's summary
    keeps the engine's words with the team's display name, so a card still says whether the
    message was routed or needs a person ("Routed to", "Review: route to"); the Task keeps the
    team's code. Every decision shows a card (no `show`), and the hook stays Mendari's own."""
    hooks = inbox_pack.manifest.cds_hooks
    assert hooks is not None and hooks.card is not None
    assert hooks.hook == "mendari-message-received"
    template = hooks.card
    assert template.show is None and set(template.summaries) == {"none"}
    assert set(TEAMS) == set(inbox_pack.manifest.questions["destination"].criteria)
    assert template.indicators == {team: shown[1] for team, shown in TEAMS.items()} | {
        "none": "warning"
    }

    [stat] = cds_hooks.invoke(router, _hook(EMERGENCY))["cards"]
    assert (stat["summary"], stat["indicator"]) == ("Review: route to Nurse (stat)", "critical")
    assert stat["suggestions"][0]["label"] == "Create a routing task"
    task = stat["suggestions"][0]["actions"][0]["resource"]
    assert task["performerType"][0]["coding"][0]["code"] == "nurse"
    [review] = cds_hooks.invoke(router, _hook(REFILL))["cards"]
    assert (review["summary"], review["indicator"]) == (
        "Review: route to Medical assistant",
        "info",
    )
    [routed] = cds_hooks.invoke(Decision(inbox_pack, providers=screened), _hook(REFILL))["cards"]
    assert (routed["summary"], routed["indicator"]) == ("Routed to Medical assistant", "info")
    assert routed["suggestions"][0]["label"] == "Create a routing task"

    # Every team's card, routed and for review, and a message with no suggested team.
    result, made = router.decide_fhir(_hook(REFILL)["prefetch"])
    for team, (name, indicator) in TEAMS.items():
        for outcome, words in (
            (Outcome.AUTO, "Routed to"),
            (Outcome.HUMAN_REVIEW, "Review: route to"),
        ):
            each = dataclasses.replace(result, outcome=outcome, recommendation=team)
            card = cds_hooks.card(inbox_pack, each, made)
            assert (card["summary"], card["indicator"]) == (f"{words} {name}", indicator), team
    none = dataclasses.replace(result, outcome=Outcome.ABSTAIN, recommendation=None)
    card = cds_hooks.card(inbox_pack, none, made)
    assert (card["summary"], card["indicator"]) == (NO_TEAM, "warning")
    assert card["suggestions"][0]["label"] == "Create a routing task"
