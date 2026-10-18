# SPDX-License-Identifier: Apache-2.0
"""Arms by provider class (`mendari.arms/1`, LP-17c, `evals/arms.py`) and `mendari scoreboard
arms`: each configuration of a pack (one variant per slot) is in exactly one arm, by the models
it asks through any slot or composite member; an arm none reaches is "not applicable"; whether a
configuration is local is `core.origin.is_local`'s answer for every slot. The five reference
packs' arms are pinned here as their manifests give them."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from mendari.cli import main
from mendari.core.origin import is_local
from mendari.core.pack import Pack
from mendari.core.registry import discover, resolve_pack
from mendari.core.spec import ProviderSlot, ProviderVariant
from mendari.errors import MendariError
from mendari.evals.arms import (
    ARMS,
    CLASSIFIER,
    CLASSIFIER_AND_LLM,
    DETERMINISTIC,
    FORMAT,
    LLM,
    MAX_CONFIGURATIONS,
    MODEL_CLASSES,
    NOT_APPLICABLE,
    Configuration,
    arm_of,
    arms,
    arms_report,
    arms_text,
    configurations,
    remote_types,
    variant_types,
)
from mendari.providers import MODEL_TYPES, uses_model

REFERENCE = sorted((p for p in discover().packs if p.origin == "maintained"), key=lambda p: p.id)
NA = NOT_APPLICABLE
# Walked from the manifests on main 06d5332f (the LP-17 plan's table, reproduced): per arm, how
# many configurations, or "not applicable".
TABLE: dict[str, tuple[int | str, int | str, int | str, int | str]] = {
    "mendari/ai-guard": (1, 2, 2, NA),
    "mendari/clinical-trial-prescreener": (2, 4, 4, NA),
    "mendari/patient-inbox-router": (2, 10, 7, 6),
    "mendari/prior-auth-preflight": (1, 2, 2, NA),
    "mendari/referral-router": (1, 8, 5, 6),
}


def counted(pack: Pack) -> tuple[int | str, ...]:
    return tuple(len(arm.configurations) or arm.status for arm in arms(pack).values())


def labels(pack: Pack, arm: str) -> list[str]:
    return [configuration.label for configuration in arms(pack)[arm].configurations]


def config(variant: ProviderVariant) -> dict[str, Any]:
    return {"type": variant.type, **(variant.model_extra or {})}


def with_slots(pack: Pack, slots: dict[str, dict[str, dict[str, Any]]]) -> Pack:
    """`pack` with these slots (slot -> variant -> config) instead of its own, each answering
    the pack's primary question and defaulting to its first variant: the manifest alone, which
    is all arms read."""
    primary = pack.manifest.policy.primary
    providers = {
        name: ProviderSlot(
            answers=[primary],
            use=next(iter(variants)),
            variants={key: ProviderVariant(**value) for key, value in variants.items()},
        )
        for name, variants in slots.items()
    }
    return replace(pack, manifest=pack.manifest.model_copy(update={"providers": providers}))


def test_the_five_packs_arms_are_their_manifests() -> None:
    assert [pack.id for pack in REFERENCE] == sorted(TABLE)
    assert {pack.id: counted(pack) for pack in REFERENCE} == TABLE
    assert list(ARMS) == [DETERMINISTIC, CLASSIFIER, LLM, CLASSIFIER_AND_LLM]
    assert list(ARMS.values()) == [
        "Mendari only",
        "+ specialised classifier",
        "+ general LLM",
        "+ both",
    ]
    # The deterministic arms with two configurations, and what the second one is.
    trial = resolve_pack("mendari/clinical-trial-prescreener")
    assert labels(trial, DETERMINISTIC) == [
        "structured=criteria,notes=rules",
        "structured=cql,notes=rules",
    ]
    inbox = resolve_pack("mendari/patient-inbox-router")
    assert labels(inbox, DETERMINISTIC) == [
        "safety_screen=rules,router=naive_bayes",
        "safety_screen=rules,router=rules",
    ]


def test_an_arm_no_configuration_reaches_is_not_applicable() -> None:
    for pack in REFERENCE:
        report = arms_report(pack)
        for arm, entry in report["arms"].items():
            reached = any(c.arm == arm for c in configurations(pack))
            assert (entry["status"] == NOT_APPLICABLE) is (not reached), (pack.id, arm)
            assert bool(entry["configurations"]) is reached
    guard = resolve_pack("mendari/ai-guard")
    only_rules = with_slots(guard, {"screen": {"rules": {"type": "rules", "file": "x.yaml"}}})
    report = arms_report(only_rules)
    assert [entry["status"] for entry in report["arms"].values()] == [
        "applicable",
        NOT_APPLICABLE,
        NOT_APPLICABLE,
        NOT_APPLICABLE,
    ]
    text = arms_text(report)
    assert "+ general LLM (llm): not applicable" in text
    assert "Mendari only (deterministic): 1 configuration\n" in text


def test_the_both_arm_exists_only_across_slots() -> None:
    """In the five packs no one variant asks both Jev and an LLM: a configuration reaches "+
    both" only with Jev in one slot and an LLM in another, in the inbox and referral packs."""
    reached = {pack.id for pack in REFERENCE if arms(pack)[CLASSIFIER_AND_LLM].configurations}
    assert reached == {"mendari/patient-inbox-router", "mendari/referral-router"}
    for pack in REFERENCE:
        slots = pack.manifest.providers
        for slot in slots.values():
            for variant in slot.variants.values():
                assert arm_of(variant_types(variant)) != CLASSIFIER_AND_LLM
        for configuration in arms(pack)[CLASSIFIER_AND_LLM].configurations:
            arms_of_slots = {
                arm_of(variant_types(slots[name].variants[variant]))
                for name, variant in configuration.providers.items()
            }
            assert {CLASSIFIER, LLM} <= arms_of_slots, configuration.label


