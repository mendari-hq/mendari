"""Provider registry. A provider type is any class implementing `Provider.answer`."""

from __future__ import annotations

from collections.abc import Mapping

from mendari.core.origin import deployment_config
from mendari.core.pack import Pack
from mendari.core.spec import ProviderVariant
from mendari.errors import PackValidationError
from mendari.providers.any_of import AnyOfProvider
from mendari.providers.base import Answer, Provider, ProviderInfo, ProviderOutput
from mendari.providers.cascade import CascadeProvider
from mendari.providers.cql import CqlProvider
from mendari.providers.criteria import CriteriaProvider
from mendari.providers.dosing import DosingProvider
from mendari.providers.jev import JevProvider
from mendari.providers.llm import LlmProvider
from mendari.providers.naive_bayes import NaiveBayesProvider
from mendari.providers.rules import RulesProvider
from mendari.providers.vote import VoteProvider

PROVIDER_TYPES: dict[str, type[Provider]] = {
    provider.type: provider
    for provider in (
        RulesProvider,
        CriteriaProvider,
        CqlProvider,
        DosingProvider,
        NaiveBayesProvider,
        JevProvider,
        LlmProvider,
        AnyOfProvider,
        CascadeProvider,
        VoteProvider,
    )
}

# The provider types that ask a model to read the input.
MODEL_TYPES = frozenset({JevProvider.type, LlmProvider.type})

__all__ = [
    "MODEL_TYPES",
    "PROVIDER_TYPES",
    "Answer",
    "Provider",
    "ProviderInfo",
    "ProviderOutput",
    "build_provider",
    "slots_without_model",
    "uses_model",
]


def build_provider(pack: Pack, slot_name: str, variant_name: str | None = None) -> Provider:
    slot = pack.manifest.providers.get(slot_name)
    if slot is None:
        raise PackValidationError(pack.id, [f"unknown provider slot {slot_name!r}"])
    variant_name = variant_name or slot.use
    variant = slot.variants.get(variant_name)
    if variant is None:
        raise PackValidationError(
            pack.id,
            [f"slot {slot_name!r} has no variant {variant_name!r} (have {sorted(slot.variants)})"],
        )
    provider_type = PROVIDER_TYPES.get(variant.type)
    if provider_type is None:
        raise PackValidationError(pack.id, [f"unknown provider type {variant.type!r}"])
    config = {"type": variant.type, **(variant.model_extra or {})}
    if pack.community:  # the deployment chooses a community pack's model and answer length
        config = deployment_config(config)
    model = config.get("model") if isinstance(config.get("model"), str) else None
    info = ProviderInfo(slot=slot_name, variant=variant_name, type=variant.type, model=model)
    questions = {key: pack.manifest.questions[key] for key in slot.answers}
    return provider_type(pack, info, questions, config)


def uses_model(variant: ProviderVariant) -> bool:
    """Whether a variant asks a model (Jev or an LLM), itself or through a composite's member."""
    members = (variant.model_extra or {}).get("members")
    types = {variant.type}
    if isinstance(members, dict):
        types |= {str(m.get("type")) for m in members.values() if isinstance(m, dict)}
    return not types.isdisjoint(MODEL_TYPES)


def slots_without_model(pack: Pack, running: Mapping[str, str]) -> list[str]:
    """The pack's slots that run without a model (`running`: slot -> variant; the pack's default
    where absent) although one of their variants has one: the offline floor."""
    return [
        slot
        for slot, spec in pack.manifest.providers.items()
        if not uses_model(spec.variants[running.get(slot, spec.use)])
        and any(uses_model(variant) for variant in spec.variants.values())
    ]
