# SPDX-License-Identifier: Apache-2.0
"""Tests for the first-class AI agent provider (`providers/agent.py`) and `mendari new --agent`."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from mendari import AuditStore, Decision, Pack, load_pack
from mendari.cli import main
from mendari.core.origin import deployment_config
from mendari.core.spec import Outcome, Question
from mendari.errors import MendariError, PackValidationError, ProviderError
from mendari.evals import evaluate, resolve_dataset
from mendari.evals.pack_tests import run_pack_tests
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
    ENDPOINT_ENV,
    TOKEN_ENV,
    AgentConfig,
    AgentProvider,
    validate_response,
)
from mendari.providers.deadline import decision_deadline
from tests.conftest import LIVE_SETTINGS, message

AGENT_URL = "https://agent.test/v1/decide"
CANARY = "canary-agent-secret-token"


@pytest.fixture
def agent_pack_dir(tmp_path: Path) -> Path:
    target = tmp_path / "agent-pack"
    assert main(["new", str(target), "--agent", "--id", "clinic/agent-pack"]) == 0
    return target


@pytest.fixture
def agent_pack(agent_pack_dir: Path) -> Pack:
    return load_pack(agent_pack_dir)


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
        "agent_version": "2.1.0",
        "tool_calls": [
            {"name": "ehr_medication_lookup", "status": "ok"},
            "red_flag_checker",
        ],
        "usage": {"input_tokens": 240, "output_tokens": 55, "cost_usd": 0.0018},
    }
    body.update(overrides)
    return body


def make_agent_provider(
    pack: Pack,
    handler: Any | None = None,
    **config_overrides: Any,
) -> AgentProvider:
    slot = pack.manifest.providers["reader"]
    base = dict(slot.variants["agent"].model_extra or {})
    config = {"type": "agent", **base, **config_overrides}
    info = ProviderInfo(slot="reader", variant="agent", type="agent")
    questions = {key: pack.manifest.questions[key] for key in slot.answers}
    client = httpx.Client(transport=httpx.MockTransport(handler)) if handler is not None else None
    return AgentProvider(pack, info, questions, config, client=client)


# -- registry & test isolation --------------------------------------------------------------------


def test_agent_provider_is_registered_as_first_class_model_provider() -> None:
    assert PROVIDER_TYPES["agent"] is AgentProvider
    assert PROVIDER_CONFIG_MODELS["agent"] is AgentConfig
    assert "agent" in MODEL_TYPES
    assert ENDPOINT_ENV in ENDPOINT_ENVS
    assert ENDPOINT_ENV in LIVE_SETTINGS and TOKEN_ENV in LIVE_SETTINGS


# -- offline mock_file execution ------------------------------------------------------------------


def test_offline_mock_file_answers_with_tool_calls_and_rationale(agent_pack: Pack) -> None:
    provider = build_provider(agent_pack, "reader", "agent")
    provider.preflight()
    output = provider.answer(message("Could you refill my lisinopril? I am running low."))

    assert output.cost_usd == 0.0
    assert output.answers["request"].label == "refill"
    assert output.answers["request"].confidence == pytest.approx(0.95)
    assert sum(output.answers["request"].probabilities.values()) == pytest.approx(1.0)
    assert output.answers["urgent_symptoms"].label == "no"
    assert output.answers["request"].provider.model == "mock-portal-agent"
    assert output.extra["agent_id"] == "starter-portal-agent"
    assert output.extra["model"] == "mock-portal-agent"
    assert output.extra["agent_version"] == "0.1.0"
    assert output.extra["mock_file"] == "fixtures/mock_agent.json"
    assert output.extra["tool_calls"] == [{"name": "intent_classifier", "status": "ok"}]
    assert "tool:intent_classifier" in output.answers["request"].evidence
    assert any("refill" in ev.lower() for ev in output.answers["request"].evidence)


def test_offline_mock_file_supports_yaml_and_exact_input_match(tmp_path: Path) -> None:
    target = tmp_path / "yaml-agent"
    assert main(["new", str(target), "--agent", "--id", "clinic/yaml-agent"]) == 0
    yaml_mock = {
        "agent_version": "1.0.0",
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
    manifest = yaml.safe_load((target / "pack.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"]["mock_file"] = "fixtures/mock_agent.yaml"
    (target / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    pack = load_pack(target)
    provider = build_provider(pack, "reader", "agent")
    matched = provider.answer(message("exact match payload"))
    assert matched.answers["request"].label == "appointment"
    assert matched.answers["urgent_symptoms"].label == "no"
    assert matched.answers["request"].provider.model == "starter-portal-agent@1.0.0"
    assert "tool:calendar_lookup" in matched.answers["request"].evidence

    fallback = provider.answer(message("something else"))
    assert fallback.answers["request"].label == "other"


def test_offline_mock_file_respects_state_fields(tmp_path: Path) -> None:
    target = tmp_path / "filtered-mock"
    assert main(["new", str(target), "--agent", "--id", "clinic/filtered-mock"]) == 0
    pack = load_pack(target)
    provider = build_provider(pack, "reader", "agent")
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
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch
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
        agent_pack,
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
    assert body["pack_id"] == "clinic/agent-pack"
    assert body["slot"] == "reader"
    assert body["agent_id"] == "starter-portal-agent"
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
    assert output.extra["agent_id"] == "starter-portal-agent"
    assert output.extra["model"] == "claude-sonnet-4-20250514"
    assert output.extra["agent_version"] == "2.1.0"
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


def test_unconfigured_agent_without_mock_or_url_fails_preflight(agent_pack: Pack) -> None:
    provider = make_agent_provider(agent_pack, mock_file=None)
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
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    from mendari.providers import http

    monkeypatch.setattr(http, "_sleep", lambda _: None)
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    provider = make_agent_provider(
        agent_pack, lambda _: httpx.Response(status, text="leaked-secret-body")
    )
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == code
    assert "leaked-secret" not in str(error.value)


def test_agent_timeout_and_deadline_fail_closed_in_decision_and_audit(
    agent_pack: Pack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)

    def slow_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("agent timed out", request=request)

    def fake_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return slow_handler(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    with AuditStore(tmp_path / "audit.db") as store:
        decision = Decision(agent_pack, store=store)
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

    provider = make_agent_provider(agent_pack, should_not_run)
    with decision_deadline(0.0), pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "timeout"
    assert not called


def test_malformed_agent_output_fails_closed_in_decision(
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch
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
    result = Decision(agent_pack).decide(message("Refill my metformin."))
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
        lambda b: b.update(agent_version="bad version!"),
        lambda b: b.update(tool_calls="not-a-list"),
        lambda b: b.update(tool_calls=[123]),
        lambda b: b.update(usage={"input_tokens": -1, "output_tokens": 0}),
        lambda b: b.update(usage={"input_tokens": True, "output_tokens": 0}),
        lambda b: b.update(usage={"input_tokens": 1, "output_tokens": 1, "cost_usd": -0.5}),
    ],
)
def test_agent_response_validation_is_strict(agent_pack: Pack, mutate: Any) -> None:
    body = agent_response()
    mutate(body)
    questions = {
        key: agent_pack.manifest.questions[key]
        for key in agent_pack.manifest.providers["reader"].answers
    }
    with pytest.raises(ProviderError) as error:
        validate_response(body, questions)
    assert error.value.code == "invalid_response"


def test_agent_rejects_oversized_response(
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    provider = make_agent_provider(agent_pack, lambda _: httpx.Response(200, content=b" " * 70_000))
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "invalid_response"


# -- security, SSRF, allow-list & secret isolation ------------------------------------------------


def test_ssrf_and_provider_allowed_origins_enforced(
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plain HTTP to non-localhost is refused
    monkeypatch.setenv(ENDPOINT_ENV, "http://agent.test/v1/decide")
    with pytest.raises(ProviderError) as error:
        make_agent_provider(agent_pack, lambda _: httpx.Response(200)).preflight()
    assert error.value.code == "invalid_endpoint"

    # Credentials in URL are refused
    monkeypatch.setenv(ENDPOINT_ENV, "https://user:pass@agent.test/v1/decide")
    with pytest.raises(ProviderError) as error:
        make_agent_provider(agent_pack, lambda _: httpx.Response(200)).preflight()
    assert error.value.code == "invalid_endpoint"

    # Localhost HTTP is allowed (local agent server)
    monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:8080/decide")
    make_agent_provider(agent_pack, lambda _: httpx.Response(200)).preflight()

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

    provider = make_agent_provider(agent_pack, spy)
    with pytest.raises(ProviderError) as error:
        provider.answer(message("hello"))
    assert error.value.code == "invalid_endpoint"
    assert not called

    # Allowed origin succeeds
    monkeypatch.setenv(ENDPOINT_ENV, "https://allowed.test/v1/decide")
    assert provider_allow_list() == ["https://allowed.test"]
    out = provider.answer(message("hello"))
    assert out.answers["request"].label == "refill" and called


def test_pack_endpoint_never_receives_deployment_agent_token(
    agent_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even on an owned pack, `MENDARI_AGENT_TOKEN` is only sent to `MENDARI_AGENT_URL`,
    never to a pack-specified `endpoint`."""
    monkeypatch.setenv(TOKEN_ENV, CANARY)
    monkeypatch.setenv(ENDPOINT_ENV, "https://deployment.test/decide")
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json=agent_response())

    provider = make_agent_provider(
        agent_pack, handler, endpoint="https://custom-pack-agent.test/decide"
    )
    out = provider.answer(message("refill my pills"))
    assert out.answers["request"].label == "refill"
    assert "authorization" not in {k.lower() for k in seen_headers}
    assert CANARY not in json.dumps(seen_headers)


