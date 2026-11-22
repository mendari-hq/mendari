# SPDX-License-Identifier: Apache-2.0
"""Where a pack comes from, and what that allows it.

A pack's origin is set by where it was found, never by its manifest (`publisher.type` is
self-declared):

- `maintained`: the agents Mendari bundles (`packs/`, bundled in the wheel).
- `community`: packs outside developers contributed by pull request, in
  `community-packs/<name>/` with id `community/<name>`; served only with
  `MENDARI_COMMUNITY_PACKS=on`.
- `hub`: packs pulled from a registry, verified, in the local cache.
- `local`: any other directory (`MENDARI_PACKS_PATH`, a path given on the command line).

A community pack recommends and never automates (`core/policy.py`): no policy rule of its own
blocks, and a person confirms every decision. It decides with local providers unless the
deployment opts it in to another variant by name (`community/<name>:slot=variant`), keeps only a
hash of its inputs, reads at most `MAX_COMMUNITY_TEXT_CHARS` of text, publishes its FHIR
terminology under its own url (`community_url`), and is at most risk level 1.

A pack the deployment does not own runs on the community engine (`bounded`, ADR-0009): its
patterns on RE2 (`core/patterns.py`), its settings capped at load (`core/caps.py`), its schema's
keywords bounded (`core/input_schema.py`), its inputs held to the limits below and its patterns
and schema checks to a CPU budget per decision. Those are the community packs, and the packs
pulled from a registry (`NOT_OWNED`) whose digest, the one `load_pack` hashed, is not that of a
bundled agent of the installed release (`core/reference.py`): a copy of a bundled agent keeps
the maintained engine, and nothing a manifest, a registry or a marker file says chooses it.
The engine is all a pulled pack shares with a community pack: it is labelled "From a registry",
may automate under its policy and may block, as before. Maintained and local packs (an
operator's own: a copy of a pulled pack in `MENDARI_PACKS_PATH` is one) stay on `re`.

Such a pack's provider configs are refused at load unless every variant and composite member
passes its provider's closed config (`providers.config_problems`; an owned pack loads with them
as warnings), and each of its model variants names the input fields it sends (`state_fields`).
Its model, answer length, timeout and effort are the deployment's (`deployment_config`): the
model it names is dropped, `max_tokens` and `timeout_ms` are held to `NOT_OWNED_CAPS` (a lower
value of its own stands), and a `reasoning_effort` above `MAX_NOT_OWNED_EFFORT` is dropped.

The namespaces are the community route's: a community pack uses `community/` and nothing else,
so it can never claim the id of a pack Mendari maintains. Packs from anywhere else are found as
they were before community packs existed; one whose id is in `community/` gets the community
limits wherever it was loaded from (`is_community`).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from mendari.core.spec import Outcome, PackManifest, RiskLevel

Origin = Literal["maintained", "community", "hub", "local", "store"]
# The origins of packs the deployment does not own, besides community packs: they run on the
# community engine unless they are a bundled agent of this release (`bounded`). `store`: a
# version in an organization's pack store (LP-20e, ADR-0016), whose digest is never a bundled
# agent's (its id is its organization's, LP-20f).
NOT_OWNED: frozenset[Origin] = frozenset({"hub", "store"})

COMMUNITY_NAMESPACE = "community"
# The namespaces of the packs Mendari maintains, which a community pack never claims.
RESERVED_NAMESPACES: Mapping[str, Origin] = {
    "mendari": "maintained",
    "openhealth": "maintained",  # the bundled agents' namespace before the rename (`legacy.py`)
}
# Where a community pack's FHIR terminology lives: its own url, never one of Mendari's.
COMMUNITY_URL = "https://mendari.co/packs/community/"
# The longest text the patterns of a pack on the community engine (`bounded`) run on (on RE2,
# their time grows with it: the caps in `core/caps.py` are measured on texts this long). A longer
# one is refused before any pattern runs.
MAX_COMMUNITY_TEXT_CHARS = 10_000
# The most values (texts, numbers, objects, lists...) and the deepest nesting such a pack reads in
# one input: its schema applies its keywords to each value, at every depth.
MAX_COMMUNITY_VALUES = 10_000
MAX_COMMUNITY_DEPTH = 32
COMMUNITY_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_COMMUNITY_RISK = RiskLevel.CLINICAL_INFORMATION
# The provider types that decide on this machine, without a call anywhere, and the composites
# that combine them. A community pack's default variants use only these.
LOCAL_PROVIDER_TYPES = frozenset({"rules", "criteria", "dosing", "naive_bayes"})
COMPOSITE_PROVIDER_TYPES = frozenset({"any_of", "cascade", "vote"})
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
COMMUNITY_LABEL = "Community · not clinically reviewed"
ORIGIN_LABELS: Mapping[Origin, str] = {
    "maintained": "Maintained by Mendari",
    "community": COMMUNITY_LABEL,
    "hub": "From a registry",
    "local": "Local pack",
    "store": "From this organization's store",
}
# What a community pack does with a decision, wherever it says so.
COMMUNITY_AUTOMATION = "Never: a community pack recommends, and a person confirms every decision."


# Beside a pulled pack in the registry cache: where it came from (LP-18d, `hub/client.py`
# writes it after verifying the archive). Hidden, so outside the pack's digest.
MARKER = ".mendari-verified"
ORIGIN_FORMAT = "mendari.origin/1"
# Why automation is held for a pack from a registry until its organization approves its digest
# (`core/decision.py`): no field in the pack lifts it.
UNAPPROVED = "a pack from a registry, not approved for automation by this organization"


def pulled_record(root: Path) -> dict[str, Any]:
    """What the verified marker beside a pulled pack records: the registry's URL, namespace,
    publisher type, signing key id, archive SHA-256, digest, when it was pulled and the recall
    the registry served then, if any. A marker from before LP-18d holds only the archive's
    SHA-256; a missing or unreadable one, nothing."""
    try:
        text = (root / MARKER).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.startswith("{"):
        return {"archive_sha256": text}
    try:
        found = json.loads(text)
    except ValueError:
        return {}
    return found if isinstance(found, dict) and found.get("format") == ORIGIN_FORMAT else {}


def is_community(pack_id: str, origin: Origin) -> bool:
    """A community pack: found in `community-packs/`, or named in the `community/` namespace
    wherever it was loaded from (a path on the command line), so its limits always apply."""
    return origin == "community" or pack_id.partition("/")[0] == COMMUNITY_NAMESPACE


def bounded(pack_id: str, origin: Origin, digest: str) -> bool:
    """A pack the deployment does not own, which runs on the community engine (module
    docstring): a community pack, or a pack of a `NOT_OWNED` origin whose `digest` (the one
    `load_pack` hashed, never one a manifest or a registry names) is not a bundled agent's."""
    if is_community(pack_id, origin):
        return True
    if origin not in NOT_OWNED:
        return False
    from mendari.core.reference import reference_digests  # `core/pack.py` imports this module

    return digest not in reference_digests()


