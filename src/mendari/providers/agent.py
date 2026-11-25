# SPDX-License-Identifier: Apache-2.0
"""AI Agent provider: wrap any external HTTP AI agent inside Mendari's governance engine.

An `agent` variant sends the agent's typed questions and (optionally filtered) input to its
service, an external HTTP agent (OpenAI, Claude, LangGraph, or any custom one), and validates the
service's structured answers against the agent's declared question schemas. Any HTTP error,
timeout, or malformed response raises `ProviderError` so Mendari's deterministic policy engine
fails closed (`HUMAN_REVIEW` / `ABSTAIN` / `BLOCK`) and records the failure in the audit trail.

Configuration & security:
- Where a live call goes is decided in one place, `AgentProvider.live`: the organization's own
  connection for the agent, set in the console (ADR-0021, `core/connections.py`); else, for an
  agent the deployment owns, the variant's own `endpoint`; else `MENDARI_AGENT_URL`; else none,
  and the variant answers from its canned examples.
- `MENDARI_AGENT_URL`: deployment-configured agent HTTP endpoint (https, or http for localhost).
- `MENDARI_AGENT_TOKEN`: optional bearer token sent ONLY when the endpoint comes from
  `MENDARI_AGENT_URL`, never to an agent-specified `endpoint` URL. An organization's connection
  carries its own token, sent only to that connection's endpoint.
- `MENDARI_PROVIDER_ALLOWED_ORIGINS`: when set, the agent endpoint's origin must be listed
  (`providers/http.py`). Without it, an organization's connection leads to the public internet
  only (`providers/reach.py`).
- An agent the deployment does not own (`Agent.bounded`) must name `state_fields`, may not set
  `endpoint`, and has its `timeout_seconds` capped by the deployment (`core/origin.py`).
- `mock_file`: optional agent-relative JSON or YAML fixture used offline when no live endpoint is
  configured, so agents validate, test, and evaluate cleanly without network calls, and
  switch to a real agent automatically when `MENDARI_AGENT_URL` is set. Its answers are canned
  (`CANNED`): on a server with sign-in, a decision on them never automates (HUMAN_REVIEW).
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeGuard

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mendari.core import actions as proposals
from mendari.core.agent import Agent
from mendari.core.spec import (
    ActionSpec,
    AgentManifest,
    Question,
    WrittenOutput,
    deployment_env_problem,
)
from mendari.errors import AgentValidationError, MendariError, ProviderError
from mendari.providers import agent_calibration, deadline, fixture, reach
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
from mendari.providers.http import (
    allowed_origins,
    post_json,
    require_allowed,
    require_https,
    require_secure,
)
from mendari.providers.repeat import MAX_RUNS, MIN_RUNS, repeated
from mendari.standards.fhir_client import origin_of

ENDPOINT_ENV = "MENDARI_AGENT_URL"
TOKEN_ENV = "MENDARI_AGENT_TOKEN"
# Why an answer read from the variant's `mock_file` is canned (`ProviderOutput.canned`): a server
# with sign-in never automates on it (`policy.apply_policy`'s `canned` cap). Stable: the
# dashboard and the audit trail show it.
CANNED = f"canned answers: {ENDPOINT_ENV} is not set"

# Where a live call's endpoint came from (`AgentProvider.live`).
Source = Literal["organization", "agent", "deployment"]


@dataclass(frozen=True)
class Connection:
    """An organization's own connection for an agent (ADR-0021): the endpoint its
    administrators set in the console, and the bearer token to send there, if they gave one.
    The token is never shown: it is in no `repr`, log line, answer or record."""

    endpoint: str
    token: str | None = field(default=None, repr=False)
    # Whether an operator of the deployment allowed this endpoint's origin for this agent in
    # this organization (`core/connections.py`): where the deployment's allow-list does not
    # list it, it is called all the same, at public addresses only.
    allowed: bool = False


# The connection the organization a call acts for set for an agent, by the agent's id; None for
# none. A server gives its own (`Decision`'s `connection_source`); the command line has none.
ConnectionSource = Callable[[str], Connection | None]


@dataclass(frozen=True)
class Live:
    """Where one live call goes: its endpoint, the token to send there (never another
    endpoint's) and which setting named it."""

    url: str
    source: Source
    token: str | None = field(default=None, repr=False)
    # An organization's connection whose origin an operator allowed (`Connection.allowed`).
    allowed: bool = False


# Same character class as `evals/share.py` `_MODEL_PART` so provenance ids are always shareable.
SERVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,127}$"
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
    service_id: str | None = Field(default=None, pattern=SERVICE_ID_PATTERN)
    system_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    instructions: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS, gt=0.0, le=120.0, allow_inf_nan=False
    )
    # Minimum necessary: when set, only these input fields are sent to the service.
    state_fields: list[str] | None = None
    # Agent-relative JSON or YAML fixture for offline testing and evaluation.
    mock_file: str | None = None
    # Spec 0.4 (ADR-0024). Per-question temperatures fitted by `mendari calibrate` (a file in
    # the agent), for the version its service reported then: `agent_calibration.py`.
    calibration: str | None = None
    # Ask each case this many times and never automate when the runs disagree: `repeat.py`.
    repeat: int | None = Field(default=None, ge=MIN_RUNS, le=MAX_RUNS)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _reject_bool_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("timeout_seconds must be a number, not a boolean")
        return value


