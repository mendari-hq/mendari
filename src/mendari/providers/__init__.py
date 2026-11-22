# SPDX-License-Identifier: Apache-2.0
"""Provider registry. A provider type is any class implementing `Provider.answer`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from mendari.core.origin import deployment_config
from mendari.core.pack import Pack
from mendari.core.spec import ProviderVariant
from mendari.errors import PackValidationError
from mendari.providers.agent import ENDPOINT_ENV as AGENT_ENDPOINT_ENV
from mendari.providers.agent import AgentConfig, AgentProvider
from mendari.providers.any_of import AnyOfConfig, AnyOfProvider
from mendari.providers.base import (
    Answer,
    FileConfig,
    Provider,
    ProviderInfo,
    ProviderOutput,
    checking_once,
    prefixed,
)
from mendari.providers.cascade import CascadeConfig, CascadeProvider
from mendari.providers.cql import CqlConfig, CqlProvider
from mendari.providers.criteria import CriteriaProvider
from mendari.providers.dosing import DosingProvider
from mendari.providers.http import allowed_origins, require_listed
from mendari.providers.jev import ENDPOINT_ENV as JEV_ENDPOINT_ENV
from mendari.providers.jev import JevConfig, JevProvider
from mendari.providers.llm import BASE_URL_ENV as LLM_BASE_URL_ENV
from mendari.providers.llm import LlmConfig, LlmProvider
from mendari.providers.naive_bayes import NaiveBayesConfig, NaiveBayesProvider
from mendari.providers.rules import RulesProvider
from mendari.providers.vote import VoteConfig, VoteProvider

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
        AgentProvider,
        AnyOfProvider,
        CascadeProvider,
        VoteProvider,
    )
}

# The provider types that ask a model to read the input.
MODEL_TYPES = frozenset({JevProvider.type, LlmProvider.type, AgentProvider.type})

# Each provider type's closed config: every key a variant (or a composite member) of that type
# may set, and nothing else (`config_problems`). Jev's and the LLM's leave out `type`, which their
# constructors take off; a composite's `members` are each one of the other types'.
PROVIDER_CONFIG_MODELS: Mapping[str, type[BaseModel]] = {
    RulesProvider.type: FileConfig,
    CriteriaProvider.type: FileConfig,
    CqlProvider.type: CqlConfig,
    DosingProvider.type: FileConfig,
    NaiveBayesProvider.type: NaiveBayesConfig,
    JevProvider.type: JevConfig,
    LlmProvider.type: LlmConfig,
    AgentProvider.type: AgentConfig,
    AnyOfProvider.type: AnyOfConfig,
    CascadeProvider.type: CascadeConfig,
    VoteProvider.type: VoteConfig,
}
# Where the generated JSON Schema of those configs is published (`config_json_schema`).
CONFIG_SCHEMA_ID = "https://mendari.co/spec/provider-configs.schema.json"
# The deployment's variables that name a remote provider's endpoint: when it lists the origins
# its providers may be called at, each one it sets must be on that list (`provider_allow_list`).
ENDPOINT_ENVS = (LLM_BASE_URL_ENV, JEV_ENDPOINT_ENV, AGENT_ENDPOINT_ENV)

__all__ = [
    "ENDPOINT_ENVS",
    "MODEL_TYPES",
    "PROVIDER_CONFIG_MODELS",
    "PROVIDER_TYPES",
    "Answer",
    "Provider",
    "ProviderInfo",
    "ProviderOutput",
    "build_provider",
    "config_json_schema",
    "config_problems",
    "provider_allow_list",
    "slots_without_model",
    "uses_model",
]


def provider_allow_list() -> list[str] | None:
    """At start-up: the origins the deployment's remote providers may be called at
    (`MENDARI_PROVIDER_ALLOWED_ORIGINS`, `providers/http.py`), sorted, or None when it is unset
    or blank (no allow-list, as before). A malformed list, or an endpoint variable the
    deployment set (`ENDPOINT_ENVS`) whose origin it leaves out, is a `MendariError` naming the
    variable. The AI Gateway's default and Vercel Connect are checked when called: an unused
    one stops nothing."""
    allowed = allowed_origins()
    if allowed is None:
        return None
    for variable in ENDPOINT_ENVS:
        require_listed(variable, allowed)
    return sorted(allowed)


def config_problems(pack: Pack, *, trained: bool = True) -> list[str]:
    """Why any variant of any slot of `pack` cannot run, as far as its provider can tell without
    the environment or the network (`Provider.config_problems`): its closed config, its pack
    files, the slot's questions and, for a pack the deployment does not own (`Pack.bounded`),
    `state_fields` on every model variant and member. Each problem names
    `providers.<slot>.variants.<variant>` first. `trained=False` leaves the trained files
    (`model_file`, `calibration`) unread, as `load_pack(missing_models_ok=True)` does.

    `load_pack` runs it: a pack the deployment does not own is refused for any problem, an owned
    one loads with them as `Pack.warnings`. `mendari validate` refuses both."""
    bounded = pack.bounded
    problems: list[str] = []
    with checking_once():  # a model file several variants name is read once
        for slot_name, slot in pack.manifest.providers.items():
            questions = {key: pack.manifest.questions[key] for key in slot.answers}
            for name, variant in slot.variants.items():
                where = f"providers.{slot_name}.variants.{name}"
                provider_type = PROVIDER_TYPES.get(variant.type)
                if provider_type is None:
                    problems.append(f"{where}: unknown provider type {variant.type!r}")
                    continue
                config = {"type": variant.type, **(variant.model_extra or {})}
                found = provider_type.config_problems(
                    pack, where, config, questions, bounded=bounded, trained=trained
                )
                problems += prefixed(where, found)
    return problems


def config_json_schema() -> dict[str, Any]:
    """The JSON Schema of every provider config (`PROVIDER_CONFIG_MODELS`): one of them, chosen
    by `type`; a composite's members are each one of the types that are not composites.
    Published as `docs/spec/provider-configs.schema.json` (`scripts/provider_config_schema.py`,
    which `--check`s it)."""
    definitions: dict[str, Any] = {}
    composites = {AnyOfProvider.type, CascadeProvider.type, VoteProvider.type}
    for kind, model in sorted(PROVIDER_CONFIG_MODELS.items()):
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, nested in schema.pop("$defs", {}).items():
            if definitions.setdefault(name, nested) != nested:
                raise ValueError(f"two provider config models define {name!r} differently")
        schema["properties"] = {**schema["properties"], "type": {"const": kind}}
        schema["required"] = sorted({"type", *schema.get("required", [])})
        schema["title"] = f"{kind} provider config"
        if kind in composites:
            schema["properties"]["members"]["additionalProperties"] = {"$ref": "#/$defs/member"}
        definitions[kind] = schema
    definitions["member"] = {
        "description": "A composite's member: a provider config of a type that is not a composite.",
        "oneOf": [{"$ref": f"#/$defs/{kind}"} for kind in sorted(set(PROVIDER_TYPES) - composites)],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONFIG_SCHEMA_ID,
        "title": "Mendari provider configs",
        "description": (
            "A provider variant's config in an agent's manifest "
            "(`providers.<slot>.variants.<name>`), "
            "one per provider type, each closed: a key it does not name is refused for an agent "
            "the deployment does not own and a warning for an owned one. Generated from the "
            "provider config models by scripts/provider_config_schema.py; do not edit."
        ),
        "oneOf": [{"$ref": f"#/$defs/{kind}"} for kind in sorted(PROVIDER_CONFIG_MODELS)],
        "$defs": definitions,
    }


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
    # A pack the deployment does not own: its model, answer length, timeout and effort are the
    # deployment's (`deployment_config`), its own values checked at load (`config_problems`).
    if pack.bounded:
        config = deployment_config(config, store=pack.origin == "store")
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
