# SPDX-License-Identifier: Apache-2.0
"""The agent kit (mendari.agent_kit): the agent's side of Mendari's agent contract. A request
Mendari would not send is refused, every answer `respond` builds passes the engine's own
`validate_response`, and the HTTP app guards its token and never leaks an error's message."""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mendari import agent_kit
from mendari.agent_kit import AgentError, Case, create_app, respond
from mendari.core.spec import Question as SpecQuestion
from mendari.providers import agent as provider
from mendari.providers import base
from mendari.providers.agent import request_body, validate_response

QUESTIONS = {
    "request": SpecQuestion.model_validate(
        {
            "instructions": "What does the patient ask for?",
            "criteria": {"refill": "A refill.", "appointment": "A visit.", "other": "Else."},
        }
    ),
    "urgent_symptoms": SpecQuestion.model_validate(
        {
            "type": "boolean",
            "instructions": "Symptoms today?",
            "criteria": {"yes": "Chest pain.", "no": "None."},
        }
    ),
}


def body_of(text: str = "Please refill my metformin.", **extra: Any) -> dict[str, Any]:
    """A request as the engine builds it."""
    return request_body(
        "my-clinic/my-agent",
        "reader",
        QUESTIONS,
        {"message": {"subject": "Refill", "body": text}},
        agent_id="my-agent",
        **extra,
    )


def checked(case: Case, answers: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """`respond`'s body, through the engine's validation."""
    return validate_response(respond(case, answers, **kwargs), QUESTIONS)


def test_the_kits_limits_are_the_engines() -> None:
    assert agent_kit.MAX_QUESTIONS == provider.MAX_QUESTIONS
    assert agent_kit.MAX_RESPONSE_BYTES == provider.MAX_RESPONSE_BYTES
    assert agent_kit.MAX_RATIONALE_CHARS == provider.MAX_RATIONALE_CHARS
    assert agent_kit.MAX_TOOL_CALLS == provider.MAX_TOOL_CALLS
    assert agent_kit.MAX_TOOL_NAME_CHARS == provider.MAX_TOOL_NAME_CHARS
    assert agent_kit.MAX_INPUT_CHARS == provider.MAX_REQUEST_CHARS
    assert agent_kit.ID_PATTERN.pattern == provider.AGENT_ID_PATTERN
    assert agent_kit.MAX_CALL_TOKENS == base.MAX_CALL_TOKENS
    assert agent_kit.MAX_CALL_COST_USD == base.MAX_CALL_COST_USD


# -- Case.parse -----------------------------------------------------------------------------------


def test_a_case_holds_what_the_engine_sent() -> None:
    case = Case.parse(body_of(system_prompt=" Be careful. ", instructions="Be brief."))
    assert (case.agent, case.slot, case.agent_id) == ("my-clinic/my-agent", "reader", "my-agent")
    assert case.instructions == "Be careful.\n\nBe brief."
    assert [q.key for q in case.questions] == ["request", "urgent_symptoms"]
    request = case.question("request")
    assert request.type == "choice" and request.choices == ("refill", "appointment", "other")
    assert request.criteria["refill"] == "A refill." and request.levels is None
    assert case.question("urgent_symptoms").type == "boolean"
    assert case.input == {"message": {"subject": "Refill", "body": "Please refill my metformin."}}
    assert case.text() == "refill\nplease refill my metformin."
    with pytest.raises(KeyError):
        case.question("missing")
    assert Case.parse(body_of()).instructions is None


ONE = {"key": "a", "choices": ["x", "y"]}


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ([], 400, "the request is not a JSON object"),
        ({"input": {}}, 400, "questions: 1 to 64 expected"),
        ({"questions": [], "input": {}}, 400, "questions: 1 to 64 expected"),
        ({"questions": [ONE] * 65, "input": {}}, 400, "questions: 1 to 64 expected"),
        ({"questions": ["a"], "input": {}}, 400, "each question needs a key"),
        ({"questions": [{"key": 1, "choices": ["x", "y"]}], "input": {}}, 400,
         "each question needs a key"),
        ({"questions": [{"key": "a", "choices": ["x"]}], "input": {}}, 400,
         "question 'a': at least two distinct choices"),
        ({"questions": [{"key": "a", "choices": ["x", "x"]}], "input": {}}, 400,
         "question 'a': at least two distinct choices"),
        ({"questions": [{"key": "a", "choices": ["x", ""]}], "input": {}}, 400,
         "question 'a': at least two distinct choices"),
        ({"questions": [{"key": "a", "choices": ["x", 2]}], "input": {}}, 400,
         "question 'a': at least two distinct choices"),
        ({"questions": [{"key": "a"}], "input": {}}, 400,
         "question 'a': at least two distinct choices"),
        ({"questions": [ONE, ONE], "input": {}}, 400, "question keys repeat"),
        ({"questions": [ONE]}, 400, "input: an object expected"),
        ({"questions": [ONE], "input": "text"}, 400, "input: an object expected"),
        ({"questions": [ONE], "input": {"t": "x" * 100_001}}, 413, "input too large"),
    ],
)  # fmt: skip
def test_a_request_the_engine_would_not_send_is_refused(
    body: Any, status: int, message: str
) -> None:
    with pytest.raises(AgentError) as raised:
        Case.parse(body)
    assert (raised.value.status, raised.value.message) == (status, message)