class AgentProvider(Provider):
    type = "agent"
    remote = True
    proposes = True

    def __init__(self, *args: Any, client: httpx.Client | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        settings, problems = self._settings(
            self.info.slot, self.config, self.questions, bounded=self.agent.bounded
        )
        if settings is None:
            raise AgentValidationError(self.agent.id, problems)
        self.settings = settings
        self._client = client
        self._connection: ConnectionSource | None = None
        self._mock: dict[str, Any] | None = None
        # What the manifest asks this slot's agent to write (spec 0.4 section 17). A composite's
        # member writes nothing: its response's `outputs` key is not read.
        self.outputs = slot_outputs(self.agent.manifest, self.info.slot, self.info.variant)
        if self.settings.mock_file is not None:
            self._mock = _load_fixture_file(
                self.agent,
                self.settings.mock_file,
                self.questions,
                self.info.slot,
                actions=self.proposes,
                outputs=self.outputs,
            )
        if self.settings.service_id is not None:
            self.info = replace(self.info, model=self.settings.service_id)
        self.calibration: agent_calibration.Calibration | None = None
        if self.settings.calibration is not None:
            self.calibration = agent_calibration.load(
                self.agent, self.info.slot, self.settings.calibration, self.questions
            )

    @classmethod
    def config_problems(
        cls,
        agent: Agent,
        where: str,
        config: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        bounded: bool,
    ) -> list[str]:
        settings, problems = cls._settings(where, config, questions, bounded=bounded)
        if settings is not None and settings.calibration is not None:
            problems = agent_calibration.file_problems(agent, where, settings.calibration)
            if problems:
                return problems
        if settings is None or settings.mock_file is None:
            return problems
        mock_file = settings.mock_file
        data, file_problems = read_problems(lambda: _read_agent_fixture(agent, mock_file))
        if file_problems:
            return file_problems
        return _fixture_data_problems(
            where,
            mock_file,
            data,
            questions,
            actions=cls.proposes,
            outputs=_outputs_at(agent.manifest, where),
        )

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
                    f"{where}: an `agent` variant of a registry or store agent may not set "
                    f"endpoint; configure {ENDPOINT_ENV} in the deployment"
                ]
            try:
                require_https(settings.endpoint, "Agent")
            except ProviderError as error:
                return None, [f"{where}: {error.detail or error.code}"]
        return settings, []

    def use_connection(self, source: ConnectionSource) -> None:
        """Where an organization's own connection for this agent is read from (a server's
        `Decision` wires it in, as it wires the budget): asked once for each call, for the
        organization that call acts for."""
        self._connection = source

    def live(self) -> Live | None:
        """Where a live call goes, or None when nothing is set and the variant answers from its
        canned examples. The order, stated here once (ADR-0021):

        1. **The organization's own connection** for this agent, which its administrators set
           in the console (`core/connections.py`), with that connection's own token. One that
           is set and cannot be read (its token does not open) fails the call
           (`credential_unavailable`): the call never falls through to another service.
        2. **The variant's own `endpoint`**, for an agent the deployment owns only (a bounded
           agent may not name one), with no token.
        3. **The deployment's `MENDARI_AGENT_URL`**, with `MENDARI_AGENT_TOKEN`.
        4. **None**: the variant's canned answers (`mock_file`), which never automate on a
           server with sign-in.

        A token goes only to the endpoint it was set with: an organization's to its own
        connection, `MENDARI_AGENT_TOKEN` to `MENDARI_AGENT_URL`, none to a manifest's."""
        if self._connection is not None:
            try:
                connection = self._connection(self.agent.id)
            except ProviderError:
                raise
            except MendariError:
                raise ProviderError(
                    "credential_unavailable", "this agent's connection cannot be read"
                ) from None
            if connection is not None:
                return Live(
                    connection.endpoint, "organization", connection.token, connection.allowed
                )
        if not self.agent.bounded and self.settings.endpoint:
            return Live(self.settings.endpoint, "agent")
        env_url = os.environ.get(ENDPOINT_ENV, "").strip()
        if env_url:
            return Live(env_url, "deployment", os.environ.get(TOKEN_ENV, "").strip() or None)
        return None

    @property
    def has_canned_examples(self) -> bool:
        """Whether the variant carries canned answers (`mock_file`) to answer from offline."""
        return self._mock is not None

    def _secure(self, live: Live) -> httpx.Client | None:
        """Check that `live` may be called, and the client to call it with (None: the
        deployment's shared one). Every endpoint is https (http to this machine only) and, where
        the deployment lists the origins its providers may be called at, one of them. An
        organization's connection leads to the public internet only where the deployment lists
        no origin, and where it lists some and not this one, which an operator allowed for this
        agent in this organization (`Live.allowed`): its host is checked here, and its
        addresses by the client that connects (`providers/reach.py`). An origin on the list is
        the deployment's own word, and is called as its own endpoints are."""
        if live.source != "organization":
            require_secure(live.url, "Agent")
            return self._client
        require_https(live.url, "Agent")
        try:
            listed = allowed_origins()
        except MendariError:
            require_allowed(live.url, "Agent")  # a list that cannot be read refuses every call
            raise
        if listed is not None and origin_of(live.url) in listed:
            return self._client
        if listed is not None and not live.allowed:
            require_allowed(live.url, "Agent")  # refused, in the list's own words
        if self._client is not None:
            return self._client
        reach.check_name(live.url)
        return reach.public_client()

    @property
    def canned(self) -> str | None:
        """Why its answers are canned now (`CANNED`): no live endpoint is set, and its
        `mock_file` answers. None when it calls an agent, or has no mock file to answer from.
        A connection that is set and cannot be read is a live one that fails (`live`): its
        answers are not canned."""
        if self._mock is None:
            return None
        try:
            return CANNED if self.live() is None else None
        except ProviderError:
            return None

    @property
    def canned_answers_vary(self) -> bool:
        """Whether its mock file can answer two inputs differently: it has `cases` to match
        against an input. With a `default` alone, every input gets the same answers."""
        return bool(self._mock and self._mock.get("cases"))

    def preflight(self) -> None:
        live = self.live()
        if live is not None:
            self._secure(live)
            return
        if self._mock is not None:
            return
        raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")

    def answer(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        """The agent's answers as the policy sees them: each call's confidence calibrated
        (`agent_calibration.calibrated`), and the call repeated when the variant says so
        (`repeat.repeated`). `answer_once` is one call, as the service answered it."""
        return repeated(
            self, self.settings.repeat, lambda: self._calibrated(self.answer_once(inputs))
        )

    def _calibrated(self, output: ProviderOutput) -> ProviderOutput:
        if self.calibration is None:
            return output
        return agent_calibration.calibrated(
            self.calibration, output, self.questions, self.settings.service_id
        )

    def answer_once(self, inputs: Mapping[str, Any]) -> ProviderOutput:
        live = self.live()
        if live is None:
            if self._mock is not None:
                return self._answer_fixture(inputs)
            raise ProviderError("not_configured", f"set {ENDPOINT_ENV}")

        client = self._secure(live)
        refuse_if_over_quota()
        deadline.refuse_if_past()

        body = request_body(
            self.agent.id,
            self.info.slot,
            self.questions,
            inputs,
            service_id=self.settings.service_id,
            system_prompt=self.settings.system_prompt,
            instructions=self.settings.instructions,
            state_fields=self.settings.state_fields,
            actions=self.agent.manifest.actions,
            outputs=self.outputs,
        )
        if len(json.dumps(body)) > MAX_REQUEST_CHARS:
            raise ProviderError("invalid_input", "request exceeds the agent size limit")

        self.charge()
        deadline.refuse_if_past()
        self.sending()

        headers = {"idempotency-key": str(uuid.uuid4())}
        if live.token:
            headers["authorization"] = f"Bearer {live.token}"
        raw = post_json(
            client,
            live.url,
            body,
            headers=headers,
            timeout_s=self.settings.timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
            retries=1,
            # Off the deployment's list and allowed by an operator: `_secure` just read the
            # list, checked the host, and gave the client that opens public addresses only.
            allowed=live.source == "organization" and live.allowed,
        )
        parsed = validate_response(raw, self.questions, actions=self.proposes, outputs=self.outputs)
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
        parsed = validate_response(
            matched, self.questions, actions=self.proposes, outputs=self.outputs
        )
        return self._build_output(parsed, used_mock=True)

    def _build_output(self, parsed: dict[str, Any], *, used_mock: bool) -> ProviderOutput:
        reported_model: str | None = parsed["model"]
        service_version: str | None = parsed["service_version"]
        tool_calls: list[Any] = parsed["tool_calls"]
        usage: dict[str, Any] = parsed["usage"]

        effective = _effective_model(reported_model, self.settings.service_id, service_version)
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
        if self.settings.service_id is not None:
            extra["service_id"] = self.settings.service_id
        if reported_model is not None:
            extra["model"] = reported_model
        if service_version is not None:
            extra["service_version"] = service_version
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
            actions=tuple(
                proposals.Proposed(item["name"], item["arguments"], slot=self.info.slot)
                for item in parsed.get("actions", ())
            ),
            outputs=dict(parsed.get("outputs", {})),
        )


