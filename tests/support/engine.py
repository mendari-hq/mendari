# SPDX-License-Identifier: Apache-2.0
"""Helpers for the engine's tests: a small agent written on the fly, and how to load it as an
agent the deployment does not own.

The bounded engine (ADR-0009: RE2, the caps, the input limits and a CPU budget per decision) runs
every agent the deployment does not own: one pulled from a registry (origin `hub`) whose digest,
the one `load_pack` hashes, is no bundled agent's. `sorter` writes such an agent, a synthetic
visit sorter with one keyword question, which its tests edit; `pulled` loads a folder as
`mendari pull` leaves an agent, so it runs on the bounded engine. Loaded as any other folder
(`load_pack`, origin `local`), the same agent is the operator's own and runs on `re`, unbounded.
"""

from __future__ import annotations

import copy
import functools
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from mendari import Decision, Pack, load_pack
from mendari.evals import leaderboard
from mendari.evals.harness import evaluate, resolve_dataset
from tests.conftest import TRIAGE_DIR

SORTER_ID = "mendari-test/visit-sorter"
RULES = "logic/rules.yaml"
SCHEMA = "schemas/input.schema.json"

# Synthetic: a front-desk message's topic, from two keyword patterns of 27 characters each.
SORTER_MANIFEST: dict[str, Any] = {
    "spec_version": "0.4",
    "id": SORTER_ID,
    "version": "0.1.0",
    "name": "Visit Sorter (test fixture)",
    "summary": (
        "Synthetic test fixture. Sorts a front-desk message by topic, for the tests of the "
        "engine that runs agents from a registry. Not for patient care."
    ),
    "publisher": {"name": "Mendari tests", "type": "community"},
    "license": "Apache-2.0",
    "risk": {
        "level": 0,
        "category": "administrative",
        "rationale": "Sorts front-desk messages by topic; no clinical content.",
    },
    "card": {
        "intended_use": "Tests of the engine. A synthetic test fixture.",
        "intended_users": ["Mendari developers"],
        "patient_population": "Synthetic messages.",
    },
    "inputs": {"schema": SCHEMA},
    "questions": {
        "topic": {
            "instructions": "What is the message about?",
            "criteria": {
                "billing": "A bill, a payment or insurance.",
                "scheduling": "Booking, moving or cancelling a visit.",
                "other": "Anything else.",
            },
        }
    },
    "providers": {
        "reader": {
            "answers": ["topic"],
            "use": "rules",
            "variants": {
                "rules": {"type": "rules", "config": RULES},
                # Never the default. Run by an agent the deployment does not own, its pinned model
                # is dropped (the deployment's is used) and its max_tokens held to the
                # deployment's cap; it sends only the fields it names.
                "llm": {
                    "type": "llm",
                    "model": "example/pinned-model",
                    "max_tokens": 30_000,
                    "state_fields": ["message"],
                },
            },
        }
    },
    "policy": {
        "primary": "topic",
        "thresholds": {"auto": 0.9, "review": 0.4},
        "fallback": "Front-desk queue.",
    },
    "tests": "tests/cases.yaml",
}
SORTER_RULES: dict[str, Any] = {
    "text_fields": ["message"],
    "questions": {
        "topic": {
            "default": "other",
            "default_score": 1.0,
            "labels": {
                "billing": [
                    {"name": "bill", "pattern": "bill|invoice|payment|insurance", "weight": 8}
                ],
                "scheduling": [
                    {"name": "visit", "pattern": "appointment|reschedule|cancel", "weight": 8}
                ],
            },
        }
    },
}
SORTER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {"message": {"type": "string", "maxLength": 2000}},
}
SORTER_CASES: list[dict[str, Any]] = [
    {
        "name": "a question about a bill is about billing",
        "input": {"message": "I have a question about my last bill."},
        "expect": {"recommendation": "billing", "answers": {"topic": "billing"}},
    },
    {
        "name": "moving a visit is scheduling",
        "input": {"message": "Can I reschedule my appointment to Friday?"},
        "expect": {"recommendation": "scheduling", "answers": {"topic": "scheduling"}},
    },
]

Edit = Callable[[dict[str, Any]], None]


def sorter(
    base: Path,
    *,
    rules: Edit | None = None,
    schema: Edit | None = None,
    manifest: Edit | None = None,
    pack_id: str = SORTER_ID,
) -> Path:
    """The visit sorter written under `base` (in a folder named after its id's name), its
    rules, input schema and manifest changed by the callables, with id `pack_id`."""
    root = base / pack_id.partition("/")[2]
    (root / "logic").mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "tests").mkdir()
    files: list[tuple[str, dict[str, Any], Edit | None, Callable[[Any], str]]] = [
        (RULES, SORTER_RULES, rules, _yaml),
        (SCHEMA, SORTER_SCHEMA, schema, json.dumps),
        ("agent.yaml", {**SORTER_MANIFEST, "id": pack_id}, manifest, _yaml),
    ]
    for relative, content, edit, dump in files:
        data = copy.deepcopy(content)
        if edit is not None:
            edit(data)
        (root / relative).write_text(dump(data), encoding="utf-8")
    (root / "tests" / "cases.yaml").write_text(_yaml(SORTER_CASES), encoding="utf-8")
    return root


def pulled(root: Path) -> Pack:
    """The agent at `root` loaded as one pulled from a registry (origin `hub`), as `mendari pull`
    leaves it: with a digest no bundled agent has, it runs on the bounded engine."""
    return load_pack(root, origin="hub")


def benchmark(agent: Path = TRIAGE_DIR, dataset: str = "test") -> dict[str, Any]:
    """The evaluation report (`mendari eval`) of the agent in folder `agent` on its dataset
    `dataset`, on its default providers: a copy of its own to change, of a run made once a
    session (a test fixture's agent commits no benchmark). The triage fixture's automates nothing,
    as its screen is the keyword rules alone."""
    return copy.deepcopy(_evaluated(str(agent), dataset))


@functools.cache
def _evaluated(agent: str, dataset: str) -> dict[str, Any]:
    pack = load_pack(agent)
    return evaluate(Decision(pack), resolve_dataset(pack, dataset))


def published(
    agent: Path,
    base: Path,
    *,
    runs: Sequence[Mapping[str, str]] = (),
    manifest: Edit | None = None,
) -> Pack:
    """A copy under `base` of the agent in folder `agent` (a fixture's, which commits none)
    carrying in `evals/` what a published agent carries: its own evaluation on its benchmark
    set, named by `evaluation.benchmark`, and with `runs` (provider choices) a leaderboard of
    that set evaluated on its default providers and on each of them. Every figure is the copy's
    own: `evals/` is outside its digest, which is the one recorded. `manifest` changes the
    copy's manifest first (another version, say)."""
    root = base / agent.name
    shutil.copytree(agent, root)
    path = root / "agent.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["evaluation"]["benchmark"] = "evals/benchmark.json"
    if manifest is not None:
        manifest(data)
    path.write_text(_yaml(data), encoding="utf-8")
    pack = load_pack(root)
    dataset = resolve_dataset(pack, pack.manifest.evaluation.benchmark_dataset or "test")
    report = evaluate(Decision(pack), dataset)
    (root / "evals").mkdir()
    (root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    if runs:
        reports = [report, *(evaluate(Decision(pack, providers=dict(r)), dataset) for r in runs)]
        board = leaderboard.build(pack, reports)
        (root / leaderboard.PATH).write_text(json.dumps(board), encoding="utf-8")
    return load_pack(root)


def _yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
