# SPDX-License-Identifier: Apache-2.0
"""Small agents built in a temporary directory, with a live agent service that a test scripts
(tests of calibration, repeated runs and the two-model check: ADR-0022).

The agent is what `mendari new` writes (the questions `request` and `urgent_symptoms`,
answered by the slot `reader`), never the bundled demo agent. Its service is a function a test
gives: `serve` points `MENDARI_AGENT_URL` at a made-up address and stands the function where the
provider's HTTP call is, so the provider's own code, its budget, its meter and its deadline all
run. Synthetic text only.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from mendari.cli import main
from mendari.providers.agent import ENDPOINT_ENV
from mendari.providers.agent_calibration import FORMAT

AGENT_URL = "https://agent.test/v1/decide"
AGENT_ID = "starter-portal-agent"  # what the scaffold names its agent
VERSION = "2.1.0"
LABELS = ("refill", "appointment", "other")
REFILL = {"message": {"body": "Please refill my metformin."}}
Respond = Callable[[dict[str, Any]], Any]


def scaffold(tmp_path: Path, name: str = "agent", edit: Any = None) -> Path:
    """A new agent in `tmp_path`, on spec 0.4, with `edit` applied to its manifest."""
    root = tmp_path / name
    assert main(["new", str(root), "--id", f"clinic/{name}"]) == 0

    def to_spec_04(manifest: dict[str, Any]) -> None:
        manifest["spec_version"] = "0.4"
        if edit is not None:
            edit(manifest)

    edit_manifest(root, to_spec_04)
    return root


def edit_manifest(root: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = root / "agent.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def variant(manifest: dict[str, Any], name: str = "agent") -> dict[str, Any]:
    found: dict[str, Any] = manifest["providers"]["reader"]["variants"][name]
    return found


def live_member(manifest: dict[str, Any], **more: Any) -> dict[str, Any]:
    """The scaffold's agent variant as a composite's member: live only (no canned answers)."""
    kept = {k: v for k, v in variant(manifest).items() if k not in ("mock_file", "calibration")}
    return {**kept, **more}


def answer(
    request: str = "refill",
    confidence: float = 0.97,
    *,
    urgent: str = "no",
    urgent_confidence: float = 0.99,
    version: str | None = VERSION,
    probabilities: dict[str, float] | None = None,
    cost: float | None = 0.001,
) -> dict[str, Any]:
    """What an agent's service answers: its answer to `request` with a confidence (or a whole
    distribution), `urgent_symptoms`, the version it reports and its usage."""
    said: dict[str, Any] = {"value": request}
    if probabilities is None:
        said["confidence"] = confidence
    else:
        said["probabilities"] = probabilities
    body: dict[str, Any] = {
        "answers": {
            "request": said,
            "urgent_symptoms": {"value": urgent, "confidence": urgent_confidence},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    if cost is not None:
        body["usage"]["cost_usd"] = cost
    if version is not None:
        body["agent_version"] = version
    return body


def serve(monkeypatch: pytest.MonkeyPatch, respond: Respond | list[Any]) -> list[dict[str, Any]]:
    """Stand `respond` where the agent's service is, and return the requests it gets. `respond`
    is a function of the request's body, or a list of answers given in turn (the last one again
    once they run out); an answer that is an exception is raised, as a failed call is."""
    requests: list[dict[str, Any]] = []

    def post(client: Any, url: str, body: dict[str, Any], **kwargs: Any) -> Any:
        assert url == AGENT_URL
        requests.append(body)
        if callable(respond):
            found = respond(body)
        else:
            found = respond[min(len(requests), len(respond)) - 1]
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setenv(ENDPOINT_ENV, AGENT_URL)
    monkeypatch.setattr("mendari.providers.agent.post_json", post)
    return requests


def calibration_file(
    root: Path,
    temperatures: dict[str, float],
    *,
    version: str = VERSION,
    agent_id: str | None = AGENT_ID,
    relative: str = "models/reader-agent-calibration.json",
    sha256: str | None = None,
    cases: int = 400,
) -> str:
    """A fitted calibration in the agent's folder, and its path as a manifest names it."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        "format": FORMAT,
        "agent": {"id": agent_id, "version": version, "model": None},
        "method": "temperature",
        "temperatures": temperatures,
        "questions": {key: {"cases": cases} for key in temperatures},
        "not_fitted": {},
        "fitted_on": {
            "dataset": "calibration",
            "sha256": sha256,
            "cases": cases,
            "date": "2026-11-24",
        },
        "failures": {},
        "cost_usd": 0.0,
    }
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return relative


