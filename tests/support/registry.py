# SPDX-License-Identifier: Apache-2.0
"""Copies of the triage fixture (`mendari-test/triage`) as the tests of the registry, the Hub
client, the agent store and the engine for agents from a registry need them: under another
publisher's id, carrying the attestations a publisher uploads, with a pattern only Python's `re`
runs, or as an outside publisher's agent that automates once approved. Each is a copy in a
test's own folder: the fixture itself is never changed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from mendari import Decision, load_agent
from mendari.core.agent import Agent
from mendari.evals import leaderboard
from mendari.evals.harness import evaluate, resolve_dataset
from tests.conftest import TRIAGE_DIR

TRIAGE = load_agent(TRIAGE_DIR)
# The engine's public address in the tests that sign people in to it (conftest's `member` and
# `signed_in` name `mendari.test` as their host).
HTTPS = "https://mendari.test"
ROUTER_RULES = "logic/router_rules.yaml"  # the router's keyword rules
SCREEN_RULES = "logic/screen_rules.yaml"  # the screen's
SCHEMA = "schemas/input.schema.json"

# An outside publisher's agent (`outside_agent`): the id the registry's recall tests pull it by.
OUTSIDE_ID = "someone/visit-sorter"
# What it automates once its organization approves it, and a rule that blocks it instead.
BILL = {"message": {"body": "I have a question about my last bill."}}
BLOCK_RULE = {
    "name": "blk",
    "when": {"question": "destination", "labels": ["billing"]},
    "outcome": "BLOCK",
    "reason": "Billing is blocked.",
}
# The longest text an agent from a registry reads, all of it emoji: what slow patterns are
# slowest on (`slow_rules`).
EMOJI = "\U0001f600" * 10_000
# Each takes about half a second on EMOJI, and the eight stay within every cap of the engine for
# agents from a registry, with the router's other patterns.
SLOW = [rf"\W{{1,299}}{i}" for i in range(8)]
# The CPU budget of a decision on that engine (1 s), one step under way and headroom for a busy
# runner: without the budget, the slow rules took 5.5 s and a fanned-out schema minutes.
BOUND_S = 6.0


def edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
    """Apply `change` to the YAML document at `path`, in place."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def edit_json(path: Path, change: Callable[[Any], None]) -> None:
    """Apply `change` to the JSON document at `path`, in place."""
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


Edit = Callable[[dict[str, Any]], None]


def triage_copy(
    base: Path,
    agent_id: str,
    *,
    rules: Edit | None = None,
    schema: Edit | None = None,
    manifest: Edit | None = None,
) -> Path:
    """The triage fixture under `agent_id`, in `base`: `rules`, `schema` and `manifest` change
    the router's rules, the input schema and the manifest."""
    root = base / agent_id.replace("/", "-")
    shutil.copytree(TRIAGE_DIR, root)
    edit_yaml(root / "agent.yaml", lambda data: data.update(id=agent_id))
    if rules is not None:
        edit_yaml(root / ROUTER_RULES, rules)
    if schema is not None:
        edit_json(root / SCHEMA, schema)
    if manifest is not None:
        edit_yaml(root / "agent.yaml", manifest)
    return root


def outside_agent(
    base: Path,
    *,
    rules: Edit | None = None,
    schema: Edit | None = None,
    manifest: Edit | None = None,
    agent_id: str = OUTSIDE_ID,
) -> Path:
    """The triage fixture as an outside publisher's agent, in `base`: under its own id, its
    screen on its canned agent and its bills automatable, so a confident decision automates
    once the organization approves it; its router's LLM variant pins a model of its own (a
    deployment's model replaces it). `rules`, `schema` and `manifest` change the router's rules,
    the input schema and the manifest last. Load it with `origin="hub"` to have it as pulled."""

    def outside(data: dict[str, Any]) -> None:
        data["providers"]["screen"]["use"] = "agent"
        data["providers"]["router"]["variants"]["llm"]["model"] = "example/pinned-model"
        del data["policy"]["labels"]["billing"]
        if manifest is not None:
            manifest(data)

    return triage_copy(base, agent_id, rules=rules, schema=schema, manifest=outside)


def slow_rules() -> Callable[[dict[str, Any]], None]:
    """A change to the router's rules: bills found by eight patterns that are slow on EMOJI (each
    within the caps), so a decision on it spends the budget of an agent from a registry."""

    def edit(rules: dict[str, Any]) -> None:
        rules["questions"]["destination"]["labels"]["billing"] = [
            {"name": f"slow{i}", "pattern": pattern, "weight": 8} for i, pattern in enumerate(SLOW)
        ]

    return edit


def long_message(schema: dict[str, Any]) -> None:
    """A change to the input schema: a message's body of any length."""
    schema["properties"]["message"]["properties"]["body"].pop("maxLength")


def fan_out(depth: int) -> dict[str, Any]:
    """`$defs/t{i}` is allOf two `$ref`s to `t{i+1}`: the leaf applies 2**depth times."""
    defs: dict[str, Any] = {
        f"t{i}": {"allOf": [{"$ref": f"#/$defs/t{i + 1}"}, {"$ref": f"#/$defs/t{i + 1}"}]}
        for i in range(depth)
    }
    return defs | {f"t{depth}": {"minLength": 1}}


def with_lookahead(root: Path) -> Path:
    """The triage copy at `root` with one more router pattern, a lookahead: Python's `re` runs
    it, RE2 cannot, so the copy loads as an owned agent and is refused on the engine for agents
    from a registry (ADR-0009)."""

    def add(rules: dict[str, Any]) -> None:
        rules["questions"]["destination"]["labels"]["pharmacy"].append(
            {"name": "a-refill-not-a-question", "pattern": "refill(?! question)", "weight": 1}
        )

    edit_yaml(root / ROUTER_RULES, add)
    return root


def attested(agent: Agent) -> Agent:
    """`agent` (a copy, changed in place) carrying what a publisher uploads about it beside its
    files: a benchmark, the evaluation of its benchmark set on its own providers, which its
    manifest names (`evaluation.benchmark`), and a leaderboard of that run. Neither file is in
    its digest; the manifest's line naming the benchmark is."""

    def named(manifest: dict[str, Any]) -> None:
        manifest["evaluation"]["benchmark"] = "evals/benchmark.json"

    edit_yaml(agent.root / "agent.yaml", named)
    agent = load_agent(agent.root)
    dataset = resolve_dataset(agent, agent.manifest.evaluation.benchmark_dataset or "test")
    report = evaluate(Decision(agent), dataset)
    (agent.root / "evals").mkdir(exist_ok=True)
    (agent.root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    board = leaderboard.build(agent, [report])
    agent.attestation(leaderboard.PATH).write_text(json.dumps(board), encoding="utf-8")
    return load_agent(agent.root)