def test_a_composite_member_sets_the_arm() -> None:
    """A composite's members are its types: rules with Jev is the classifier's arm, a cascade
    from naive Bayes to Jev too (and remote, through Jev). A composite with both a Jev and an
    LLM member reaches "+ both" in one slot, which no reference pack does."""
    inbox = resolve_pack("mendari/patient-inbox-router")
    router = inbox.manifest.providers["router"].variants["cascade"]
    assert variant_types(router) == {"cascade", "naive_bayes", "jev"}
    assert arm_of(variant_types(router)) == CLASSIFIER
    assert remote_types(router) == {"jev"}
    screen = inbox.manifest.providers["safety_screen"].variants
    assert arm_of(variant_types(screen["rules_and_llm"])) == LLM
    assert arm_of(variant_types(screen["rules_and_jev"])) == CLASSIFIER

    guard = resolve_pack("mendari/ai-guard")
    both = {"type": "vote", "members": {"jev": {"type": "jev"}, "llm": {"type": "llm"}}}
    local = {
        "type": "any_of",
        "members": {"rules": {"type": "rules"}, "bayes": {"type": "naive_bayes"}},
    }
    pack = with_slots(guard, {"screen": {"both": both, "local": local}})
    found = {c.label: c for c in configurations(pack)}
    assert found["screen=both"].arm == CLASSIFIER_AND_LLM
    assert found["screen=both"].remote_types == ("jev", "llm") and not found["screen=both"].local
    assert found["screen=local"].arm == DETERMINISTIC and found["screen=local"].local
    assert found["screen=local"].remote_types == ()


def test_naive_bayes_is_deterministic_and_local_and_cql_is_deterministic_but_remote() -> None:
    inbox = {c.label: c for c in configurations(resolve_pack("mendari/patient-inbox-router"))}
    bayes = inbox["safety_screen=rules,router=naive_bayes"]
    assert (bayes.arm, bayes.local, bayes.remote_types) == (DETERMINISTIC, True, ())
    trial = {c.label: c for c in configurations(resolve_pack("mendari/clinical-trial-prescreener"))}
    cql = trial["structured=cql,notes=rules"]
    assert (cql.arm, cql.local, cql.remote_types) == (DETERMINISTIC, False, ("cql",))
    jev = trial["structured=cql,notes=jev"]
    assert (jev.arm, jev.local, jev.remote_types) == (CLASSIFIER, False, ("cql", "jev"))


