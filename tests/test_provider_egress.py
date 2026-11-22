# SPDX-License-Identifier: Apache-2.0
"""The provider egress allow-list (LP-16e, conflict 20): `MENDARI_PROVIDER_ALLOWED_ORIGINS`.

Unset (the default), nothing changes: every provider endpoint comes only from the deployment's
own variables. Set, a server whose LLM or Jev endpoint variable names an origin it leaves out
does not start, and every request a remote provider would make to another origin (the AI
Gateway's default, Jev's model list and Vercel Connect included) is refused before a connection
or a credential (`invalid_endpoint`), so the decision goes to a person. Requests are answered by
the transport every httpx client uses, and no socket opens."""

from __future__ import annotations

import functools
import itertools
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
from mendari.providers import credentials, http, jev, llm
from mendari.providers.base import Provider
from mendari.providers.credentials import ConnectorToken
from mendari.server.app import create_app
from mendari.settings import default_providers
from tests.conftest import message

ALLOW = "MENDARI_PROVIDER_ALLOWED_ORIGINS"
GATEWAY = "https://ai-gateway.vercel.sh"
JEV = "https://jev.test"
CONNECT = "https://api.vercel.com"
TEXT = "Can I get a refill of my blood pressure tablets?"
_connectors = itertools.count()


def answer(request: httpx.Request) -> httpx.Response:
    """What an LLM, Jev, Jev's model list or Vercel Connect answers: every label equally
    likely, one model, one key."""
    path = request.url.path
    if path.endswith("/v1/models"):
        return httpx.Response(200, json={"models": [{"name": "jev-1", "release_date": "2026"}]})
    if "/connect/token/" in path:
        return httpx.Response(200, json={"token": "k-connect", "expiresAt": 4_102_444_800_000})
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
    answers = {}
    for key, question in body["questions"].items():
        labels = list(question["criteria"])
        answers[key] = {
            "type": "choice",
            "choice": labels[0],
            "confidence": 1 / len(labels),
            "probabilities": {label: 1 / len(labels) for label in labels},
        }
    usage = {"input_tokens": 1, "output_tokens": 0}
    return httpx.Response(200, json={"model": "jev-1", "answers": answers, "usage": usage})


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every request any httpx client sends, answered by `answer`; a socket refuses to open.
    The deployment has an AI Gateway key and Jev (an endpoint, a model and a key)."""
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
    monkeypatch.setenv(jev.ENDPOINT_ENV, f"{JEV}/v1/decide")
    monkeypatch.setenv(jev.MODEL_ENV, "jev-1")
    monkeypatch.setenv(jev.API_KEY_ENV, "k-jev")
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
    """The inbox with an LLM screen (on the AI Gateway unless LLM_BASE_URL says otherwise) and a
    Jev router."""
    return Decision(pack, providers={"safety_screen": "llm", "router": "jev"})


def test_no_allow_list_keeps_todays_endpoints(
    sent: list[httpx.Request], inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Conflict 20's default: unset, or blank, the calls go where they went before, and the
    server starts whatever endpoint the deployment set."""
    for unset in (None, " "):
        if unset is None:
            monkeypatch.delenv(ALLOW, raising=False)
        else:
            monkeypatch.setenv(ALLOW, unset)
        before = len(sent)
        result = remote(inbox_pack).decide(message(TEXT))
        assert result.errors == {}
        assert origins(sent[before:]) == [GATEWAY, JEV]
        assert jev.list_models() == [{"name": "jev-1", "description": None, "release_date": "2026"}]
    monkeypatch.setenv(llm.BASE_URL_ENV, "https://models.test/v1")
    app = create_app(db_path=tmp_path / "a.db", default_providers=default_providers())
    assert TestClient(app).get("/api/health").json()["provider_allowed_origins"] is None


