"""Provider registry. A provider type is any class implementing `Provider.answer`."""

from __future__ import annotations

from mendari.core.pack import Pack
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

__all__ = [
    "PROVIDER_TYPES",
    "Answer",
    "Provider",
    "ProviderInfo",
    "ProviderOutput",
    "build_provider",
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
    model = config.get("model") if isinstance(config.get("model"), str) else None
    info = ProviderInfo(slot=slot_name, variant=variant_name, type=variant.type, model=model)
    questions = {key: pack.manifest.questions[key] for key in slot.answers}
    return provider_type(pack, info, questions, config)