def slot_outputs(manifest: AgentManifest, slot: str, variant: str) -> dict[str, WrittenOutput]:
    """The written outputs the manifest declares for `slot`, in its order, when `variant` is one
    of the slot's own variants and an `agent`; none otherwise (a composite's member, whose
    variant is named `<variant>.<member>`, writes nothing)."""
    declared = manifest.providers.get(slot)
    own = declared.variants.get(variant) if declared is not None else None
    if own is None or own.type != AgentProvider.type:
        return {}
    return {name: output for name, output in manifest.outputs.items() if output.slot == slot}


def _outputs_at(manifest: AgentManifest, where: str) -> dict[str, WrittenOutput]:
    """`slot_outputs` for the variant a config problem names (`providers.<slot>.variants.<v>`)."""
    for slot, declared in manifest.providers.items():
        for variant in declared.variants:
            if where == f"providers.{slot}.variants.{variant}":
                return slot_outputs(manifest, slot, variant)
    return {}


def request_body(
    agent_id: str,
    slot: str,
    questions: Mapping[str, Question],
    inputs: Mapping[str, Any],
    *,
    service_id: str | None = None,
    system_prompt: str | None = None,
    instructions: str | None = None,
    state_fields: list[str] | None = None,
    actions: Mapping[str, ActionSpec] | None = None,
    outputs: Mapping[str, WrittenOutput] | None = None,
) -> dict[str, Any]:
    """The JSON payload POSTed to an external agent endpoint. `actions` are the actions the
    agent's manifest declares (spec 0.4 section 4.1), sent in the manifest's order; an agent
    that declares none gets no `actions` key, and its requests are what they always were.
    `outputs` are the texts the manifest asks this slot's agent to write (section 17), each
    `{name, description, max_chars}`, likewise left out when there are none."""
    state = select_fields(inputs, state_fields) if state_fields is not None else dict(inputs)
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "slot": slot,
        "service_id": service_id,
        "questions": [agent_question(key, question) for key, question in questions.items()],
        "input": state,
    }
    if system_prompt is not None:
        body["system_prompt"] = system_prompt
    if instructions is not None:
        body["instructions"] = instructions
    if actions:
        body["actions"] = [spec.request(name) for name, spec in actions.items()]
    if outputs:
        body["outputs"] = [output.request(name) for name, output in outputs.items()]
    return body