def bounded_reader(pack_id: str, origin: Origin) -> str:
    """What a reason says of a pack on the community engine (`bounded`): "a community pack",
    "a pack from a store" or "a pack from a registry"."""
    if is_community(pack_id, origin):
        return "a community pack"
    return "a pack from a store" if origin == "store" else "a pack from a registry"


def shown_origin(pack_id: str, origin: Origin) -> Origin:
    """The origin a pack is labelled with: community wherever its limits apply (`is_community`),
    else where it was found."""
    return "community" if is_community(pack_id, origin) else origin


def namespace_problem(pack_id: str, origin: Origin) -> str | None:
    """Why a pack of this origin may not use its id's namespace, if it may not: a community
    pack never claims a namespace of the packs Mendari maintains. Other origins are found as
    they always were."""
    namespace = pack_id.partition("/")[0]
    if origin != "community" or namespace not in RESERVED_NAMESPACES:
        return None
    return f"the namespace {namespace}/ is reserved to the packs Mendari maintains"


def community_url(pack_id: str) -> str:
    """Where a community pack's FHIR terminology lives: `https://mendari.co/packs/<id>/`."""
    return f"{COMMUNITY_URL}{pack_id.removeprefix(COMMUNITY_NAMESPACE + '/')}/"


def performer_problem(pack_id: str, system: str | None) -> str | None:
    """Why a community pack may not use `system` (its FHIR mapping's `task.performer_system`),
    if it may not: it lives under the pack's own url, so it never writes into a CodeSystem of
    Mendari's or of another pack."""
    if system is None:
        return None
    own = community_url(pack_id)
    if system.startswith(own) and len(system) > len(own):
        return None
    return (
        f"fhir/mapping.yaml: task.performer_system is {system!r}; a community pack's FHIR "
        f"systems live under its own url, e.g. {own}CodeSystem/performer"
    )


