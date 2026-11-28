# SPDX-License-Identifier: Apache-2.0
"""Actions: what an agent asks to have done besides answer (spec §13, ADR-0023).

An agent's manifest declares the actions it may propose, each with an effect
(`core/spec.py` `ActionSpec`). The agent proposes some with its answers, or a caller asks about
one for a decision already recorded; this module checks a proposal against the declaration
(`check`), and reads a decision's actions back from its chained events (`state`). What an effect
needs before an action is released is the runtime's, beside the risk caps (`core/policy.py`
`EFFECT_GATES`, `disposition`). Mendari authorizes an action and records that it did: it runs
none, and holds no credential to a chart or to a patient channel.

Two things here are the agent's text and can hold patient data: an action's arguments, and a
name the manifest does not declare. Neither is ever part of a reason, a decision's stored
record, a log line or a chained event: the chain keeps a hash of the arguments and the declared
name only, and the text is kept apart as a decision's input is (`core/audit.py`, the event's
note). `ActionResult.chained` and `ActionResult.kept` are the two halves.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from jsonschema.protocols import Validator

from mendari.core import input_schema
from mendari.core.agent import not_json
from mendari.core.patterns import OutOfTime
from mendari.core.spec import ActionSpec, AgentManifest, Effect

MAX_PROPOSED = 16  # the actions one response proposes
MAX_ARGUMENT_CHARS = 16_000  # one action's arguments, as canonical JSON
MAX_ARGUMENT_DEPTH = 8
MAX_PROBLEMS = 10  # what one refused proposal says of its arguments
# An action's name, declared or proposed (spec sections 13.1 and 13.6).
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
# The same shape as an API description states it (a JSON Schema `pattern` has no `\Z`).
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PROPOSED = "action.proposed"  # the event of a proposal; its note is the kept arguments
RESOLVED = "action.resolved"  # the event of a held action's release or denial

# What may be done with a proposed action: `released` (the caller may run it), `held` (it waits
# for its decision's reviewer), `blocked` (it must not be run) or `denied` (a reviewer refused
# it). Only `held` changes afterwards, once, when its decision's review is resolved.
Disposition = Literal["released", "held", "blocked", "denied"]
Reason = Literal[
    # released
    "with_decision",  # its effect needs only a decision that is not BLOCK
    "automated",  # with an AUTO decision
    "confirmed",  # the reviewer confirmed the decision's recommendation
    # held
    "awaiting_review",
    # blocked
    "undeclared",  # the manifest declares no such action
    "invalid_arguments",  # its arguments do not match its schema
    "composite_member",  # proposed by a composite's member, which this version does not take
    "agent_changed",  # asked for a decision another version of the agent made
    "decision_blocked",  # its decision is BLOCK
    "no_recommendation",  # an abstention, or a review without a recommendation: nobody confirms
    "no_reviewer",  # needs a person, and its decision is not waiting for one
    "not_automated",  # the agent may not automate now (a hold, an approval no longer in force)
    "earlier_refusal",  # an earlier proposal on this decision was refused
    "review_closed",  # asked after the decision's review was resolved
    # denied
    "overridden",  # the reviewer overrode the decision's recommendation
    "reviewer_denied",  # the reviewer confirmed the recommendation and refused this action
]
# The reasons that refuse a proposal for what it is, whatever its decision: each also stops the
# decision from being automated (`policy.action_stops`).
REFUSALS: frozenset[str] = frozenset(
    {"undeclared", "invalid_arguments", "composite_member", "agent_changed"}
)
Source = Literal["answer", "request"]  # proposed with the agent's answer, or asked for afterwards


@dataclass(frozen=True)
class Proposed:
    """One action as proposed: its name and arguments, which provider slot proposed it, why
    its carrier refuses it, if it does (a composite sets `composite_member`), and whether a
    second signal did not agree on it (`disputed`: only some of a variant's repeated runs
    proposed it, or a check's second member proposed another; `providers/repeat.py`,
    `providers/check.py`). A disputed proposal is never released without a person
    (`policy.gate_of`)."""

    name: str
    arguments: Mapping[str, Any]
    slot: str | None = None
    refused: Reason | None = None
    disputed: bool = False


@dataclass(frozen=True)
class Checked:
    """A proposal held against the manifest: the declared action (None: undeclared), why it is
    refused if it is, and what is wrong with its arguments, each as "<where>: <schema rule>",
    never a value."""

    proposed: Proposed
    spec: ActionSpec | None
    refusal: Reason | None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionResult:
    """A proposed action and what may be done with it."""

    id: str
    name: str  # as proposed: the agent's text when not `declared`
    effect: Effect | None  # None: undeclared
    declared: bool
    disposition: Disposition
    reason: Reason
    arguments: Mapping[str, Any]
    arguments_hash: str
    source: Source = "answer"
    slot: str | None = None
    problems: tuple[str, ...] = ()
    # Whether the declared action's arguments can hold free text (`ActionSpec.free_text`,
    # ADR-0027): chained, so a review knows it must name the action to release it.
    free_text: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The action as its caller gets it back, arguments included: the caller sent them, or
        its own agent did."""
        shown: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "effect": None if self.effect is None else self.effect.value,
            "disposition": self.disposition,
            "reason": self.reason,
            "arguments": dict(self.arguments),
            "arguments_hash": self.arguments_hash,
        }
        if self.problems:
            shown["problems"] = list(self.problems)
        return shown

    def chained(self) -> dict[str, Any]:
        """The action as the audit chain keeps it: ids, codes and a hash. Never the arguments,
        and never a name the manifest does not declare."""
        chained: dict[str, Any] = {
            "action_id": self.id,
            "name": self.name if self.declared else None,
            "effect": None if self.effect is None or not self.declared else self.effect.value,
            "disposition": self.disposition,
            "reason": self.reason,
            "arguments_hash": self.arguments_hash,
            "source": self.source,
            "slot": self.slot,
        }
        if self.free_text and self.declared:  # only then: every other payload is as it was
            chained["free_text"] = True
        return chained

    def kept(self) -> str:
        """What is kept apart from the chain, as a decision's input is: the name as proposed and
        the arguments, as canonical JSON (the event's note)."""
        return canonical({"name": self.name, "arguments": self.arguments})


