# SPDX-License-Identifier: Apache-2.0
"""Arms by provider class (`mendari.arms/1`, LP-17c): which of a pack's provider configurations
each arm of the scoreboard holds, worked out from its manifest alone. Nothing is run, called or
written.

A configuration is one variant for each of the pack's slots, so a pack's configurations are the
product of its slots' variants, in the manifest's order. Its arm is the class of the models it
asks, through any of its slots (`providers.MODEL_TYPES`): Jev is the specialised classifier, an
LLM the general model.

- `deterministic`, "Mendari only": no model (rules, criteria, dosing, naive Bayes, CQL).
- `classifier`, "+ specialised classifier": Jev in at least one slot, and no LLM.
- `llm`, "+ general LLM": an LLM in at least one slot, and no Jev.
- `classifier_and_llm`, "+ both": Jev and an LLM, in one slot or in two.

A composite's members count as its own types (`variant_types`, one level: composites cannot
nest). The arm says what a configuration asks, not where it runs: each configuration also says
whether it is local (`core.origin.is_local`: every slot decides on this machine) and names the
types it calls off the machine. CQL is deterministic but remote: it calls Mendari's own CQL
engine (`MENDARI_CQL_URL`), not a model. An arm none of a pack's configurations reaches is "not
applicable". The report names the pack, its slots and their variants, never a case."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mendari.core.origin import COMPOSITE_PROVIDER_TYPES, LOCAL_PROVIDER_TYPES, is_local
from mendari.core.pack import Pack
from mendari.core.spec import ProviderVariant
from mendari.errors import MendariError
from mendari.providers import MODEL_TYPES
from mendari.providers.jev import JevProvider
from mendari.providers.llm import LlmProvider

FORMAT = "mendari.arms/1"
DETERMINISTIC = "deterministic"
CLASSIFIER = "classifier"
LLM = "llm"
CLASSIFIER_AND_LLM = "classifier_and_llm"
# The arms in the scoreboard's order, with the names it shows.
ARMS: dict[str, str] = {
    DETERMINISTIC: "Mendari only",
    CLASSIFIER: "+ specialised classifier",
    LLM: "+ general LLM",
    CLASSIFIER_AND_LLM: "+ both",
}
APPLICABLE = "applicable"
NOT_APPLICABLE = "not applicable"
# The class of each model type: every type in `providers.MODEL_TYPES` has one, so a new model
# type cannot fall into an arm unseen (tests/test_arms.py).
MODEL_CLASSES: dict[str, str] = {JevProvider.type: CLASSIFIER, LlmProvider.type: LLM}
# More configurations than this are refused, never listed: the product of a pack's slots grows
# fast, and no scoreboard runs that many.
MAX_CONFIGURATIONS = 10_000


def variant_types(variant: ProviderVariant) -> frozenset[str]:
    """The provider types `variant` runs: its own and its composite members' (one level, as
    `providers.uses_model` reads them; composites cannot nest)."""
    members = (variant.model_extra or {}).get("members")
    types = {variant.type}
    if isinstance(members, dict):
        types |= {str(m.get("type")) for m in members.values() if isinstance(m, dict)}
    return frozenset(types)


def arm_of(types: Iterable[str]) -> str:
    """The arm of a configuration that runs `types`, from the classes of the model types among
    them (`MODEL_CLASSES`): `deterministic` with none."""
    classes = {MODEL_CLASSES[kind] for kind in MODEL_TYPES.intersection(types)}
    if not classes:
        return DETERMINISTIC
    if classes == {CLASSIFIER, LLM}:
        return CLASSIFIER_AND_LLM
    (single,) = classes
    return single


def remote_types(variant: ProviderVariant) -> frozenset[str]:
    """The types through which `variant` calls something off this machine: none for a local
    variant (`core.origin.is_local`), else its types that are neither local nor composites. A
    composite whose members cannot be read is not local, and so counts as remote itself."""
    if is_local(_config(variant)):
        return frozenset()
    found = variant_types(variant) - LOCAL_PROVIDER_TYPES - COMPOSITE_PROVIDER_TYPES
    return found or frozenset({variant.type})


@dataclass(frozen=True)
class Configuration:
    """One variant for each of a pack's slots (`providers`: slot to variant, in the manifest's
    order), its arm, whether it is local, and the types it calls off the machine."""

    providers: dict[str, str]
    arm: str
    local: bool
    remote_types: tuple[str, ...]

    @property
    def label(self) -> str:
        """`slot=variant` pairs, comma-separated, as `mendari leaderboard --run` takes them."""
        return ",".join(f"{slot}={variant}" for slot, variant in self.providers.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": dict(self.providers),
            "local": self.local,
            "remote_types": list(self.remote_types),
        }


@dataclass(frozen=True)
class Arm:
    """An arm (`ARMS`) and the configurations of one pack in it, in the manifest's order."""

    id: str
    name: str
    configurations: tuple[Configuration, ...]

    @property
    def status(self) -> str:
        return APPLICABLE if self.configurations else NOT_APPLICABLE


