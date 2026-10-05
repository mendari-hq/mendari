"""Where a pack comes from, and what that allows it.

A pack's origin is set by where it was found, never by its manifest (`publisher.type` is
self-declared):

- `maintained`: the reference packs Mendari ships (`packs/`, bundled in the wheel).
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

The namespaces are the community route's: a community pack uses `community/` and nothing else,
so it can never claim the id of a pack Mendari maintains. Packs from anywhere else are found as
they were before community packs existed; one whose id is in `community/` gets the community
limits wherever it was loaded from (`is_community`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from mendari.core.spec import Outcome, PackManifest, RiskLevel

Origin = Literal["maintained", "community", "hub", "local"]

COMMUNITY_NAMESPACE = "community"
# The namespaces of the packs Mendari maintains, which a community pack never claims.
RESERVED_NAMESPACES: Mapping[str, Origin] = {
    "mendari": "maintained",
    "openhealth": "maintained",  # the reference packs' id before the rename (`legacy.py`)
}
# Where a community pack's FHIR terminology lives: its own url, never one of Mendari's.
COMMUNITY_URL = "https://mendari.co/packs/community/"
# The longest text a community pack's patterns run on: what the pattern probe times them on
# (`community/regex_safety.py`). A longer one is refused before any pattern runs.
MAX_COMMUNITY_TEXT_CHARS = 10_000
COMMUNITY_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_COMMUNITY_RISK = RiskLevel.CLINICAL_INFORMATION
# The provider types that decide on this machine, without a call anywhere, and the composites
# that combine them. A community pack's default variants use only these.
LOCAL_PROVIDER_TYPES = frozenset({"rules", "criteria", "dosing", "naive_bayes"})
COMPOSITE_PROVIDER_TYPES = frozenset({"any_of", "cascade", "vote"})
# Variant keys a community pack may set but the deployment decides: which model a remote
# provider calls, and how much it may answer.
DEPLOYMENT_CHOSEN_KEYS = ("model", "max_tokens")
# What every surface says about where a pack comes from (the API, the dashboard, the Decision
# Card, CDS Hooks, the docs landing). Set by origin, never by the manifest; neither label claims
# clinical review, which no pack has had through Mendari.
COMMUNITY_LABEL = "Community · not clinically reviewed"
ORIGIN_LABELS: Mapping[Origin, str] = {
    "maintained": "Maintained by Mendari",
    "community": COMMUNITY_LABEL,
    "hub": "From a registry",
    "local": "Local pack",
}
# What a community pack does with a decision, wherever it says so.
COMMUNITY_AUTOMATION = "Never: a community pack recommends, and a person confirms every decision."


def is_community(pack_id: str, origin: Origin) -> bool:
    """A community pack: found in `community-packs/`, or named in the `community/` namespace
    wherever it was loaded from (a path on the command line), so its limits always apply."""
    return origin == "community" or pack_id.partition("/")[0] == COMMUNITY_NAMESPACE


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


def long_text(value: Any, limit: int = MAX_COMMUNITY_TEXT_CHARS) -> str | None:
    """Where in an input (`message.body`) a text, or a key, is longer than `limit` characters,
    or "the input's texts together" when its texts and keys, each with one separator, are: a
    rule's patterns run on its text fields joined (`providers/base.py` `text_from`), and the
    schema's patterns on every key (`patternProperties`, `propertyNames`), so the whole is held
    to the length the patterns were timed on. A community pack refuses such an input before
    any of its patterns runs. Walked without recursion, however deep the input."""
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
                    total += len(key) + 1
                    if total > limit:
                        return "the input's texts together"
                stack.append((f"{where}.{key}" if where else str(key), child))
        elif isinstance(item, list | tuple):
            stack += [(f"{where}[{i}]", child) for i, child in reversed(list(enumerate(item)))]
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


def deployment_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """A community pack's provider config without the keys the deployment chooses
    (`DEPLOYMENT_CHOSEN_KEYS`), in the variant and each composite member."""
    kept = {key: value for key, value in config.items() if key not in DEPLOYMENT_CHOSEN_KEYS}
    members = kept.get("members")
    if isinstance(members, dict):
        kept["members"] = {
            name: deployment_config(member) if isinstance(member, dict) else member
            for name, member in members.items()
        }
    return kept