# -- respond ---------------------------------------------------------------------------------------


def test_probabilities_are_normalized_and_the_value_is_the_most_probable() -> None:
    case = Case.parse(body_of())
    shown = respond(
        case, {"request": {"refill": 3, "appointment": -1}, "urgent_symptoms": {"no": 1}}
    )
    request = shown["answers"]["request"]
    assert request == {
        "value": "refill",
        "probabilities": {"refill": 1.0, "appointment": 0.0, "other": 0.0},
    }
    shown = respond(
        case,
        {
            "request": {"refill": 0.2, "appointment": 0.5, "other": 0.3},
            "urgent_symptoms": {"yes": 2, "no": 6},
        },
    )
    assert shown["answers"]["request"]["value"] == "appointment"
    assert shown["answers"]["urgent_symptoms"] == {
        "value": "no",
        "probabilities": {"yes": 0.25, "no": 0.75},
    }


def test_a_choice_and_a_confidence_are_kept_as_given() -> None:
    case = Case.parse(body_of())
    shown = checked(case, {"request": ("refill", 0.95), "urgent_symptoms": ["no", 0.5]})
    assert shown["answers"]["request"]["confidence"] == 0.95
    assert shown["answers"]["request"]["probabilities"]["other"] == pytest.approx(0.025)
    assert shown["answers"]["urgent_symptoms"]["label"] == "no"
    # Uniform chance exactly is allowed, for any number of choices.
    assert checked(case, {"request": ("other", 1 / 3), "urgent_symptoms": ("yes", 1)})


UNIFORM = "the answer to 'request' needs a confidence from uniform chance to 1"


@pytest.mark.parametrize(
    ("request_answer", "message"),
    [
        ({"refill": math.nan}, "the answer to 'request' has a malformed probability"),
        ({"refill": math.inf}, "the answer to 'request' has a malformed probability"),
        ({"refill": "0.9"}, "the answer to 'request' has a malformed probability"),
        ({"refill": True}, "the answer to 'request' has a malformed probability"),
        ({"refill": 0, "other": -2}, "the answer to 'request' has no probability mass"),
        ({}, "the answer to 'request' has no probability mass"),
        ({"refill": 1, "urgent": 1}, "the answer to 'request' weighs a label that is not a choice"),
        (("refil", 0.9), "the answer to 'request' is not one of its choices"),
        (("refill", 0.3), UNIFORM),
        (("refill", 1.01), UNIFORM),
        (("refill", math.nan), UNIFORM),
        (("refill", True), UNIFORM),
        ("refill", "the answer to 'request' is neither probabilities nor (choice, confidence)"),
        (("refill", 0.9, "why"),
         "the answer to 'request' is neither probabilities nor (choice, confidence)"),
    ],
)  # fmt: skip
def test_an_answer_the_engine_would_refuse_is_an_error(request_answer: Any, message: str) -> None:
    with pytest.raises(AgentError) as raised:
        respond(Case.parse(body_of()), {"request": request_answer, "urgent_symptoms": ("no", 0.9)})
    assert (raised.value.status, raised.value.message) == (502, message)


