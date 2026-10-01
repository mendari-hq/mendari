from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mendari import Pack
from mendari.errors import PackValidationError, ProviderError
from mendari.providers import Provider, ProviderInfo, build_provider
from mendari.providers.jev import JevProvider, validate_response
from mendari.providers.naive_bayes import tokenize, train
from mendari.providers.rules import within_one_edit
from tests.conftest import message

# -- rules -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def red_flags(inbox_pack: Pack) -> Provider:
    return build_provider(inbox_pack, "safety_screen", "rules")


def urgency(provider: Provider, body: str) -> tuple[str, float, tuple[str, ...]]:
    result = provider.answer(message(body)).answers["urgency"]
    return result.label, result.probabilities["urgent"], result.evidence


def test_red_flag_escalates_with_named_evidence(red_flags: Provider) -> None:
    label, p_urgent, evidence = urgency(red_flags, "Crushing chest pain since this morning")
    assert label == "urgent" and p_urgent > 0.9
    assert "urgent:chest-pain" in evidence


def test_negated_red_flag_is_ignored(red_flags: Provider) -> None:
    label, p_urgent, _ = urgency(red_flags, "No chest pain or trouble breathing, just a cough.")
    assert label == "routine" and p_urgent < 0.35


def test_negation_scope_ends_at_contrast(red_flags: Provider) -> None:
    label, _, _ = urgency(red_flags, "No fever, but now I have chest pain.")
    assert label == "urgent"


def test_typos_in_red_flags_are_normalised(red_flags: Provider) -> None:
    label, _, evidence = urgency(red_flags, "having itght chset pressure right now")
    assert label == "urgent"
    assert "typo:chset->chest" in evidence


def test_typo_normalisation_keeps_first_letter(red_flags: Provider) -> None:
    # "painted" is one edit from "fainted", but a different first letter blocks the rewrite.
    label, _, evidence = urgency(red_flags, "I painted the kitchen yesterday.")
    assert label == "routine" and not evidence


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("chest", "chest", True),
        ("chset", "chest", True),  # transposition
        ("chst", "chest", True),  # deletion
        ("chesst", "chest", True),  # insertion
        ("chext", "chest", True),  # substitution
        ("cheats", "chest", False),
        ("tsehc", "chest", False),
    ],
)
def test_within_one_edit(a: str, b: str, expected: bool) -> None:
    assert within_one_edit(a, b) is expected


# -- naive Bayes -------------------------------------------------------------------------------


def test_naive_bayes_answers_with_a_distribution(inbox_pack: Pack) -> None:
    provider = build_provider(inbox_pack, "router", "naive_bayes")
    answer = provider.answer(message("I need a refill on my metformin")).answers["destination"]
    assert answer.label == "medical_assistant"
    assert sum(answer.probabilities.values()) == pytest.approx(1.0)
    assert answer.provider.model and answer.provider.model.startswith("naive_bayes@")


def test_naive_bayes_training_is_calibrated() -> None:
    records = [
        {"input": message(text), "labels": {"q": label}}
        for text, label in [
            ("pay my bill", "billing"),
            ("bill charge copay", "billing"),
            ("refund my bill", "billing"),
            ("book appointment", "front_desk"),
            ("cancel appointment", "front_desk"),
            ("reschedule my appointment", "front_desk"),
        ]
        * 5
    ]
    model = train(
        records,
        calibration=records[:6],
        question="q",
        labels=["billing", "front_desk"],
        text_fields=["message.body"],
    )
    assert model["temperature"] > 0
    assert (
        model["training"]["calibration"]["nll_after"]
        <= model["training"]["calibration"]["nll_before"]
    )
    assert "my bill" in model["tokens"]  # bigrams are features


def test_tokenize_keeps_accents_and_bigrams() -> None:
    assert tokenize("Recibí una factura") == [
        "recibí",
        "una",
        "factura",
        "recibí una",
        "una factura",
    ]


# -- Jev ---------------------------------------------------------------------------------------

# Neutral test values: the real endpoint and model are deployment settings, never defaults.
ENDPOINT = "https://jev.test/v1/decide"
MODEL = "jev-test-model"


@pytest.fixture
def jev_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("JEV_API_URL", ENDPOINT)
    monkeypatch.setenv("JEV_MODEL", MODEL)
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    return monkeypatch


def jev_body(choice: str = "nurse", model: str = MODEL, **overrides: Any) -> dict[str, Any]:
    labels = ["front_desk", "medical_assistant", "nurse", "physician", "billing"]
    probabilities = {label: (0.8 if label == choice else 0.05) for label in labels}
    body = {
        "model": model,
        "answers": {
            "destination": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": probabilities,
            }
        },
        "usage": {"input_tokens": 120, "output_tokens": 8},
    }
    body.update(overrides)
    return body


