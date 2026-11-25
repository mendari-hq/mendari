# SPDX-License-Identifier: Apache-2.0
"""Where a pack comes from, and what that allows it.

A pack's origin is set by where it was found, never by its manifest (`publisher.type` is
self-declared):

- `maintained`: the agents Mendari bundles (`packs/`, bundled in the wheel).
- `hub`: packs pulled from a registry, verified, in the local cache.
- `local`: any other directory (`MENDARI_AGENTS_PATH`, a path given on the command line).

An agent the deployment does not own runs on the bounded engine (`bounded`, ADR-0009): its
patterns on RE2 (`core/patterns.py`), its settings capped at load (`core/caps.py`), its schema's
keywords bounded (`core/input_schema.py`), its inputs held to the limits below and its patterns
and schema checks to a CPU budget per decision. Those are the agents of an origin the deployment
does not own (`NOT_OWNED`) whose digest, the one `load_pack` hashed, is not that of a bundled
agent of the installed release (`core/reference.py`): a copy of a bundled agent keeps the
maintained engine, and nothing a manifest, a registry or a marker file says chooses it. Such an
agent is labelled by where it was found ("From a registry"), and keeps its policy: it may
automate under it and may block. Maintained and local agents (an operator's own: a copy of a
pulled agent in `MENDARI_AGENTS_PATH` is one) stay on `re`.

Such a pack's provider configs are refused at load unless every variant and composite member
passes its provider's closed config (`providers.config_problems`; an owned pack loads with them
as warnings), and each of its model variants names the input fields it sends (`state_fields`).
Its model, answer length, timeout and effort are the deployment's (`deployment_config`): the
model it names is dropped, `max_tokens` and `timeout_ms` are held to `NOT_OWNED_CAPS` (a lower
value of its own stands), and a `reasoning_effort` above `MAX_NOT_OWNED_EFFORT` is dropped.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

Origin = Literal["maintained", "hub", "local", "store"]
# The origins of agents the deployment does not own: they run on the bounded engine unless they
# are a bundled agent of this release (`bounded`). `store`: a version in an organization's agent
# store (LP-20e, ADR-0016), whose digest is never a bundled agent's (its id is its
# organization's, LP-20f).
NOT_OWNED: frozenset[Origin] = frozenset({"hub", "store"})

# The namespaces of the agents Mendari maintains.
RESERVED_NAMESPACES: Mapping[str, Origin] = {"mendari": "maintained"}
# The longest text the patterns of an agent on the bounded engine (`bounded`) run on (on RE2,
# their time grows with it: the caps in `core/caps.py` are measured on texts this long). A longer
# one is refused before any pattern runs.
MAX_BOUNDED_TEXT_CHARS = 10_000
# The most values (texts, numbers, objects, lists...) and the deepest nesting such an agent reads
# in one input: its schema applies its keywords to each value, at every depth.
MAX_BOUNDED_VALUES = 10_000
MAX_BOUNDED_DEPTH = 32
# The provider types that decide on this machine, without a call anywhere, and the composites
# that combine them.
LOCAL_PROVIDER_TYPES = frozenset({"rules", "criteria", "dosing", "naive_bayes"})
COMPOSITE_PROVIDER_TYPES = frozenset({"any_of", "cascade", "vote", "check"})
# Variant keys a pack the deployment does not own (`bounded`) may set but the deployment decides:
# which model a remote provider calls. Its answer length and timeout are held to the provider
# defaults below, a lower value of its own kept, and its effort to `MAX_NOT_OWNED_EFFORT`.
DEPLOYMENT_CHOSEN_KEYS = ("model", "endpoint")
NOT_OWNED_CAPS: Mapping[str, Mapping[str, int | float]] = {
    "llm": {"max_tokens": 4096, "timeout_ms": 30_000},
    "jev": {"timeout_ms": 5_000},
    "agent": {"timeout_seconds": 15.0},
}
# Variant keys a pack from a store (LP-20e) may set but the deployment decides, beyond those of
# any pack it does not own: its price per call (the recorded cost is the deployment's), whether
# it shares a call with the decision's other questions, and an LLM's temperature. Dropped, so the
# provider's defaults apply. A pack from a registry keeps them (a change would change its
# decisions).
STORE_DEPLOYMENT_KEYS = ("cost_per_1k_input_tokens_usd", "batch", "temperature")
# `reasoning_effort`, lowest first: a value above the most a not-owned pack keeps is dropped, so
# the model's own default applies.
EFFORTS = ("none", "low", "medium", "high")
MAX_NOT_OWNED_EFFORT = "low"
# What every surface says about where a pack comes from (the API, the dashboard, the Decision
# Card, CDS Hooks, the docs landing). Set by origin, never by the manifest; neither label claims
# clinical review, which no pack has had through Mendari.
ORIGIN_LABELS: Mapping[Origin, str] = {
    "maintained": "Maintained by Mendari",
    "hub": "From a registry",
    "local": "Local agent",
    "store": "From this organization's store",
}


# Beside a pulled pack in the registry cache: where it came from (LP-18d, `hub/client.py`
# writes it after verifying the archive). Hidden, so outside the pack's digest.
MARKER = ".mendari-verified"
ORIGIN_FORMAT = "mendari.origin/1"
# Why automation is held for a pack from a registry until its organization approves its digest
# (`core/decision.py`): no field in the pack lifts it.
UNAPPROVED = "an agent from a registry, not approved for automation by this organization"


def pulled_record(root: Path) -> dict[str, Any]:
    """What the verified marker beside a pulled pack records: the registry's URL, namespace,
    publisher type, signing key id, archive SHA-256, digest, when it was pulled and the recall
    the registry served then, if any; a missing or unreadable one, nothing."""
    try:
        text = (root / MARKER).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        found = json.loads(text)
    except ValueError:
        return {}
    return found if isinstance(found, dict) and found.get("format") == ORIGIN_FORMAT else {}


def bounded(origin: Origin, digest: str) -> bool:
    """An agent the deployment does not own, which runs on the bounded engine (module
    docstring): an agent of a `NOT_OWNED` origin whose `digest` (the one `load_pack` hashed,
    never one a manifest or a registry names) is not a bundled agent's."""
    if origin not in NOT_OWNED:
        return False
    from mendari.core.reference import reference_digests  # `core/pack.py` imports this module

    return digest not in reference_digests()


