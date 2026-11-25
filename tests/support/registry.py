# SPDX-License-Identifier: Apache-2.0
"""Copies of the triage fixture (`mendari-test/triage`) as the tests of the registry, the Hub
client, the agent store and the engine for agents from a registry need them: under another
publisher's id, carrying the attestations a publisher uploads, with a pattern only Python's `re`
runs, or as an outside publisher's agent that automates once approved. Each is a copy in a
test's own folder: the fixture itself is never changed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from mendari import Decision, load_pack
from mendari.core.pack import Pack
from mendari.evals import leaderboard
from mendari.evals.harness import evaluate, resolve_dataset
from tests.conftest import TRIAGE_DIR

TRIAGE = load_pack(TRIAGE_DIR)
ROUTER_RULES = "logic/router_rules.yaml"  # the router's keyword rules
SCHEMA = "schemas/input.schema.json"


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


def attested(pack: Pack) -> Pack:
    """`pack` (a copy, changed in place) carrying what a publisher uploads about it beside its
    files: a benchmark, the evaluation of its benchmark set on its own providers, which its
    manifest names (`evaluation.benchmark`), and a leaderboard of that run. Neither file is in
    its digest; the manifest's line naming the benchmark is."""

    def named(manifest: dict[str, Any]) -> None:
        manifest["evaluation"]["benchmark"] = "evals/benchmark.json"

    edit_yaml(pack.root / "agent.yaml", named)
    pack = load_pack(pack.root)
    dataset = resolve_dataset(pack, pack.manifest.evaluation.benchmark_dataset or "test")
    report = evaluate(Decision(pack), dataset)
    (pack.root / "evals").mkdir(exist_ok=True)
    (pack.root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    board = leaderboard.build(pack, [report])
    pack.attestation(leaderboard.PATH).write_text(json.dumps(board), encoding="utf-8")
    return load_pack(pack.root)
