# SPDX-License-Identifier: Apache-2.0
"""Tests for the first-class AI agent provider (`providers/agent.py`) and `mendari new`."""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import Agent, AuditStore, Decision, load_agent
from mendari.cli import main
from mendari.core.origin import deployment_config
from mendari.core.spec import Outcome, Question
from mendari.errors import AgentValidationError, MendariError, ProviderError
from mendari.evals import evaluate, resolve_dataset
from mendari.evals.behaviour_tests import run_behaviour_tests
from mendari.providers import (
    ENDPOINT_ENVS,
    MODEL_TYPES,
    PROVIDER_CONFIG_MODELS,
    PROVIDER_TYPES,
    ProviderInfo,
    build_provider,
    provider_allow_list,
)
from mendari.providers.agent import (
    CANNED,
    ENDPOINT_ENV,
    TOKEN_ENV,
    AgentConfig,
    AgentProvider,
    validate_response,
)
from mendari.providers.deadline import decision_deadline
from tests.conftest import (
    DEMO_AGENT_DIR,
    LIVE_SETTINGS,
    encrypt_mode,
    encrypted_store,
    message,
    signed_in,
)

ROOT = Path(__file__).resolve().parents[1]
AGENT_URL = "https://agent.test/v1/decide"
CANARY = "canary-agent-secret-token"


@pytest.fixture
def http_agent_dir(tmp_path: Path) -> Path:
    target = tmp_path / "http-agent"
    assert main(["new", str(target), "--id", "clinic/http-agent"]) == 0
    return target


@pytest.fixture
def http_agent(http_agent_dir: Path) -> Agent:
    return load_agent(http_agent_dir)


def agent_response(
    request_label: str = "refill",
    request_conf: float = 0.95,
    urgent: str | bool = "no",
    urgent_conf: float = 0.96,
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "answers": {
            "request": {
                "value": request_label,
                "confidence": request_conf,
                "rationale": "Matched refill intent.",
            },
            "urgent_symptoms": {
                "value": urgent,
                "confidence": urgent_conf,
                "rationale": "No red-flag symptoms present.",
            },
        },
        "model": "claude-sonnet-4-20250514",
        "service_version": "2.1.0",
        "tool_calls": [
            {"name": "ehr_medication_lookup", "status": "ok"},
            "red_flag_checker",
        ],
        "usage": {"input_tokens": 240, "output_tokens": 55, "cost_usd": 0.0018},
    }
    body.update(overrides)
    return body


def make_agent_provider(
    agent: Agent,
    handler: Any | None = None,
    **config_overrides: Any,
) -> AgentProvider:
    slot = agent.manifest.providers["reader"]
    base = dict(slot.variants["agent"].model_extra or {})
    config = {"type": "agent", **base, **config_overrides}
    info = ProviderInfo(slot="reader", variant="agent", type="agent")
    questions = {key: agent.manifest.questions[key] for key in slot.answers}
    client = httpx.Client(transport=httpx.MockTransport(handler)) if handler is not None else None
    return AgentProvider(agent, info, questions, config, client=client)


# -- registry & test isolation --------------------------------------------------------------------


def test_agent_provider_is_registered_as_first_class_model_provider() -> None:
    assert PROVIDER_TYPES["agent"] is AgentProvider
    assert PROVIDER_CONFIG_MODELS["agent"] is AgentConfig
    assert "agent" in MODEL_TYPES
    assert ENDPOINT_ENV in ENDPOINT_ENVS
    assert ENDPOINT_ENV in LIVE_SETTINGS and TOKEN_ENV in LIVE_SETTINGS


# -- offline mock_file execution ------------------------------------------------------------------


def test_offline_mock_file_answers_with_tool_calls_and_rationale(http_agent: Agent) -> None:
    provider = build_provider(http_agent, "reader", "agent")
    provider.preflight()
    output = provider.answer(message("Could you refill my lisinopril? I am running low."))

    assert output.cost_usd == 0.0
    assert output.answers["request"].label == "refill"
    assert output.answers["request"].confidence == pytest.approx(0.95)
    assert sum(output.answers["request"].probabilities.values()) == pytest.approx(1.0)
    assert output.answers["urgent_symptoms"].label == "no"
    assert output.answers["request"].provider.model == "mock-portal-agent"
    assert output.extra["service_id"] == "starter-portal-agent"
    assert output.extra["model"] == "mock-portal-agent"
    assert output.extra["service_version"] == "0.1.0"
    assert output.extra["mock_file"] == "fixtures/mock_agent.json"
    assert output.extra["tool_calls"] == [{"name": "intent_classifier", "status": "ok"}]
    assert "tool:intent_classifier" in output.answers["request"].evidence
    assert any("refill" in ev.lower() for ev in output.answers["request"].evidence)


