"""Jev adapter: fast, bounded probabilistic answers to typed questions.

A pack's questions map one-to-one onto Jev questions (instructions + label criteria):
`choice` → `choice`, `boolean` → `noul` (P(yes)), `score` → `score` (ordered levels). Jev
returns a probability for every label, so no prompt engineering or output parsing is needed.
Responses are validated strictly; anything unexpected is an `invalid_response`, never a
best-effort guess.

All the Jev questions of one decision go in one call (see `providers.batch`): variants that
share endpoint, model and credentials are merged, and the state sent is the union of their
`state_fields`. A variant opts out with `batch: false`.

Deployment configuration (none of it lives in the pack):
- `JEV_API_URL`: the endpoint to call (required; https, or http for localhost only).
- `JEV_MODEL`: the model id (required unless the pack variant pins `model`).
- `JEV_API_KEY` for local use, or `JEV_CONNECTOR` (e.g. `jev/my-project`) to obtain the key
  from a Vercel Connect connector. Keys never reach logs, errors or audit records.

These variable names are fixed; a pack that tries to name other variables is refused at load.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Hashable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mendari.core.spec import Question, deployment_env_problem
from mendari.errors import MendariError, PackValidationError, ProviderError
from mendari.providers.base import (
    Answer,
    Provider,
    ProviderOutput,
    batch_state,
    refuse_if_over_quota,
)
from mendari.providers.batch import batched_answer, unwrap
from mendari.providers.credentials import key_source, resolve_key
from mendari.providers.http import post_json, require_secure

MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"
# Published limits (docs.typesafe.ai/api and /models): 2-255 options per Choice (as in the
# spec), 2-10 levels per Score, 64k tokens per request. There is no per-request question
# limit; this cap only keeps one batched request well inside the token budget.
MAX_QUESTIONS = 64
MAX_SCORE_LEVELS = 10
# Published price of jev-1.13.0 (docs.typesafe.ai/models): $0.042 per million input tokens;
# output tokens are free. A variant can override it.
PRICE_PER_1K_INPUT_TOKENS_USD = 0.000042
MAX_REQUEST_CHARS = 80_000
MAX_RESPONSE_BYTES = 65_536
MAX_TOKENS = 1_000_000
# The deployment's settings. Fixed: a pack never chooses which variables are read.
API_KEY_ENV = "JEV_API_KEY"
CONNECTOR_ENV = "JEV_CONNECTOR"
ENDPOINT_ENV = "JEV_API_URL"
MODEL_ENV = "JEV_MODEL"


class JevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = Field(default=None, pattern=MODEL_PATTERN)
    timeout_ms: int = Field(default=5000, ge=100, le=10_000)
    # Minimum necessary: when set, only these input fields are sent to the provider.
    state_fields: list[str] | None = None
    cost_per_1k_input_tokens_usd: float | None = Field(default=PRICE_PER_1K_INPUT_TOKENS_USD, ge=0)
    # Share one call with the decision's other Jev questions (union of state_fields).
    batch: bool = True


class JevProvider(Provider):
    type = "jev"
    remote = True
    max_batch_questions = MAX_QUESTIONS

    def __init__(self, *args: Any, client: httpx.Client | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        settings = {k: v for k, v in self.config.items() if k != "type"}
        problem = deployment_env_problem(self.config)  # also checked at load (core/spec.py)
        if problem is not None:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {problem}"])
        try:
            self.settings = JevConfig.model_validate(settings)
        except ValidationError as error:
            raise PackValidationError(self.pack.id, [f"{self.info.slot}: {error}"]) from None
        if not 1 <= len(self.questions) <= MAX_QUESTIONS:
            raise PackValidationError(
                self.pack.id,
                [f"{self.info.slot}: 1-{MAX_QUESTIONS} questions per Jev variant"],
            )
        too_many = [
            key
            for key, question in self.questions.items()
            if question.type == "score" and len(question.labels) > MAX_SCORE_LEVELS
        ]
        if too_many:
            problem = f"{self.info.slot}: Jev scores have at most {MAX_SCORE_LEVELS} levels"
            raise PackValidationError(self.pack.id, [f"{problem}: {too_many}"])
        self._client = client
        # The deployment's JEV_MODEL wins over a model pinned in the pack.
        model = os.environ.get(MODEL_ENV, "").strip() or self.settings.model
        if model is not None and not re.match(MODEL_PATTERN, model):
            raise PackValidationError(self.pack.id, [f"{MODEL_ENV}: invalid model id"])
        self.model = model
        self.info = replace(self.info, model=model)

    def preflight(self) -> None:
        self._configured()

    def _configured(self) -> tuple[str, str]:
        """(model, endpoint), or `not_configured` before any call is attempted or counted."""
        endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
        if self.model is None:
            raise ProviderError("not_configured", f"set {MODEL_ENV}")
        if not endpoint:
            raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")
        _require_secure(endpoint)
        return self.model, endpoint

    def batch_key(self) -> Hashable | None:
        if not self.settings.batch:
            return None
        settings = self.settings
        return (
            os.environ.get(ENDPOINT_ENV, "").strip(),
            self.model,
            settings.timeout_ms,
            settings.cost_per_1k_input_tokens_usd,
        )

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        shared = batched_answer(self, inputs)
        return shared if shared is not None else unwrap(self.answer_batch([self], inputs)[0])

    def answer_batch(
        self, providers: list[Provider], inputs: Mapping[str, Any]
    ) -> list[ProviderOutput | ProviderError]:
        """One Jev call for `providers` (this one first). The call's usage and cost are
        reported once, by the first provider that answered; a malformed answer fails only the
        provider that asked that question."""
        batch = [provider for provider in providers if isinstance(provider, JevProvider)]
        if len(batch) != len(providers):
            raise TypeError("a Jev batch holds only Jev providers")
        model, endpoint = self._configured()
        refuse_if_over_quota()  # over quota: no credential fetched, no call
        token = resolve_key(key_env=API_KEY_ENV, connector_env=CONNECTOR_ENV)
        self.charge()  # after the credential: a call that cannot be made uses no budget

        questions = {key: q for provider in batch for key, q in provider.questions.items()}
        body = request_body(model, batch, questions, inputs)
        if len(json.dumps(body)) > MAX_REQUEST_CHARS:
            raise ProviderError("invalid_input", "request exceeds Jev's size limit")

        raw = self._send(endpoint, token, body)
        effective, answers, usage, problems = parse_response(raw, questions)
        failed = {p for p in batch if any(key in problems for key in p.questions)}
        first = next((p for p in batch if p not in failed), None)
        price = self.settings.cost_per_1k_input_tokens_usd  # output tokens are free
        extra: dict[str, Any] = {"requested_model": model, "effective_model": effective}
        if len(batch) > 1:
            extra["batch"] = [f"{p.info.slot}/{p.info.variant}" for p in batch]
        outputs: list[ProviderOutput | ProviderError] = []
        for provider in batch:
            if provider in failed:
                key = next(k for k in provider.questions if k in problems)
                outputs.append(ProviderError("invalid_response", problems[key]))
                continue
            lead = provider is first
            answered_by = replace(provider.info, model=effective)
            outputs.append(
                ProviderOutput(
                    answers={
                        key: Answer(
                            question=key,
                            label=answers[key]["label"],
                            confidence=answers[key]["confidence"],
                            probabilities=answers[key]["probabilities"],
                            provider=answered_by,
                            evidence=_evidence(answers[key]),
                        )
                        for key in provider.questions
                    },
                    input_tokens=usage["input_tokens"] if lead else 0,
                    output_tokens=usage["output_tokens"] if lead else 0,
                    cost_usd=None
                    if price is None
                    else (usage["input_tokens"] / 1000 * price if lead else 0.0),
                    extra=extra,
                )
            )
        return outputs

    def _send(self, endpoint: str, token: str, body: dict[str, Any]) -> Any:
        return post_json(
            self._client,
            endpoint,
            body,
            headers={"authorization": f"Bearer {token}"},
            timeout_s=self.settings.timeout_ms / 1000,
            retries=1,
            max_bytes=MAX_RESPONSE_BYTES,
        )


def deployment_configured() -> bool:
    """Whether this deployment has Jev set up: an endpoint, a model and a key or connector. Set
    up with a model id or an endpoint Jev could never be called with is an error (at startup)."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip()
    if not (endpoint and model and key_source(key_env=API_KEY_ENV, connector_env=CONNECTOR_ENV)):
        return False
    if not re.match(MODEL_PATTERN, model):
        raise MendariError(f"{MODEL_ENV} is not a Jev model id")
    try:
        _require_secure(endpoint)
    except ProviderError:
        raise MendariError(f"{ENDPOINT_ENV} must use https (http only for localhost)") from None
    return True


