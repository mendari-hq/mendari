"""Jev adapter: fast, bounded probabilistic answers to typed choice questions.

A pack's questions map one-to-one onto Jev `choice` questions (instructions + label criteria),
and Jev returns a probability for every label, so no prompt engineering or output parsing is
needed. Responses are validated strictly; anything unexpected is an `invalid_response`, never a
best-effort guess.

Deployment configuration (none of it lives in the pack):
- `JEV_API_URL`: the endpoint to call (required; https, or http for localhost only).
- `JEV_MODEL`: the model id (required unless the pack variant pins `model`).
- `JEV_API_KEY` for local use, or `JEV_CONNECTOR` (e.g. `jev/openhealth-hub`) to obtain the key
  from a Vercel Connect connector. Keys never reach logs, errors or audit records.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openhealth.errors import PackValidationError, ProviderError
from openhealth.providers.base import Answer, Provider, ProviderOutput, get_path
from openhealth.providers.credentials import resolve_key

MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"
MAX_QUESTIONS = 8
MAX_REQUEST_CHARS = 80_000
MAX_RESPONSE_BYTES = 65_536
MAX_TOKENS = 1_000_000


class JevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = Field(default=None, pattern=MODEL_PATTERN)
    timeout_ms: int = Field(default=5000, ge=100, le=10_000)
    # Minimum necessary: when set, only these input fields are sent to the provider.
    state_fields: list[str] | None = None
    api_key_env: str = "JEV_API_KEY"
    connector_env: str = "JEV_CONNECTOR"
    endpoint_env: str = "JEV_API_URL"
    model_env: str = "JEV_MODEL"
    cost_per_1k_tokens_usd: float | None = Field(default=None, ge=0)


class JevProvider(Provider):
    type = "jev"
    remote = True

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
        # The deployment's JEV_MODEL wins over a model pinned in the pack.
        model = os.environ.get(self.settings.model_env, "").strip() or self.settings.model
        if model is not None and not re.match(MODEL_PATTERN, model):
            raise PackValidationError(
                self.pack.id, [f"{self.settings.model_env}: invalid model id"]
            )
        self.model = model
        self.info = replace(self.info, model=model)

    def preflight(self) -> None:
        self._configured()

    def _configured(self) -> tuple[str, str]:
        """(model, endpoint), or `not_configured` before any call is attempted or counted."""
        endpoint = os.environ.get(self.settings.endpoint_env, "").strip()
        if self.model is None:
            raise ProviderError("not_configured", f"set {self.settings.model_env}")
        if not endpoint:
            raise ProviderError("not_configured", f"set {self.settings.endpoint_env}")
        _require_secure(endpoint)
        return self.model, endpoint

    def request_body(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        fields = self.settings.state_fields
        state = dict(inputs) if fields is None else _select(inputs, fields)
        return {
            "model": self.model,
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
        model, endpoint = self._configured()
        token = resolve_key(
            key_env=self.settings.api_key_env, connector_env=self.settings.connector_env
        )

        body = self.request_body(inputs)
        if len(json.dumps(body)) > MAX_REQUEST_CHARS:
            raise ProviderError("invalid_input", "request exceeds Jev's size limit")

        raw = self._send(endpoint, token, body)
        effective, answers, usage = validate_response(raw, self.questions)
        total = usage["input_tokens"] + usage["output_tokens"]
        price = self.settings.cost_per_1k_tokens_usd
        answered_by = replace(self.info, model=effective)
        return ProviderOutput(
            answers={
                key: Answer(
                    question=key,
                    label=answer["choice"],
                    confidence=answer["confidence"],
                    probabilities=answer["probabilities"],
                    provider=answered_by,
                )
                for key, answer in answers.items()
            },
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=None if price is None else total / 1000 * price,
            extra={"requested_model": model, "effective_model": effective},
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


def list_models(
    *, endpoint_env: str = "JEV_API_URL", timeout_s: float = 10.0
) -> list[dict[str, Any]]:
    """Models and aliases available to this deployment's Jev account (GET /v1/models).

    Useful for pinning a concrete model instead of an alias. Returns names and metadata only.
    """
    endpoint = os.environ.get(endpoint_env, "").strip()
    if not endpoint:
        raise ProviderError("not_configured", f"set {endpoint_env}")
    _require_secure(endpoint)
    parsed = urlparse(endpoint)
    url = f"{parsed.scheme}://{parsed.netloc}/v1/models"
    token = resolve_key(key_env="JEV_API_KEY", connector_env="JEV_CONNECTOR")
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            response = client.get(url, headers={"authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        raise ProviderError("provider_unavailable", type(error).__name__) from None
    if response.status_code in (401, 403):
        raise ProviderError("credential_unavailable")
    if not response.is_success:
        raise ProviderError("provider_unavailable", f"HTTP {response.status_code}")
    models = response.json().get("models")
    if not isinstance(models, list):
        raise ProviderError("invalid_response", "models must be a list")
    return [
        {k: m.get(k) for k in ("name", "description", "release_date")}
        for m in models
        if isinstance(m, dict)
    ]


PROBABILITY_SUM_TOLERANCE = 0.01  # Jev documents probabilities as summing to "approximately 1"


def validate_response(
    raw: Any, questions: Mapping[str, Any]
) -> tuple[str, dict[str, dict[str, Any]], dict[str, int]]:
    """Strict validation against Jev's published schema. Raises `invalid_response`.

    Returns (effective model, answers, usage). The effective model can differ from the one
    requested when the request used an alias such as `jev-latest`; the effective one is what
    gets recorded in audit provenance.
    """

    def invalid(detail: str) -> ProviderError:
        return ProviderError("invalid_response", detail)

    if not isinstance(raw, dict):
        raise invalid("response is not an object")
    effective = raw.get("model")
    if not isinstance(effective, str) or not re.match(MODEL_PATTERN, effective):
        raise invalid("missing or malformed model")
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
        if abs(sum(probabilities.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
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
    return effective, validated, {"input_tokens": tokens_in, "output_tokens": tokens_out}


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