def jev_provider(inbox_pack: Pack, handler: Any) -> JevProvider:
    slot = inbox_pack.manifest.providers["router"]
    config = {
        "type": "jev",
        **(slot.variants["jev"].model_extra or {}),
        "cost_per_1k_input_tokens_usd": 0.5,
    }
    info = ProviderInfo(slot="router", variant="jev", type="jev")
    questions = {"destination": inbox_pack.manifest.questions["destination"]}
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JevProvider(inbox_pack, info, questions, config, client=client)


def test_jev_request_is_minimal_and_typed(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=jev_body())

    provider = jev_provider(inbox_pack, handler)
    inputs = {
        "message": {"body": "rash on my arm", "sent_at": "2026-01-01T00:00:00Z"},
        "patient": {"age_band": "40-64", "preferred_language": "en"},
    }
    output = provider.answer(inputs)

    assert seen["auth"] == "Bearer test-key"
    assert seen["url"] == ENDPOINT
    assert seen["body"]["model"] == MODEL
    # state_fields keeps sent_at and age_band out of the request (minimum necessary).
    assert seen["body"]["state"] == {
        "message": {"body": "rash on my arm"},
        "patient": {"preferred_language": "en"},
    }
    question = seen["body"]["questions"]["destination"]
    assert question["type"] == "choice" and set(question["criteria"]) >= {"nurse", "billing"}
    answer = output.answers["destination"]
    assert (answer.label, answer.confidence) == ("nurse", 0.8)
    assert answer.provider.model == MODEL  # recorded for provenance
    assert output.cost_usd == pytest.approx(120 / 1000 * 0.5)  # output tokens are free


@pytest.mark.parametrize("missing", ["JEV_API_URL", "JEV_MODEL"])
def test_jev_endpoint_and_model_are_required_configuration(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch, missing: str
) -> None:
    jev_env.delenv(missing)
    provider = jev_provider(inbox_pack, lambda _: httpx.Response(200, json=jev_body()))
    with pytest.raises(ProviderError) as error:
        provider.preflight()
    assert error.value.code == "not_configured" and missing in str(error.value)


def test_jev_rejects_malformed_model_ids(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    jev_env.setenv("JEV_MODEL", "bad model id!")
    with pytest.raises(PackValidationError, match="JEV_MODEL"):
        jev_provider(inbox_pack, lambda _: httpx.Response(200))


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "rate_limited"), (401, "credential_unavailable"), (503, "provider_unavailable")],
)
def test_jev_http_errors(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch, status: int, code: str
) -> None:
    provider = jev_provider(inbox_pack, lambda _: httpx.Response(status, text="secret detail"))
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hi"))
    assert error.value.code == code
    assert "secret" not in str(error.value)


def test_jev_timeout(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError, match="timeout"):
        jev_provider(inbox_pack, handler).answer(message("hi"))