@dataclass(frozen=True)
class Asked:
    """What a caller is told of an action it asks about for a recorded decision
    (`Decision.authorize_action`), as the chain keeps it: `name` and `effect` only when the
    agent version that made the decision declares the action."""

    name: str | None
    effect: str | None
    disposition: Disposition
    reason: Reason
    free_text: bool = False  # as `ActionResult.free_text`: only for a declared action


def new_id() -> str:
    return str(uuid.uuid4())


def canonical(value: Any) -> str:
    """JSON as hashes and notes take it: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def plain_hash(arguments: Any) -> str:
    """The unkeyed `sha256:` of an action's arguments, for a decision made without a store (a
    store keys it under its deployment's input hash key, `PhiPolicy.arguments_hash`)."""
    return "sha256:" + hashlib.sha256(canonical(arguments).encode()).hexdigest()


def shape_problem(name: Any, arguments: Any) -> str | None:
    """Why this is not a proposal spec section 13.6 allows, or None: the name's shape, and
    arguments that are a JSON object within the limits. For both ways in: a response that
    breaks it is malformed (`invalid_response`), a request that does is refused (422). Never
    echoes the name or a value."""
    if not isinstance(name, str) or NAME.match(name) is None:
        return f"an action's name must match {NAME.pattern}"
    if not isinstance(arguments, dict):
        return "an action's arguments must be an object"
    depth = _depth(arguments, MAX_ARGUMENT_DEPTH)
    if depth is None:
        return "an action's arguments must have text keys"
    if depth > MAX_ARGUMENT_DEPTH:
        return f"an action's arguments nest at most {MAX_ARGUMENT_DEPTH} deep"
    unfit = not_json(arguments)
    if unfit is not None:
        return f"an action's arguments hold {unfit}"
    if len(canonical(arguments)) > MAX_ARGUMENT_CHARS:
        return f"an action's arguments are at most {MAX_ARGUMENT_CHARS:,} characters as JSON"
    return None


def _depth(value: Any, limit: int) -> int | None:
    """How deep objects and lists nest in `value`, counted without recursion and no further
    than one past `limit`; None for an object with a key that is not text."""
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                return None
            children: Iterable[Any] = item.values()
        elif isinstance(item, list | tuple):
            children = item
        else:
            continue
        deepest = max(deepest, depth)
        if deepest > limit:
            return deepest
        stack.extend((child, depth + 1) for child in children)
    return deepest


def proposals_problem(raw: Any) -> str | None:
    """Why the `actions` of an agent's response are not what the contract allows (spec
    section 13.6), or None: a list of at most `MAX_PROPOSED` objects, each `name` and optional
    `arguments` and no other key, each within `shape_problem`."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_PROPOSED:
        return f"actions must be a list of at most {MAX_PROPOSED}"
    for item in raw:
        if not isinstance(item, dict) or not {"name"} <= set(item) <= {"name", "arguments"}:
            return "an action is an object with `name` and optional `arguments`"
        problem = shape_problem(item["name"], item.get("arguments", {}))
        if problem is not None:
            return problem
    return None


def key(proposal: Proposed) -> tuple[str, str]:
    """What makes two proposals the same one: the name and the arguments, as canonical JSON."""
    return proposal.name, canonical(proposal.arguments)


def distinct(proposals: Iterable[Proposed]) -> list[Proposed]:
    """The proposals in order, two with the same name and the same arguments counted once: the
    first of them, and the stricter one where they differ (one that is refused, or disputed)."""
    kept: dict[tuple[str, str], Proposed] = {}
    for proposal in proposals:
        first = kept.setdefault(key(proposal), proposal)
        if (proposal.refused and not first.refused) or (proposal.disputed and not first.disputed):
            kept[key(proposal)] = replace(
                first,
                refused=first.refused or proposal.refused,
                disputed=first.disputed or proposal.disputed,
            )
    return list(kept.values())


def validators(manifest: AgentManifest) -> dict[str, Validator]:
    """A validator for each declared action that has an arguments schema. The schema is of the
    closed subset the manifest's load checked (`spec.action_schema_problems`): no pattern runs."""
    return {
        name: input_schema.validator(spec.json_schema, formats=False)
        for name, spec in manifest.actions.items()
        if spec.json_schema is not None
    }