def test_a_call_outside_the_allow_list_is_refused_before_it_leaves(
    sent: list[httpx.Request], inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AI Gateway's default is not on the list: the LLM screen is refused before its key is
    read or anything is sent, recorded as `invalid_endpoint`, and nothing is automated. Jev,
    listed, still answers. An endpoint a command finds set after start-up, Jev off the list
    (before its key comes from Vercel Connect), and a list a command cannot read, likewise."""
    monkeypatch.setenv(ALLOW, JEV)
    result = remote(inbox_pack).decide(message(TEXT))
    assert result.errors == {"urgency": "invalid_endpoint"}
    assert result.outcome is not Outcome.AUTO
    assert origins(sent) == [JEV]

    screen = Decision(inbox_pack, providers={"safety_screen": "llm"})
    monkeypatch.setenv(llm.BASE_URL_ENV, "https://models.test/v1")
    monkeypatch.setenv(llm.LOCAL_KEY_ENV, "k-models")
    assert screen.decide(message(TEXT)).errors == {"urgency": "invalid_endpoint"}

    monkeypatch.setenv(ALLOW, GATEWAY)
    monkeypatch.delenv(llm.BASE_URL_ENV)
    monkeypatch.delenv(jev.API_KEY_ENV)
    monkeypatch.setenv(jev.CONNECTOR_ENV, f"jev/egress-{next(_connectors)}")
    result = remote(inbox_pack).decide(message(TEXT))
    assert result.errors == {"destination": "invalid_endpoint"}
    assert origins(sent) == [JEV, GATEWAY]  # the screen's call, and nothing to Jev or Vercel

    monkeypatch.setenv(ALLOW, "ai-gateway.vercel.sh")  # no scheme: not an origin
    result = screen.decide(message(TEXT))
    assert result.errors == {"urgency": "invalid_endpoint"}
    assert result.outcome is not Outcome.AUTO

    monkeypatch.setenv(ALLOW, GATEWAY)  # the last guard, below every provider's own check
    with pytest.raises(ProviderError) as refused:
        http.post_json(None, "https://elsewhere.test/v1", {}, headers={}, timeout_s=1)
    assert refused.value.code == "invalid_endpoint"
    assert len(sent) == 2


def test_jev_off_the_allow_list_reads_no_connector_key_and_spends_no_budget(
    sent: list[httpx.Request],
    charged: list[str],
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jev's key from Vercel Connect, whose origin is listed, and Jev's endpoint, not listed:
    Jev's own check refuses the call before the connector is asked for a key (no identity token
    read, nothing sent to Vercel) and before the day's Jev budget is charged. `post_json`'s
    last guard would refuse the request too, but only after all of that. Listed, the same
    decision reads the token, fetches the key, charges one call and asks Jev."""
    identities: list[str] = []

    def identity() -> str:
        identities.append("read")
        return "oidc-token"

    monkeypatch.setattr(credentials, "vercel_oidc_token", identity)
    monkeypatch.delenv(jev.API_KEY_ENV)
    monkeypatch.setenv(jev.CONNECTOR_ENV, f"jev/egress-{next(_connectors)}")
    monkeypatch.setenv(ALLOW, f"{GATEWAY},{CONNECT}")
    with AuditStore(":memory:") as store:
        router = Decision(
            inbox_pack, providers={"router": "jev"}, budget=CallBudget(store, {"jev": None})
        )
        result = router.decide(message(TEXT))
        assert result.errors == {"destination": "invalid_endpoint"}
        assert result.outcome is not Outcome.AUTO
        assert sent == [] and identities == [] and charged == []
        assert store.usage("jev") == 0

        monkeypatch.setenv(ALLOW, f"{GATEWAY},{CONNECT},{JEV}")
        assert router.decide(message(TEXT)).errors == {}
        assert origins(sent) == [CONNECT, JEV]
        assert identities == ["read"] and charged == ["jev"]
        assert store.usage("jev") == 1


def test_an_llm_off_the_allow_list_reads_no_key_and_spends_no_budget(
    sent: list[httpx.Request],
    charged: list[str],
    inbox_pack: Pack,
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
    monkeypatch.setenv(ALLOW, f"{JEV},{CONNECT}")
    with AuditStore(":memory:") as store:
        screen = Decision(
            inbox_pack,
            providers={"safety_screen": "llm"},
            budget=CallBudget(store, {"llm": None}),
        )
        for base_url in (None, "https://models.test/v1"):
            if base_url is not None:
                monkeypatch.setenv(llm.BASE_URL_ENV, base_url)
            result = screen.decide(message(TEXT))
            assert result.errors == {"urgency": "invalid_endpoint"}
            assert result.outcome is not Outcome.AUTO
            assert sent == [] and keys == [] and charged == []
            assert store.usage("llm") == 0

        monkeypatch.delenv(llm.BASE_URL_ENV)
        monkeypatch.setenv(ALLOW, GATEWAY)
        assert screen.decide(message(TEXT)).errors == {}
        assert origins(sent) == [GATEWAY]
        assert keys == [llm.GATEWAY_URL] and charged == ["llm"]
        assert store.usage("llm") == 1


def test_jev_models_and_vercel_connect_follow_the_allow_list(
    sent: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jev's model list is read from Jev's origin, and a key from a Vercel Connect connector
    from Vercel's: each only when the list names it. Off it, no identity token is read and
    nothing is sent."""
    monkeypatch.setenv(ALLOW, "https://other.test")
    with pytest.raises(ProviderError) as refused:
        jev.list_models()
    assert refused.value.code == "invalid_endpoint"
    identities: list[str] = []

    def identity() -> str:
        identities.append("read")
        return "oidc-token"

    token = ConnectorToken(f"jev/egress-{next(_connectors)}", oidc=identity)
    with pytest.raises(ProviderError) as refused:
        token.get()
    assert refused.value.code == "invalid_endpoint"
    assert "Vercel Connect" in refused.value.detail
    assert sent == [] and identities == []

    monkeypatch.setenv(ALLOW, f"{JEV},{CONNECT}")
    assert [model["name"] for model in jev.list_models()] == ["jev-1"]
    assert token.get() == "k-connect"
    assert [str(r.url).partition("/v1/")[0] for r in sent] == [JEV, CONNECT]
    assert sent[1].headers["authorization"] == "Bearer oidc-token"


@pytest.mark.parametrize("variable", [llm.BASE_URL_ENV, jev.ENDPOINT_ENV])
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


def test_jev_set_up_off_the_allow_list_stops_the_server_before_its_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mendari serve` and the Vercel app read the Jev defaults before they build the app: Jev
    set up at an origin the list leaves out stops them there, naming JEV_API_URL."""
    monkeypatch.setenv(jev.ENDPOINT_ENV, f"{JEV}/v1/decide")
    monkeypatch.setenv(jev.MODEL_ENV, "jev-1")
    monkeypatch.setenv(jev.API_KEY_ENV, "k-jev")
    monkeypatch.setenv(ALLOW, GATEWAY)
    with pytest.raises(MendariError, match=f"{jev.ENDPOINT_ENV} is not on {ALLOW}"):
        default_providers()
    monkeypatch.setenv(ALLOW, f"{GATEWAY},{JEV}")
    assert default_providers() == {}  # it starts: a release names no Jev defaults (JEV_DEFAULTS)


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


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "[]", "null"])
def test_a_model_list_that_is_not_a_json_object_is_an_invalid_response(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """A 200 with an HTML page or a JSON array raised ValueError or AttributeError out of the
    provider; every provider failure is a ProviderError."""
    monkeypatch.setenv(ALLOW, JEV)
    monkeypatch.setenv(jev.ENDPOINT_ENV, f"{JEV}/v1/decide")
    monkeypatch.setenv(jev.API_KEY_ENV, "k")

    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/html"})

    transport = httpx.MockTransport(odd)
    monkeypatch.setattr(httpx, "Client", functools.partial(httpx.Client, transport=transport))
    with pytest.raises(ProviderError) as refused:
        jev.list_models()
    assert refused.value.code == "invalid_response"
