# SPDX-License-Identifier: Apache-2.0
"""The agent kit (mendari.agent_kit): the agent's side of Mendari's agent contract. A request
Mendari would not send is refused, every answer `respond` builds passes the engine's own
`validate_response`, and the HTTP app guards its token and never leaks an error's message."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mendari import agent_kit
from mendari.agent_kit import (
    Action,
    AgentError,
    Authorization,
    Case,
    authorize,
    create_app,
    respond,
)
from mendari.core import actions as engine_actions
from mendari.core import spec
from mendari.core.spec import ActionSpec
from mendari.core.spec import Question as SpecQuestion
from mendari.errors import ProviderError
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
        service_id="my-agent",
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
    assert agent_kit.ID_PATTERN.pattern == provider.SERVICE_ID_PATTERN
    assert agent_kit.MAX_CALL_TOKENS == base.MAX_CALL_TOKENS
    assert agent_kit.MAX_CALL_COST_USD == base.MAX_CALL_COST_USD
    # Proposed actions (spec §13.6): the engine's own limits and its name's shape.
    assert agent_kit.MAX_ACTIONS == engine_actions.MAX_PROPOSED == spec.MAX_ACTIONS
    assert agent_kit.MAX_ARGUMENT_CHARS == engine_actions.MAX_ARGUMENT_CHARS
    assert agent_kit.MAX_ARGUMENT_DEPTH == engine_actions.MAX_ARGUMENT_DEPTH
    assert agent_kit.ACTION_NAME.pattern == engine_actions.NAME.pattern


# -- Case.parse -----------------------------------------------------------------------------------


def test_a_case_holds_what_the_engine_sent() -> None:
    case = Case.parse(body_of(system_prompt=" Be careful. ", instructions="Be brief."))
    assert (case.agent_id, case.slot, case.service_id) == (
        "my-clinic/my-agent",
        "reader",
        "my-agent",
    )
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
        (None, {"service_version": "1.0 beta"}, "the agent's service_version is not a valid id"),
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


@pytest.mark.parametrize("name", ["model", "service_version"])
def test_an_id_with_a_final_newline_is_refused_as_the_engine_refuses_it(name: str) -> None:
    """A version read from a file keeps its newline (`open("VERSION").read()`). The kit took
    it, as `$` also matches before a final newline; the engine refused every answer that
    carried it, so every case went to a person. The kit refuses it where it is made."""
    case = Case.parse(body_of())
    given = {"request": ("refill", 0.9), "urgent_symptoms": ("no", 0.9)}
    read: dict[str, Any] = {name: "1.0.0\n"}
    with pytest.raises(ProviderError, match=f"malformed {name}"):
        validate_response({**respond(case, given), **read}, QUESTIONS)
    with pytest.raises(AgentError) as raised:
        respond(case, given, **read)
    said = (raised.value.status, raised.value.message)
    assert said == (502, f"the agent's {name} is not a valid id")
    stripped: dict[str, Any] = {name: "1.0.0"}
    assert checked(case, given, **stripped)[name] == "1.0.0"


def test_provenance_rationales_usage_and_tool_calls_reach_the_engine() -> None:
    case = Case.parse(body_of())
    shown = checked(
        case,
        {"request": ("refill", 0.95), "urgent_symptoms": ("no", 0.9)},
        rationales={"request": "  Asks for\na refill. " + "x" * 600, "urgent_symptoms": " "},
        model="my-model@2026-01",
        service_version="1.4.0",
        usage={"input_tokens": 412, "output_tokens": 38, "cost_usd": 0.0004},
        tool_calls=["  classifier ", {"name": "screen", "status": "ok"}],
    )
    assert shown["answers"]["request"]["rationale"].startswith("Asks for a refill. xx")
    assert len(shown["answers"]["request"]["rationale"]) == 500
    assert shown["answers"]["urgent_symptoms"]["rationale"] is None
    assert (shown["model"], shown["service_version"]) == ("my-model@2026-01", "1.4.0")
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
    app = create_app(answering, title="Test", service_version="2.0.0")
    assert TestClient(app).get("/health").json() == {"status": "ok", "service_version": "2.0.0"}
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


# -- actions (spec §13): what the case offers, what the agent proposes, and asking before running --

DECLARED = {
    "look_up": ActionSpec.model_validate({"description": "Read the list.", "effect": "read"}),
    "send_reply": ActionSpec.model_validate(
        {
            "description": "Send a reply.",
            "effect": "contact",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string", "maxLength": 2000}},
            },
        }
    ),
}
BOTH = {"request": ("refill", 0.95), "urgent_symptoms": ("no", 0.9)}


def offered() -> Case:
    return Case.parse(body_of(actions=DECLARED))


def test_a_case_holds_the_actions_the_manifest_declares() -> None:
    case = offered()
    assert case.actions == (
        Action("look_up", "Read the list.", "read", None),
        Action("send_reply", "Send a reply.", "contact", DECLARED["send_reply"].json_schema),
    )
    assert case.action("send_reply").effect == "contact"
    with pytest.raises(KeyError):
        case.action("delete_chart")
    assert Case.parse(body_of()).actions == ()  # an older manifest's request: none
    for wrong in (
        "send_reply",
        [{"name": "send_reply"}],  # no effect
        [{"name": "Send Reply", "effect": "contact"}],
        [{"effect": "read"}],
        ["send_reply"],
        [{"name": "a", "effect": "read"}] * 2,  # a name twice
        [{"name": f"a{n}", "effect": "read"} for n in range(agent_kit.MAX_ACTIONS + 1)],
    ):
        with pytest.raises(AgentError) as refused:
            Case.parse({**body_of(), "actions": wrong})
        assert refused.value.status == 400


def test_respond_proposes_actions_the_engine_takes() -> None:
    case = offered()
    shown = respond(
        case,
        BOTH,
        actions=[("send_reply", {"message": "Received."}), {"name": "look_up"}],
    )
    assert shown["actions"] == [
        {"name": "send_reply", "arguments": {"message": "Received."}},
        {"name": "look_up", "arguments": {}},
    ]
    validated = validate_response(shown, QUESTIONS, actions=True)
    assert validated["actions"] == shown["actions"]
    assert "actions" not in respond(case, BOTH) and "actions" not in respond(case, BOTH, actions=[])
    # The kit does not check the schema: Mendari does, and blocks that one action.
    unchecked = respond(case, BOTH, actions=[("send_reply", {"message": 5})])
    assert validate_response(unchecked, QUESTIONS, actions=True)["actions"][0]["arguments"] == {
        "message": 5
    }


def nested(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(levels - 1):
        value = {"in": value}
    return value


@pytest.mark.parametrize(
    "proposed",
    [
        [("delete_chart", {})],  # not offered
        [("send_reply", ["a list"])],
        [("send_reply", "text")],
        [("send_reply",)],
        ["send_reply"],
        [{"name": "send_reply", "arguments": {}, "effect": "read"}],
        [{"arguments": {}}],
        [("send_reply", {"message": "x" * agent_kit.MAX_ARGUMENT_CHARS})],
        [("send_reply", nested(agent_kit.MAX_ARGUMENT_DEPTH + 1))],
        [("send_reply", {"message": float("nan")})],
        [("send_reply", {"message": {1, 2}})],
        [("send_reply", {1: "x"})],
        [("look_up", {})] * (agent_kit.MAX_ACTIONS + 1),
        "send_reply",
        {"name": "send_reply"},
    ],
)
def test_a_proposal_the_contract_does_not_allow_is_refused_before_it_is_sent(
    proposed: Any,
) -> None:
    with pytest.raises(AgentError) as refused:
        respond(offered(), BOTH, actions=proposed)
    assert refused.value.status == 502
    assert "x" * 50 not in refused.value.message  # never an argument's value


def test_a_case_that_offers_no_action_proposes_none() -> None:
    with pytest.raises(AgentError, match="does not offer"):
        respond(Case.parse(body_of()), BOTH, actions=[("send_reply", {"message": "x"})])


_arguments = st.dictionaries(
    st.text(max_size=8),
    st.recursive(
        st.one_of(st.none(), st.booleans(), st.integers(-9, 9), st.text(max_size=20)),
        lambda inner: st.one_of(
            st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=5), inner, max_size=3)
        ),
        max_leaves=6,
    ),
    max_size=4,
)


@settings(derandomize=True, max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(list(DECLARED)), _arguments), max_size=agent_kit.MAX_ACTIONS
    ),
    st.booleans(),
)
def test_every_proposal_respond_builds_passes_the_engines_validation(
    proposed: list[tuple[str, dict[str, Any]]], as_objects: bool
) -> None:
    given_actions: list[Any] = (
        [{"name": name, "arguments": arguments} for name, arguments in proposed]
        if as_objects
        else list(proposed)
    )
    shown = respond(offered(), BOTH, actions=given_actions)
    validated = validate_response(shown, QUESTIONS, actions=True)
    assert validated["actions"] == [
        {"name": name, "arguments": arguments} for name, arguments in proposed
    ]
    for item in validated["actions"]:
        assert engine_actions.shape_problem(item["name"], item["arguments"]) is None


class _Answering:
    """A local HTTP server that answers every POST with what it was told to."""

    def __init__(self, status: int = 200, body: Any = None, raw: bytes | None = None) -> None:
        import http.server

        outer = self
        self.seen: list[tuple[str, dict[str, str], Any]] = []
        self.status, self.body, self.raw = status, body, raw

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                sent = json.loads(self.rfile.read(length) or b"null")
                outer.seen.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, sent)
                )
                payload = outer.raw if outer.raw is not None else json.dumps(outer.body).encode()
                self.send_response(outer.status)
                if outer.status in (301, 302, 307):
                    self.send_header("location", "http://127.0.0.1:9/elsewhere")
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Answering:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


RELEASED = {"id": "a-1", "disposition": "released", "reason": "automated", "repeated": False}
UNREACHABLE = Authorization("blocked", "unreachable", None)


def test_authorize_asks_mendari_and_reads_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    with _Answering(body=RELEASED) as mendari:
        found = authorize(
            "my-clinic/my-agent", "d-1", "send_reply", {"message": "Hi"}, url=mendari.url, key="k1"
        )
        assert found == Authorization("released", "automated", "a-1") and found.released
        ((path, headers, sent),) = mendari.seen
        assert path == "/api/agents/my-clinic/my-agent/actions"
        assert headers["authorization"] == "Bearer k1"
        assert sent == {"decision_id": "d-1", "name": "send_reply", "arguments": {"message": "Hi"}}
        # From the environment, with no arguments and no key.
        monkeypatch.setenv("MENDARI_URL", mendari.url + "/")
        monkeypatch.delenv("MENDARI_KEY", raising=False)
        assert authorize("my-clinic/my-agent", "d-1", "look_up").released
        _, headers, sent = mendari.seen[-1]
        assert "authorization" not in headers and sent["arguments"] == {}
    for disposition in ("held", "blocked", "denied"):
        with _Answering(body={**RELEASED, "disposition": disposition, "id": None}) as mendari:
            found = authorize("a/b", "d-1", "look_up", url=mendari.url)
            assert (found.disposition, found.released, found.action_id) == (
                disposition,
                False,
                None,
            )


@pytest.mark.parametrize(
    "answering",
    [
        {"status": 500, "body": RELEASED},
        {"status": 404, "body": {"detail": "no decision"}},
        {"status": 403, "body": RELEASED},
        {"status": 302, "body": RELEASED},  # a redirect is not followed
        {"body": {"id": "a-1", "reason": "automated"}},  # no disposition
        {"body": {"disposition": "approved", "reason": "x"}},  # not one of the four
        {"body": {"disposition": "released"}},  # no reason
        {"body": ["released"]},
        {"raw": b"not json"},
    ],
)
def test_authorize_fails_closed_on_any_answer_it_cannot_trust(answering: dict[str, Any]) -> None:
    with _Answering(**answering) as mendari:
        found = authorize("my-clinic/my-agent", "d-1", "send_reply", url=mendari.url)
    assert found == UNREACHABLE and not found.released


def test_authorize_never_raises_for_a_failure_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_URL", raising=False)
    assert authorize("a/b", "d-1", "look_up") == UNREACHABLE  # no address
    for url in (
        "http://mendari.example.org",  # not https, and not this machine
        "ftp://mendari.example.org",
        "mendari.example.org",
        "https://",
        "http://[::1",  # not a URL
        "http://127.0.0.1:9",  # nothing listens
    ):
        assert authorize("a/b", "d-1", "look_up", url=url, timeout_s=2) == UNREACHABLE, url
    with _Answering(body=RELEASED) as mendari:
        for agent in ("../../admin", "a/b/c", "A/B", "", "a b/c"):
            assert authorize(agent, "d-1", "look_up", url=mendari.url) == UNREACHABLE, agent
        assert authorize("a/b", "d-1", "look_up", {"x": float("nan")}, url=mendari.url) == (
            UNREACHABLE
        )
        assert mendari.seen == []  # none of them was sent


def test_authorize_times_out_closed() -> None:
    import socket

    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)  # accepts, and never answers
        url = f"http://127.0.0.1:{listening.getsockname()[1]}"
        assert authorize("a/b", "d-1", "look_up", url=url, timeout_s=0.3) == UNREACHABLE
