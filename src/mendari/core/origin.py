"""Where a pack comes from, and what that allows it.

A pack's origin is set by where it was found, never by its manifest (`publisher.type` is
self-declared):

- `maintained`: the reference packs Mendari ships (`packs/`, bundled in the wheel).
- `community`: packs outside developers contributed by pull request, in
  `community-packs/<name>/` with id `community/<name>`; served only with
  `MENDARI_COMMUNITY_PACKS=on`.
- `hub`: packs pulled from a registry, verified, in the local cache.
- `local`: any other directory (`MENDARI_PACKS_PATH`, a path given on the command line).

A community pack recommends and never automates (`core/policy.py`), decides with local
providers unless the deployment opts it in to another variant by name
(`community/<name>:slot=variant`), keeps only a hash of its inputs, and is at most risk level 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from mendari.core.spec import PackManifest, RiskLevel

Origin = Literal["maintained", "community", "hub", "local"]

COMMUNITY_NAMESPACE = "community"
# Namespaces only one origin may use: a pack elsewhere claiming one is refused at load.
RESERVED_NAMESPACES: Mapping[str, Origin] = {
    "mendari": "maintained",
    "openhealth": "maintained",  # the reference packs' id before the rename (`legacy.py`)
    COMMUNITY_NAMESPACE: "community",
}
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
    """Why a pack of this origin may not use its id's namespace, if it may not."""
    namespace = pack_id.partition("/")[0]
    owner = RESERVED_NAMESPACES.get(namespace)
    if owner is None or owner == origin:
        return None
    where = "community-packs/" if owner == "community" else "the packs Mendari maintains"
    return f"the namespace {namespace}/ is reserved to {where}; this pack is {origin}"


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
