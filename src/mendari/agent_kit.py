# SPDX-License-Identifier: Apache-2.0
"""The agent kit: put any model, framework or service behind Mendari's agent contract.

Mendari POSTs each decision's questions and input to an agent (`providers/agent.py`,
`request_body`) and accepts only answers that pass `validate_response`. This module owns both
sides of that contract for the agent's author, so a handler only has to answer:

    from mendari.agent_kit import Case, create_app, respond

    def handler(case: Case) -> dict:
        text = case.text()
        urgent = "chest pain" in text
        return respond(case, {
            "request": ("refill", 0.95) if "refill" in text else ("other", 0.6),
            "urgent_symptoms": {"yes": 0.9, "no": 0.1} if urgent else ("no", 0.95),
        })

    app = create_app(handler, title="My agent", agent_version="1.0.0")

`create_app` serves `POST /decide` (bearer token from `AGENT_TOKEN`) and `GET /health`; run it
with any ASGI server (`uvicorn module:app`). The handler gets a parsed, checked `Case` and
returns what `respond` builds; it may be a plain or an `async` function.

It depends only on the standard library and FastAPI, so a starter can carry a copy of this file
(`mendari_agent_kit.py`) and deploy on its own. It never logs a request's input.
"""

from __future__ import annotations

import hmac
import inspect
import json
import math
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

__all__ = ["AgentError", "Case", "Question", "create_app", "respond"]

# The engine's limits (providers/agent.py and providers/base.py; tests/test_agent_kit.py checks
# they agree): an answer outside them would be refused there, so it is refused here first.
MAX_QUESTIONS = 64
MAX_INPUT_CHARS = 100_000
MAX_BODY_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 65_536
MAX_RATIONALE_CHARS = 500
MAX_TOOL_CALLS = 64
MAX_TOOL_NAME_CHARS = 128
MAX_CALL_TOKENS = 10_000_000
MAX_CALL_COST_USD = 1_000.0
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,127}$")


class AgentError(Exception):
    """A request the agent will not answer, or an answer it cannot give: `status` is the HTTP
    status `create_app` returns and `message` its error text, so neither may hold case text."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Question:
    """One question Mendari asks: answer it with one of `choices`."""

    key: str
    type: str  # "choice", "boolean" (choices yes/no) or "score" (ordered `levels`)
    instructions: str
    choices: tuple[str, ...]
    criteria: dict[str, str]  # each choice's criterion, written for people
    levels: dict[str, Any] | None = None  # a score question's {choice: {score, criterion}}


@dataclass(frozen=True)
class Case:
    """One decision's request, parsed and checked (`Case.parse`)."""

    agent: str | None  # the agent's id in Mendari (`pack_id`)
    slot: str | None
    agent_id: str | None  # the name the manifest gives this agent
    questions: tuple[Question, ...]
    input: dict[str, Any]  # only the fields the manifest's `state_fields` send
    instructions: str | None  # the manifest's `system_prompt` and `instructions`, joined

    @classmethod
    def parse(cls, body: Any) -> Case:
        """The request body as a `Case`; `AgentError` (400, or 413 for a large input) if it is
        not one Mendari would send."""
        if not isinstance(body, dict):
            raise AgentError(400, "the request is not a JSON object")
        items = body.get("questions")
        if not isinstance(items, list) or not 0 < len(items) <= MAX_QUESTIONS:
            raise AgentError(400, f"questions: 1 to {MAX_QUESTIONS} expected")
        questions = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                raise AgentError(400, "each question needs a key")
            choices = item.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) < 2
                or not all(isinstance(choice, str) and choice for choice in choices)
                or len(set(choices)) != len(choices)
            ):
                raise AgentError(400, f"question {item['key']!r}: at least two distinct choices")
            questions.append(_question(item, choices))
        if len({question.key for question in questions}) != len(questions):
            raise AgentError(400, "question keys repeat")
        if not isinstance(body.get("input"), dict):
            raise AgentError(400, "input: an object expected")
        if len(json.dumps(body["input"])) > MAX_INPUT_CHARS:
            raise AgentError(413, "input too large")
        said = [
            body[name].strip()
            for name in ("system_prompt", "instructions")
            if isinstance(body.get(name), str) and body[name].strip()
        ]
        return cls(
            agent=_text(body.get("pack_id")),
            slot=_text(body.get("slot")),
            agent_id=_text(body.get("agent_id")),
            questions=tuple(questions),
            input=body["input"],
            instructions="\n\n".join(said) or None,
        )

    def question(self, key: str) -> Question:
        """The question asked under `key` (`KeyError` if it was not asked)."""
        for question in self.questions:
            if question.key == key:
                return question
        raise KeyError(key)

    def text(self) -> str:
        """Every string in the input, joined by new lines and lower-cased: for keyword rules."""
        return "\n".join(_strings(self.input)).lower()


