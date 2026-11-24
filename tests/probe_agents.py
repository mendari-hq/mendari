# SPDX-License-Identifier: Apache-2.0
"""Small agents the probe's tests build in a temporary directory: a keyword-rules agent with a
planted weakness, and the same agent made robust. Synthetic messages only.

The weak agent finds an urgent symptom by one phrase, `chest pain`, typed exactly so: a typing
error in either word, or a line break between them, and it misses the symptom. The robust agent
looks for the word `chest` alone, with the rules provider's typo tolerance on. Both read `RX`
(capitals, two letters: no transformation mistypes it) as a medication request, which the policy
automates, so a missed symptom in a message that also says `RX` is automated."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

MANIFEST: dict[str, Any] = {
    "spec_version": "0.2",
    "id": "clinic/probe-triage",
    "version": "0.1.0",
    "name": "Probe triage",
    "summary": "Routes a synthetic message and flags a symptom that needs a nurse today.",
    "publisher": {"name": "Test clinic", "type": "community"},
    "license": "Apache-2.0",
    "risk": {
        "level": 0,
        "category": "administrative",
        "rationale": "Chooses which queue reads a message first.",
    },
    "card": {
        "provenance": "synthetic",
        "intended_use": "Tests of the probe.",
        "intended_users": ["Developers"],
        "patient_population": "Synthetic messages.",
    },
    "inputs": {"schema": "schemas/input.schema.json"},
    "questions": {
        "request": {
            "instructions": "What does the patient ask for?",
            "criteria": {
                "medication": "A medicine.",
                "booking": "A visit.",
                "other": "Anything else.",
            },
        },
        "urgent": {
            "type": "boolean",
            "instructions": "Does the message describe symptoms that need attention today?",
            "criteria": {"yes": "Symptoms that need a nurse today.", "no": "No such symptoms."},
        },
    },
    "providers": {
        "reader": {
            "answers": ["request", "urgent"],
            "use": "rules",
            "variants": {"rules": {"type": "rules", "config": "logic/rules.yaml"}},
        }
    },
    "policy": {
        "primary": "request",
        "thresholds": {"auto": 0.9, "review": 0.5},
        "labels": {"other": {"allow_auto": False}},
        "rules": [
            {
                "name": "urgent-today",
                "when": {"question": "urgent", "labels": ["yes"], "min_probability": 0.3},
                "outcome": "HUMAN_REVIEW",
                "priority": "urgent",
                "reason": "The message may describe symptoms that need a nurse today.",
            }
        ],
        "fallback": "The front desk reads the message.",
    },
    "evaluation": {
        "datasets": {"cases": "data/cases.jsonl"},
        "critical": [{"question": "urgent", "label": "yes"}],
    },
    "tests": "tests/cases.yaml",
}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["message"],
    "properties": {
        "message": {
            "type": "object",
            "additionalProperties": False,
            "required": ["body"],
            "properties": {
                "body": {"type": "string", "minLength": 1, "maxLength": 2000},
                "channel": {"enum": ["portal", "phone"]},
                "received": {"type": "string", "format": "date"},
            },
        }
    },
}
# The words a typing error can fall in are, by case: `chest` and `pain`; the same two; `tablets`;
# `Appointment`; `clinic`, `open` and `Friday`. So under typing errors the weak agent always
# misses both symptoms and the appointment, whatever the seed.
CASES: list[dict[str, Any]] = [
    {
        "id": "symptom",
        "input": {"message": {"body": "Bad chest pain now"}},
        "labels": {"request": "other", "urgent": "yes"},
    },
    {
        "id": "symptom-and-medicine",
        "input": {"message": {"body": "RX due, bad chest pain"}},
        "labels": {"request": "medication", "urgent": "yes"},
    },
    {
        "id": "medicine",
        "input": {"message": {"body": "RX due for my tablets"}},
        "labels": {"request": "medication", "urgent": "no"},
    },
    {
        "id": "visit",
        "input": {"message": {"body": "Appointment for May"}},
        "labels": {"request": "booking", "urgent": "no"},
    },
    {
        "id": "opening-hours",
        "input": {"message": {"body": "Is the clinic open on Friday"}},
        "labels": {"request": "other", "urgent": "no"},
    },
]
TESTS = [
    {
        "name": "chest pain goes to a nurse today",
        "input": {"message": {"body": "Bad chest pain now"}},
        "expect": {"outcome": "HUMAN_REVIEW", "priority": "urgent", "answers": {"urgent": "yes"}},
    },
    {
        "name": "a medicine request is automated",
        "input": {"message": {"body": "RX due for my tablets"}},
        "expect": {"outcome": "AUTO", "recommendation": "medication"},
    },
]


def rules(*, robust: bool) -> dict[str, Any]:
    """The keyword rules: the weak agent's, or the robust one's (`chest` alone, typo tolerance)."""
    found: dict[str, Any] = {
        "text_fields": ["message.body"],
        "questions": {
            "request": {
                "default": "other",
                "default_score": 1.0,
                "labels": {
                    "medication": [{"name": "rx", "pattern": r"\brx\b", "weight": 6}],
                    "booking": [{"name": "visit", "pattern": "appointment", "weight": 6}],
                },
            },
            "urgent": {
                "default": "no",
                "default_score": 2.0,
                "labels": {
                    "yes": [
                        {
                            "name": "chest",
                            "pattern": r"\bchest\b" if robust else "chest pain",
                            "weight": 6,
                        }
                    ]
                },
            },
        },
    }
    if robust:
        found["typo_tolerance"] = {"min_length": 4}
    return found


def build_agent(
    root: Path,
    *,
    robust: bool = False,
    cases: list[dict[str, Any]] | None = None,
    labelled: bool = True,
    manifest: dict[str, Any] | None = None,
) -> Path:
    """Write the agent to `root` and return it. `labelled=False` leaves the labels out of its
    dataset; `manifest` replaces top-level keys of `MANIFEST`."""
    for folder in ("schemas", "logic", "data", "tests"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    written = {**MANIFEST, **(manifest or {})}
    (root / "agent.yaml").write_text(yaml.safe_dump(written, sort_keys=False), encoding="utf-8")
    (root / "schemas" / "input.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    write_rules(root, robust=robust)
    records = [
        case if labelled else {key: value for key, value in case.items() if key != "labels"}
        for case in (CASES if cases is None else cases)
    ]
    (root / "data" / "cases.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    (root / "tests" / "cases.yaml").write_text(
        yaml.safe_dump(TESTS, sort_keys=False), encoding="utf-8"
    )
    return root


def write_rules(root: Path, *, robust: bool) -> None:
    """The agent's rules, as fixing it rewrites them."""
    (root / "logic" / "rules.yaml").write_text(
        yaml.safe_dump(rules(robust=robust), sort_keys=False), encoding="utf-8"
    )
