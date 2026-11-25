# SPDX-License-Identifier: Apache-2.0
"""The critical-label invariant, held for every maintained agent (spec §8): here the
agents the tests bundle, the test fixtures' (`mendari-test/triage`, `clinical-review`,
`chart-summary`).

For every configuration an agent can run without calling a service (each slot's local variants,
`core.origin.is_local`: keyword rules, or a composite of those; an `agent` variant is a service,
whose canned answers stand in for one), on every dataset it declares:

1. In the agent's default deployment (no governance approval), no true case of a critical label
   is automated: a person sees every one.
2. With a governance approval (risk levels 2-3 only; it changes nothing below), an automated
   true critical case carries the correct primary recommendation. The clinical review fixture,
   for example, may automate "see today" for a patient who should be seen today, never "care at
   home".

An agent that breaks either fails here, naming the configuration, dataset and case ids."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from mendari import Agent, Decision, Outcome
from mendari.core.agent import load_agent
from mendari.core.origin import is_local
from mendari.core.policy import OVERRIDABLE_RISK, GovernanceOverride
from mendari.core.registry import discover
from mendari.evals.harness import resolve_dataset
from tests.conftest import FIXTURE_AGENTS

APPROVAL = GovernanceOverride(approved_by="the safety invariant test", reference="no ticket")


def _maintained() -> list[Agent]:
    return sorted((p for p in discover().agents if p.origin == "maintained"), key=lambda p: p.id)


AGENTS = _maintained()


def offline_configurations(agent: Agent) -> list[dict[str, str]]:
    """Every combination of each slot's local variants."""
    local = {
        slot_name: [
            name
            for name, variant in slot.variants.items()
            if is_local({"type": variant.type, **(variant.model_extra or {})})
        ]
        for slot_name, slot in agent.manifest.providers.items()
    }
    return [dict(zip(local, combo, strict=True)) for combo in itertools.product(*local.values())]


def _violations(agent: Agent, configuration: dict[str, str], governed: bool) -> list[str]:
    manifest = agent.manifest
    primary = manifest.policy.primary
    decision = Decision(agent, providers=configuration, governance=APPROVAL if governed else None)
    found = []
    for name in manifest.evaluation.datasets:
        for index, record in enumerate(resolve_dataset(agent, name).records):
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


def test_every_maintained_agent_is_checked() -> None:
    """Every agent the tests bundle, by the id its manifest gives."""
    bundled = {
        yaml.safe_load((folder / "agent.yaml").read_text())["id"]
        for folder in FIXTURE_AGENTS.iterdir()
        if (folder / "agent.yaml").is_file()
    }
    assert bundled and {agent.id for agent in AGENTS} == bundled
    for agent in AGENTS:
        assert agent.manifest.evaluation.critical, f"{agent.id} declares no critical label"
        assert offline_configurations(agent), f"{agent.id} has no configuration that runs offline"


@pytest.mark.parametrize("agent", AGENTS, ids=lambda p: p.id)
def test_no_critical_case_is_automated_by_default(agent: Agent) -> None:
    problems = {
        str(configuration): found
        for configuration in offline_configurations(agent)
        if (found := _violations(agent, configuration, governed=False))
    }
    assert problems == {}


@pytest.mark.parametrize(
    "agent", [p for p in AGENTS if p.manifest.risk.level in OVERRIDABLE_RISK], ids=lambda p: p.id
)
def test_an_approved_agent_automates_critical_cases_only_correctly(agent: Agent) -> None:
    problems = {
        str(configuration): found
        for configuration in offline_configurations(agent)
        if (found := _violations(agent, configuration, governed=True))
    }
    assert problems == {}


def test_the_check_catches_an_automated_critical_case(agent_copy: Callable[..., Path]) -> None:
    """Without its automation gate the triage fixture automates the same-day symptoms its
    keyword rules miss, as a patient words them (the gate of #130): the check must see them."""
    target = agent_copy(policy=lambda policy: policy.pop("automation_requires"))
    ungated = load_agent(target)
    found = _violations(ungated, {"router": "rules", "screen": "rules"}, False)
    assert any(line.startswith("critical ") for line in found), found
    assert all("urgency=yes" in line for line in found)