def agent_question(key: str, question: Question) -> dict[str, Any]:
    """One of the agent's questions, serialized for its service."""
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


def validate_response(
    raw: Any,
    questions: Mapping[str, Question],
    *,
    actions: bool = False,
    outputs: Mapping[str, WrittenOutput] | None = None,
) -> dict[str, Any]:
    """Strict validation of an external agent's JSON response. Raises `invalid_response`.
    With `actions` (an `agent` variant's), the response's proposed `actions` are read and held to
    the contract's shape (`_validate_actions`); without, the key is not read at all. With
    `outputs` (what the manifest asks this slot to write), the response's `outputs` are read
    and held to them (`_validate_outputs`); with none declared the key is not read either."""
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
    service_version = _optional_id(raw.get("service_version"), "service_version")
    tool_calls = _validate_tool_calls(raw.get("tool_calls"))
    usage = _validate_usage(raw.get("usage"))

    validated = {
        "answers": validated_answers,
        "model": model,
        "service_version": service_version,
        "tool_calls": tool_calls,
        "usage": usage,
    }
    if actions:
        validated["actions"] = _validate_actions(raw.get("actions"))
    if outputs:
        validated["outputs"] = _validate_outputs(raw.get("outputs"), outputs)
    return validated


# Control characters a written text may not carry: all but the newline and the tab.
_OUTPUT_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")


