# SPDX-License-Identifier: Apache-2.0
"""The critical-label invariant, held for every maintained pack (spec 0.2, §8).

For every configuration a pack can run without calling a service (each slot's local variants:
rules, naive Bayes, criteria, dosing, or a composite of those), on every dataset it declares:

1. In the pack's default deployment (no governance approval), no true case of a critical label
   is automated: a person sees every one.
2. With a governance approval (risk levels 2-3 only; it changes nothing below), an automated
   true critical case carries the correct primary recommendation. The trial prescreener, for
   example, may automate "not eligible" on a correctly found exclusion, never "candidate".

A pack that breaks either fails here, naming the configuration, dataset and case ids."""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

import pytest
import yaml

from mendari import Decision, Outcome, Pack
from mendari.core.origin import is_local
from mendari.core.pack import load_pack
from mendari.core.policy import OVERRIDABLE_RISK, GovernanceOverride
from mendari.core.registry import discover
from mendari.evals.harness import resolve_dataset
from tests.conftest import INBOX_DIR

APPROVAL = GovernanceOverride(approved_by="the safety invariant test", reference="no ticket")


def _maintained() -> list[Pack]:
    return sorted((p for p in discover().packs if p.origin == "maintained"), key=lambda p: p.id)


PACKS = _maintained()


def offline_configurations(pack: Pack) -> list[dict[str, str]]:
    """Every combination of each slot's local variants."""
    local = {
        slot_name: [
            name
            for name, variant in slot.variants.items()
            if is_local({"type": variant.type, **(variant.model_extra or {})})
        ]
        for slot_name, slot in pack.manifest.providers.items()
    }
    return [dict(zip(local, combo, strict=True)) for combo in itertools.product(*local.values())]


def _violations(pack: Pack, configuration: dict[str, str], governed: bool) -> list[str]:
    manifest = pack.manifest
    primary = manifest.policy.primary
    decision = Decision(pack, providers=configuration, governance=APPROVAL if governed else None)
    found = []
    for name in manifest.evaluation.datasets:
        for index, record in enumerate(resolve_dataset(pack, name).records):
            labels = record.get("labels", {})
            critical = [
                f"{c.question}={c.label}"
                for c in manifest.evaluation.critical
                if labels.get(c.question) == c.label
            ]
            if not critical:
                continue
            result = decision.decide(record["input"])
            if result.outcome is not Outcome.AUTO:
                continue
            truth = labels.get(primary)
            if governed and truth is not None and result.recommendation == truth:
                continue
            case = record.get("id", f"line {index + 1}")
            found.append(
                f"{name} {case} ({', '.join(critical)}): AUTO {result.recommendation!r}"
                + (f", truth {truth!r}" if governed else "")
            )
    return found


def test_every_maintained_pack_is_checked() -> None:
    assert len(PACKS) >= 5
    for pack in PACKS:
        assert pack.manifest.evaluation.critical, f"{pack.id} declares no critical label"
        assert offline_configurations(pack), f"{pack.id} has no configuration that runs offline"


@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.id)
def test_no_critical_case_is_automated_by_default(pack: Pack) -> None:
    problems = {
        str(configuration): found
        for configuration in offline_configurations(pack)
        if (found := _violations(pack, configuration, governed=False))
    }
    assert problems == {}


@pytest.mark.parametrize(
    "pack", [p for p in PACKS if p.manifest.risk.level in OVERRIDABLE_RISK], ids=lambda p: p.id
)
def test_an_approved_pack_automates_critical_cases_only_correctly(pack: Pack) -> None:
    problems = {
        str(configuration): found
        for configuration in offline_configurations(pack)
        if (found := _violations(pack, configuration, governed=True))
    }
    assert problems == {}


def test_the_check_catches_an_automated_critical_case(tmp_path: Path) -> None:
    """Without its automation gate the inbox pack automates missed emergencies (#130): the
    check must see them."""
    target = tmp_path / "inbox"
    shutil.copytree(INBOX_DIR, target)
    policy = yaml.safe_load((target / "policy.yaml").read_text())
    del policy["automation_requires"]
    (target / "policy.yaml").write_text(yaml.safe_dump(policy, sort_keys=False))
    ungated = load_pack(target)
    found = _violations(ungated, {"safety_screen": "rules", "router": "naive_bayes"}, False)
    assert any(line.startswith("red_flags_holdout ") for line in found), found
    assert all("urgency=urgent" in line for line in found)