def test_bounded_pack_cannot_set_endpoint_or_steal_agent_token(
    agent_pack_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untrusted/bounded packs cannot set `endpoint`, must declare `state_fields`, have
    `timeout_seconds` capped, and can never exfiltrate `MENDARI_AGENT_TOKEN`."""
    manifest = yaml.safe_load((agent_pack_dir / "pack.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"]["endpoint"] = "https://evil.test/steal"
    (agent_pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    # Refused at load for a bounded (hub) pack
    with pytest.raises(PackValidationError, match="may not set endpoint"):
        load_pack(agent_pack_dir, origin="hub")

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

    # Even if a bounded pack is loaded cleanly and run against deployment's MENDARI_AGENT_URL,
    # no request ever goes to evil.test
    manifest["providers"]["reader"]["variants"]["agent"].pop("endpoint")
    (agent_pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    bounded_pack = load_pack(agent_pack_dir, origin="hub")
    assert bounded_pack.bounded

    # Direct constructor also refuses `endpoint` on bounded pack
    with pytest.raises(PackValidationError, match="may not set endpoint"):
        make_agent_provider(bounded_pack, endpoint="https://evil.test/steal")

    # Bounded pack without `state_fields` is refused at load
    manifest["providers"]["reader"]["variants"]["agent"].pop("state_fields")
    (agent_pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(PackValidationError, match="must name its state_fields"):
        load_pack(agent_pack_dir, origin="hub")


@pytest.mark.parametrize("field", ["endpoint_env", "token_env", "api_key_env"])
def test_agent_pack_may_not_choose_deployment_env_vars(agent_pack_dir: Path, field: str) -> None:
    manifest = yaml.safe_load((agent_pack_dir / "pack.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["agent"][field] = "PACK_SAFETY_CANARY"
    (agent_pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(PackValidationError, match=f"Remove: {field}"):
        load_pack(agent_pack_dir)


# -- composites & end-to-end `mendari new --agent` ------------------------------------------------


def test_agent_works_inside_composite_safety_screen(
    agent_pack_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrap an external agent with deterministic keyword rules via `any_of`: if the agent fails,
    the deterministic safety screen still escalates urgent symptoms and never automates."""
    manifest = yaml.safe_load((agent_pack_dir / "pack.yaml").read_text())
    manifest["providers"]["reader"]["variants"]["guarded"] = {
        "type": "any_of",
        "escalate": {"request": "other", "urgent_symptoms": "yes"},
        "members": {
            "rules": {"type": "rules", "config": "logic/rules.yaml"},
            "agent": {
                "type": "agent",
                "agent_id": "triage-agent",
                "state_fields": ["message.body"],
                "mock_file": "fixtures/mock_agent.json",
            },
        },
    }
    (agent_pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    pack = load_pack(agent_pack_dir)
    decision = Decision(pack, providers={"reader": "guarded"})
    urgent = decision.decide(message("I have crushing chest pain right now."))
    assert urgent.outcome is Outcome.HUMAN_REVIEW
    assert urgent.priority.value == "urgent"
    assert urgent.answers["urgent_symptoms"].label == "yes"


def test_mendari_new_agent_end_to_end_offline_and_live_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "clinic-agent"
    assert (
        main(
            [
                "new",
                str(target),
                "--agent",
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
        raise AssertionError("offline agent pack must not open a network socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    assert main(["validate", str(target)]) == 0
    assert main(["test", str(target)]) == 0
    assert main(["eval", str(target), "--dataset", "examples"]) == 0
    assert main(["eval", str(target), "--dataset", "examples", "--provider", "reader=rules"]) == 0

    pack = load_pack(target)
    decision = Decision(pack)
    assert all(outcome.passed for outcome in run_pack_tests(decision))
    report = evaluate(decision, resolve_dataset(pack, "examples"))
    assert report["questions"]["request"]["accuracy"] == 1.0
    assert report["questions"]["urgent_symptoms"]["accuracy"] == 1.0

    # 2. Setting MENDARI_AGENT_URL switches the exact same pack to the live external agent
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
