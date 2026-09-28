from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from openhealth import AuditStore, CallBudget, Decision, Outcome, Pack, load_pack
from openhealth.errors import ProviderError
from openhealth.providers import Provider, ProviderInfo
from openhealth.providers.base import ProviderOutput
from openhealth.providers.batch import batched_answer, batching
from openhealth.providers.jev import JevProvider, jev_question, validate_response
from tests.conftest import message

TRIAL_MINI = Path(__file__).parent / "fixtures" / "packs" / "trial-mini"
DESTINATIONS = ["front_desk", "medical_assistant", "nurse", "physician", "billing"]
ROUTINE = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."


@pytest.fixture
def jev_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    return monkeypatch


def choice(labels: list[str], picked: str, p: float = 0.9) -> dict[str, Any]:
    rest = (1 - p) / (len(labels) - 1)
    probabilities = {label: (p if label == picked else rest) for label in labels}
    return {"type": "choice", "choice": picked, "confidence": p, "probabilities": probabilities}


def fake_jev(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list[dict[str, Any]]:
    """Answer whatever inbox questions each call asks; return the request bodies."""
    calls: list[dict[str, Any]] = []
    canned = {
        "destination": choice(DESTINATIONS, "medical_assistant", 0.96),
        "urgency": choice(["urgent", "routine"], "routine", 0.98),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if status != 200:
            return httpx.Response(status)
        answers = {key: canned[key] for key in body["questions"]}
        usage = {"input_tokens": 100, "output_tokens": 10}
        return httpx.Response(200, json={"model": "jev-9.9.9", "answers": answers, "usage": usage})

    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    return calls


def both_jev(inbox_pack: Pack, **kwargs: Any) -> Decision:
    return Decision(
        inbox_pack, providers={"router": "jev", "safety_screen": "rules_and_jev"}, **kwargs
    )


# -- one call per decision ---------------------------------------------------------------------


def test_router_and_safety_screen_share_one_call(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    calls = fake_jev(jev_env)
    with AuditStore(":memory:") as store:
        result = both_jev(inbox_pack, budget=CallBudget(store, {"jev": 10}))(message(ROUTINE))
        assert store.usage("jev") == 1  # one budget unit per call, not per question
    assert len(calls) == 1
    assert set(calls[0]["questions"]) == {"destination", "urgency"}
    assert calls[0]["state"] == {"message": {"body": ROUTINE}}  # union of state_fields
    assert result.answers["destination"].label == "medical_assistant"
    assert result.answers["urgency"].label == "routine"
    assert result.outcome is Outcome.AUTO and result.errors == {}
    assert (result.input_tokens, result.output_tokens) == (100, 10)  # counted once


def test_a_failed_call_fails_every_batched_question(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    calls = fake_jev(jev_env, status=503)
    result = both_jev(inbox_pack)(message("I just started to vomit a lot of blood"))
    assert len(calls) == 1
    assert result.errors == {
        "destination": "provider_unavailable",
        "urgency": "jev:provider_unavailable",
    }
    assert result.priority.value == "stat"  # the rules half of the screen still escalates
    assert result.outcome is not Outcome.AUTO


def test_an_exhausted_budget_fails_the_batch_without_calling(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    calls = fake_jev(jev_env)
    with AuditStore(":memory:") as store:
        result = both_jev(inbox_pack, budget=CallBudget(store, {"jev": 0}))(message(ROUTINE))
    assert calls == []
    assert result.errors == {
        "destination": "budget_exhausted",
        "urgency": "jev:budget_exhausted",
    }
    assert result.outcome is not Outcome.AUTO


def test_a_variant_can_opt_out_of_batching(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    calls = fake_jev(jev_env)
    decision = both_jev(inbox_pack)
    router = decision.providers["router"]
    assert isinstance(router, JevProvider)
    router.settings = router.settings.model_copy(update={"batch": False})
    decision(message(ROUTINE))
    assert sorted(len(call["questions"]) for call in calls) == [1, 1]


def test_batches_do_not_leak_between_decisions(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    calls = fake_jev(jev_env)
    decision = both_jev(inbox_pack)
    decision(message(ROUTINE))
    decision(message("Can I pay my bill online?"))
    assert [call["state"]["message"]["body"] for call in calls] == [
        ROUTINE,
        "Can I pay my bill online?",
    ]


# -- the generic batching rules ----------------------------------------------------------------


class Counting(Provider):
    """A fake batchable provider; every call is recorded on the class."""

    type = "counting"
    max_batch_questions = 3
    calls: list[list[str]] = []

    def __init__(self, questions: list[str], key: str = "k", fail: bool = False):
        self.questions = dict.fromkeys(questions)  # type: ignore[assignment]
        self.key = key
        self.fail = fail
        self.info = ProviderInfo(slot=questions[0], variant="v", type=self.type)

    def batch_key(self) -> str:
        return self.key

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        shared = batched_answer(self, inputs)
        return shared if shared is not None else self.answer_batch([self], inputs)[0]

    def answer_batch(self, providers: list[Provider], inputs: Mapping[str, Any]) -> list[Any]:
        Counting.calls.append([q for p in providers for q in p.questions])
        if self.fail:
            raise ProviderError("timeout")
        return [ProviderOutput(answers={}, extra={"for": p.info.slot}) for p in providers]


@pytest.fixture(autouse=True)
def reset_calls() -> None:
    Counting.calls = []


def test_grouping_respects_keys_limits_and_duplicates() -> None:
    providers = [
        Counting(["a", "b"]),
        Counting(["c"]),  # joins a+b: 3 questions, the limit
        Counting(["d"]),  # would exceed the limit: new group
        Counting(["a"]),  # asks "a" again: joins d's group instead
        Counting(["e"], key="other"),  # different key: alone, never batched
    ]
    with batching(providers):
        outputs = [p.answer({}) for p in providers]
    assert Counting.calls == [["a", "b", "c"], ["d", "a"], ["e"]]
    assert [o.extra["for"] for o in outputs] == ["a", "c", "d", "a", "e"]


def test_the_call_happens_only_when_a_member_answers() -> None:
    first, second = Counting(["a"]), Counting(["b"])
    with batching([first, second]):
        pass
    assert Counting.calls == []
    with batching([first, second]):
        second.answer({})  # the second member leads when it asks first
        first.answer({})
    assert Counting.calls == [["b", "a"]]


def test_errors_reach_every_member() -> None:
    providers = [Counting(["a"], fail=True), Counting(["b"])]
    with batching(providers):
        for provider in providers:
            with pytest.raises(ProviderError, match="timeout"):
                provider.answer({})
    assert len(Counting.calls) == 1


# -- noul and score ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def trial() -> Pack:
    return load_pack(TRIAL_MINI)


def test_question_types_map_to_jev_types(trial: Pack) -> None:
    q = trial.manifest.questions
    assert jev_question(q["inc_adult"]) == {
        "type": "noul",
        "instructions": "Is the patient 18 or older?",
        "criteria": {"true": "Yes.", "false": "No."},
    }
    assert jev_question(q["severity"])["criteria"] == [
        "Well controlled.",
        "Partially controlled.",
        "Uncontrolled or complicated.",
    ]
    assert jev_question(q["inc_t2d"])["type"] == "choice"


def score_answer(legend: dict[str, Any], probabilities: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "score",
        "score": 1.8,
        "confidence": 0.7,
        "legend": legend,
        "probabilities": probabilities,
    }


LEVELS = ["Well controlled.", "Partially controlled.", "Uncontrolled or complicated."]


@pytest.mark.parametrize(
    "legend",
    [
        {"0": LEVELS[0], "1": LEVELS[1], "2": LEVELS[2]},
        {LEVELS[0]: 0, LEVELS[1]: 1, LEVELS[2]: 2},
    ],
)
def test_noul_and_score_answers(trial: Pack, legend: dict[str, Any]) -> None:
    keys = list(legend)
    questions = {k: trial.manifest.questions[k] for k in ("inc_adult", "severity")}
    raw = {
        "model": "jev-9.9.9",
        "answers": {
            "inc_adult": {"type": "noul", "noul": 0.2},
            "severity": score_answer(legend, dict(zip(keys, [0.1, 0.2, 0.7], strict=True))),
        },
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    _, answers, _ = validate_response(raw, questions)
    assert answers["inc_adult"] == {
        "label": "no",
        "confidence": 0.8,
        "probabilities": {"yes": 0.2, "no": 0.8},
    }
    assert answers["severity"]["label"] == "severe"
    assert answers["severity"]["confidence"] == 0.7
    assert answers["severity"]["probabilities"] == {"mild": 0.1, "moderate": 0.2, "severe": 0.7}


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": 1.2},
        {"type": "noul", "noul": True},
        {"type": "choice", "choice": "yes", "confidence": 1, "probabilities": {"yes": 1}},
        score_answer({"0": "a", "1": "b"}, {"0": 0.5, "1": 0.5}),  # too few levels
        score_answer({"a": 0, "b": 1, "c": 2}, {"a": 0.2, "b": 0.3, "c": 0.5}),  # unknown text
        score_answer({LEVELS[0]: 2, LEVELS[1]: 1, LEVELS[2]: 0}, dict.fromkeys(LEVELS, 1 / 3)),
        score_answer({"0": "x", "1": "y", "2": "z"}, {"0": 0.2, "1": 0.3}),
        score_answer({"0": "x", "1": "y", "2": "z"}, {"0": 0.5, "1": 0.5, "2": 0.5}),
    ],
)
def test_noul_and_score_validation_is_strict(trial: Pack, answer: dict[str, Any]) -> None:
    key = "inc_adult" if answer["type"] in ("noul", "choice") else "severity"
    raw = {
        "model": "jev-9.9.9",
        "answers": {key: answer},
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
    with pytest.raises(ProviderError, match="invalid_response"):
        validate_response(raw, {key: trial.manifest.questions[key]})


def test_score_answers_get_expected_values_in_decisions(
    trial: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["questions"]["severity"]["type"] == "score"
        answer = score_answer({"0": "a", "1": "b", "2": "c"}, {"0": 0.1, "1": 0.2, "2": 0.7})
        return httpx.Response(
            200,
            json={
                "model": "jev-9.9.9",
                "answers": {"severity": answer},
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    info = ProviderInfo(slot="screen", variant="jev", type="jev")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = JevProvider(
        trial,
        info,
        {"severity": trial.manifest.questions["severity"]},
        {"type": "jev"},
        client=client,
    )
    answer = provider.answer({"summary": "uncontrolled"}).answers["severity"]
    assert answer.label == "severe" and answer.provider.model == "jev-9.9.9"
    expected = trial.manifest.questions["severity"].expected_score(answer.probabilities)
    assert expected == pytest.approx(0.1 * 1 + 0.2 * 2 + 0.7 * 3)


def test_a_malformed_router_answer_keeps_the_safety_answer(
    inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch, jev_env: pytest.MonkeyPatch
) -> None:
    broken = {**choice(DESTINATIONS, "nurse"), "choice": "pharmacist"}

    def handler(request: httpx.Request) -> httpx.Response:
        answers = {"destination": broken, "urgency": choice(["urgent", "routine"], "urgent", 0.97)}
        usage = {"input_tokens": 100, "output_tokens": 10}
        return httpx.Response(200, json={"model": "jev-9.9.9", "answers": answers, "usage": usage})

    original = JevProvider.__init__

    def init(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(JevProvider, "__init__", init)
    result = both_jev(inbox_pack)(message("Squeezing feeling in my chest going into my jaw"))
    assert result.answers["urgency"].label == "urgent" and result.priority.value == "stat"
    assert result.errors == {"destination": "invalid_response"}
    assert result.input_tokens == 100  # counted once, by the provider that answered


def test_a_member_error_fails_only_that_member() -> None:
    class Partial(Counting):
        def answer_batch(self, providers: list[Provider], inputs: Mapping[str, Any]) -> list[Any]:
            Counting.calls.append([q for p in providers for q in p.questions])
            return [
                ProviderError("invalid_response", "bad")
                if "bad" in p.questions
                else ProviderOutput(answers={})
                for p in providers
            ]

    good, bad = Partial(["good"]), Partial(["bad"])
    with batching([good, bad]):
        good.answer({})
        with pytest.raises(ProviderError, match="invalid_response"):
            bad.answer({})
    assert len(Counting.calls) == 1
