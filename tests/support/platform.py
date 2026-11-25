# SPDX-License-Identifier: Apache-2.0
"""What the platform and server tests decide with: the triage fixture as a server serves it.

Synthetic test fixture. Not clinically reviewed. Not for patient care.

`tests/fixtures/reference-agents/triage` (`mendari-test/triage`, `TRIAGE_DIR`) routes a
synthetic patient message to the team that reads it first. On its default providers (the
keyword rules in both slots) nothing is automated (its policy's `automation_requires`); a test
that needs AUTO with sign-in on runs `calm_agent` (tests/conftest.py). This module gives the
tests:

- `TRIAGE` and `TRIAGE_API`: its id, and its routes' prefix;
- messages that reach each of its outcomes on its default providers (`URGENT`, `REFILL`, ...);
- `COMMUNICATION`, `PATIENT` and `BUNDLE`: a synthetic message as an EHR sends it (its FHIR
  mapping takes a Communication in);
- `bundled_agents`: the fixtures' agents a test server serves, read from their manifests, so no
  test counts them or names one it does not decide with;
- `local_site`: a local site's datasets for the server's `datasets_dir`;
- `attested`: a copy of the fixture with what a maintained agent commits about itself (its
  benchmark and leaderboard), which the fixture itself does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mendari import Decision, Pack, load_pack
from mendari.core.registry import SearchPath, discover
from mendari.evals import evaluate, leaderboard, resolve_dataset
from tests.conftest import CLINICAL_DIR, FIXTURE_AGENTS, TRIAGE_DIR

TRIAGE = "mendari-test/triage"
TRIAGE_API = f"/api/agents/{TRIAGE}"
CLINICAL = "mendari-test/clinical-review"

# Messages, each with what the fixture decides on its default providers (the keyword rules).
# HUMAN_REVIEW, a nurse at priority stat (rule `possible-emergency`):
URGENT = {"message": {"body": "I have had chest pain since last night."}}
# HUMAN_REVIEW, the pharmacy at priority routine (AUTO with the screen's agent):
REFILL = {"message": {"body": "Please renew my prescription for metformin."}}
# HUMAN_REVIEW, the front desk (AUTO with the screen's agent):
APPOINTMENT = {"message": {"body": "Can I move my appointment to Friday?"}}
# HUMAN_REVIEW, billing (never AUTO: `allow_auto: false`):
BILL = {"message": {"body": "Why was I charged twice this month?"}}
# ABSTAIN, no suggestion:
UNPLACED = {"message": {"body": "I am not sure who to ask about my visit last week."}}
# BLOCK (rule `abusive-message`):
ABUSIVE = {"message": {"body": "You people are useless."}}
# BLOCK: the input schema refuses it.
BLANK = {"message": {"body": ""}}

# A synthetic message as an EHR sends it: the triage fixture's FHIR mapping reads the
# Communication (its topic, its text and its channel) and not the Patient beside it. Its text is
# a possible emergency (HUMAN_REVIEW, a nurse at priority stat: a critical card).
COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Help"},
    "medium": [{"coding": [{"system": "x", "code": "sms"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [
        {"contentString": "I have had chest pain since last night"},
        {"contentString": "and it is getting worse."},
    ],
}
PATIENT: dict[str, Any] = {"resourceType": "Patient", "id": "pat-1", "birthDate": "1950-03-02"}
BUNDLE: dict[str, Any] = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": COMMUNICATION}, {"resource": PATIENT}],
}


def bundled_agents() -> list[Pack]:
    """The agents a test server serves as bundled (the fixtures' folder, `FIXTURE_AGENTS`), by
    id, read from their manifests: whichever are there."""
    found = discover([SearchPath(FIXTURE_AGENTS, "maintained")])
    return sorted(found.packs, key=lambda agent: agent.id)


def local_site(directory: Path) -> Path:
    """`directory` holding a local site's datasets, as a server's `datasets_dir` reads them:
    `local-site-example` labels the triage fixture's questions (its same-day symptoms, most worded
    as a patient might, which its keyword rules route worse than its benchmark set), and
    `clinical-local-site-example` the clinical-review fixture's, so it is offered to that agent
    only."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in (
        ("local-site-example", TRIAGE_DIR / "data" / "critical.jsonl"),
        ("clinical-local-site-example", CLINICAL_DIR / "data" / "test.jsonl"),
    ):
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        lines = [json.dumps({**record, "id": f"local-{i:05d}"}) for i, record in enumerate(records)]
        (directory / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


def attested(root: Path) -> Pack:
    """The copy of the triage fixture at `root` (`agent_copy`, its manifest naming
    `evals/benchmark.json` as its benchmark: `names_its_benchmark`), with what a maintained agent
    commits about itself: its benchmark, an evaluation of its default providers on its benchmark
    set, and a leaderboard of that and of its screen run by its canned agent."""
    agent = load_pack(root)
    dataset = resolve_dataset(agent, agent.manifest.evaluation.benchmark_dataset or "test")
    report = evaluate(Decision(agent), dataset)
    screened = evaluate(Decision(agent, providers={"screen": "agent"}, origin_cap=False), dataset)
    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "benchmark.json").write_text(json.dumps(report), encoding="utf-8")
    board = leaderboard.build(agent, [report, screened])
    (root / leaderboard.PATH).write_text(json.dumps(board), encoding="utf-8")
    return load_pack(root)


def names_its_benchmark(manifest: dict[str, Any]) -> None:
    """An `agent_copy` manifest edit: the copy names `evals/benchmark.json` as its benchmark."""
    manifest["evaluation"]["benchmark"] = "evals/benchmark.json"
