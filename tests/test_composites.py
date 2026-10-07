# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest

from mendari import AuditStore, CallBudget, Pack
from mendari.errors import PackValidationError, ProviderError
from mendari.providers import PROVIDER_TYPES, Provider, ProviderInfo
from mendari.providers.calibration import fit_temperature, nll, rescale
from mendari.providers.jev import JevProvider
from tests.conftest import message

REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."
NB = {
    "type": "naive_bayes",
    "model_file": "models/destination-nb.json",
    "text_fields": ["message.subject", "message.body"],
}
JEV = {"type": "jev", "state_fields": ["message.subject", "message.body"]}
DESTINATIONS = ["front_desk", "medical_assistant", "nurse", "physician", "billing"]


@pytest.fixture
def jev_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every Jev call answers `nurse` with p=0.9; returns the request bodies."""
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        probabilities = {label: (0.9 if label == "nurse" else 0.025) for label in DESTINATIONS}
        answer = {"type": "choice", "choice": "nurse", "confidence": 0.9}
        return httpx.Response(
            200,
            json={
                "model": "jev-9.9.9",
                "answers": {"destination": {**answer, "probabilities": probabilities}},
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    return calls


def router(inbox_pack: Pack, config: dict[str, Any]) -> Provider:
    info = ProviderInfo(slot="router", variant="combo", type=config["type"])
    questions = {"destination": inbox_pack.manifest.questions["destination"]}
    return PROVIDER_TYPES[config["type"]](inbox_pack, info, questions, config)


# -- cascade -----------------------------------------------------------------------------------


def test_cascade_keeps_confident_answers_local(
    inbox_pack: Pack, jev_calls: list[dict[str, Any]]
) -> None:
    cascade = router(
        inbox_pack, {"type": "cascade", "accept_at": 0.5, "members": {"nb": NB, "jev": JEV}}
    )
    answer = cascade.answer(message(REFILL)).answers["destination"]
    assert jev_calls == []  # the remote member was never needed
    assert answer.label == "medical_assistant"
    assert (answer.provider.variant, answer.provider.type) == ("combo", "cascade")
    assert (answer.provider.model or "").startswith("nb:")
    assert answer.evidence[0] == "cascade: nb accepted"


def test_cascade_escalates_when_unsure(inbox_pack: Pack, jev_calls: list[dict[str, Any]]) -> None:
    cascade = router(
        inbox_pack, {"type": "cascade", "accept_at": 1.0, "members": {"nb": NB, "jev": JEV}}
    )
    output = cascade.answer(message(REFILL))
    answer = output.answers["destination"]
    assert len(jev_calls) == 1
    assert (answer.label, answer.provider.model) == ("nurse", "jev:jev-9.9.9")
    assert (
        answer.evidence[0].startswith("cascade: nb 0.99")
        and "< 1.000; jev accepted" in answer.evidence[0]
    )
    assert output.failures == {} and output.input_tokens == 100


def test_cascade_falls_back_when_the_last_member_fails(
    inbox_pack: Pack, jev_calls: list[dict[str, Any]]
) -> None:
    cascade = router(
        inbox_pack, {"type": "cascade", "accept_at": 1.0, "members": {"nb": NB, "jev": JEV}}
    )
    with AuditStore(":memory:") as store:
        cascade.use_budget(CallBudget(store, {"jev": 0}).acquire)
        output = cascade.answer(message(REFILL))
    answer = output.answers["destination"]
    assert jev_calls == []  # refused by the budget before any call
    assert (answer.provider.model or "").startswith("nb:")
    assert "last member failed" in answer.evidence[0]
    assert output.failures == {"destination": "jev:budget_exhausted"}  # never AUTO


def test_cascade_passes_a_failed_member_to_the_next(inbox_pack: Pack) -> None:
    # Jev first but unconfigured (no endpoint): naive Bayes answers, the failure is recorded.
    cascade = router(
        inbox_pack, {"type": "cascade", "accept_at": 0.99, "members": {"jev": JEV, "nb": NB}}
    )
    output = cascade.answer(message(REFILL))
    assert (output.answers["destination"].provider.model or "").startswith("nb:")
    assert output.failures == {"destination": "jev:not_configured"}
    assert (
        output.answers["destination"].evidence[0].startswith("cascade: jev failed (not_configured)")
    )


def test_cascade_fails_when_every_member_fails(inbox_pack: Pack) -> None:
    config = {"type": "cascade", "accept_at": 0.9, "members": {"a": JEV, "b": JEV}}
    with pytest.raises(ProviderError, match="not_configured"):
        router(inbox_pack, config).answer(message(REFILL))


# -- vote --------------------------------------------------------------------------------------


def test_vote_is_a_weighted_average(inbox_pack: Pack, jev_calls: list[dict[str, Any]]) -> None:
    config = {"type": "vote", "weights": {"nb": 1, "jev": 3}, "members": {"nb": NB, "jev": JEV}}
    nb_alone = router(
        inbox_pack, {"type": "cascade", "accept_at": 0.01, "members": {"nb": NB, "jev": JEV}}
    )
    p_nb = nb_alone.answer(message(REFILL)).answers["destination"].probabilities
    answer = router(inbox_pack, config).answer(message(REFILL)).answers["destination"]
    p_jev = {label: (0.9 if label == "nurse" else 0.025) for label in DESTINATIONS}
    for label in DESTINATIONS:
        assert answer.probabilities[label] == pytest.approx((p_nb[label] + 3 * p_jev[label]) / 4)
    assert math.isclose(sum(answer.probabilities.values()), 1.0)
    assert answer.label == max(answer.probabilities, key=lambda k: answer.probabilities[k])
    assert any(item.startswith("jev:nurse (0.90, w=3)") for item in answer.evidence)


def test_vote_leaves_out_failed_members(inbox_pack: Pack) -> None:
    config = {"type": "vote", "members": {"nb": NB, "jev": JEV}}  # Jev unconfigured
    output = router(inbox_pack, config).answer(message(REFILL))
    assert output.answers["destination"].label == "medical_assistant"
    assert output.failures == {"destination": "jev:not_configured"}


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (
            {"type": "vote", "weights": {"nb": 1}, "members": {"nb": NB, "jev": JEV}},
            "weights must name",
        ),
        (
            {"type": "vote", "weights": {"nb": 1, "jev": 0}, "members": {"nb": NB, "jev": JEV}},
            "positive",
        ),
        ({"type": "vote", "members": {"nb": NB}}, "members"),
        ({"type": "cascade", "accept_at": 0, "members": {"nb": NB, "jev": JEV}}, "accept_at"),
        (
            {"type": "cascade", "accept_at": 0.9, "members": {"nb": NB, "inner": {"type": "vote"}}},
            "unknown member type 'vote'",
        ),
        (
            {"type": "vote", "members": {"nb": NB, "x": {"type": "gpt"}}},
            "unknown member type 'gpt'",
        ),
    ],
)
def test_composite_validation(inbox_pack: Pack, config: dict[str, Any], problem: str) -> None:
    with pytest.raises(PackValidationError, match=problem):
        router(inbox_pack, config)


def test_composites_expose_members_to_budgets_and_batching(inbox_pack: Pack) -> None:
    cascade = router(
        inbox_pack, {"type": "cascade", "accept_at": 0.9, "members": {"nb": NB, "jev": JEV}}
    )
    assert [leaf.type for leaf in cascade.leaves()] == ["naive_bayes", "jev"]
    assert cascade.remote
    assert cascade.info.model is not None and " → " in cascade.info.model


# -- calibration -------------------------------------------------------------------------------


def test_temperature_softens_an_overconfident_model() -> None:
    # 70% right, but always 99% sure: the fitted temperature must be > 1 and lower the NLL.
    scored = [
        ({"a": math.log(0.99), "b": math.log(0.01)}, "a" if i < 7 else "b") for i in range(10)
    ]
    temperature, before, after = fit_temperature(scored)
    assert temperature > 1 and after < before
    assert after == pytest.approx(nll(scored, temperature))
    calibrated = rescale({"a": 0.99, "b": 0.01}, temperature)
    assert calibrated["a"] == pytest.approx(0.7, abs=0.02)
    assert rescale({"a": 0.6, "b": 0.4}, 1.0) == pytest.approx({"a": 0.6, "b": 0.4})