def check(
    manifest: AgentManifest, checkers: Mapping[str, Validator], proposed: Proposed
) -> Checked:
    """Hold one proposal against the manifest. Its carrier's refusal wins; then a name the
    manifest does not declare is `undeclared`, and arguments that fail the action's schema are
    `invalid_arguments`, with at most `MAX_PROBLEMS` problems. What cannot be checked is
    refused: a check that runs out of time or depth is `invalid_arguments` too."""
    spec = manifest.actions.get(proposed.name)
    if proposed.refused is not None:
        return Checked(proposed, spec, proposed.refused)
    if spec is None:
        return Checked(proposed, None, "undeclared")
    checker = checkers.get(proposed.name)
    if checker is None:
        return Checked(proposed, spec, None)
    try:
        found = []
        for error in checker.iter_errors(dict(proposed.arguments)):
            found.append(f"{_where(error.path)}: {_rule(error.validator, error.validator_value)}")
            if len(found) >= MAX_PROBLEMS * 4:
                break
    except (OutOfTime, RecursionError, OverflowError):
        return Checked(proposed, spec, "invalid_arguments", ("(root): it cannot be checked",))
    if not found:
        return Checked(proposed, spec, None)
    return Checked(
        proposed, spec, "invalid_arguments", tuple(sorted(dict.fromkeys(found))[:MAX_PROBLEMS])
    )


_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _where(path: Iterable[Any]) -> str:
    """A path in the arguments. A key the schema does not name is the agent's text, so only a
    key shaped like a property's name is shown (the schema's objects allow no other)."""
    parts = [str(p) if isinstance(p, int) or _FIELD.match(str(p)) else "*" for p in path]
    return "/".join(parts) or "(root)"


def _rule(keyword: Any, expected: Any) -> str:
    """What the schema requires, in words built from the schema alone (`expected` is the
    manifest's own), never from the arguments."""
    if keyword == "required":
        return "a required field is missing"
    if keyword == "type":
        return f"must be of type {expected}"
    if keyword in ("enum", "const"):
        return "is not one of the values the schema allows"
    if keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        bound = "at least" if str(keyword).startswith("min") else "at most"
        unit = "characters" if str(keyword).endswith("Length") else "items"
        return f"must have {bound} {expected} {unit}"
    if keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        relation = {
            "minimum": "at least",
            "maximum": "at most",
            "exclusiveMinimum": "more than",
            "exclusiveMaximum": "less than",
        }[str(keyword)]
        return f"must be {relation} {expected}"
    if keyword == "additionalProperties":
        return "has a field the schema does not allow"
    return "does not meet the schema"


def state(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A decision's actions as they stand, read from its events in chain order: one item for
    each `action.proposed`, updated by its `action.resolved`. `arguments_status` is the
    proposal's note as `AuditStore.reveal_inputs` showed it (`open`, `sealed`, `erased` or
    `missing`; `not_kept` when nothing was kept); `arguments`, and for an undeclared action
    `proposed_name`, only when it is open."""
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        action_id = payload.get("action_id")
        if event.get("event") == PROPOSED and isinstance(action_id, str):
            item: dict[str, Any] = {
                "id": action_id,
                "name": payload.get("name"),
                "effect": payload.get("effect"),
                "disposition": payload.get("disposition"),
                "reason": payload.get("reason"),
                "source": payload.get("source"),
                "slot": payload.get("slot"),
                "arguments_hash": payload.get("arguments_hash"),
                "proposed_at": event.get("created_at"),
                "resolved_at": None,
                "resolved_by": None,
                "arguments_status": "not_kept",
                "arguments": None,
            }
            if isinstance(payload.get("note_ref"), Mapping):
                item["arguments_status"] = event.get("note_status") or "sealed"
                kept = _kept(event.get("note")) if item["arguments_status"] == "open" else None
                if kept is not None:
                    item["arguments"] = kept["arguments"]
                    if item["name"] is None:
                        item["proposed_name"] = kept["name"]
            found[action_id] = item
        elif event.get("event") == RESOLVED and action_id in found:
            found[action_id].update(
                disposition=payload.get("disposition"),
                reason=payload.get("reason"),
                resolved_at=event.get("created_at"),
                resolved_by=payload.get("reviewer"),
            )
    return list(found.values())


def _kept(note: Any) -> dict[str, Any] | None:
    """A proposal's kept note (`ActionResult.kept`), parsed; None for anything else."""
    if not isinstance(note, str):
        return None
    try:
        kept = json.loads(note)
    except ValueError:
        return None
    if not isinstance(kept, dict) or not isinstance(kept.get("arguments"), dict):
        return None
    return {"name": kept.get("name"), "arguments": kept["arguments"]}


def held(actions: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The actions that still wait for their decision's reviewer."""
    return [action for action in actions if action.get("disposition") == "held"]