def test_jev_needs_credentials(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    jev_env.delenv("JEV_API_KEY")
    with pytest.raises(ProviderError, match="credential_unavailable"):
        jev_provider(inbox_pack, lambda _: httpx.Response(200)).answer(message("hi"))


def test_jev_refuses_plain_http_endpoints(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    jev_env.setenv("JEV_API_URL", "http://jev.test/v1/decide")
    with pytest.raises(ProviderError, match="invalid_endpoint"):
        jev_provider(inbox_pack, lambda _: httpx.Response(200)).answer(message("hi"))


def test_jev_rejects_oversized_responses(inbox_pack: Pack, jev_env: pytest.MonkeyPatch) -> None:
    provider = jev_provider(inbox_pack, lambda _: httpx.Response(200, content=b" " * 70_000))
    with pytest.raises(ProviderError, match="invalid_response"):
        provider.answer(message("hi"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(model=""),
        lambda b: b.update(model=None),
        lambda b: b["answers"]["destination"].update(choice="pharmacist"),
        lambda b: b["answers"]["destination"].update(choice="billing"),  # not the argmax
        lambda b: b["answers"]["destination"]["probabilities"].update(nurse=0.9),  # sums to 1.1
        lambda b: b["answers"]["destination"].update(confidence=1.5),
        lambda b: b["answers"]["destination"]["probabilities"].pop("billing"),
        lambda b: b["usage"].update(input_tokens=-1),
        lambda b: b["usage"].update(output_tokens=True),
        lambda b: b["answers"].update(extra={}),
    ],
)
def test_jev_response_validation_is_strict(inbox_pack: Pack, mutate: Any) -> None:
    body = jev_body()
    mutate(body)
    questions = {"destination": inbox_pack.manifest.questions["destination"]}
    with pytest.raises(ProviderError, match="invalid_response"):
        validate_response(body, questions)


def test_jev_alias_records_the_model_that_actually_answered(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    jev_env.setenv("JEV_MODEL", "jev-latest")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json=jev_body(model="jev-2.0.1"))

    output = jev_provider(inbox_pack, handler).answer(message("rash on my arm"))
    assert seen["model"] == "jev-latest"
    assert output.answers["destination"].provider.model == "jev-2.0.1"
    assert output.extra == {"requested_model": "jev-latest", "effective_model": "jev-2.0.1"}


def test_jev_accepts_approximately_normalised_probabilities(inbox_pack: Pack) -> None:
    body = jev_body()
    body["answers"]["destination"]["probabilities"]["billing"] += 0.004  # sums to 1.004
    questions = {"destination": inbox_pack.manifest.questions["destination"]}
    effective, answers, _ = validate_response(body, questions)
    assert effective == MODEL and answers["destination"]["label"] == "nurse"


# -- alignment with Jev's documentation (docs.typesafe.ai) -------------------------------------


def test_jev_confidence_is_the_chosen_labels_probability(inbox_pack: Pack) -> None:
    # Jev's `confidence` summarises the whole distribution (the docs' example: billing 0.88,
    # confidence 0.81); our answer's confidence is the label's probability, like every provider.
    body = jev_body()
    body["answers"]["destination"].update(
        confidence=0.81,
        probabilities={
            "front_desk": 0.0,
            "medical_assistant": 0.02,
            "nurse": 0.88,
            "physician": 0.1,
            "billing": 0.0,
        },
    )
    questions = {"destination": inbox_pack.manifest.questions["destination"]}
    _, answers, _ = validate_response(body, questions)
    assert answers["destination"]["confidence"] == 0.88
    assert answers["destination"]["jev_confidence"] == 0.81


def test_jev_answers_show_jevs_own_confidence_and_cost_input_tokens_only(
    inbox_pack: Pack, jev_env: pytest.MonkeyPatch
) -> None:
    slot = inbox_pack.manifest.providers["router"]
    config = {"type": "jev", **(slot.variants["jev"].model_extra or {})}  # published price
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jev_body()))
    )
    provider = JevProvider(
        inbox_pack,
        ProviderInfo(slot="router", variant="jev", type="jev"),
        {"destination": inbox_pack.manifest.questions["destination"]},
        config,
        client=client,
    )
    output = provider.answer(message("rash on my arm"))
    assert output.answers["destination"].evidence == ("jev confidence 0.80",)
    assert output.cost_usd == pytest.approx(120 * 0.042 / 1_000_000)  # $0.042 per Mtok input


def test_jev_scores_have_at_most_ten_levels(inbox_pack: Pack) -> None:
    from mendari.core.spec import Question

    levels = {f"l{i}": {"score": i, "criterion": f"Level {i}."} for i in range(11)}
    question = Question.model_validate({"type": "score", "instructions": "x", "levels": levels})
    with pytest.raises(PackValidationError, match="at most 10 levels"):
        JevProvider(
            inbox_pack,
            ProviderInfo(slot="s", variant="jev", type="jev"),
            {"q": question},
            {"type": "jev"},
        )


@pytest.mark.parametrize(
    ("responses", "calls", "code"),
    [
        ([httpx.Response(529), httpx.Response(200, json=jev_body())], 2, None),
        ([httpx.Response(429), httpx.Response(429)], 2, "rate_limited"),
        ([httpx.Response(429, headers={"retry-after": "30"})], 1, "rate_limited"),
        ([httpx.Response(503)], 1, "provider_unavailable"),
    ],
)
def test_jev_retries_once_when_rate_limited_or_overloaded(
    inbox_pack: Pack,
    jev_env: pytest.MonkeyPatch,
    responses: list[httpx.Response],
    calls: int,
    code: str | None,
) -> None:
    from mendari.providers import http

    waits: list[float] = []
    jev_env.setattr(http, "_sleep", waits.append)
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    provider = jev_provider(inbox_pack, handler)
    if code is None:
        assert provider.answer(message("rash")).answers["destination"].label == "nurse"
    else:
        with pytest.raises(ProviderError) as error:
            provider.answer(message("rash"))
        assert error.value.code == code
    assert len(seen) == calls
    assert waits == ([0.5] if calls == 2 else [])  # a 30 s Retry-After is not waited out