def test_offline_mock_file_supports_yaml_and_exact_input_match(tmp_path: Path) -> None:
    target = tmp_path / "yaml-agent"
    assert main(["new", str(target), "--id", "clinic/yaml-agent"]) == 0
    yaml_mock = {
        "service_version": "1.0.0",
        "cases": [
            {
                "match": {"input": {"message": {"body": "exact match payload"}}},
                "response": {
                    "answers": {
                        "request": {"value": "appointment", "confidence": 0.92},
                        "urgent_symptoms": {"value": False, "confidence": 0.99},
                    },
                    "tool_calls": ["calendar_lookup"],
                },
            }
        ],
        "default": {
            "answers": {
                "request": {"value": "other", "confidence": 0.6},
                "urgent_symptoms": {"value": "no", "confidence": 0.9},
            }
        },
    }
    (target / "fixtures" / "mock_agent.yaml").write_text(yaml.safe_dump(yaml_mock))
    manifest = yaml.safe_load((target / "agent.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"]["mock_file"] = "fixtures/mock_agent.yaml"
    (target / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    agent = load_agent(target)
    provider = build_provider(agent, "reader", "agent")
    matched = provider.answer(message("exact match payload"))
    assert matched.answers["request"].label == "appointment"
    assert matched.answers["urgent_symptoms"].label == "no"
    assert matched.answers["request"].provider.model == "starter-portal-agent@1.0.0"
    assert "tool:calendar_lookup" in matched.answers["request"].evidence

    fallback = provider.answer(message("something else"))
    assert fallback.answers["request"].label == "other"


def test_a_mock_file_number_too_large_for_a_float_is_the_agents_problem(tmp_path: Path) -> None:
    """YAML reads an integer of a few hundred digits as an `int`, which no float holds: in a
    mock file, as a confidence or a price, it is the agent's problem, listed at load and by
    `mendari validate` and refused when the variant is built, never a raw error."""
    target = tmp_path / "huge-mock"
    assert main(["new", str(target), "--id", "clinic/huge-mock"]) == 0
    manifest = yaml.safe_load((target / "agent.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"]["mock_file"] = "fixtures/mock_agent.yaml"
    (target / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    huge = 10**400
    for response, problem in (
        (agent_response(request_conf=huge), "has malformed confidence"),
        (agent_response(usage={"cost_usd": huge}), "usage cost is malformed"),
    ):
        mock = {"default": response}
        (target / "fixtures" / "mock_agent.yaml").write_text(yaml.safe_dump(mock))
        agent = load_agent(target)  # an agent the deployment owns: warned, not refused
        assert any(problem in warning for warning in agent.warnings)
        assert main(["validate", str(target)]) == 1
        with pytest.raises(AgentValidationError, match=problem):
            build_provider(agent, "reader", "agent")


def test_offline_mock_file_respects_state_fields(tmp_path: Path) -> None:
    target = tmp_path / "filtered-mock"
    assert main(["new", str(target), "--id", "clinic/filtered-mock"]) == 0
    agent = load_agent(target)
    provider = build_provider(agent, "reader", "agent")
    # "chest pain" only appears in an unshared field outside `state_fields`
    output = provider.answer(
        {
            "message": {"body": "Please refill my metformin."},
            "internal_notes": "patient history mentions chest pain last year",
        }
    )
    assert output.answers["request"].label == "refill"
    assert output.answers["urgent_symptoms"].label == "no"


# -- live HTTP agent execution --------------------------------------------------------------------


def test_live_http_agent_request_contract_state_fields_and_metadata(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    monkeypatch.setenv(TOKEN_ENV, "live-agent-secret")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(
        http_agent,
        handler,
        instructions="Prioritize clinical safety over automation.",
    )
    provider.preflight()
    output = provider.answer(
        {
            "message": {"subject": "Rx", "body": "Please refill my lisinopril."},
            "patient": {"ssn": "000-00-0000", "mrn": "MRN-999"},
        }
    )

    assert seen["url"] == AGENT_URL
    assert seen["auth"] == "Bearer live-agent-secret"
    body = seen["body"]
    assert body["agent_id"] == "clinic/http-agent"
    assert body["slot"] == "reader"
    assert body["service_id"] == "starter-portal-agent"
    assert body["instructions"] == "Prioritize clinical safety over automation."
    assert "system_prompt" in body
    # Data minimization: `patient` is excluded by `state_fields: [message.subject, message.body]`
    assert body["input"] == {"message": {"subject": "Rx", "body": "Please refill my lisinopril."}}
    assert "patient" not in body["input"]
    assert "MRN-999" not in json.dumps(body)
    assert "000-00-0000" not in json.dumps(body)
    assert [q["key"] for q in body["questions"]] == ["request", "urgent_symptoms"]
    assert body["questions"][0]["choices"] == ["refill", "appointment", "other"]
    assert body["questions"][1]["type"] == "boolean"

    assert output.input_tokens == 240
    assert output.output_tokens == 55
    assert output.cost_usd == pytest.approx(0.0018)
    assert output.extra["service_id"] == "starter-portal-agent"
    assert output.extra["model"] == "claude-sonnet-4-20250514"
    assert output.extra["service_version"] == "2.1.0"
    assert output.extra["tool_calls"] == [
        {"name": "ehr_medication_lookup", "status": "ok"},
        "red_flag_checker",
    ]
    ans = output.answers["request"]
    assert ans.label == "refill" and ans.confidence == pytest.approx(0.95)
    assert ans.provider.model == "claude-sonnet-4-20250514"
    assert "Matched refill intent." in ans.evidence
    assert "tool:ehr_medication_lookup" in ans.evidence
    assert "tool:red_flag_checker" in ans.evidence


def test_live_agent_handles_boolean_and_score_questions_and_explicit_probabilities() -> None:
    questions = {
        "urgent": Question.model_validate({"type": "boolean", "instructions": "Urgent?"}),
        "severity": Question.model_validate(
            {
                "type": "score",
                "instructions": "Severity level",
                "levels": {
                    "low": {"score": 0, "criterion": "Mild."},
                    "moderate": {"score": 1, "criterion": "Moderate."},
                    "high": {"score": 2, "criterion": "Severe."},
                },
            }
        ),
    }
    raw = {
        "answers": {
            "urgent": {"value": True, "confidence": 0.9},
            "severity": {
                "value": "high",
                "confidence": 0.8,
                "probabilities": {"low": 0.05, "moderate": 0.15, "high": 0.8},
            },
        },
        "model": "gpt-4o",
    }
    parsed = validate_response(raw, questions)
    assert parsed["answers"]["urgent"]["label"] == "yes"
    assert parsed["answers"]["urgent"]["probabilities"] == pytest.approx({"yes": 0.9, "no": 0.1})
    assert parsed["answers"]["severity"]["label"] == "high"
    expected_score = questions["severity"].expected_score(
        parsed["answers"]["severity"]["probabilities"]
    )
    assert expected_score == pytest.approx(0.15 * 1 + 0.8 * 2)


def test_unconfigured_agent_without_mock_or_url_fails_preflight(http_agent: Agent) -> None:
    provider = make_agent_provider(http_agent, mock_file=None)
    with pytest.raises(ProviderError) as error:
        provider.preflight()
    assert error.value.code == "not_configured"
    assert ENDPOINT_ENV in str(error.value)


# -- fail-closed error handling & audit trail -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "credential_unavailable"),
        (403, "credential_unavailable"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
def test_agent_http_errors_raise_audit_safe_provider_error(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    from mendari.providers import http

    monkeypatch.setattr(http, "_sleep", lambda _: None)
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    provider = make_agent_provider(
        http_agent, lambda _: httpx.Response(status, text="leaked-secret-body")
    )
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == code
    assert "leaked-secret" not in str(error.value)


def test_agent_timeout_and_deadline_fail_closed_in_decision_and_audit(
    http_agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)

    def slow_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("agent timed out", request=request)

    def fake_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return slow_handler(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    with AuditStore(tmp_path / "audit.db") as store:
        decision = Decision(http_agent, store=store)
        result = decision.decide(message("Refill my lisinopril please."))
        assert result.outcome is Outcome.ABSTAIN
        assert result.recommendation is None
        assert result.errors == {"request": "timeout", "urgent_symptoms": "timeout"}
        stored = store.get(result.id)
        assert stored is not None
        assert stored["record"]["outcome"] == "ABSTAIN"
        assert stored["record"]["errors"] == {"request": "timeout", "urgent_symptoms": "timeout"}

    # Expired decision deadline refuses before opening any connection
    called = False

    def should_not_run(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(http_agent, should_not_run)
    with decision_deadline(0.0), pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "timeout"
    assert not called


def test_malformed_agent_output_fails_closed_in_decision(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)

    def bad_handler(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answers": {
                    "request": {"value": "hallucinated_label", "confidence": 0.99},
                    "urgent_symptoms": {"value": "no", "confidence": 0.99},
                }
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", bad_handler)
    result = Decision(http_agent).decide(message("Refill my metformin."))
    assert result.outcome is Outcome.ABSTAIN
    assert result.errors == {
        "request": "invalid_response",
        "urgent_symptoms": "invalid_response",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("answers"),
        lambda b: b["answers"].pop("urgent_symptoms"),
        lambda b: b["answers"].update(extra={"value": "x", "confidence": 0.5}),
        lambda b: b["answers"]["request"].update(value="not_a_choice"),
        lambda b: b["answers"]["urgent_symptoms"].update(value="maybe"),
        lambda b: b["answers"]["urgent_symptoms"].update(value=1),
        lambda b: b["answers"]["request"].update(confidence=1.5),
        lambda b: b["answers"]["request"].update(confidence=-0.1),
        lambda b: b["answers"]["request"].update(confidence=True),
        lambda b: b["answers"]["request"].update(
            probabilities={"refill": 0.9, "appointment": 0.9, "other": 0.0}
        ),
        lambda b: b["answers"]["request"].update(
            value="other",
            confidence=0.9,
            probabilities={"refill": 0.9, "appointment": 0.05, "other": 0.05},
        ),
        lambda b: b["answers"]["request"].update(rationale=123),
        lambda b: b.update(model="https://evil.test/model"),
        lambda b: b.update(service_version="bad version!"),
        lambda b: b.update(tool_calls="not-a-list"),
        lambda b: b.update(tool_calls=[123]),
        lambda b: b.update(usage={"input_tokens": -1, "output_tokens": 0}),
        lambda b: b.update(usage={"input_tokens": True, "output_tokens": 0}),
        lambda b: b.update(usage={"input_tokens": 1, "output_tokens": 1, "cost_usd": -0.5}),
    ],
)
def test_agent_response_validation_is_strict(http_agent: Agent, mutate: Any) -> None:
    body = agent_response()
    mutate(body)
    questions = {
        key: http_agent.manifest.questions[key]
        for key in http_agent.manifest.providers["reader"].answers
    }
    with pytest.raises(ProviderError) as error:
        validate_response(body, questions)
    assert error.value.code == "invalid_response"


def test_a_number_too_large_for_a_float_fails_closed_in_decision_and_audit(
    http_agent: Agent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python's `json` reads an integer of a few hundred digits as an `int`, which no float
    holds: as a confidence, a probability or a price it is a malformed answer
    (`invalid_response`), as the float `1e400` is, so the decision fails closed and is
    recorded, never a crash."""
    huge = 10**400
    questions = {
        key: http_agent.manifest.questions[key]
        for key in http_agent.manifest.providers["reader"].answers
    }
    for mutate in (
        lambda b: b["answers"]["request"].update(confidence=huge),
        lambda b: b["answers"]["urgent_symptoms"].update(probabilities={"yes": huge, "no": 0.96}),
        lambda b: b.update(usage={"input_tokens": 1, "output_tokens": 1, "cost_usd": huge}),
    ):
        body = agent_response()
        mutate(body)
        with pytest.raises(ProviderError) as error:
            validate_response(body, questions)
        assert error.value.code == "invalid_response"

    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    answered = agent_response(request_conf=huge)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda self, request: httpx.Response(200, json=answered),
    )
    with AuditStore(tmp_path / "audit.db") as store:
        result = Decision(http_agent, store=store).decide(message("Refill my metformin."))
        assert result.outcome is Outcome.ABSTAIN
        assert result.errors == {
            "request": "invalid_response",
            "urgent_symptoms": "invalid_response",
        }
        stored = store.get(result.id)
        assert stored is not None and stored["record"]["errors"] == result.errors


def test_agent_rejects_oversized_response(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    provider = make_agent_provider(http_agent, lambda _: httpx.Response(200, content=b" " * 70_000))
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_a_request_json_cannot_carry_is_refused_before_it_is_sent(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch, number: float
) -> None:
    """JSON has no NaN and no infinity: an input holding one, whatever let it through, cannot be
    sent to the agent's service. The provider refuses it (`invalid_input`) and nothing is sent,
    never a raw error out of the HTTP client's encoder."""
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(http_agent, handler)
    with pytest.raises(ProviderError) as error:
        provider.answer({"message": {"subject": number, "body": "Refill my metformin."}})
    assert error.value.code == "invalid_input" and sent == []
    provider.answer(message("Refill my metformin."))  # a request that can be sent still is
    assert len(sent) == 1 and json.loads(sent[0].content)["input"] == message(
        "Refill my metformin."
    )


# -- security, SSRF, allow-list & secret isolation ------------------------------------------------


def test_ssrf_and_provider_allowed_origins_enforced(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plain HTTP to non-localhost is refused
    monkeypatch.setenv(ENDPOINT_ENV, "http://agent.test/v1/decide")
    with pytest.raises(ProviderError) as error:
        make_agent_provider(http_agent, lambda _: httpx.Response(200)).preflight()
    assert error.value.code == "invalid_endpoint"

    # Credentials in URL are refused
    monkeypatch.setenv(ENDPOINT_ENV, "https://user:pass@agent.test/v1/decide")
    with pytest.raises(ProviderError) as error:
        make_agent_provider(http_agent, lambda _: httpx.Response(200)).preflight()
    assert error.value.code == "invalid_endpoint"

    # Localhost HTTP is allowed (local agent server)
    monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:8080/decide")
    make_agent_provider(http_agent, lambda _: httpx.Response(200)).preflight()

    # MENDARI_PROVIDER_ALLOWED_ORIGINS enforced at startup (`provider_allow_list`) and call time
    monkeypatch.setenv(ENDPOINT_ENV, "https://blocked.test/v1/decide")
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", "https://allowed.test")
    with pytest.raises(MendariError, match=ENDPOINT_ENV):
        provider_allow_list()

    called = False

    def spy(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(http_agent, spy)
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "invalid_endpoint"
    assert not called

    # Allowed origin succeeds
    monkeypatch.setenv(ENDPOINT_ENV, "https://allowed.test/v1/decide")
    assert provider_allow_list() == ["https://allowed.test"]
    out = provider.answer(message("hello"))
    assert out.answers["request"].label == "refill" and called


def test_agent_endpoint_never_receives_deployment_agent_token(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even on an owned agent, `MENDARI_AGENT_TOKEN` is only sent to `MENDARI_AGENT_URL`,
    never to an agent-specified `endpoint`."""
    monkeypatch.setenv(TOKEN_ENV, CANARY)
    monkeypatch.setenv(ENDPOINT_ENV, "https://deployment.test/decide")
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(
        http_agent, handler, endpoint="https://custom-agent-agent.test/decide"
    )
    out = provider.answer(message("refill my pills"))
    assert out.answers["request"].label == "refill"
    assert "authorization" not in {k.lower() for k in seen_headers}
    assert CANARY not in json.dumps(seen_headers)


def test_bounded_agent_cannot_set_endpoint_or_steal_agent_token(
    http_agent_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untrusted/bounded agents cannot set `endpoint`, must declare `state_fields`, have
    `timeout_seconds` capped, and can never exfiltrate `MENDARI_AGENT_TOKEN`."""
    manifest = yaml.safe_load((http_agent_dir / "agent.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"]["endpoint"] = "https://evil.test/steal"
    (http_agent_dir / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    # Refused at load for a bounded (hub) agent
    with pytest.raises(AgentValidationError, match="may not set endpoint"):
        load_agent(http_agent_dir, origin="hub")

    # `deployment_config` also strips `endpoint` and caps `timeout_seconds`
    cleaned = deployment_config(
        {
            "type": "agent",
            "endpoint": "https://evil.test/steal",
            "timeout_seconds": 90.0,
            "state_fields": ["message.body"],
        }
    )
    assert "endpoint" not in cleaned
    assert cleaned["timeout_seconds"] == 15.0

    # Even if a bounded agent is loaded cleanly and run against deployment's MENDARI_AGENT_URL,
    # no request ever goes to evil.test
    manifest["providers"]["reader"]["variants"]["agent"].pop("endpoint")
    (http_agent_dir / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    bounded_agent = load_agent(http_agent_dir, origin="hub")
    assert bounded_agent.bounded

    # Direct constructor also refuses `endpoint` on bounded agent
    with pytest.raises(AgentValidationError, match="may not set endpoint"):
        make_agent_provider(bounded_agent, endpoint="https://evil.test/steal")

    # Bounded agent without `state_fields` is refused at load
    manifest["providers"]["reader"]["variants"]["agent"].pop("state_fields")
    (http_agent_dir / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(AgentValidationError, match="must name its state_fields"):
        load_agent(http_agent_dir, origin="hub")


@pytest.mark.parametrize("field", ["endpoint_env", "token_env", "api_key_env"])
def test_http_agent_may_not_choose_deployment_env_vars(http_agent_dir: Path, field: str) -> None:
    manifest = yaml.safe_load((http_agent_dir / "agent.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"][field] = "AGENT_SAFETY_CANARY"
    (http_agent_dir / "agent.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(AgentValidationError, match=f"Remove: {field}"):
        load_agent(http_agent_dir)


# -- end-to-end `mendari new` ---------------------------------------------------------------------


def test_mendari_new_agent_end_to_end_offline_and_live_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "clinic-agent"
    assert (
        main(
            [
                "new",
                str(target),
                "--id",
                "my-clinic/clinic-agent",
                "--publisher",
                "My Clinic",
            ]
        )
        == 0
    )

    # 1. Offline: validate, test, eval (agent + rules comparison) all pass with zero sockets
    def no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("offline agent must not open a network socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    assert main(["validate", str(target)]) == 0
    assert main(["test", str(target)]) == 0
    assert main(["eval", str(target), "--dataset", "examples"]) == 0
    assert main(["eval", str(target), "--dataset", "examples", "--provider", "reader=rules"]) == 0

    agent = load_agent(target)
    decision = Decision(agent)
    assert all(outcome.passed for outcome in run_behaviour_tests(decision))
    report = evaluate(decision, resolve_dataset(agent, "examples"))
    assert report["questions"]["request"]["accuracy"] == 1.0
    assert report["questions"]["urgent_symptoms"]["accuracy"] == 1.0

    # 2. Setting MENDARI_AGENT_URL switches the exact same agent to its live service
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    monkeypatch.setenv(TOKEN_ENV, "prod-token")
    live_calls: list[httpx.Request] = []

    def live_transport(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        live_calls.append(request)
        payload = json.loads(request.content)
        text = payload["input"]["message"]["body"].lower()
        urgent = "yes" if "chest pain" in text else "no"
        req = "refill" if "refill" in text else "appointment" if "appointment" in text else "other"
        return httpx.Response(200, json=agent_response(request_label=req, urgent=urgent))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", live_transport)
    assert main(["test", str(target)]) == 0
    assert len(live_calls) == 3
    assert all(str(r.url) == AGENT_URL for r in live_calls)
    assert all(r.headers.get("authorization") == "Bearer prod-token" for r in live_calls)


def test_the_landing_pages_example_is_the_demo_agents_decision() -> None:
    """The public page's example decision and the demo agent's figure (web/index.html) are typed
    in by hand: the page names the bundled agent's current version, and shows what that version
    decides on the message and how many of its bundled examples it gets right."""
    agent = load_agent(ROOT / "agents" / "portal-message-agent")
    decision = Decision(agent)
    site = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    figure = site[site.index('<figure class="decision"') : site.index("</figure>")]
    named = re.search(r"mendari/portal-message-agent (\S+) · offline mock agent", figure)
    assert agent.id == "mendari/portal-message-agent"
    assert named is not None and named.group(1) == agent.version
    body = re.search(r"<blockquote>“(.+?)”</blockquote>", figure)
    assert body is not None
    shown = dict(re.findall(r"<div><dt>([\w ]+)</dt><dd>(\w+)</dd></div>", figure))
    result = decision.decide(message(body.group(1)))
    assert {question: answer.label for question, answer in result.answers.items()} == {
        "request": shown["Request"],
        "urgent_symptoms": shown["Urgent symptoms"],
    }
    assert f'<span class="chip chip-review">{result.outcome.value}</span>' in figure
    assert f"Suggested: {result.recommendation} · {result.priority.value}</span>" in figure
    assert result.confidence is not None
    assert f"<b>{result.confidence:.2f} sure</b>" in figure
    assert f"Confident ({result.confidence:.2f})" in figure
    assert result.rule == "same-day-symptoms"

    right = evaluate(decision, resolve_dataset(agent, "examples"))["proportions"]["accuracy"]
    assert f'<span class="k">{right["k"]} / {right["n"]}</span>' in site


# -- canned answers never automate on a server with sign-in (owner decision, 2026-11-22) ---------

REFILL = message("Could you refill my lisinopril? I am running low.")
CANNED_REASON = "canned answers: MENDARI_AGENT_URL is not set; a person decides"


def test_canned_answers_are_said_to_be_canned_and_live_ones_are_not(
    http_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    assert CANNED == "canned answers: MENDARI_AGENT_URL is not set"
    assert make_agent_provider(http_agent).answer(message("refill please")).canned == CANNED
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    live = make_agent_provider(
        http_agent, lambda request: httpx.Response(200, json=agent_response())
    )
    assert live.answer(message("refill please")).canned is None


def test_with_the_canned_cap_the_demo_agents_routine_refill_goes_to_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap only lowers automation: the same answers, recommendation and priority, with the
    reason; without it (`mendari demo`, tests, open mode) the refill is AUTO as before."""
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    agent = load_agent(DEMO_AGENT_DIR)
    before = Decision(agent).decide(REFILL)
    assert before.outcome is Outcome.AUTO and CANNED_REASON not in before.reasons
    capped = Decision(agent, canned_cap=True).decide(REFILL)
    assert capped.outcome is Outcome.HUMAN_REVIEW
    assert capped.recommendation == before.recommendation == "refill"
    assert capped.priority == before.priority
    assert capped.reasons == (*before.reasons, CANNED_REASON)
    # Already going to a person: the outcome is the rule's, and the reason says it is canned.
    urgent = message("Please refill my inhaler. Also I have had chest pain since last night.")
    review = Decision(agent, canned_cap=True).decide(urgent)
    assert review.outcome is Outcome.HUMAN_REVIEW and review.rule == "same-day-symptoms"
    assert review.reasons[-1] == CANNED_REASON


def test_with_the_canned_cap_a_live_agent_still_automates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    monkeypatch.setattr(
        "mendari.providers.agent.post_json", lambda *args, **kwargs: agent_response()
    )
    result = Decision(load_agent(DEMO_AGENT_DIR), canned_cap=True).decide(REFILL)
    assert result.outcome is Outcome.AUTO and result.recommendation == "refill"
    assert not any(reason.startswith("canned answers") for reason in result.reasons)


def test_a_server_with_sign_in_never_automates_on_canned_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production (sign-in on) without MENDARI_AGENT_URL: the bundled agent's routine refill is
    HUMAN_REVIEW, and the decision record and its audit trail say why. In open mode, as on a
    laptop running the dashboard demo, it is AUTO as before."""
    from fastapi.testclient import TestClient

    from mendari.server.app import create_app
    from tests.conftest import DASHBOARD

    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    route = "/api/agents/mendari/portal-message-agent/decide"
    agents = [ROOT / "agents"]
    open_mode = TestClient(create_app(db_path=tmp_path / "open.db", agents_paths=agents))
    assert open_mode.post(route, json=REFILL).json()["outcome"] == "AUTO"

    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari-tests")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    db = tmp_path / "signed.db"
    app = create_app(db_path=db, agents_paths=agents)
    with encrypted_store(db) as served:
        headers = signed_in(served, role="reviewer")
    client = TestClient(app, base_url="https://mendari.test", headers=DASHBOARD)
    decided = client.post(route, json=REFILL, headers=headers).json()
    assert decided["outcome"] == "HUMAN_REVIEW", decided["reasons"]
    assert decided["recommendation"] == "refill"
    assert decided["reasons"][-1] == CANNED_REASON
    record = client.get(f"/api/audit/{decided['id']}", headers=headers).json()
    assert record["outcome"] == "HUMAN_REVIEW" and record["review_status"] == "pending"
    assert record["record"]["reasons"][-1] == CANNED_REASON
    assert record["events"][0]["event"] == "decision.created"
