# SPDX-License-Identifier: Apache-2.0
"""Mendari's own code systems and identifier systems as FHIR R4 CodeSystem and NamingSystem
resources, generated from the code and the agents a deployment serves, so they cannot drift from
what Mendari emits. The server serves them and CI loads them into the HL7 validator
(`scripts/fhir_terminology.py`), so a code Mendari emits that its CodeSystem lacks is an error.

| CodeSystem | Codes | In |
|---|---|---|
| `…/fhir/CodeSystem/decision-outcome` | `Outcome` | Task.businessStatus |
| `…/fhir/CodeSystem/audit-event` | events chained on a decision | AuditEvent.subtype |
| `…/fhir/CodeSystem/agent-version` | `id@version` of the agents served | Task.meta.tag |
| `…/fhir/CodeSystem/card-override-reasons` | why a card was overridden | `overrideReasons` |
| an agent's `task.performer_system` | what `FhirAdapter.to_task` routes to | Task.performerType |
| `…/agents/{agent}/CodeSystem/{question}` | labels, by their criteria | Task.output, `$decide` |

The agent-version system is a `fragment`: decisions made by packs no longer served keep their
codes. Agents that share a performer system share one CodeSystem, with the union of their codes.
The NamingSystems are for decision ids, input hashes, and agent versions acting as the engine.

**Versions.** One `url|version` must never name two contents, in any deployment. The fixed
systems carry the Mendari version (a test pins their content to it). A system whose content
depends on the agents served carries, after the Mendari version, a digest of those agents' ids and
digests; an agent's question systems carry the agent's version and its digest prefix.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from mendari import __version__
from mendari.core.agent import Agent
from mendari.core.spec import Outcome
from mendari.errors import MendariError
from mendari.sandbox.code_systems import CHANNEL_SYSTEM, CHANNELS, SANDBOX_TAG
from mendari.standards import audit_fhir
from mendari.standards.fhir import (
    AGENT_ID_SYSTEM,
    AGENT_TAG_SYSTEM,
    DECISION_ID_SYSTEM,
    MANUAL_TRIAGE,
    OUTCOME_SYSTEM,
    FhirAdapter,
    display,
    narrative,
)

PUBLISHER = "Mendari"
NAMING_SYSTEMS_DATE = "2026-10-02"  # when their definitions last changed (pinned by a test)
_OWN = "https://mendari.co/fhir/CodeSystem/"
_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}\Z")
# Where an agent's own CodeSystems live, one per question (`question_system`).
AGENTS = "https://mendari.co/agents"

OUTCOMES: dict[Outcome, tuple[str, str]] = {
    Outcome.AUTO: (
        "Automated",
        "The recommendation may be acted on without a person in the loop (Task.status ready).",
    ),
    Outcome.HUMAN_REVIEW: (
        "Human review",
        "A person confirms or corrects the recommendation before anyone acts on it "
        "(Task.status requested).",
    ),
    Outcome.ABSTAIN: (
        "Abstain",
        "No recommendation: the existing manual workflow handles the case. Any proposed action "
        "is the agent's fallback, never a recommended action.",
    ),
    Outcome.BLOCK: (
        "Block",
        "The action must not happen: the input was invalid or a blocking policy rule matched. "
        "Any proposed action is the agent's fallback, never the blocked action.",
    ),
}
_ACTIONS = {"C": "create", "R": "read", "U": "update", "D": "delete", "E": "execute"}
# Why a clinician overrode a CDS Hooks card (its `overrideReasons`, and the reason card feedback
# may name, LP-23f): three neutral codes (owner decision 8), each with its display.
OVERRIDE_REASONS_SYSTEM = f"{_OWN}card-override-reasons"
OVERRIDE_REASONS: dict[str, tuple[str, str]] = {
    "not-relevant": ("Not relevant", "The card does not apply to this patient or this order."),
    "already-addressed": (
        "Already addressed",
        "What the card suggests is already done, or under way.",
    ),
    "clinical-judgement": (
        "Clinical judgement",
        "The clinician decided otherwise, on their own clinical judgement.",
    ),
}
NAMING_SYSTEMS = (
    (
        "decision-id",
        DECISION_ID_SYSTEM,
        "Mendari decision id",
        "A decision's id: the Task's identifier, and what its AuditEvents and Provenances target.",
    ),
    (
        "input-hash",
        audit_fhir.INPUT_HASH_SYSTEM,
        "Mendari input hash",
        "A hash of a decision's input, as Provenance.entity identifies it; the input itself is "
        "never exported. Under a deployment's input hash key (required when it keeps PHI) it is "
        "`hmac-sha256:`, the key's id (16 hex digits), `:` and 64 hex digits: an HMAC-SHA256 "
        "only that deployment can recompute. Without a key it is `sha256:` and 64 hex digits, "
        "the plain SHA-256 of the input's canonical JSON.",
    ),
    (
        "audit-subject",
        audit_fhir.SUBJECT_SYSTEM,
        "Mendari audit subject",
        "What an administrative AuditEvent is about, by its chain id: a kind of record and "
        "its id, joined by a colon (`key:`, `client:`, `governance:`, `hold:`, `eval:`, "
        "`org:`, `membership:`, `invitation:` or `access:`, then the id), or one of the "
        "deployment's own chain ids.",
    ),
    (
        "agent-version",
        AGENT_ID_SYSTEM,
        "Mendari agent version",
        "An agent version (`namespace/name@version`) acting as the engine, in "
        "AuditEvent.agent and Provenance.agent. In Task.meta.tag the same value is a code of "
        f"{AGENT_TAG_SYSTEM}.",
    ),
)


@dataclass(frozen=True)
class Terminology:
    """The terminology resources for the agents a deployment serves, found by id or by url."""

    resources: tuple[dict[str, Any], ...]

    @classmethod
    def for_agents(cls, agents: Iterable[Agent]) -> Terminology:
        return cls(tuple(resources(agents)))

    @classmethod
    def serving(cls, agents: Iterable[Agent]) -> tuple[Terminology, dict[str, str]]:
        """The terminology for every agent that can have one, and why the others cannot (a FHIR
        mapping that does not load, an id or url another agent already uses). A server skips
        such an agent's terminology and reports it, rather than not starting."""
        kept: list[Agent] = []
        problems: dict[str, str] = {}
        for agent in sorted(agents, key=lambda p: p.id):
            try:
                resources([*kept, agent])
            except MendariError as error:
                problems[agent.id] = f"terminology: {error}"
                continue
            except Exception as error:  # the agent's content, not Mendari's: named, not quoted
                problems[agent.id] = f"terminology: {type(error).__name__}"
                continue
            kept.append(agent)
        return cls.for_agents(kept), problems

    def get(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        return next(
            (
                r
                for r in self.resources
                if r["resourceType"] == resource_type and r["id"] == resource_id
            ),
            None,
        )

    def by_url(self, url: str, version: str | None = None) -> dict[str, Any] | None:
        """A CodeSystem by its canonical url (and version), or a NamingSystem by its uri."""
        for resource in self.resources:
            if resource["resourceType"] == "CodeSystem":
                if resource["url"] == url and version in (None, resource["version"]):
                    return resource
            elif version is None and any(u["value"] == url for u in resource["uniqueId"]):
                return resource
        return None


def resources(agents: Iterable[Agent]) -> list[dict[str, Any]]:
    """Every CodeSystem and NamingSystem for these agents (one version of each, as served)."""
    served = sorted(agents, key=lambda agent: agent.id)
    ids = [agent.id for agent in served]
    repeated = sorted({agent_id for agent_id in ids if ids.count(agent_id) > 1})
    if repeated:
        raise MendariError(f"terminology for one version of each agent, not several of {repeated}")
    found = [
        _outcomes(),
        _audit_events(),
        _administrative_events(),
        _override_reasons(),
        _agent_versions(served),
        *_performers(served),
        *(system for agent in served for system in _questions(agent)),
        *(_naming_system(*spec) for spec in NAMING_SYSTEMS),
    ]
    _check(found)
    return found


def sandbox_resources() -> list[dict[str, Any]]:
    """The sandbox's own systems (`sandbox/code_systems.py`), experimental and never served:
    its synthetic-data tag and its Communications' message channel. CI loads them so the
    sandbox's samples validate."""
    return [
        _code_system(
            SANDBOX_TAG["system"],
            "sandbox",
            "Mendari sandbox tag",
            "Marks a resource Mendari's sandbox generated.",
            __version__,
            [_concept("synthetic", "Synthetic", "Generated test data, never a real person's.")],
            experimental=True,
        ),
        _code_system(
            CHANNEL_SYSTEM,
            "message-channel",
            "Mendari sandbox message channel",
            "How a synthetic patient message arrived (Communication.medium).",
            __version__,
            [_concept(code, shown) for code, shown in CHANNELS.items()],
            experimental=True,
        ),
    ]


def question_system(agent_id: str, question: str) -> str:
    """The CodeSystem of a question's labels (`_questions` generates it): what a Task's outputs
    (`task_fhir.py`) and `$decide`'s answers (`decide_op.py`) code a label in."""
    return f"{AGENTS}/{agent_id}/CodeSystem/{question}"


def resource_id(text: str) -> str:
    """A valid FHIR id from any text: a slug, cut to 64 characters with a hash suffix."""
    slug = re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")
    if slug and len(slug) <= 64:
        return slug
    return f"{slug[:55].rstrip('-.')}-{hashlib.sha256(text.encode()).hexdigest()[:8]}".lstrip("-")


def override_reasons() -> list[dict[str, str]]:
    """The reasons a card offers for overriding it (CDS Hooks 2.0 `overrideReasons`), as Codings
    of `OVERRIDE_REASONS_SYSTEM`, each with its display."""
    return [
        {"system": OVERRIDE_REASONS_SYSTEM, "code": code, "display": shown}
        for code, (shown, _) in OVERRIDE_REASONS.items()
    ]


def _override_reasons() -> dict[str, Any]:
    return _code_system(
        OVERRIDE_REASONS_SYSTEM,
        "card-override-reasons",
        "Mendari card override reason",
        "Why a clinician overrode a CDS Hooks card from Mendari without taking its suggestion: "
        "the card's overrideReasons, and the reason its feedback may give.",
        __version__,
        [_concept(code, shown, meaning) for code, (shown, meaning) in OVERRIDE_REASONS.items()],
    )


def _outcomes() -> dict[str, Any]:
    return _code_system(
        OUTCOME_SYSTEM,
        "decision-outcome",
        "Mendari decision outcome",
        "What the runtime may do with a decision, as Task.businessStatus.",
        __version__,
        [_concept(outcome.value, *OUTCOMES[outcome]) for outcome in Outcome],
    )


def _audit_events() -> dict[str, Any]:
    concepts = [
        _concept(
            event,
            shown,
            f"{shown}: chained in Mendari's audit trail, exported as an AuditEvent with action "
            f"{action} ({_ACTIONS[action]}).",
        )
        for event, (action, shown) in audit_fhir._EVENTS.items()
    ]
    return _code_system(
        audit_fhir.EVENT_SYSTEM,
        "audit-event",
        "Mendari audit event",
        "The events of a decision's hash-chained audit trail, as AuditEvent.subtype.",
        __version__,
        concepts,
    )


def _administrative_events() -> dict[str, Any]:
    concepts = [
        _concept(
            event,
            shown,
            f"{shown}: chained in Mendari's audit trail on no decision, exported as an "
            f"AuditEvent with action {action} ({_ACTIONS[action]}).",
        )
        for event, (action, shown, _) in audit_fhir._ADMINISTRATIVE.items()
    ]
    return _code_system(
        audit_fhir.ADMINISTRATIVE_SYSTEM,
        "audit-administrative-event",
        "Mendari administrative audit event",
        "The events of Mendari's audit trail that are on no decision (an organization's keys, "
        "clients, people, approvals, holds and reports, and the deployment's own), as "
        "AuditEvent.subtype.",
        __version__,
        concepts,
    )


def _agent_versions(served: list[Agent]) -> dict[str, Any]:
    return _code_system(
        AGENT_TAG_SYSTEM,
        "agent-version",
        "Mendari agent",
        "Agent versions (`namespace/name@version`), as Task.meta.tag. Lists the agents "
        "this deployment serves; decisions made by other agents or versions keep their codes, so "
        "this is a fragment.",
        _served_version(served),
        [
            _concept(agent.ref, f"{agent.manifest.name} {agent.version}", agent.manifest.summary)
            for agent in served
        ],
        content="fragment",
    )


def _performers(served: list[Agent]) -> Iterator[dict[str, Any]]:
    systems: dict[str, list[FhirAdapter]] = {}
    for agent in served:
        if agent.manifest.inputs.fhir is not None:
            adapter = FhirAdapter.for_agent(agent)
            systems.setdefault(adapter.mapping.task.performer_system, []).append(adapter)
    for url, adapters in sorted(systems.items()):
        concepts: dict[str, dict[str, str]] = {}
        for adapter in adapters:
            for code, values in adapter.performer_codes().items():
                concepts.setdefault(code, _performer(adapter.agent, code, values))
        refs = ", ".join(f"`{adapter.agent.ref}`" for adapter in adapters)
        shown = _human(url.rstrip("/").rsplit("/", 1)[-1])
        yield _code_system(
            url,
            resource_id(url.removeprefix(_OWN) if url.startswith(_OWN) else url.split("//")[-1]),
            f"Mendari performer: {shown}",
            f"Who handles a decision, as Task.performerType: the codes {refs} can route to "
            "(fhir/mapping.yaml, `task.performer`).",
            _served_version([adapter.agent for adapter in adapters]),
            list(concepts.values()),
        )


def _performer(agent: Agent, code: str, values: Mapping[str, str]) -> dict[str, str]:
    """A performer code, defined by the answer that routes to it."""
    manifest = agent.manifest
    if not values:
        return _concept(code, _human(code), f"Every {manifest.name} decision goes here.")
    if len(values) > 1:
        return _concept(code, _human(code), "; ".join(f"{k} {v}" for k, v in values.items()))
    ((name, label),) = values.items()
    if name == "priority":
        return _concept(code, _human(code), f"Priority {label}.")
    if name == "recommendation" and label == MANUAL_TRIAGE:
        return _concept(code, _human(code), "No recommendation: the manual workflow handles it.")
    question = manifest.questions[manifest.policy.primary if name == "recommendation" else name]
    if label not in question.criteria:
        return _concept(code, _human(code), f"`{name}` has no answer (its provider failed).")
    return _concept(code, _human(code), question.criteria[label])


def _questions(agent: Agent) -> Iterator[dict[str, Any]]:
    version = f"{agent.version}+{agent.digest.removeprefix('sha256:')[:8]}"
    for key, question in agent.manifest.questions.items():
        yield _code_system(
            question_system(agent.id, key),
            resource_id(f"{agent.id}-{key}"),
            f"{agent.manifest.name}: {key}",
            question.instructions,
            version,
            [
                _concept(label, question.criteria[label], question.criteria[label])
                for label in question.labels
            ],
        )


def _code_system(
    url: str,
    id_: str,
    title: str,
    description: str,
    version: str,
    concepts: list[dict[str, str]],
    *,
    content: str = "complete",
    experimental: bool = False,
) -> dict[str, Any]:
    rows = [(concept["code"], concept["display"]) for concept in concepts]
    name = _name(title)
    resource: dict[str, Any] = {
        "resourceType": "CodeSystem",
        "id": id_,
        "text": narrative(f"{title} ({url}, version {version}).", rows),
        "url": url,
        "version": version,
        "name": name,
        "title": title,
        "status": "active",
        "experimental": experimental,
        "publisher": PUBLISHER,
        "description": " ".join(description.split()),
        "caseSensitive": True,
        "content": content,
    }
    if content == "complete":
        resource["count"] = len(concepts)
    resource["concept"] = concepts
    return resource


def _naming_system(id_: str, uri: str, title: str, description: str) -> dict[str, Any]:
    return {
        "resourceType": "NamingSystem",
        "id": id_,
        "text": narrative(f"{title} ({uri}). {description}"),
        "name": _name(title),
        "status": "active",
        "kind": "identifier",
        "date": NAMING_SYSTEMS_DATE,
        "publisher": PUBLISHER,
        "description": description,
        "uniqueId": [{"type": "uri", "value": uri, "preferred": True}],
    }


def _concept(code: str, shown: str, definition: str | None = None) -> dict[str, str]:
    concept = {"code": code, "display": display(shown)}
    if definition:
        concept["definition"] = " ".join(definition.split())
    return concept


def _served_version(agents: Iterable[Agent]) -> str:
    """The Mendari version and a digest of the agents (ids and digests) a system is made from."""
    listed = "\n".join(sorted(f"{agent.id} {agent.digest}" for agent in agents))
    return f"{__version__}+{hashlib.sha256(listed.encode()).hexdigest()[:8]}"


def _name(title: str) -> str:
    """A computable name (PascalCase, starting with a capital letter, at most 255 characters)."""
    name = "".join(w[:1].upper() + w[1:] for w in re.split(r"[^A-Za-z0-9]+", title) if w)
    return (name if name[:1].isupper() else f"Mendari{name}")[:255]


def _human(code: str) -> str:
    return code.replace("_", " ").replace("-", " ").strip().capitalize() or code


def _check(found: list[dict[str, Any]]) -> None:
    """Ids are valid, and no two resources share an id or a url (e.g. an agent whose performer
    system is one of Mendari's fixed systems)."""
    seen: set[tuple[str, str]] = set()
    for resource in found:
        keys = [("id", f"{resource['resourceType']}/{resource['id']}")]
        if resource["resourceType"] == "CodeSystem":
            keys.append(("url", resource["url"]))
        for key in keys:
            if key in seen:
                raise MendariError(f"two terminology resources share the {key[0]} {key[1]}")
            seen.add(key)
        if not _ID.match(resource["id"]):
            raise MendariError(f"not a FHIR id: {resource['id']!r}")