def rule_problems(manifest: PackManifest) -> list[str]:
    """Why a community pack's policy rules cannot stand: none of them blocks, since a blocked
    case is final at once and no person sees it."""
    return [
        f"policy rule {rule.name!r} has outcome BLOCK; a community pack recommends and a person "
        "confirms every decision: use HUMAN_REVIEW (with a priority) instead"
        for rule in manifest.policy.rules
        if rule.outcome is Outcome.BLOCK
    ]


def shape_problem(value: Any, reader: str = "a community pack") -> str | None:
    """Why an input is too large in shape for a pack on the community engine (`reader`, from
    `bounded_reader`), or None: more than `MAX_COMMUNITY_VALUES` values, or nested deeper than
    `MAX_COMMUNITY_DEPTH` (its schema is applied to every value, and jsonschema recurses into
    every level). Walked without recursion, stopping at the first one past either limit."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_COMMUNITY_VALUES:
            return f"more than {MAX_COMMUNITY_VALUES:,} values, the most {reader} reads"
        if depth > MAX_COMMUNITY_DEPTH:
            return f"nested more than {MAX_COMMUNITY_DEPTH} deep, the most {reader} reads"
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
    value: Any, limit: int = MAX_COMMUNITY_TEXT_CHARS, *, count_keys: bool = True
) -> str | None:
    """Where in an input (`message.body`) a text, or a key, is longer than `limit` characters,
    or "the input's texts together" when its texts (and keys, with `count_keys`), each with one
    separator, are: a rule's patterns run on its text fields joined (`providers/base.py`
    `text_from`), and the schema's on every key when it has `patternProperties` or
    `propertyNames` (`caps.patterns_on_keys`, which says `count_keys`), so the whole is held to
    the length the patterns' work is bounded for (`core/caps.py`). A pack on the community
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


def community_problems(manifest: PackManifest, folder: str) -> list[str]:
    """Why a pack found in `community-packs/<folder>/` cannot be served, if it cannot."""
    problems = []
    if not COMMUNITY_NAME.match(folder):
        problems.append(f"the folder name {folder!r} must be lowercase letters, digits and dashes")
    if manifest.id != f"{COMMUNITY_NAMESPACE}/{folder}":
        problems.append(
            f"a community pack in community-packs/{folder}/ has the id "
            f"{COMMUNITY_NAMESPACE}/{folder}, not {manifest.id}"
        )
    if manifest.risk.level > MAX_COMMUNITY_RISK:
        problems.append(
            f"risk level {int(manifest.risk.level)}: a community pack is at most risk level "
            f"{int(MAX_COMMUNITY_RISK)}"
        )
    if manifest.audit.store_input != "hash":
        problems.append(
            "a community pack keeps only a hash of its inputs (audit.store_input: hash)"
        )
    problems += rule_problems(manifest)
    for slot_name, slot in manifest.providers.items():
        variant = slot.variants[slot.use]
        config = {"type": variant.type, **(variant.model_extra or {})}
        if not is_local(config):
            problems.append(
                f"providers.{slot_name}: the default variant {slot.use!r} must be local "
                f"({', '.join(sorted(LOCAL_PROVIDER_TYPES))}, or a composite of those)"
            )
    return problems


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