def configurations(pack: Pack) -> list[Configuration]:
    """Every configuration of `pack`: the product of its slots' variants, in the manifest's
    order. A pack with more than `MAX_CONFIGURATIONS` is a `MendariError` that says how many."""
    slots = pack.manifest.providers
    count = math.prod(len(slot.variants) for slot in slots.values())
    if count > MAX_CONFIGURATIONS:
        raise MendariError(
            f"{pack.ref}: {count:,} provider configurations, more than the "
            f"{MAX_CONFIGURATIONS:,} the scoreboard lists"
        )
    found = []
    for chosen in itertools.product(*(slot.variants.items() for slot in slots.values())):
        variants = [variant for _, variant in chosen]
        types = frozenset().union(*(variant_types(variant) for variant in variants))
        remote = frozenset().union(*(remote_types(variant) for variant in variants))
        found.append(
            Configuration(
                providers=dict(zip(slots, (name for name, _ in chosen), strict=True)),
                arm=arm_of(types),
                local=all(is_local(_config(variant)) for variant in variants),
                remote_types=tuple(sorted(remote)),
            )
        )
    return found


def arms(pack: Pack) -> dict[str, Arm]:
    """Each arm, in `ARMS`'s order, with the configurations of `pack` in it. Every configuration
    is in exactly one arm; an arm with none is "not applicable"."""
    found = configurations(pack)
    return {
        arm: Arm(arm, name, tuple(c for c in found if c.arm == arm)) for arm, name in ARMS.items()
    }


def arms_report(pack: Pack) -> dict[str, Any]:
    """`mendari.arms/1` for `pack`: the pack's id, version and digest, how many configurations
    it has, and each arm's name, status and configurations."""
    chosen = arms(pack)
    return {
        "format": FORMAT,
        "pack": {"id": pack.id, "version": pack.version, "digest": pack.digest},
        "configurations": sum(len(arm.configurations) for arm in chosen.values()),
        "arms": {
            arm.id: {
                "name": arm.name,
                "status": arm.status,
                "configurations": [configuration.to_dict() for configuration in arm.configurations],
            }
            for arm in chosen.values()
        },
    }


def arms_text(report: dict[str, Any]) -> str:
    """The report for a terminal: each arm with its configurations, written as `--run` takes
    them, and where each runs; or "not applicable"."""
    pack = report["pack"]
    lines = [
        f"Arms of {pack['id']}@{pack['version']} ({report['format']}): "
        f"{_counted(report['configurations'])}"
    ]
    for arm, entry in report["arms"].items():
        listed = entry["configurations"]
        if not listed:
            lines.append(f"  {entry['name']} ({arm}): {NOT_APPLICABLE}")
            continue
        lines.append(f"  {entry['name']} ({arm}): {_counted(len(listed))}")
        for configuration in listed:
            pairs = ",".join(f"{slot}={name}" for slot, name in configuration["providers"].items())
            remote = configuration["remote_types"]
            where = f"remote: {', '.join(remote)}" if remote else "local"
            lines.append(f"    {pairs}  ({where})")
    return "\n".join(lines)


def _config(variant: ProviderVariant) -> dict[str, Any]:
    """A variant's config, as `core.origin.is_local` and the provider read it."""
    return {"type": variant.type, **(variant.model_extra or {})}


def _counted(count: int) -> str:
    return f"{count} configuration{'' if count == 1 else 's'}"
