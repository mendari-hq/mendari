# SPDX-License-Identifier: Apache-2.0
"""AI Agent provider: wrap any external HTTP AI agent inside Mendari's governance engine.

An `agent` variant sends a pack's typed questions and (optionally filtered) input to an external
agent service (OpenAI, Claude, LangGraph, or any custom HTTP agent) and validates the agent's
structured answers against the pack's declared question schemas. Any HTTP error, timeout, or
malformed response raises `ProviderError` so Mendari's deterministic policy engine fails closed
(`HUMAN_REVIEW` / `ABSTAIN` / `BLOCK`) and records the failure in the audit trail.

Configuration & security:
- `MENDARI_AGENT_URL`: deployment-configured agent HTTP endpoint (https, or http for localhost).
- `MENDARI_AGENT_TOKEN`: optional bearer token sent ONLY when the endpoint comes from
  `MENDARI_AGENT_URL`, never to a pack-specified `endpoint` URL.
- `MENDARI_PROVIDER_ALLOWED_ORIGINS`: when set, the agent endpoint's origin must be listed
  (`providers/http.py`).
- A pack the deployment does not own (`Pack.bounded`) must name `state_fields`, may not set
  `endpoint`, and has its `timeout_seconds` capped by the deployment (`core/origin.py`).
- `mock_file`: optional pack-relative JSON or YAML fixture used offline when no live endpoint is
  configured, so agent packs validate, test, and evaluate cleanly without network calls, and
  switch to a real agent automatically when `MENDARI_AGENT_URL` is set. Its answers are canned
  (`CANNED`): on a server with sign-in, a decision on them never automates (HUMAN_REVIEW).
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, TypeGuard

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mendari.core.pack import Pack
from mendari.core.spec import Question, deployment_env_problem
from mendari.errors import PackValidationError, ProviderError
from mendari.providers import deadline
from mendari.providers.base import (
    MAX_CALL_COST_USD,
    MAX_CALL_TOKENS,
    Answer,
    Provider,
    ProviderOutput,
    read_problems,
    refuse_if_over_quota,
    select_fields,
    state_fields_problem,
)
from mendari.providers.http import post_json, require_https, require_secure

ENDPOINT_ENV = "MENDARI_AGENT_URL"
TOKEN_ENV = "MENDARI_AGENT_TOKEN"
# Why an answer read from the variant's `mock_file` is canned (`ProviderOutput.canned`): a server
# with sign-in never automates on it (`policy.apply_policy`'s `canned` cap). Stable: the
# dashboard and the audit trail show it.
CANNED = f"canned answers: {ENDPOINT_ENV} is not set"

# Same character class as `evals/share.py` `_MODEL_PART` so provenance ids are always shareable.
AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,127}$"
MAX_QUESTIONS = 64
MAX_REQUEST_CHARS = 100_000
MAX_RESPONSE_BYTES = 65_536
MAX_PROMPT_CHARS = 16_000
MAX_TOOL_CALLS = 64
MAX_RATIONALE_CHARS = 500
MAX_TOOL_NAME_CHARS = 128
PROBABILITY_SUM_TOLERANCE = 0.01
DEFAULT_TIMEOUT_SECONDS = 15.0


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str | None = None
    agent_id: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    system_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    instructions: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS, gt=0.0, le=120.0, allow_inf_nan=False
    )
    # Minimum necessary: when set, only these input fields are sent to the agent.
    state_fields: list[str] | None = None
    # Pack-relative JSON or YAML fixture for offline testing and evaluation.
    mock_file: str | None = None

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _reject_bool_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be a number, not a boolean")
        return value


class AgentProvider(Provider):
    type = "agent"
    remote = True

    def __init__(self, *args: Any, client: httpx.Client | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        settings, problems = self._settings(
            self.info.slot, self.config, self.questions, bounded=self.pack.bounded
        )
        if settings is None:
            raise PackValidationError(self.pack.id, problems)
        self.settings = settings
        self._client = client
        self._mock: dict[str, Any] | None = None
        if self.settings.mock_file is not None:
            self._mock = _load_fixture_file(
                self.pack, self.settings.mock_file, self.questions, self.info.slot
            )
        if self.settings.agent_id is not None:
            self.info = replace(self.info, model=self.settings.agent_id)

    @classmethod
    def config_problems(
        cls,
        pack: Pack,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
        trained: bool,
    ) -> list[str]:
        settings, problems = cls._settings(where, config, questions, bounded=bounded)
        if settings is None or settings.mock_file is None:
            return problems
        mock_file = settings.mock_file
        data, file_problems = read_problems(lambda: _read_pack_fixture(pack, mock_file))
        if file_problems:
            return file_problems
        return _fixture_data_problems(where, mock_file, data, questions)

    @staticmethod
    def _settings(
        where: str, config: Mapping[str, Any], questions: Mapping[str, Question], *, bounded: bool
    ) -> tuple[AgentConfig | None, list[str]]:
        """The variant's closed settings (`AgentConfig`), or None and why not."""
        problem = deployment_env_problem(dict(config))
        if problem is not None:
            return None, [f"{where}: {problem}"]
        try:
            settings = AgentConfig.model_validate({k: v for k, v in config.items() if k != "type"})
        except ValidationError as error:
            return None, [f"{where}: {error}"]
        if not 1 <= len(questions) <= MAX_QUESTIONS:
            return None, [f"{where}: 1-{MAX_QUESTIONS} questions per agent variant"]
        if bounded and not settings.state_fields:
            return None, [f"{where}: {state_fields_problem(AgentProvider.type)}"]
        if settings.endpoint is not None:
            if bounded:
                return None, [
                    f"{where}: an `agent` variant of a community or registry agent may not set "
                    f"endpoint; configure {ENDPOINT_ENV} in the deployment"
                ]
            try:
                require_https(settings.endpoint, "Agent")
            except ProviderError as error:
                return None, [f"{where}: {error.detail or error.code}"]
        return settings, []

    def _resolve_endpoint(self) -> tuple[str | None, bool]:
        """Return `(url, from_env)` for a live call, or `(None, False)` when none is set.

        On a bounded (unowned) pack, a pack-specified `endpoint` is never used.
        `MENDARI_AGENT_TOKEN` is sent only when `from_env` is True, so a pack-specified URL can
        never receive the deployment's agent token.
        """
        if not self.pack.bounded and self.settings.endpoint:
            return self.settings.endpoint, False
        env_url = os.environ.get(ENDPOINT_ENV, "").strip()
        if env_url:
            return env_url, True
        return None, False

    def preflight(self) -> None:
        url, _ = self._resolve_endpoint()
        if url is not None:
            require_secure(url, "Agent")
            return
        if self._mock is not None:
            return
        raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        url, from_env = self._resolve_endpoint()
        if url is None:
            if self._mock is not None:
                return self._answer_fixture(inputs)
            raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")

        require_secure(url, "Agent")
        refuse_if_over_quota()
        deadline.refuse_if_past()

        body = request_body(
            self.pack.id,
            self.info.slot,
            self.questions,
            inputs,
            agent_id=self.settings.agent_id,
            system_prompt=self.settings.system_prompt,
            instructions=self.settings.instructions,
            state_fields=self.settings.state_fields,
        )
        if len(json.dumps(body)) > MAX_REQUEST_CHARS:
            raise ProviderError("invalid_input", "request exceeds the agent size limit")

        self.charge()
        deadline.refuse_if_past()
        self.sending()

        token = os.environ.get(TOKEN_ENV, "").strip() if from_env else ""
        headers = {"idempotency-key": str(uuid.uuid4())}
        if token:
            headers["authorization"] = f"Bearer {token}"
        raw = post_json(
            self._client,
            url,
            body,
            headers=headers,
            timeout_s=self.settings.timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
            retries=1,
        )
        parsed = validate_response(raw, self.questions)
        usage = parsed["usage"]
        self.metered(usage["input_tokens"], usage["output_tokens"], usage["cost_usd"])
        return self._build_output(parsed, used_mock=False)

    def _answer_fixture(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        assert self._mock is not None
        state = (
            select_fields(inputs, self.settings.state_fields)
            if self.settings.state_fields is not None
            else dict(inputs)
        )
        matched = _match_fixture_response(self._mock, state)
        if matched is None:
            raise ProviderError("invalid_response", "no mock case matched input")
        parsed = validate_response(matched, self.questions)
        return self._build_output(parsed, used_mock=True)

    def _build_output(self, parsed: dict[str, Any], *, used_mock: bool) -> ProviderOutput:
        reported_model: str | None = parsed["model"]
        agent_version: str | None = parsed["agent_version"]
        tool_calls: list[Any] = parsed["tool_calls"]
        usage: dict[str, Any] = parsed["usage"]

        effective = _effective_model(reported_model, self.settings.agent_id, agent_version)
        answered_by = replace(self.info, model=effective or self.info.model)

        tool_evidence = tuple(_tool_evidence(tool_calls))
        answers: dict[str, Answer] = {}
        for key, question in self.questions.items():
            item = parsed["answers"][key]
            evidence_parts: list[str] = []
            if item["rationale"]:
                evidence_parts.append(item["rationale"])
            evidence_parts.extend(tool_evidence)
            probabilities = item["probabilities"]
            answers[key] = Answer(
                question=key,
                label=item["label"],
                confidence=item["confidence"],
                probabilities=probabilities,
                provider=answered_by,
                evidence=tuple(evidence_parts),
                value=question.expected_score(probabilities),
            )

        extra: dict[str, Any] = {}
        if self.settings.agent_id is not None:
            extra["agent_id"] = self.settings.agent_id
        if reported_model is not None:
            extra["model"] = reported_model
        if agent_version is not None:
            extra["agent_version"] = agent_version
        if tool_calls:
            extra["tool_calls"] = tool_calls
        if used_mock and self.settings.mock_file is not None:
            extra["mock_file"] = self.settings.mock_file

        cost_usd = usage["cost_usd"]
        if used_mock and cost_usd is None:
            cost_usd = 0.0

        return ProviderOutput(
            answers=answers,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=cost_usd,
            extra=extra,
            canned=CANNED if used_mock else None,
        )


def request_body(
    pack_id: str,
    slot: str,
    questions: Mapping[str, Question],
    inputs: Mapping[str, Any],
    *,
    agent_id: str | None = None,
    system_prompt: str | None = None,
    instructions: str | None = None,
    state_fields: list[str] | None = None,
) -> dict[str, Any]:
    """The JSON payload POSTed to an external agent endpoint."""
    state = select_fields(inputs, state_fields) if state_fields is not None else dict(inputs)
    body: dict[str, Any] = {
        "pack_id": pack_id,
        "slot": slot,
        "agent_id": agent_id,
        "questions": [agent_question(key, question) for key, question in questions.items()],
        "input": state,
    }
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    if instructions is not None:
        body["instructions"] = instructions
    return body


def agent_question(key: str, question: Question) -> dict[str, Any]:
    """A pack question serialized for an external agent."""
    item: dict[str, Any] = {
        "key": key,
        "type": question.type,
        "instructions": question.instructions,
        "choices": list(question.labels),
        "criteria": {label: question.criteria[label] for label in question.labels},
    }
    if question.type == "score" and question.levels:
        item["levels"] = {
            label: {
                "score": question.levels[label].score,
                "criterion": question.levels[label].criterion,
            }
            for label in question.labels
        }
    return item


def validate_response(raw: Any, questions: Mapping[str, Question]) -> dict[str, Any]:
    """Strict validation of an external agent's JSON response. Raises `invalid_response`."""
    if not isinstance(raw, dict):
        raise _invalid("response is not an object")

    answers = raw.get("answers")
    if not isinstance(answers, dict):
        raise _invalid("answers must be an object")
    if set(answers) != set(questions):
        raise _invalid("answers do not match the questions asked")

    validated_answers: dict[str, dict[str, Any]] = {}
    for key, question in questions.items():
        validated_answers[key] = _validate_answer(key, question, answers[key])

    model = _optional_id(raw.get("model"), "model")
    agent_version = _optional_id(raw.get("agent_version"), "agent_version")
    tool_calls = _validate_tool_calls(raw.get("tool_calls"))
    usage = _validate_usage(raw.get("usage"))

    return {
        "answers": validated_answers,
        "model": model,
        "agent_version": agent_version,
        "tool_calls": tool_calls,
        "usage": usage,
    }


def _validate_answer(key: str, question: Question, answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise _invalid(f"answer {key!r} is malformed")
    if "value" not in answer:
        raise _invalid(f"answer {key!r} is missing value")
    label = _parse_label(key, question, answer["value"])

    raw_probabilities = answer.get("probabilities")
    raw_confidence = answer.get("confidence")

    if raw_probabilities is not None:
        probabilities = _validate_probabilities(key, question, raw_probabilities)
        if probabilities[label] < max(probabilities.values()) - 1e-5:
            raise _invalid(f"answer {key!r} value is not the most probable label")
        if raw_confidence is not None:
            if not _probability(raw_confidence):
                raise _invalid(f"answer {key!r} has malformed confidence")
            if abs(float(raw_confidence) - probabilities[label]) > PROBABILITY_SUM_TOLERANCE:
                raise _invalid(f"answer {key!r} confidence does not match probabilities")
        confidence = probabilities[label]
    else:
        if not _probability(raw_confidence):
            raise _invalid(f"answer {key!r} has malformed confidence")
        confidence = float(raw_confidence)
        if confidence < (1.0 / len(question.labels)) - 1e-6:
            raise _invalid(f"answer {key!r} confidence is below uniform chance for a chosen label")
        other_labels = [candidate for candidate in question.labels if candidate != label]
        remainder = max(0.0, 1.0 - confidence)
        share = remainder / len(other_labels)
        probabilities = {
            candidate: (confidence if candidate == label else share)
            for candidate in question.labels
        }

    rationale = answer.get("rationale")
    if rationale is not None:
        if not isinstance(rationale, str):
            raise _invalid(f"answer {key!r} rationale must be a string")
        rationale = _CTRL_CHARS.sub(" ", rationale).strip()[:MAX_RATIONALE_CHARS] or None

    return {
        "label": label,
        "confidence": confidence,
        "probabilities": probabilities,
        "rationale": rationale,
    }


def _parse_label(key: str, question: Question, value: Any) -> str:
    if question.type == "boolean":
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, str) and value in ("yes", "no"):
            return value
        raise _invalid(f"answer {key!r} value must be yes/no or boolean")
    if not isinstance(value, str) or value not in question.criteria:
        raise _invalid(f"answer {key!r} value is not a declared choice")
    return value


def _validate_probabilities(key: str, question: Question, probabilities: Any) -> dict[str, float]:
    if not isinstance(probabilities, dict) or set(probabilities) != set(question.criteria):
        raise _invalid(f"answer {key!r} has malformed probabilities")
    if not all(_probability(p) for p in probabilities.values()):
        raise _invalid(f"answer {key!r} has malformed probabilities")
    total = sum(float(p) for p in probabilities.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise _invalid(f"answer {key!r} probabilities do not sum to 1")
    return {label: float(probabilities[label]) / total for label in question.labels}


def _optional_id(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "://" in value or not re.fullmatch(AGENT_ID_PATTERN, value):
        raise _invalid(f"malformed {name}")
    return value


def _validate_tool_calls(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TOOL_CALLS:
        raise _invalid("tool_calls must be a bounded list")
    validated: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            trimmed = item.strip()[:MAX_TOOL_NAME_CHARS]
            if not trimmed:
                raise _invalid("tool_calls entry must not be empty")
            validated.append(trimmed)
        elif isinstance(item, dict):
            validated.append(dict(item))
        else:
            raise _invalid("tool_calls entries must be strings or objects")
    return validated


def _validate_usage(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    if not isinstance(raw, dict):
        raise _invalid("usage must be an object")
    tokens_in = raw.get("input_tokens", raw.get("prompt_tokens", 0))
    tokens_out = raw.get("output_tokens", raw.get("completion_tokens", 0))
    if (
        not isinstance(tokens_in, int)
        or not isinstance(tokens_out, int)
        or isinstance(tokens_in, bool)
        or isinstance(tokens_out, bool)
        or tokens_in < 0
        or tokens_out < 0
    ):
        raise _invalid("usage is malformed")
    if tokens_in + tokens_out > MAX_CALL_TOKENS:
        raise _invalid("usage is out of range")
    cost = raw.get("cost_usd", raw.get("cost"))
    if cost is None:
        cost_usd: float | None = None
    elif (
        isinstance(cost, int | float)
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and 0 <= cost <= MAX_CALL_COST_USD
    ):
        cost_usd = float(cost)
    else:
        raise _invalid("usage cost is malformed")
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": cost_usd}


def _effective_model(
    reported_model: str | None, agent_id: str | None, agent_version: str | None
) -> str | None:
    if reported_model is not None:
        return reported_model
    if agent_id is not None and agent_version is not None:
        combined = f"{agent_id}@{agent_version}"
        if len(combined) <= 128:
            return combined
    return agent_id or agent_version


_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _tool_evidence(tool_calls: list[Any]) -> list[str]:
    names: list[str] = []
    for item in tool_calls:
        if isinstance(item, str):
            name = _CTRL_CHARS.sub(" ", item).strip()[:MAX_TOOL_NAME_CHARS]
        elif isinstance(item, dict):
            raw_name = item.get("name") or item.get("tool")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            name = _CTRL_CHARS.sub(" ", raw_name).strip()[:MAX_TOOL_NAME_CHARS]
        else:
            continue
        if name:
            names.append(f"tool:{name}")
    return names


def _probability(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _invalid(detail: str) -> ProviderError:
    return ProviderError("invalid_response", detail)


# -- offline mock fixture ------------------------------------------------------------------------


def _read_pack_fixture(pack: Pack, relative: str) -> Any:
    if relative.endswith((".yaml", ".yml")):
        return pack.read_yaml(relative)
    return pack.read_json(relative)


def _load_fixture_file(
    pack: Pack, relative: str, questions: Mapping[str, Question], where: str
) -> dict[str, Any]:
    data = _read_pack_fixture(pack, relative)
    problems = _fixture_data_problems(where, relative, data, questions)
    if problems:
        raise PackValidationError(pack.id, problems)
    assert isinstance(data, dict)
    return data


def _fixture_data_problems(
    where: str, relative: str, data: Any, questions: Mapping[str, Question]
) -> list[str]:
    if not isinstance(data, dict):
        return [f"{where}: {relative}: mock file must be a mapping"]
    allowed_keys = {"model", "agent_version", "cases", "default"}
    extra = sorted(set(data) - allowed_keys)
    if extra:
        return [f"{where}: {relative}: unexpected mock file keys {extra}"]
    if "cases" not in data and "default" not in data:
        return [f"{where}: {relative}: mock file needs `cases` or `default`"]

    problems: list[str] = []
    for key in ("model", "agent_version"):
        if data.get(key) is not None:
            try:
                _optional_id(data[key], key)
            except ProviderError as error:
                problems.append(f"{where}: {relative}: {error.detail}")

    cases = data.get("cases", [])
    if not isinstance(cases, list):
        return [*problems, f"{where}: {relative}: `cases` must be a list"]

    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "match" not in case or "response" not in case:
            problems.append(
                f"{where}: {relative}: cases[{index}] must be a mapping with `match` and `response`"
            )
            continue
        match_problem = _match_spec_problem(case["match"])
        if match_problem is not None:
            problems.append(f"{where}: {relative}: cases[{index}].match: {match_problem}")
            continue
        merged = _merge_fixture_response(data, case["response"])
        try:
            validate_response(merged, questions)
        except ProviderError as error:
            problems.append(f"{where}: {relative}: cases[{index}].response: {error.detail}")

    if "default" in data and data["default"] is not None:
        merged = _merge_fixture_response(data, data["default"])
        try:
            validate_response(merged, questions)
        except ProviderError as error:
            problems.append(f"{where}: {relative}: default: {error.detail}")

    return problems


def _match_spec_problem(spec: Any) -> str | None:
    if isinstance(spec, str):
        return None if spec.strip() else "must not be empty"
    if isinstance(spec, list):
        if not spec or not all(isinstance(s, str) and s.strip() for s in spec):
            return "must be a non-empty list of strings"
        return None
    if isinstance(spec, dict):
        allowed = {"contains_any", "contains_all", "exclude_any", "input"}
        extra = sorted(set(spec) - allowed)
        if extra:
            return f"unexpected keys {extra}"
        if not spec:
            return "must not be empty"
        for key in ("contains_any", "contains_all", "exclude_any"):
            if key in spec:
                val = spec[key]
                if not isinstance(val, list) or not all(isinstance(s, str) and s for s in val):
                    return f"{key} must be a list of non-empty strings"
        if "input" in spec and not isinstance(spec["input"], dict):
            return "input must be a mapping"
        return None
    return "must be a string, list of strings, or mapping"


def _merge_fixture_response(root: Mapping[str, Any], response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    merged = dict(response)
    if "model" not in merged and root.get("model") is not None:
        merged["model"] = root["model"]
    if "agent_version" not in merged and root.get("agent_version") is not None:
        merged["agent_version"] = root["agent_version"]
    return merged


def _flatten_strings(value: Any) -> str:
    parts: list[str] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list | tuple):
            stack.extend(reversed(item))
    return "\n".join(parts).lower()


def _subset_match(expected: Mapping[str, Any], actual: Any) -> bool:
    if not isinstance(actual, Mapping):
        return False
    for key, wanted in expected.items():
        if key not in actual:
            return False
        if isinstance(wanted, Mapping):
            if not _subset_match(wanted, actual[key]):
                return False
        elif isinstance(wanted, bool) or isinstance(actual[key], bool):
            if type(actual[key]) is not type(wanted) or actual[key] != wanted:
                return False
        elif actual[key] != wanted:
            return False
    return True


def _case_matches(spec: Any, text: str, state: Mapping[str, Any]) -> bool:
    if isinstance(spec, str):
        return spec.lower() in text
    if isinstance(spec, list):
        return any(needle.lower() in text for needle in spec)
    assert isinstance(spec, dict)
    if "contains_any" in spec and not any(s.lower() in text for s in spec["contains_any"]):
        return False
    if "contains_all" in spec and not all(s.lower() in text for s in spec["contains_all"]):
        return False
    if "exclude_any" in spec and any(s.lower() in text for s in spec["exclude_any"]):
        return False
    return not ("input" in spec and not _subset_match(spec["input"], state))


def _match_fixture_response(mock: Mapping[str, Any], state: Mapping[str, Any]) -> Any | None:
    text = _flatten_strings(state)
    for case in mock.get("cases", []):
        if _case_matches(case["match"], text, state):
            return _merge_fixture_response(mock, case["response"])
    if mock.get("default") is not None:
        return _merge_fixture_response(mock, mock["default"])
    return None