def bounded_reader(origin: Origin) -> str:
    """What a reason says of an agent on the bounded engine (`bounded`): "an agent from a
    store" or "an agent from a registry"."""
    return "an agent from a store" if origin == "store" else "an agent from a registry"


def shape_problem(value: Any, reader: str) -> str | None:
    """Why an input is too large in shape for an agent on the bounded engine (`reader`, from
    `bounded_reader`), or None: more than `MAX_BOUNDED_VALUES` values, or nested deeper than
    `MAX_BOUNDED_DEPTH` (its schema is applied to every value, and jsonschema recurses into
    every level). Walked without recursion, stopping at the first one past either limit."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_BOUNDED_VALUES:
            return f"more than {MAX_BOUNDED_VALUES:,} values, the most {reader} reads"
        if depth > MAX_BOUNDED_DEPTH:
            return f"nested more than {MAX_BOUNDED_DEPTH} deep, the most {reader} reads"
        if isinstance(item, Mapping):
            stack += [(child, depth + 1) for child in item.values()]
        elif isinstance(item, list | tuple):
            stack += [(child, depth + 1) for child in item]
    return None


_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_MAX_WHERE_LEN = 200


def _child_where(where: str, segment: str) -> str:
    """Append a sanitized path segment, capping total length so client keys never leak."""
    if len(where) >= _MAX_WHERE_LEN:
        return where
    safe = segment if _SAFE_KEY.match(segment) else "*"
    joined = f"{where}.{safe}" if where else safe
    return joined if len(joined) <= _MAX_WHERE_LEN else f"{joined[: _MAX_WHERE_LEN - 1]}…"


def long_text(
    value: Any, limit: int = MAX_BOUNDED_TEXT_CHARS, *, count_keys: bool = True
) -> str | None:
    """Where in an input (`message.body`) a text, or a key, is longer than `limit` characters,
    or "the input's texts together" when its texts (and keys, with `count_keys`), each with one
    separator, are: a rule's patterns run on its text fields joined (`providers/base.py`
    `text_from`), and the schema's on every key when it has `patternProperties` or
    `propertyNames` (`caps.patterns_on_keys`, which says `count_keys`), so the whole is held to
    the length the patterns' work is bounded for (`core/caps.py`). An agent on the bounded
    engine (`bounded`) refuses such an input before any of its patterns runs. Walked without
    recursion, however deep the input."""
    stack: list[tuple[str, Any]] = [("", value)]
    total = -1  # the first text has no separator before it
    while stack:
        where, item = stack.pop()
        if isinstance(item, str):
            if len(item) > limit:
                return where or "(root)"
            total += len(item) + 1
            if total > limit:
                return "the input's texts together"
        elif isinstance(item, Mapping):
            for key, child in reversed(list(item.items())):
                if isinstance(key, str):
                    if len(key) > limit:
                        return where or "(root)"
                    if count_keys:  # else held to the length alone: no pattern runs on it
                        total += len(key) + 1
                        if total > limit:
                            return "the input's texts together"
                stack.append((_child_where(where, str(key)), child))
        elif isinstance(item, list | tuple):
            stack += [
                (f"{where}[{i}]" if len(where) < _MAX_WHERE_LEN else where, child)
                for i, child in reversed(list(enumerate(item)))
            ]
    return None


def is_local(config: Mapping[str, Any]) -> bool:
    """A provider config that decides without calling anything: a local type, or a composite
    whose members all are."""
    kind = config.get("type")
    if kind in LOCAL_PROVIDER_TYPES:
        return True
    members = config.get("members")
    if kind not in COMPOSITE_PROVIDER_TYPES or not isinstance(members, dict) or not members:
        return False
    return all(
        isinstance(member, dict) and member.get("type") in LOCAL_PROVIDER_TYPES
        for member in members.values()
    )


def deployment_config(config: Mapping[str, Any], *, store: bool = False) -> dict[str, Any]:
    """The provider config a pack the deployment does not own (`bounded`) runs with, in the
    variant and each composite member: without the keys the deployment chooses
    (`DEPLOYMENT_CHOSEN_KEYS`, and for a pack from a store, `store`, `STORE_DEPLOYMENT_KEYS`),
    its `max_tokens` and `timeout_ms` at most `NOT_OWNED_CAPS` (an absent key stays absent), and
    its `reasoning_effort` kept only up to `MAX_NOT_OWNED_EFFORT`. The pack's own values were
    checked at load, before this (`providers.config_problems`)."""
    chosen = DEPLOYMENT_CHOSEN_KEYS + (STORE_DEPLOYMENT_KEYS if store else ())
    kept = {key: value for key, value in config.items() if key not in chosen}
    for key, cap in NOT_OWNED_CAPS.get(str(kept.get("type")), {}).items():
        if key in kept and (number := _config_number(kept[key], cap)) is not None:
            kept[key] = min(number, cap)
    effort = kept.get("reasoning_effort", "none")
    if effort not in EFFORTS or EFFORTS.index(effort) > EFFORTS.index(MAX_NOT_OWNED_EFFORT):
        del kept["reasoning_effort"]
    members = kept.get("members")
    if isinstance(members, dict):
        kept["members"] = {
            name: deployment_config(member, store=store) if isinstance(member, dict) else member
            for name, member in members.items()
        }
    return kept


_INT = TypeAdapter(int)
_FLOAT = TypeAdapter(float)


def _config_number(value: Any, cap: int | float) -> int | float | None:
    """`value` as a provider's config model reads a numeric setting: pydantic's lax mode, so
    `"32000"`, `32000.0` and `"32000.0"` are 32000 for integer caps (`LlmConfig`, `JevConfig`),
    and finite floats for float caps (`AgentConfig.timeout_seconds`). None for what the models
    refuse (a bool, `32000.5` for int caps, `"x"`): `load_pack` refused a pack with one."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(cap, float):
            parsed = _FLOAT.validate_python(value)
            return parsed if math.isfinite(parsed) else None
        return _INT.validate_python(value)
    except ValidationError:
        return None