@pytest.mark.parametrize(
    ("answers", "kwargs", "message"),
    [
        ({"request": ("refill", 0.9)}, {}, "the agent left 'urgent_symptoms' unanswered"),
        ({"request": ("refill", 0.9), "urgent_symptoms": ("no", 0.9), "extra": ("x", 1)}, {},
         "the agent answered a question it was not asked"),
        (None, {"model": "https://model.example"}, "the agent's model is not a valid id"),
        (None, {"model": ""}, "the agent's model is not a valid id"),
        (None, {"agent_version": "1.0 beta"}, "the agent's agent_version is not a valid id"),
        (None, {"usage": {"input_tokens": -1}}, "the agent's usage is malformed"),
        (None, {"usage": {"input_tokens": True}}, "the agent's usage is malformed"),
        (None, {"usage": {"input_tokens": 10_000_001}}, "the agent's usage is malformed"),
        (None, {"usage": {"cost_usd": math.inf}}, "the agent's cost is malformed"),
        (None, {"usage": {"cost_usd": 1001}}, "the agent's cost is malformed"),
        (None, {"tool_calls": ["x"] * 65}, "tool_calls: at most 64"),
        (None, {"tool_calls": "search"}, "tool_calls: at most 64"),
        (None, {"tool_calls": [" "]}, "each tool call is a name or an object"),
        (None, {"tool_calls": [{"name": object()}]}, "the agent's answer is not plain JSON"),
        (None, {"tool_calls": [{"name": "x" * 1100}] * 60},
         "the agent's answer is larger than Mendari accepts"),
    ],
)  # fmt: skip
def test_a_response_the_engine_would_refuse_is_an_error(
    answers: dict[str, Any] | None, kwargs: dict[str, Any], message: str
) -> None:
    given = answers or {"request": ("refill", 0.9), "urgent_symptoms": ("no", 0.9)}
    with pytest.raises(AgentError) as raised:
        respond(Case.parse(body_of()), given, **kwargs)
    assert (raised.value.status, raised.value.message) == (502, message)


def test_provenance_rationales_usage_and_tool_calls_reach_the_engine() -> None:
    case = Case.parse(body_of())
    shown = checked(
        case,
        {"request": ("refill", 0.95), "urgent_symptoms": ("no", 0.9)},
        rationales={"request": "  Asks for\na refill. " + "x" * 600, "urgent_symptoms": " "},
        model="my-model@2026-01",
        agent_version="1.4.0",
        usage={"input_tokens": 412, "output_tokens": 38, "cost_usd": 0.0004},
        tool_calls=["  classifier ", {"name": "screen", "status": "ok"}],
    )
    assert shown["answers"]["request"]["rationale"].startswith("Asks for a refill. xx")
    assert len(shown["answers"]["request"]["rationale"]) == 500
    assert shown["answers"]["urgent_symptoms"]["rationale"] is None
    assert (shown["model"], shown["agent_version"]) == ("my-model@2026-01", "1.4.0")
    assert shown["usage"] == {"input_tokens": 412, "output_tokens": 38, "cost_usd": 0.0004}
    assert shown["tool_calls"] == ["classifier", {"name": "screen", "status": "ok"}]


# Generated questions (choice or boolean) and answers in either form: whatever `respond`
# returns, the engine's own validation accepts, with the label `respond` chose.
LABEL = st.from_regex(r"[a-z][a-z0-9_]{0,6}", fullmatch=True)
WEIGHT = st.one_of(
    st.floats(min_value=-5, max_value=1e9, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-3, max_value=10**6),
    st.just(0.0),
)


@st.composite
def questions(draw: st.DrawFn) -> dict[str, SpecQuestion]:
    keys = draw(st.lists(LABEL, min_size=1, max_size=6, unique=True))
    asked = {}
    for key in keys:
        if draw(st.booleans()):
            criteria = {"yes": "Yes.", "no": "No."}
            asked[key] = SpecQuestion.model_validate(
                {"type": "boolean", "instructions": "?", "criteria": criteria}
            )
        else:
            labels = draw(st.lists(LABEL, min_size=2, max_size=12, unique=True))
            asked[key] = SpecQuestion.model_validate(
                {"instructions": "?", "criteria": {label: "A criterion." for label in labels}}
            )
    return asked


