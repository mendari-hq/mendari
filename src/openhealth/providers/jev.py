"""Jev adapter: fast, bounded probabilistic answers to typed choice questions.

A pack's questions map one-to-one onto Jev `choice` questions (instructions + label criteria),
and Jev returns a probability for every label, so no prompt engineering or output parsing is
needed. Responses are validated strictly; anything unexpected is an `invalid_response`, never a
best-effort guess. Credentials are read from the environment and never reach audit records.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openhealth.errors import PackValidationError, ProviderError
from openhealth.providers.base import Answer, Provider, ProviderOutput, get_path

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL_PATTERN = r"^jev-\d+\.\d+\.\d+$"
MAX_QUESTIONS = 8
MAX_REQUEST_CHARS = 80_000
MAX_RESPONSE_BYTES = 65_536
MAX_TOKENS = 1_000_000


class JevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(pattern=MODEL_PATTERN)
    timeout_ms: int = Field(default=5000, ge=100, le=10_000)
    # Minimum necessary: when set, only these input fields are sent to the provider.
    state_fields: list[str] | None = None
    api_key_env: str = "JEV_API_KEY"
    endpoint_env: str = "JEV_API_URL"
    cost_per_1k_tokens_usd: float | None = Field(default=None, ge=0)


class JevProvider(Provider):
    type = "jev"

    def __init__(self, *args: Any, client: httpx.Client | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        settings = {k: v for k, v in self.config.items() if k != "type"}
        try:
            self.settings = JevConfig.model_validate(settings)
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {error}"]) from None
        if not 1 <= len(self.questions) <= MAX_QUESTIONS:
            raise PackValidationError(
                self.pack.id,
                [f"{self.info.slot}: Jev accepts 1-{MAX_QUESTIONS} questions per call"],
            )
        self._client = client

    def request_body(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        fields = self.settings.state_fields
        state = dict(inputs) if fields is None else _select(inputs, fields)
        return {
            "model": self.settings.model,
            "state": state,
            "questions": {
                key: {
                    "type": "choice",
                    "instructions": q.instructions,
                    "criteria": dict(q.criteria),
                }
                for key, q in self.questions.items()
            },
        }

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        token = os.environ.get(self.settings.api_key_env, "").strip()
        if not token:
            raise ProviderError("credential_unavailable", f"{self.settings.api_key_env} is not set")
        endpoint = os.environ.get(self.settings.endpoint_env, "").strip() or DEFAULT_ENDPOINT
        _require_secure(endpoint)

        body = self.request_body(inputs)
        if len(json.dumps(body)) > MAX_REQUEST_CHARS:
            raise ProviderError("invalid_input", "request exceeds Jev's size limit")

        raw = self._send(endpoint, token, body)
        answers, usage = validate_response(raw, self.questions, self.settings.model)
        total = usage["input_tokens"] + usage["output_tokens"]
        price = self.settings.cost_per_1k_tokens_usd
        return ProviderOutput(
            answers={
                key: Answer(
                    question=key,
                    label=answer["choice"],
                    confidence=answer["confidence"],
                    probabilities=answer["probabilities"],
                    provider=self.info,
                )
                for key, answer in answers.items()
            },
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=None if price is None else total / 1000 * price,
        )

    def _send(self, endpoint: str, token: str, body: dict[str, Any]) -> Any:
        timeout = httpx.Timeout(self.settings.timeout_ms / 1000)
        client = self._client or httpx.Client(timeout=timeout, follow_redirects=False)
        try:
            with client.stream(
                "POST",
                endpoint,
                json=body,
                headers={"authorization": f"Bearer {token}"},
                timeout=timeout,
            ) as response:
                if response.status_code == 429:
                    raise ProviderError("rate_limited")
                if response.status_code in (401, 403):
                    raise ProviderError("credential_unavailable")
                if not response.is_success:
                    raise ProviderError("provider_unavailable", f"HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ProviderError("invalid_response", "response too large")
                    chunks.append(chunk)
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.HTTPError as error:
            raise ProviderError("provider_unavailable", type(error).__name__) from None
        finally:
            if self._client is None:
                client.close()
        try:
            return json.loads(b"".join(chunks))
        except ValueError:
            raise ProviderError("invalid_response", "body is not JSON") from None


def validate_response(
    raw: Any, questions: Mapping[str, Any], model: str
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Strict validation, mirroring the reference gateway client. Raises `invalid_response`."""

    def invalid(detail: str) -> ProviderError:
        return ProviderError("invalid_response", detail)

    if not isinstance(raw, dict) or raw.get("model") != model:
        raise invalid("missing or mismatched model")
    answers, usage = raw.get("answers"), raw.get("usage")
    if not isinstance(answers, dict) or not isinstance(usage, dict):
        raise invalid("answers and usage must be objects")
    if set(answers) != set(questions):
        raise invalid("answers do not match the questions asked")

    validated = {}
    for key, question in questions.items():
        answer = answers[key]
        labels = set(question.criteria)
        if (
            not isinstance(answer, dict)
            or answer.get("type") != "choice"
            or answer.get("choice") not in labels
            or not _probability(answer.get("confidence"))
            or not isinstance(answer.get("probabilities"), dict)
        ):
            raise invalid(f"answer {key!r} is malformed")
        probabilities = answer["probabilities"]
        if set(probabilities) != labels or not all(map(_probability, probabilities.values())):
            raise invalid(f"answer {key!r} has malformed probabilities")
        if abs(sum(probabilities.values()) - 1) > 0.001:
            raise invalid(f"answer {key!r} probabilities do not sum to 1")
        if probabilities[answer["choice"]] < max(probabilities.values()) - 0.00001:
            raise invalid(f"answer {key!r} choice is not the most probable label")
        validated[key] = {
            "choice": answer["choice"],
            "confidence": float(answer["confidence"]),
            "probabilities": {label: float(p) for label, p in probabilities.items()},
        }

    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if (
        not isinstance(tokens_in, int)
        or not isinstance(tokens_out, int)
        or isinstance(tokens_in, bool)
        or isinstance(tokens_out, bool)
        or tokens_in < 0
        or tokens_out < 0
        or tokens_in + tokens_out > MAX_TOKENS
    ):
        raise invalid("usage is malformed")
    return validated, {"input_tokens": tokens_in, "output_tokens": tokens_out}


def _probability(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _select(inputs: Mapping[str, Any], fields: list[str]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for dotted in fields:
        value = get_path(inputs, dotted)
        if value is None:
            continue
        *parents, leaf = dotted.split(".")
        node = state
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return state


_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|::1)$")


def _require_secure(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme == "https" or (
        parsed.scheme == "http" and _LOCAL_HOST.match(parsed.hostname or "")
    ):
        return
    raise ProviderError(
        "invalid_endpoint", "Jev endpoints must use https (http only for localhost)"
    )
