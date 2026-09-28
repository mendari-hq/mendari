"""Provider registry. A provider type is any class implementing `Provider.answer`."""

from __future__ import annotations

from openhealth.core.pack import Pack
from openhealth.errors import PackValidationError
from openhealth.providers.any_of import AnyOfProvider
from openhealth.providers.base import Answer, Provider, ProviderInfo, ProviderOutput
from openhealth.providers.jev import JevProvider
from openhealth.providers.naive_bayes import NaiveBayesProvider
from openhealth.providers.rules import RulesProvider

PROVIDER_TYPES: dict[str, type[Provider]] = {
    provider.type: provider
    for provider in (RulesProvider, NaiveBayesProvider, JevProvider, AnyOfProvider)
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