@st.composite
def an_answer(draw: st.DrawFn, labels: list[str]) -> Any:
    if draw(st.booleans()):
        label = draw(st.sampled_from(labels))
        return (label, draw(st.floats(min_value=1 / len(labels), max_value=1.0)))
    weights = draw(st.dictionaries(st.sampled_from(labels), WEIGHT))
    weights[draw(st.sampled_from(labels))] = draw(st.floats(min_value=1e-9, max_value=1e9))
    return weights


@settings(derandomize=True, max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])  # fmt: skip
@given(st.data())
def test_every_answer_respond_builds_passes_the_engines_validation(data: st.DataObject) -> None:
    asked = data.draw(questions())
    case = Case.parse(request_body("p/a", "slot", asked, {"text": "case"}))
    answers = {key: data.draw(an_answer(list(q.labels))) for key, q in asked.items()}
    rationales = data.draw(st.dictionaries(st.sampled_from(list(asked)), st.text(max_size=700)))
    shown = respond(case, answers, rationales=rationales, model="m", usage={"input_tokens": 1})
    validated = validate_response(shown, asked)
    for key, given_answer in answers.items():
        label = validated["answers"][key]["label"]
        if isinstance(given_answer, tuple):
            assert label == given_answer[0]
            assert validated["answers"][key]["confidence"] == pytest.approx(given_answer[1])
        else:
            probabilities = validated["answers"][key]["probabilities"]
            assert probabilities[label] == pytest.approx(max(probabilities.values()), abs=1e-5)
            assert sum(probabilities.values()) == pytest.approx(1.0)


# -- create_app -----------------------------------------------------------------------------------


def answering(case: Case) -> dict[str, Any]:
    return respond(case, {"request": ("refill", 0.95), "urgent_symptoms": ("no", 0.9)})


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("AGENT_TOKEN", "s3cret-token")
    monkeypatch.delenv("AGENT_ALLOW_OPEN", raising=False)
    return "s3cret-token"


def decide(app: Any, body: Any = None, header: str | bytes | None = None) -> Any:
    headers: dict[bytes, bytes] = {}
    if header is not None:
        headers[b"authorization"] = header.encode() if isinstance(header, str) else header
    return TestClient(app).post(
        "/decide",
        json=body_of() if body is None else body,
        headers=headers,
    )


def test_health_answers_without_a_token(token: str) -> None:
    app = create_app(answering, title="Test", agent_version="2.0.0")
    assert TestClient(app).get("/health").json() == {"status": "ok", "agent_version": "2.0.0"}
    assert TestClient(app).get("/docs").status_code == 404


def test_the_token_guards_decide(token: str) -> None:
    app = create_app(answering)
    assert decide(app).status_code == 401
    assert decide(app, header="Bearer wrong").status_code == 401
    assert decide(app, header="s3cret-token").status_code == 401
    assert decide(app, header="Basic s3cret-token").status_code == 401
    refused = decide(app, header="Bearer s3cret-tokén".encode())
    assert refused.status_code == 401 and refused.json() == {"error": "unauthorized"}
    assert decide(app, header="Bearer s3cret-token").status_code == 200
    answered = decide(app, header="bearer s3cret-token")
    assert answered.json()["answers"]["request"] == {"value": "refill", "confidence": 0.95}


def test_without_a_token_decide_is_closed_unless_open_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ALLOW_OPEN", raising=False)
    app = create_app(answering)
    assert decide(app).status_code == 401
    assert decide(app, header="Bearer anything").status_code == 401
    monkeypatch.setenv("AGENT_ALLOW_OPEN", "true")
    assert decide(app).status_code == 401
    monkeypatch.setenv("AGENT_ALLOW_OPEN", "1")
    assert decide(app).status_code == 200
    # A token, once set, is required even with AGENT_ALLOW_OPEN.
    monkeypatch.setenv("AGENT_TOKEN", "s3cret-token")
    assert decide(app).status_code == 401


AUTH = "Bearer s3cret-token"


def test_a_bad_request_is_refused_before_the_handler_runs(token: str) -> None:
    calls: list[Case] = []

    def handler(case: Case) -> dict[str, Any]:
        calls.append(case)
        return answering(case)

    app = create_app(handler)
    client = TestClient(app)
    headers = {"authorization": AUTH}
    not_json = client.post("/decide", content=b"{not json", headers=headers)
    assert (not_json.status_code, not_json.json()) == (400, {"error": "the request is not JSON"})
    too_big = client.post("/decide", content=b" " * 1_000_001, headers=headers)
    assert too_big.status_code == 413
    no_input = decide(app, {"questions": [ONE]}, AUTH)
    assert (no_input.status_code, no_input.json()) == (400, {"error": "input: an object expected"})
    assert calls == []


