# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mendari import AuditStore, CallBudget, Decision, Outcome, Pack
from mendari.evals import evaluate, resolve_dataset
from mendari.providers import build_provider
from mendari.providers.jev import JevProvider
from tests.conftest import message

GI_BLEED = "I just started to vomit a lot of blood "  # the reported miss, verbatim
SUBTLE = "Squeezing feeling in my chest going into my jaw"  # the keyword rules miss this
ROUTINE = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."


@pytest.fixture
def jev_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    return monkeypatch


def fake_jev(monkeypatch: pytest.MonkeyPatch, p_urgent: float | None) -> list[dict[str, Any]]:
    """Route every Jev call to a fake that answers urgency with P(urgent) = p_urgent
    (or fails with HTTP 503 when p_urgent is None). Returns the captured request bodies."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if p_urgent is None:
            return httpx.Response(503)
        probabilities = {"urgent": p_urgent, "routine": round(1 - p_urgent, 6)}
        choice = max(probabilities, key=lambda k: probabilities[k])
        return httpx.Response(
            200,
            json={
                "model": "jev-9.9.9",
                "answers": {
                    "urgency": {
                        "type": "choice",
                        "choice": choice,
                        "confidence": probabilities[choice],
                        "probabilities": probabilities,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    return calls


def screen(inbox_pack: Pack, **kwargs: Any) -> Decision:
    return Decision(inbox_pack, providers={"safety_screen": "rules_and_jev"}, **kwargs)


def test_jev_catches_what_the_rules_miss(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    fake_jev(jev_env, p_urgent=0.97)
    result = screen(inbox_pack)(message(SUBTLE))
    urgency = result.answers["urgency"]
    assert (result.outcome, result.priority.value) == (Outcome.HUMAN_REVIEW, "stat")
    assert urgency.provider.model == "jev:jev-9.9.9"  # which member decided is audited
    assert "jev:p(urgent)=0.97" in urgency.evidence


def test_rules_still_escalate_when_jev_says_routine(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    fake_jev(jev_env, p_urgent=0.02)
    result = screen(inbox_pack)(message(GI_BLEED))
    assert result.priority.value == "stat"
    assert (result.answers["urgency"].provider.model or "").startswith("rules:")
    assert "rules:urgent:gi-bleeding" in result.answers["urgency"].evidence


def test_both_routine_allows_normal_routing(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    calls = fake_jev(jev_env, p_urgent=0.01)
    result = screen(inbox_pack)(message(ROUTINE))
    assert result.answers["urgency"].label == "routine"
    assert result.outcome is Outcome.AUTO
    assert calls[0]["state"] == {"message": {"body": ROUTINE}}  # minimum necessary


def test_jev_outage_keeps_the_rules_but_blocks_automation(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    fake_jev(jev_env, p_urgent=None)
    result = screen(inbox_pack)(message(ROUTINE))  # AUTO when both screens answer
    assert result.answers["urgency"].label == "routine"  # rules still answered
    assert result.errors == {"urgency": "jev:provider_unavailable"}
    assert result.outcome is Outcome.HUMAN_REVIEW  # screen incomplete: never AUTO
    assert any("provider failure" in reason for reason in result.reasons)
    emergency = screen(inbox_pack)(message(GI_BLEED))
    assert emergency.priority.value == "stat"  # the offline floor still escalates


def test_composite_jev_calls_count_against_the_budget(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    fake_jev(jev_env, p_urgent=0.01)
    with AuditStore(":memory:") as store:
        decision = screen(inbox_pack, budget=CallBudget(store, {"jev": 1}))
        decision(message(ROUTINE))
        second = decision(message(ROUTINE))
        assert store.usage("jev") == 1
    assert second.errors == {"urgency": "jev:budget_exhausted"}
    assert second.outcome is Outcome.HUMAN_REVIEW


def test_composite_is_marked_remote(inbox_pack: Pack) -> None:
    assert build_provider(inbox_pack, "safety_screen", "rules_and_jev").remote
    assert not build_provider(inbox_pack, "safety_screen", "rules").remote


def test_rules_floor_keeps_its_red_flag_coverage(
    inbox_pack: Pack, screened: dict[str, str]
) -> None:
    """Regression guard for the offline floor on the tuning set (the held-out set is reported
    in the Decision Card, not asserted: keyword rules are known not to generalise). Beside a
    model that finds nothing urgent, the rules alone keep every red flag from being automated."""
    router = Decision(inbox_pack, providers=screened)
    report = evaluate(router, resolve_dataset(router.pack, "red_flags"))
    urgent = report["questions"]["urgency"]["per_label"]["urgent"]
    assert urgent["recall"] == 1.0 and urgent["precision"] == 1.0
    assert report["decisions"]["critical"][0]["auto_missed"] == 0
