# SPDX-License-Identifier: Apache-2.0
"""The provider egress allow-list (LP-16e, conflict 20): `MENDARI_PROVIDER_ALLOWED_ORIGINS`.

Unset (the default), nothing changes: every provider endpoint comes only from the deployment's
own variables. Set, a server whose LLM or agent endpoint variable names an origin it leaves out
does not start, and every request a remote provider would make to another origin (the AI
Gateway's default included) is refused before a connection or a credential
(`invalid_endpoint`), so the decision goes to a person. Requests are answered by the transport
every httpx client uses, and no socket opens. The decisions are the triage fixture's, its
router an agent service and its screen an LLM."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, CallBudget, Decision, Pack
from mendari.core.spec import Outcome
from mendari.errors import MendariError, ProviderError
from mendari.providers import http, llm
from mendari.providers.agent import ENDPOINT_ENV as AGENT_ENDPOINT_ENV
from mendari.providers.agent import TOKEN_ENV as AGENT_TOKEN_ENV
from mendari.providers.base import Provider
from mendari.server.app import create_app
from tests.conftest import message

ALLOW = "MENDARI_PROVIDER_ALLOWED_ORIGINS"
GATEWAY = "https://ai-gateway.vercel.sh"
AGENT = "https://agent.test"
TEXT = "Can I get a refill of my blood pressure tablets?"
# What the screen's failure records: its two questions, each.
SCREEN_REFUSED = {"urgency": "invalid_endpoint", "abusive": "invalid_endpoint"}


def answer(request: httpx.Request) -> httpx.Response:
    """What an LLM or an agent service answers: every label equally likely."""
    body = json.loads(request.content)
    if "messages" in body:
        asked = body["response_format"]["json_schema"]["schema"]["properties"]
        content = {
            key: {label: 1 / len(spec["properties"]) for label in spec["properties"]}
            for key, spec in asked.items()
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    answers = {
        question["key"]: {"value": question["choices"][0], "confidence": 0.5}
        for question in body["questions"]
    }
    return httpx.Response(200, json={"model": "agent-1", "answers": answers})


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request any httpx client sends, answered by `answer`; a socket refuses to open.
    The deployment has an AI Gateway key and an agent service (an endpoint and a token)."""
    requests: list[httpx.Request] = []

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer(request)

    def no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a request left through a socket")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k-gateway")
    monkeypatch.setenv(llm.MODEL_ENV, "provider/model-1")  # the deployment's (no built-in default)
    monkeypatch.setenv(AGENT_ENDPOINT_ENV, f"{AGENT}/v1/decide")
    monkeypatch.setenv(AGENT_TOKEN_ENV, "k-agent")
    return requests


@pytest.fixture
def charged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The provider types `charge()` is called for; each call still reaches the budget."""
    calls: list[str] = []
    charge = Provider.charge

    def spy(self: Provider) -> None:
        calls.append(self.info.type)
        charge(self)

    monkeypatch.setattr(Provider, "charge", spy)
    return calls


def origins(requests: list[httpx.Request]) -> list[str]:
    return [f"{r.url.scheme}://{r.url.netloc.decode()}" for r in requests]


def remote(pack: Pack) -> Decision:
    """The triage fixture with its agent service for a router (asked first) and an LLM screen
    (on the AI Gateway unless LLM_BASE_URL says otherwise)."""
    return Decision(pack, providers={"router": "agent", "screen": "llm"})


def test_no_allow_list_keeps_todays_endpoints(
    sent: list[httpx.Request], triage_agent: Pack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Conflict 20's default: unset, or blank, the calls go where they went before, and the
    server starts whatever endpoint the deployment set."""
    for unset in (None, " "):
        if unset is None:
            monkeypatch.delenv(ALLOW, raising=False)
        else:
            monkeypatch.setenv(ALLOW, unset)
        before = len(sent)
        result = remote(triage_agent).decide(message(TEXT))
        assert result.errors == {}
        assert origins(sent[before:]) == [AGENT, GATEWAY]
    monkeypatch.setenv(llm.BASE_URL_ENV, "https://models.test/v1")
    app = create_app(db_path=tmp_path / "a.db")
    assert TestClient(app).get("/api/health").json()["provider_allowed_origins"] is None