def test_a_large_body_is_refused_as_it_arrives_and_deep_json_is_not_json(token: str) -> None:
    """A body with no declared length is read only up to the limit, never held whole; a
    declared length over it is refused before any is read; JSON nested past the parser's depth
    is a 400 like any other request that is not JSON, never a 500."""
    calls: list[Case] = []
    app = create_app(lambda case: calls.append(case) or answering(case))  # type: ignore[func-returns-value]

    # Straight through ASGI (the test client reads a body whole before the app sees it): 6.4 MB
    # sent 64 KB at a time with no content-length.
    received: list[int] = []
    answered: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        received.append(1)
        return {"type": "http.request", "body": b" " * 65_536, "more_body": len(received) < 100}

    async def send(message: MutableMapping[str, Any]) -> None:
        answered.append(dict(message))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/decide",
        "raw_path": b"/decide",
        "query_string": b"",
        "headers": [(b"authorization", AUTH.encode())],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    assert answered[0]["status"] == 413
    assert len(received) < 100  # it stopped reading past the limit

    client = TestClient(app)
    headers = {"authorization": AUTH}
    declared = client.post(
        "/decide", content=b"{}", headers={**headers, "content-length": "1000001"}
    )
    assert declared.status_code == 413
    deep = client.post("/decide", content=b"[" * 200_000 + b"]" * 200_000, headers=headers)
    assert (deep.status_code, deep.json()) == (400, {"error": "the request is not JSON"})
    assert calls == []


def test_an_agent_error_returns_its_status_and_message(token: str) -> None:
    def handler(case: Case) -> dict[str, Any]:
        raise AgentError(422, "the agent cannot answer this kind of case")

    refused = decide(create_app(handler), header=AUTH)
    assert refused.status_code == 422
    assert refused.json() == {"error": "the agent cannot answer this kind of case"}
    # So does one `respond` raises: a missing answer.
    missing = decide(create_app(lambda case: respond(case, {})), header=AUTH)
    assert missing.status_code == 502
    assert missing.json() == {"error": "the agent left 'request' unanswered"}


def test_any_other_exception_is_a_502_naming_only_its_type(token: str) -> None:
    def handler(case: Case) -> dict[str, Any]:
        raise RuntimeError(f"upstream said: patient Jane Doe wrote {case.text()!r}, key sk-ant-99")

    failed = decide(create_app(handler), body_of("Jane Doe here, chest pain."), AUTH)
    assert failed.status_code == 502
    assert failed.json() == {"error": "the agent could not answer (RuntimeError)"}
    for leaked in ("Jane", "chest", "sk-ant", "upstream"):
        assert leaked not in failed.text

    nothing = decide(create_app(lambda case: None), header=AUTH)  # type: ignore[arg-type,return-value]
    assert (nothing.status_code, nothing.json()) == (502, {"error": "the agent returned no answer"})


def test_async_and_sync_handlers_both_answer(token: str) -> None:
    threads: list[str] = []

    async def async_handler(case: Case) -> dict[str, Any]:
        await asyncio.sleep(0)
        threads.append(threading.current_thread().name)
        return answering(case)

    def sync_handler(case: Case) -> dict[str, Any]:
        threads.append(threading.current_thread().name)
        return answering(case)

    class AsyncCallable:
        async def __call__(self, case: Case) -> dict[str, Any]:
            return answering(case)

    for handler in (async_handler, sync_handler, AsyncCallable()):
        answered = decide(create_app(handler), header=AUTH)
        assert answered.status_code == 200, answered.text
        assert answered.json()["answers"]["urgent_symptoms"]["value"] == "no"

    async def failing(case: Case) -> dict[str, Any]:
        raise ValueError("secret detail")

    failed = decide(create_app(failing), header=AUTH)
    assert failed.json() == {"error": "the agent could not answer (ValueError)"}
    # A plain handler runs off the event loop's thread, so a blocking call cannot stall it.
    assert threads[0] != threads[1]
