# SPDX-License-Identifier: Apache-2.0
"""The model-backed demo agent (examples/claude-agent): Mendari's agent contract, answered by
Claude. The model is a fake here (no network, no key); what is checked is everything around it:
the request it makes, the answers it builds, that Mendari's own validation accepts them, and
that the engine governs them end to end."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import agent_kit
from mendari.agent_kit import Case
from mendari.core.agent import load_agent
from mendari.core.decision import Decision
from mendari.providers.agent import ENDPOINT_ENV, TOKEN_ENV, request_body, validate_response

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "claude-agent"
DEMO = ROOT / "agents" / "portal-message-agent"
URL = "https://demo-agent.test/decide"
REQUESTS = ("refill", "appointment", "other")


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The example carries a copy of the kit (mendari_agent_kit.py, the same bytes as
# mendari.agent_kit: tests/test_agent_starters.py), so it deploys alone; here it is the kit.
sys.modules["mendari_agent_kit"] = agent_kit
agent = _module("agent", EXAMPLE / "agent.py")
service = _module("claude_agent_service", EXAMPLE / "api" / "index.py")


class FakeMessages:
    """Claude, faked: reads the case from the prompt and answers per question by keyword, with
    probabilities that need normalizing (they sum to 2)."""

    def __init__(self, **reply: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = kwargs["messages"][0]["content"]
        case = re.search(r"<case>\n(.*)\n</case>", content, re.S)
        text = json.loads(case.group(1))["message"]["body"].lower() if case else ""
        urgent = any(w in text for w in ("chest", "breath", "elephant"))
        request = "refill" if "refill" in text else "appointment" if "appoint" in text else "other"
        answer = {
            "request": {
                "probabilities": {c: (1.9 if c == request else 0.05) for c in REQUESTS},
                "rationale": "Asked for " + request + ".",
            },
            "urgent_symptoms": {
                "probabilities": {"yes": 1.8 if urgent else 0.1, "no": 0.2 if urgent else 1.9},
                "rationale": "Same-day symptom described." if urgent else "No symptom described.",
            },
        }  # fmt: skip
        return SimpleNamespace(
            stop_reason=self.reply.get("stop_reason", "end_turn"),
            content=[SimpleNamespace(type="text", text=self.reply.get("text", json.dumps(answer)))],
            usage=SimpleNamespace(input_tokens=310, output_tokens=60),
            model="claude-opus-5-5",
        )


def case_of(body: str) -> Case:
    return Case.parse(mendari_request(body))


def mendari_request(body: str) -> dict[str, Any]:
    """The body Mendari sends for the demo agent, built by the engine itself."""
    agent = load_agent(DEMO)
    slot = agent.manifest.providers["reader"]
    questions = {key: agent.manifest.questions[key] for key in slot.answers}
    config = slot.variants["agent"].model_extra or {}
    return request_body(
        agent.id,
        "reader",
        questions,
        {"message": {"body": body}},
        service_id=config.get("service_id"),
        system_prompt=config.get("system_prompt"),
        state_fields=config.get("state_fields"),
    )


def test_the_request_to_claude_is_governed_by_its_schema_and_defaults() -> None:
    fake = FakeMessages()
    agent.answer(case_of("Please refill my metformin."), fake, environ={})
    (call,) = fake.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    schema = call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["request", "urgent_symptoms"]
    probabilities = schema["properties"]["urgent_symptoms"]["properties"]["probabilities"]
    assert (
        probabilities["required"] == ["yes", "no"]
        and probabilities["additionalProperties"] is False
    )
    # The system prompt never changes with the case; the case is data, after the questions.
    assert call["system"] == agent.SYSTEM
    content = call["messages"][0]["content"]
    assert content.index("Questions:") < content.index("<case>")
    assert "Operator instructions:" in content


def test_its_answers_pass_mendaris_own_validation() -> None:
    case = case_of("I have had chest pain since last night. Refill too please.")
    shown = agent.answer(case, FakeMessages(), environ={})
    loaded = load_agent(DEMO)
    questions = {key: loaded.manifest.questions[key] for key in ("request", "urgent_symptoms")}
    checked = validate_response(shown, questions)
    assert checked["answers"]["urgent_symptoms"]["label"] == "yes"
    assert checked["answers"]["urgent_symptoms"]["confidence"] == pytest.approx(0.9)
    assert shown["answers"]["request"]["value"] == "refill"
    assert sum(shown["answers"]["request"]["probabilities"].values()) == pytest.approx(1.0)
    assert shown["usage"] == {"input_tokens": 310, "output_tokens": 60, "cost_usd": 0.00244}
    assert shown["model"] == "claude-opus-5-5" and shown["service_version"] == agent.SERVICE_VERSION


@pytest.mark.parametrize(
    ("reply", "says"),
    [
        ({"stop_reason": "refusal"}, "declined"),
        ({"stop_reason": "max_tokens"}, "cut off"),
        ({"text": "not json"}, "no structured answer"),
        ({"text": json.dumps({
            "request": {"probabilities": {"refill": "high"}},
            "urgent_symptoms": {"probabilities": {"yes": 1, "no": 0}},
        })}, "malformed probability"),
        ({"text": json.dumps({"request": {"rationale": "x"}})}, "unanswered"),
        ({"text": json.dumps({
            "request": {"probabilities": {"refill": 0, "appointment": 0, "other": 0}},
            "urgent_symptoms": {"probabilities": {"yes": 1, "no": 0}},
        })}, "no probability mass"),
    ],
)  # fmt: skip
def test_a_bad_model_reply_is_an_error_so_mendari_fails_closed(
    reply: dict[str, Any], says: str
) -> None:
    with pytest.raises(agent.AgentError) as raised:
        agent.answer(case_of("Hello?"), FakeMessages(**reply), environ={})
    assert raised.value.status == 502 and says in raised.value.message


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ([], 400),
        ({"questions": [], "input": {}}, 400),
        ({"questions": [{"key": "a", "choices": ["x", "x"]}], "input": {}}, 400),
        ({"questions": [{"key": "a", "choices": ["x", "y"]}] * 2, "input": {}}, 400),
        ({"questions": [{"key": "a", "choices": ["x", "y"]}], "input": "text"}, 400),
        ({"questions": [{"key": "a", "choices": ["x", "y"]}], "input": {"t": "x" * 100_001}}, 413),
    ],
)
def test_a_malformed_request_is_refused_before_any_model_call(
    body: Any, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_ALLOW_OPEN", "1")
    fake = FakeMessages()
    monkeypatch.setattr(service, "messages", lambda: fake)
    refused = TestClient(service.app).post("/decide", json=body)
    assert refused.status_code == status and fake.calls == []


def test_the_http_service_answers_refuses_and_never_echoes_an_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_TOKEN", "s3cret")
    fake = FakeMessages()
    monkeypatch.setattr(service, "messages", lambda: fake)
    client = TestClient(service.app)
    body = mendari_request("Can I book an appointment for Friday?")
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/decide", json=body).status_code == 401
    answered = client.post("/decide", json=body, headers={"authorization": "Bearer s3cret"})
    assert answered.status_code == 200
    assert answered.json()["answers"]["request"]["value"] == "appointment"

    class Broken:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("upstream said: patient Jane Doe, key sk-ant-123")

    monkeypatch.setattr(service, "messages", lambda: Broken())
    failed = client.post("/decide", json=body, headers={"authorization": "Bearer s3cret"})
    assert failed.status_code == 502
    assert "Jane" not in failed.text and "sk-ant" not in failed.text


def test_the_engine_governs_the_live_agent_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo agent's own manifest, switched to this service by MENDARI_AGENT_URL: Mendari
    calls it with the token, and its policy decides what the answers mean."""
    monkeypatch.setenv(ENDPOINT_ENV, URL)
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    monkeypatch.setenv("AGENT_TOKEN", "s3cret")
    fake = FakeMessages()
    monkeypatch.setattr(service, "messages", lambda: fake)
    client = TestClient(service.app)
    seen: list[httpx.Request] = []

    def to_service(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = client.post(
            "/decide",
            content=request.content,
            headers={"authorization": request.headers.get("authorization", "")},
        )
        return httpx.Response(reply.status_code, content=reply.content)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", to_service)
    decision = Decision(load_agent(DEMO))
    urgent = decision({"message": {"body": "It feels like an elephant is sitting on my chest."}})
    assert (urgent.outcome.value, urgent.priority.value) == ("HUMAN_REVIEW", "urgent")
    routine = decision({"message": {"body": "Could you refill my lisinopril?"}})
    assert (routine.outcome.value, routine.recommendation) == ("AUTO", "refill")
    assert routine.cost_usd == pytest.approx(0.00244)
    assert all(str(r.url) == URL for r in seen)
    assert all(r.headers["authorization"] == "Bearer s3cret" for r in seen)