Probabilities = Mapping[str, float]
Answer = Probabilities | tuple[str, float]


def respond(
    case: Case,
    answers: Mapping[str, Answer],
    *,
    rationales: Mapping[str, str] | None = None,
    model: str | None = None,
    agent_version: str | None = None,
    usage: Mapping[str, Any] | None = None,
    tool_calls: Sequence[str | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The response body for `case`, which Mendari's `validate_response` accepts by construction.

    `answers` has one entry per question asked, and no other, each either
    - `{choice: probability}`: weights over the choices (a choice left out weighs 0, a negative
      weight counts as 0), normalized to sum to 1; the answer is the most probable choice; or
    - `(choice, confidence)`: the confidence at least uniform chance (1 / number of choices)
      and at most 1.
    `rationales` (optional, per question) are kept as evidence, cut to 500 characters, so they
    should never repeat personal details from the case. `usage` is `{input_tokens,
    output_tokens, cost_usd?}`; `tool_calls` are names or objects with a `name`.

    An answer that cannot be given raises `AgentError` (502), which `create_app` returns, so
    Mendari fails closed: the case goes to a person.
    """
    asked = {question.key for question in case.questions}
    if set(answers) - asked:
        raise AgentError(502, "the agent answered a question it was not asked")
    body: dict[str, Any] = {"answers": {}}
    for question in case.questions:
        if question.key not in answers:
            raise AgentError(502, f"the agent left {question.key!r} unanswered")
        item = _answer(question, answers[question.key])
        rationale = (rationales or {}).get(question.key)
        if isinstance(rationale, str) and rationale.strip():
            item["rationale"] = " ".join(rationale.split())[:MAX_RATIONALE_CHARS]
        body["answers"][question.key] = item
    for name, value in (("model", model), ("agent_version", agent_version)):
        if value is not None:
            if not isinstance(value, str) or "://" in value or not ID_PATTERN.match(value):
                raise AgentError(502, f"the agent's {name} is not a valid id")
            body[name] = value
    if usage is not None:
        body["usage"] = _usage(usage)
    if tool_calls:
        body["tool_calls"] = _tool_calls(tool_calls)
    try:
        size = len(json.dumps(body, allow_nan=False).encode())
    except (TypeError, ValueError):
        raise AgentError(502, "the agent's answer is not plain JSON") from None
    if size > MAX_RESPONSE_BYTES:
        raise AgentError(502, "the agent's answer is larger than Mendari accepts")
    return body


Handler = Callable[[Case], dict[str, Any] | Awaitable[dict[str, Any]]]


def create_app(
    handler: Handler,
    *,
    title: str = "Mendari agent",
    agent_version: str | None = None,
) -> FastAPI:
    """An HTTP service for `handler`: `POST /decide` and `GET /health`.

    `/decide` needs `Authorization: Bearer <AGENT_TOKEN>` (Mendari sends its
    `MENDARI_AGENT_TOKEN`); with `AGENT_TOKEN` unset every request is refused (401) unless
    `AGENT_ALLOW_OPEN=1`, for local development only. The body becomes a `Case`, the handler's
    return value (`respond(...)`) the response. An `AgentError` returns its status and message;
    any other exception returns 502 naming only the exception's type, never its message, which
    may hold case text or a credential.
    """
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    # An `async def` handler runs on the event loop; a plain one in a worker thread, so a
    # blocking model call never stalls the server.
    is_async = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        type(handler).__call__
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "agent_version": agent_version}

    @app.post("/decide")
    async def decide(request: Request) -> JSONResponse:
        if not _authorized(request.headers.get("authorization")):
            return _error(401, "unauthorized")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "the request is too large")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "the request is not JSON")
        try:
            case = Case.parse(body)
            if is_async:
                result: Any = await handler(case)  # type: ignore[misc]
            else:
                result = await run_in_threadpool(handler, case)
                if inspect.isawaitable(result):
                    result = await result
            if not isinstance(result, dict):
                raise AgentError(502, "the agent returned no answer")
            return JSONResponse(result)
        except AgentError as error:
            return _error(error.status, error.message)
        except Exception as error:  # its message may hold case text or a credential
            return _error(502, f"the agent could not answer ({type(error).__name__})")

    return app


# -- helpers --------------------------------------------------------------------------------------


def _authorized(header: str | None, environ: Mapping[str, str] = os.environ) -> bool:
    token = environ.get("AGENT_TOKEN", "").strip()
    if not token:
        return environ.get("AGENT_ALLOW_OPEN", "") == "1"
    scheme, _, given = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        given.strip().encode(), token.encode()
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _question(item: dict[str, Any], choices: list[str]) -> Question:
    criteria = item.get("criteria")
    levels = item.get("levels")
    return Question(
        key=item["key"],
        type=_text(item.get("type")) or "choice",
        instructions=_text(item.get("instructions")) or "",
        choices=tuple(choices),
        criteria=(
            {label: text for label, text in criteria.items() if isinstance(text, str)}
            if isinstance(criteria, dict)
            else {}
        ),
        levels=levels if isinstance(levels, dict) else None,
    )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _answer(question: Question, given: Any) -> dict[str, Any]:
    key = question.key
    if isinstance(given, Mapping):
        if set(given) - set(question.choices):
            raise AgentError(502, f"the answer to {key!r} weighs a label that is not a choice")
        weights = {}
        for choice in question.choices:
            weight = _number(given.get(choice, 0.0))
            if weight is None:
                raise AgentError(502, f"the answer to {key!r} has a malformed probability")
            weights[choice] = max(0.0, weight)
        total = sum(weights.values())
        if total <= 0 or not math.isfinite(total):
            raise AgentError(502, f"the answer to {key!r} has no probability mass")
        probabilities = {choice: round(w / total, 6) for choice, w in weights.items()}
        value = max(question.choices, key=lambda choice: probabilities[choice])
        return {"value": value, "probabilities": probabilities}
    if isinstance(given, tuple | list) and len(given) == 2:
        label, confidence = given[0], _number(given[1])
        if not isinstance(label, str) or label not in question.choices:
            raise AgentError(502, f"the answer to {key!r} is not one of its choices")
        if confidence is None or not 1.0 / len(question.choices) <= confidence <= 1.0:
            raise AgentError(
                502, f"the answer to {key!r} needs a confidence from uniform chance to 1"
            )
        return {"value": label, "confidence": confidence}
    raise AgentError(
        502, f"the answer to {key!r} is neither probabilities nor (choice, confidence)"
    )


def _usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    tokens = [usage.get("input_tokens", 0), usage.get("output_tokens", 0)]
    if (
        not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in tokens)
        or sum(tokens) > MAX_CALL_TOKENS
    ):
        raise AgentError(502, "the agent's usage is malformed")
    shown: dict[str, Any] = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
    if usage.get("cost_usd") is not None:
        cost = _number(usage["cost_usd"])
        if cost is None or not 0 <= cost <= MAX_CALL_COST_USD:
            raise AgentError(502, "the agent's cost is malformed")
        shown["cost_usd"] = cost
    return shown


def _tool_calls(calls: Sequence[str | Mapping[str, Any]]) -> list[Any]:
    if isinstance(calls, str) or len(calls) > MAX_TOOL_CALLS:
        raise AgentError(502, f"tool_calls: at most {MAX_TOOL_CALLS}")
    shown: list[Any] = []
    for call in calls:
        if isinstance(call, str) and call.strip():
            shown.append(call.strip()[:MAX_TOOL_NAME_CHARS])
        elif isinstance(call, Mapping):
            shown.append(dict(call))
        else:
            raise AgentError(502, "each tool call is a name or an object")
    return shown