def test_a_call_outside_the_allow_list_is_refused_before_it_leaves(
    sent: list[httpx.Request],
    charged: list[str],
    triage_agent: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AI Gateway's default is not on the list: the LLM screen is refused before its key is
    read or anything is sent, recorded as `invalid_endpoint`, and nothing is automated. The
    agent service, listed, still answers. An endpoint a command finds set after start-up, the
    agent service off the list (before its token is sent or a call charged), and a list a
    command cannot read, likewise."""
    monkeypatch.setenv(ALLOW, AGENT)
    result = remote(triage_agent).decide(message(TEXT))
    assert result.errors == SCREEN_REFUSED
    assert result.outcome is not Outcome.AUTO
    assert origins(sent) == [AGENT]

    screen = Decision(triage_agent, providers={"screen": "llm"})
    monkeypatch.setenv(llm.BASE_URL_ENV, "https://models.test/v1")
    monkeypatch.setenv(llm.LOCAL_KEY_ENV, "k-models")
    assert screen.decide(message(TEXT)).errors == SCREEN_REFUSED

    monkeypatch.setenv(ALLOW, GATEWAY)
    monkeypatch.delenv(llm.BASE_URL_ENV)
    result = remote(triage_agent).decide(message(TEXT))
    assert result.errors == {"destination": "invalid_endpoint"}
    assert origins(sent) == [AGENT, GATEWAY]  # the screen's call, and nothing to the agent's

    monkeypatch.setenv(ALLOW, "ai-gateway.vercel.sh")  # no scheme: not an origin
    result = screen.decide(message(TEXT))
    assert result.errors == SCREEN_REFUSED
    assert result.outcome is not Outcome.AUTO

    monkeypatch.setenv(ALLOW, GATEWAY)  # the last guard, below every provider's own check
    with pytest.raises(ProviderError) as refused:
        http.post_json(None, "https://elsewhere.test/v1", {}, headers={}, timeout_s=1)
    assert refused.value.code == "invalid_endpoint"
    assert len(sent) == 2
    assert charged == ["agent", "llm"]  # a call refused is never charged


def test_an_llm_off_the_allow_list_reads_no_key_and_spends_no_budget(
    sent: list[httpx.Request],
    charged: list[str],
    triage_agent: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list names nothing for the LLM, at the AI Gateway's default or at an `LLM_BASE_URL`
    a command finds set: its own check refuses the call before its key is read (the gateway
    key, or `LLM_API_KEY`) and before the day's LLM budget is charged. Listed, the same
    decision reads the key, charges one call and asks the gateway."""
    keys: list[str] = []
    token = llm.LlmProvider._token

    def key(self: llm.LlmProvider) -> str | None:
        keys.append(self.base_url)
        return token(self)

    monkeypatch.setattr(llm.LlmProvider, "_token", key)
    monkeypatch.setenv(llm.LOCAL_KEY_ENV, "k-models")
    monkeypatch.setenv(ALLOW, AGENT)
    with AuditStore(":memory:") as store:
        screen = Decision(
            triage_agent,
            providers={"screen": "llm"},
            budget=CallBudget(store, {"llm": None}),
        )
        for base_url in (None, "https://models.test/v1"):
            if base_url is not None:
                monkeypatch.setenv(llm.BASE_URL_ENV, base_url)
            result = screen.decide(message(TEXT))
            assert result.errors == SCREEN_REFUSED
            assert result.outcome is not Outcome.AUTO
            assert sent == [] and keys == [] and charged == []
            assert store.usage("llm") == 0

        monkeypatch.delenv(llm.BASE_URL_ENV)
        monkeypatch.setenv(ALLOW, GATEWAY)
        assert screen.decide(message(TEXT)).errors == {}
        assert origins(sent) == [GATEWAY]
        assert keys == [llm.GATEWAY_URL] and charged == ["llm"]
        assert store.usage("llm") == 1


@pytest.mark.parametrize("variable", [llm.BASE_URL_ENV, AGENT_ENDPOINT_ENV])
def test_a_provider_endpoint_outside_the_allow_list_stops_the_start(
    variable: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An endpoint variable the deployment set, off the list, stops the server at start-up,
    naming the variable and never its value; listed, it starts. A malformed list stops it too.
    The AI Gateway's default, which nothing may use, stops nothing: it is refused when called."""
    monkeypatch.setenv(variable, "https://a.test/v1")
    monkeypatch.setenv(ALLOW, "https://b.test")
    with pytest.raises(MendariError) as stopped:
        create_app(db_path=tmp_path / "a.db")
    assert variable in str(stopped.value) and "a.test" not in str(stopped.value)

    monkeypatch.setenv(ALLOW, "https://b.test, https://a.test")
    create_app(db_path=tmp_path / "b.db")
    monkeypatch.setenv(ALLOW, "a.test")
    with pytest.raises(MendariError, match=ALLOW):
        create_app(db_path=tmp_path / "c.db")
    monkeypatch.delenv(variable)
    monkeypatch.setenv(ALLOW, "https://b.test")
    create_app(db_path=tmp_path / "d.db")


def test_health_reports_the_provider_allow_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Signed-in health shows the list as read at start-up (sorted; null when unset), beside the
    FHIR allow-list."""
    monkeypatch.setenv(ALLOW, "https://b.test, https://a.test/")
    health = TestClient(create_app(db_path=tmp_path / "a.db")).get("/api/health").json()
    assert health["provider_allowed_origins"] == ["https://a.test", "https://b.test"]
    monkeypatch.delenv(ALLOW)
    health = TestClient(create_app(db_path=tmp_path / "b.db")).get("/api/health").json()
    assert health["provider_allowed_origins"] is None