def _validate_outputs(raw: Any, declared: Mapping[str, WrittenOutput]) -> dict[str, str]:
    """What a response wrote (spec 0.4 section 17), by name: an object of texts, each for an
    output the manifest declares for this slot and no longer than its `max_chars`. Anything else
    is a malformed response (`invalid_response`), and the detail never echoes a text. Control
    characters other than the newline and the tab become a space; a text that is then blank is
    no output, as one left out is."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _invalid("outputs must be an object")
    if set(raw) - set(declared):
        raise _invalid("outputs names an output this slot does not declare")
    written: dict[str, str] = {}
    for name, output in declared.items():
        if name not in raw:
            continue
        text = raw[name]
        if not isinstance(text, str):
            raise _invalid(f"output {name!r} is not text")
        if len(text) > output.max_chars:
            raise _invalid(f"output {name!r} is longer than its max_chars")
        cleaned = clean_output(text)
        if cleaned:
            written[name] = cleaned
    return written


def clean_output(text: str) -> str:
    """A written text as it is kept and graded: control characters other than the newline and
    the tab a space, and no blank at either end. Empty for a text that says nothing."""
    return _OUTPUT_CTRL.sub(" ", text).strip()


def _validate_actions(raw: Any) -> list[dict[str, Any]]:
    """The actions a response proposes (spec 0.4 section 4.2), each `{name, arguments}`: a list
    of at most `MAX_PROPOSED` objects with `name` and optional `arguments` (an object within
    the limits) and no other key. Anything else is a malformed response (`invalid_response`),
    and the detail never echoes a name or a value. Whether a name is declared, and whether its
    arguments match its schema, is not checked here: that blocks one action, not the response
    (`core/actions.py` `check`)."""
    problem = proposals.proposals_problem(raw)
    if problem is not None:
        raise _invalid(problem)
    return [
        {"name": item["name"], "arguments": dict(item.get("arguments", {}))} for item in raw or ()
    ]


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
    if not isinstance(value, str) or "://" in value or not re.fullmatch(SERVICE_ID_PATTERN, value):
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
    reported_model: str | None, service_id: str | None, service_version: str | None
) -> str | None:
    if reported_model is not None:
        return reported_model
    if service_id is not None and service_version is not None:
        combined = f"{service_id}@{service_version}"
        if len(combined) <= 128:
            return combined
    return service_id or service_version


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


def _read_agent_fixture(agent: Agent, relative: str) -> Any:
    if relative.endswith((".yaml", ".yml")):
        return agent.read_yaml(relative)
    return agent.read_json(relative)


def _load_fixture_file(
    agent: Agent,
    relative: str,
    questions: Mapping[str, Question],
    where: str,
    *,
    actions: bool = False,
    outputs: Mapping[str, WrittenOutput] | None = None,
) -> dict[str, Any]:
    data = _read_agent_fixture(agent, relative)
    problems = _fixture_data_problems(
        where, relative, data, questions, actions=actions, outputs=outputs
    )
    if problems:
        raise AgentValidationError(agent.id, problems)
    assert isinstance(data, dict)
    return data


def _fixture_data_problems(
    where: str,
    relative: str,
    data: Any,
    questions: Mapping[str, Question],
    *,
    actions: bool = False,
    outputs: Mapping[str, WrittenOutput] | None = None,
) -> list[str]:
    if not isinstance(data, dict):
        return [f"{where}: {relative}: mock file must be a mapping"]
    allowed_keys = {"model", "service_version", "cases", "default"}
    extra = sorted(set(data) - allowed_keys)
    if extra:
        return [f"{where}: {relative}: unexpected mock file keys {extra}"]
    if "cases" not in data and "default" not in data:
        return [f"{where}: {relative}: mock file needs `cases` or `default`"]

    problems: list[str] = []
    for key in ("model", "service_version"):
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
        match_problem = fixture.match_problem(case["match"])
        if match_problem is not None:
            problems.append(f"{where}: {relative}: cases[{index}].match: {match_problem}")
            continue
        merged = _merge_fixture_response(data, case["response"])
        try:
            validate_response(merged, questions, actions=actions, outputs=outputs)
        except ProviderError as error:
            problems.append(f"{where}: {relative}: cases[{index}].response: {error.detail}")

    if "default" in data and data["default"] is not None:
        merged = _merge_fixture_response(data, data["default"])
        try:
            validate_response(merged, questions, actions=actions, outputs=outputs)
        except ProviderError as error:
            problems.append(f"{where}: {relative}: default: {error.detail}")

    return problems


def _merge_fixture_response(root: Mapping[str, Any], response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    merged = dict(response)
    if "model" not in merged and root.get("model") is not None:
        merged["model"] = root["model"]
    if "service_version" not in merged and root.get("service_version") is not None:
        merged["service_version"] = root["service_version"]
    return merged


def _match_fixture_response(mock: Mapping[str, Any], state: Mapping[str, Any]) -> Any | None:
    text = fixture.flatten(state)
    for case in mock.get("cases", []):
        if fixture.matches(case["match"], text, state):
            return _merge_fixture_response(mock, case["response"])
    if mock.get("default") is not None:
        return _merge_fixture_response(mock, mock["default"])
    return None