def list_models(*, timeout_s: float = 10.0) -> list[dict[str, Any]]:
    """Models and aliases available to this deployment's Jev account (GET /v1/models).

    Useful for pinning a concrete model instead of an alias. Returns names and metadata only.
    """
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")
    _require_secure(endpoint)
    parsed = urlparse(endpoint)
    url = f"{parsed.scheme}://{parsed.netloc}/v1/models"
    token = resolve_key(key_env=API_KEY_ENV, connector_env=CONNECTOR_ENV)
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


def request_body(
    model: str,
    providers: list[JevProvider],
    questions: Mapping[str, Question],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """The request for one call. The state is the union of the providers' `state_fields`
    (the whole input if any of them sends everything)."""
    state = batch_state(inputs, [provider.settings.state_fields for provider in providers])
    return {
        "model": model,
        "state": state,
        "questions": {key: jev_question(question) for key, question in questions.items()},
    }


def jev_question(question: Question) -> dict[str, Any]:
    if question.type == "boolean":
        criteria = {"true": question.criteria["yes"], "false": question.criteria["no"]}
        return {"type": "noul", "instructions": question.instructions, "criteria": criteria}
    if question.type == "score":
        # Jev scores levels by position (0, 1, ...); `labels` is ordered by score.
        levels = [question.criteria[label] for label in question.labels]
        return {"type": "score", "instructions": question.instructions, "criteria": levels}
    return {
        "type": "choice",
        "instructions": question.instructions,
        "criteria": dict(question.criteria),
    }


PROBABILITY_SUM_TOLERANCE = 0.01  # Jev documents probabilities as summing to "approximately 1"


def validate_response(
    raw: Any, questions: Mapping[str, Question]
) -> tuple[str, dict[str, dict[str, Any]], dict[str, int]]:
    """Strict validation against Jev's published schema. Raises `invalid_response`.

    Returns (effective model, answers, usage). The effective model can differ from the one
    requested when the request used an alias such as `jev-latest`; the effective one is what
    gets recorded in audit provenance.
    """
    effective, answers, usage, problems = parse_response(raw, questions)
    if problems:
        raise _invalid(next(iter(problems.values())))
    return effective, answers, usage


def parse_response(
    raw: Any, questions: Mapping[str, Question]
) -> tuple[str, dict[str, dict[str, Any]], dict[str, int], dict[str, str]]:
    """As `validate_response`, but a malformed answer to one question is reported in the last
    element (question -> detail) instead of failing the other questions' answers."""
    if not isinstance(raw, dict):
        raise _invalid("response is not an object")
    effective = raw.get("model")
    if not isinstance(effective, str) or not re.match(MODEL_PATTERN, effective):
        raise _invalid("missing or malformed model")
    answers, usage = raw.get("answers"), raw.get("usage")
    if not isinstance(answers, dict) or not isinstance(usage, dict):
        raise _invalid("answers and usage must be objects")
    if set(answers) != set(questions):
        raise _invalid("answers do not match the questions asked")

    validated: dict[str, dict[str, Any]] = {}
    problems: dict[str, str] = {}
    for key, question in questions.items():
        answer = answers[key]
        expected = {"boolean": "noul", "score": "score"}.get(question.type, "choice")
        try:
            if not isinstance(answer, dict) or answer.get("type") != expected:
                raise _invalid(f"answer {key!r} is malformed")
            if expected == "noul":
                validated[key] = _noul(key, answer)
            elif expected == "score":
                validated[key] = _score(key, question, answer)
            else:
                validated[key] = _choice(key, question, answer)
        except ProviderError as error:
            problems[key] = error.detail

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
        raise _invalid("usage is malformed")
    usage_out = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    return effective, validated, usage_out, problems


def _invalid(detail: str) -> ProviderError:
    return ProviderError("invalid_response", detail)


def _choice(key: str, question: Question, answer: dict[str, Any]) -> dict[str, Any]:
    labels = set(question.criteria)
    if (
        answer.get("choice") not in labels
        or not _probability(answer.get("confidence"))
        or not isinstance(answer.get("probabilities"), dict)
    ):
        raise _invalid(f"answer {key!r} is malformed")
    probabilities = answer["probabilities"]
    if set(probabilities) != labels:
        raise _invalid(f"answer {key!r} has malformed probabilities")
    probabilities = _distribution(key, probabilities)
    if probabilities[answer["choice"]] < max(probabilities.values()) - 0.00001:
        raise _invalid(f"answer {key!r} choice is not the most probable label")
    # Jev's `confidence` summarises the shape of the whole distribution (docs.typesafe.ai/
    # confidence); the answer's confidence is the chosen label's probability, as for every other
    # provider, so policy thresholds and calibration mean the same thing across providers.
    return {
        "label": answer["choice"],
        "confidence": probabilities[answer["choice"]],
        "probabilities": probabilities,
        "jev_confidence": float(answer["confidence"]),
    }


def _noul(key: str, answer: dict[str, Any]) -> dict[str, Any]:
    p_yes = answer.get("noul")
    if not isinstance(p_yes, int | float) or not _probability(p_yes):
        raise _invalid(f"answer {key!r} is malformed")
    probabilities = {"yes": float(p_yes), "no": 1.0 - float(p_yes)}
    label = "yes" if probabilities["yes"] >= 0.5 else "no"
    return {"label": label, "confidence": probabilities[label], "probabilities": probabilities}


def _score(key: str, question: Question, answer: dict[str, Any]) -> dict[str, Any]:
    """Jev keys score levels by position; `legend` maps its keys to our ordered levels."""
    labels = question.labels
    levels = [question.criteria[label] for label in labels]
    legend, probabilities = answer.get("legend"), answer.get("probabilities")
    score = answer.get("score")
    if (
        not isinstance(legend, dict)
        or not isinstance(probabilities, dict)
        or set(probabilities) != set(legend)
        or not _probability(answer.get("confidence"))
        or not isinstance(score, int | float)
        or isinstance(score, bool)
        or not math.isfinite(score)
        or not 0 <= score <= len(labels) - 1
    ):
        raise _invalid(f"answer {key!r} is malformed")
    positions = _legend_positions(legend, levels)
    if positions is None:
        raise _invalid(f"answer {key!r} legend does not match the requested levels")
    by_label = {labels[positions[k]]: p for k, p in probabilities.items()}
    distribution = _distribution(key, by_label)
    label = max(distribution, key=lambda item: distribution[item])
    # Confidence is the probability of the most likely level, as for every other provider.
    return {
        "label": label,
        "confidence": distribution[label],
        "probabilities": distribution,
        "jev_confidence": float(answer["confidence"]),
    }


def _evidence(answer: Mapping[str, Any]) -> tuple[str, ...]:
    """Jev's own confidence statistic, kept visible next to the probabilities."""
    jev_confidence = answer.get("jev_confidence")
    return () if jev_confidence is None else (f"jev confidence {jev_confidence:.2f}",)


def _legend_positions(legend: dict[str, Any], levels: list[str]) -> dict[str, int] | None:
    """Accepts {"0": <criterion>, ...} or {<criterion>: 0, ...}; anything else is None."""
    count = len(levels)
    if set(legend) == {str(i) for i in range(count)}:
        return {k: int(k) for k in legend}
    if len(set(levels)) == count and set(legend) == set(levels):
        positions = {text: levels.index(text) for text in legend}
        if all(
            isinstance(v, int | float) and not isinstance(v, bool) and v == positions[k]
            for k, v in legend.items()
        ):
            return positions
    return None


def _distribution(key: str, probabilities: dict[str, Any]) -> dict[str, float]:
    if not all(map(_probability, probabilities.values())):
        raise _invalid(f"answer {key!r} has malformed probabilities")
    if abs(sum(probabilities.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
        raise _invalid(f"answer {key!r} probabilities do not sum to 1")
    return {label: float(p) for label, p in probabilities.items()}


def _probability(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _require_secure(endpoint: str) -> None:
    require_secure(endpoint, "Jev")