def calibrated_agent(tmp_path: Path, temperatures: dict[str, float], **more: Any) -> Path:
    """A scaffolded agent whose variant names a calibration with these temperatures (and any
    other key of the variant in `more`)."""
    root = scaffold(tmp_path)
    relative = calibration_file(root, temperatures)

    def add(manifest: dict[str, Any]) -> None:
        variant(manifest).update({"calibration": relative, **more})

    edit_manifest(root, add)
    return root


# -- a simulated agent: over-confident by a known temperature --------------------------------------

_TOKEN = re.compile(r"\b([a-z]+-\d{5})\b")


def right_probability(confidence: float, temperature: float, labels: int = len(LABELS)) -> float:
    """How often an answer stated with `confidence` is right, for an agent whose confidence is
    off by `temperature`: the stated confidence, temperature-scaled over `labels` labels."""
    odds = math.exp(math.log(confidence * (labels - 1) / (1 - confidence)) / temperature)
    return odds / (odds + labels - 1)


class Simulated:
    """An agent's service whose answers a test knows: each case names itself by a token in its
    message, and the service answers it from a table. Its confidence in `request` is
    over-stated by `temperature`: an answer it gives with confidence c is right as often as c
    scaled by that temperature says. `flaky` of its wrong answers change between runs (the
    second of every three), and `flaky_right` of its right ones."""

    def __init__(
        self,
        temperature: float = 2.5,
        *,
        flaky: float = 0.0,
        flaky_right: float = 0.0,
        version: str = VERSION,
    ):
        self.temperature = temperature
        self.flaky = flaky
        self.flaky_right = flaky_right
        self.version = version
        self.table: dict[str, tuple[str, float, str, bool]] = {}
        self.asked: dict[str, int] = {}

    def cases(self, prefix: str, n: int, seed: int) -> list[dict[str, Any]]:
        """`n` labelled cases of a set, each with a row in the table."""
        rng = random.Random(seed)
        lines = []
        for number in range(n):
            token = f"{prefix}-{number:05d}"
            said = rng.choice(LABELS[:2])
            confidence = rng.choice([0.99, 0.97, 0.95, 0.9, 0.8, 0.7])
            right = rng.random() < right_probability(confidence, self.temperature)
            truth = said if right else rng.choice([label for label in LABELS if label != said])
            if right:
                changes = self.flaky_right > 0 and rng.random() < self.flaky_right
            else:
                changes = rng.random() < self.flaky
            self.table[token] = (said, confidence, truth, changes)
            lines.append(
                {
                    "id": token,
                    "input": {"message": {"body": f"A synthetic message, {token}."}},
                    "labels": {"request": truth, "urgent_symptoms": "no"},
                }
            )
        return lines

    def write(self, root: Path, name: str, n: int, seed: int) -> Path:
        """A dataset of the agent, `data/<name>.jsonl`, named in its manifest."""
        lines = self.cases(name[:4], n, seed)
        path = root / "data" / f"{name}.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")

        def name_it(manifest: dict[str, Any]) -> None:
            manifest["evaluation"]["datasets"][name] = f"data/{name}.jsonl"

        edit_manifest(root, name_it)
        return path

    def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        found = _TOKEN.search(body["input"]["message"]["body"])
        assert found is not None
        token = found.group(1)
        said, confidence, _, flaky = self.table[token]
        self.asked[token] = self.asked.get(token, 0) + 1
        if flaky and self.asked[token] % 3 == 2:  # the second of every three runs differs
            said = "appointment" if said == "refill" else "refill"
        return answer(said, confidence, version=self.version)