def test_every_configuration_is_in_exactly_one_arm() -> None:
    for pack in REFERENCE:
        every = [c.label for c in configurations(pack)]
        expected = 1
        for slot in pack.manifest.providers.values():
            expected *= len(slot.variants)
        assert len(every) == len(set(every)) == expected, pack.id
        in_arms = [label for arm in ARMS for label in labels(pack, arm)]
        assert sorted(in_arms) == sorted(every), pack.id
        assert arms_report(pack)["configurations"] == expected
        for configuration in configurations(pack):
            assert set(configuration.providers) == set(pack.manifest.providers)


def test_the_arm_agrees_with_uses_model_on_every_variant() -> None:
    """A variant alone is outside the deterministic arm exactly when `providers.uses_model`
    says it asks a model, and names a remote type exactly when `is_local` says it is not local;
    a configuration is local exactly when every slot's variant is."""
    for pack in REFERENCE:
        slots = pack.manifest.providers
        for slot in slots.values():
            for variant in slot.variants.values():
                asks = arm_of(variant_types(variant)) != DETERMINISTIC
                assert asks is uses_model(variant), (pack.id, variant)
                assert (not remote_types(variant)) is is_local(config(variant)), (pack.id, variant)
        for configuration in configurations(pack):
            variants = [slots[name].variants[v] for name, v in configuration.providers.items()]
            assert configuration.local is all(is_local(config(v)) for v in variants)
            assert configuration.local is (not configuration.remote_types)


def test_every_model_type_has_a_class() -> None:
    assert set(MODEL_CLASSES) == MODEL_TYPES
    assert arm_of(["rules", "criteria", "dosing", "naive_bayes", "cql", "any_of"]) == DETERMINISTIC
    assert arm_of(["any_of", "rules", "jev"]) == CLASSIFIER
    assert arm_of(["llm"]) == LLM
    assert arm_of(["jev", "llm"]) == CLASSIFIER_AND_LLM


def test_too_many_configurations_are_refused_before_any_is_listed() -> None:
    guard = resolve_pack("mendari/ai-guard")
    variants = {f"v{i}": {"type": "rules"} for i in range(10)}
    slots = {f"s{i}": variants for i in range(5)}  # 10 ** 5 configurations
    assert MAX_CONFIGURATIONS == 10**4  # as the spec says: 10,000
    with pytest.raises(MendariError, match="100,000 provider configurations"):
        arms(with_slots(guard, slots))
    fits = with_slots(guard, {f"s{i}": variants for i in range(4)})  # exactly the most listed
    assert len(configurations(fits)) == MAX_CONFIGURATIONS


def test_a_configuration_is_written_as_run_takes_it() -> None:
    configuration = Configuration({"router": "llm", "safety_screen": "rules"}, LLM, False, ("llm",))
    assert configuration.label == "router=llm,safety_screen=rules"
    assert configuration.to_dict() == {
        "providers": {"router": "llm", "safety_screen": "rules"},
        "local": False,
        "remote_types": ["llm"],
    }


def test_the_command_prints_each_arm(capsys: pytest.CaptureFixture[str]) -> None:
    """It writes nothing either (tests/test_read_only_commands.py, `PACK_ONLY`)."""
    pack = resolve_pack("mendari/referral-router")
    assert main(["scoreboard", "arms", "mendari/referral-router"]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"Arms of {pack.ref} ({FORMAT}): 20 configurations\n")
    assert "  Mendari only (deterministic): 1 configuration\n" in text
    assert (
        "    specialty_reader=naive_bayes,triage_screen=rules,checklist=criteria  (local)\n" in text
    )
    assert "  + both (classifier_and_llm): 6 configurations\n" in text
    assert main(["scoreboard", "arms", "mendari/referral-router", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == arms_report(pack)
    assert printed["format"] == FORMAT
    assert printed["pack"] == {"id": pack.id, "version": pack.version, "digest": pack.digest}
