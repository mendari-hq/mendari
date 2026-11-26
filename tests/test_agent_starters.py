# SPDX-License-Identifier: Apache-2.0
"""The agent starters (examples/http-agent, examples/langgraph-agent, examples/claude-agent):
each carries the kit as it is in Mendari, and the demo agent's own manifest, pointed at a
starter by MENDARI_AGENT_URL, is governed end to end: urgent messages to a nurse, a clear
refill automated. LangGraph is not a dependency of Mendari: its starter is skipped without it."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import agent_kit
from mendari.core.agent import load_agent
from mendari.core.decision import Decision
from mendari.providers.agent import ENDPOINT_ENV, TOKEN_ENV

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
DEMO = ROOT / "agents" / "portal-message-agent"
URL = "https://starter-agent.test/decide"
STARTERS = ("http-agent", "langgraph-agent", "claude-agent")


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_starter(name: str) -> ModuleType:
    """The starter's api/index.py, with its own agent.py as `agent` and the kit as its copy."""
    folder = EXAMPLES / name
    # The starter's mendari_agent_kit.py is the kit, byte for byte (the test below).
    sys.modules["mendari_agent_kit"] = agent_kit
    prefix = name.replace("-", "_")
    agent = _module(f"{prefix}_agent", folder / "agent.py")
    saved = sys.modules.get("agent")
    sys.modules["agent"] = agent  # what api/index.py imports
    try:
        return _module(f"{prefix}_service", folder / "api" / "index.py")
    finally:
        if saved is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = saved


@pytest.mark.parametrize("name", STARTERS)
def test_each_starter_carries_the_kit_as_it_is_in_mendari(name: str) -> None:
    """A starter deploys alone (on Vercel only its folder is uploaded), so it carries a copy of
    the kit: the same bytes as src/mendari/agent_kit.py. After changing the kit, copy it again:
    for d in examples/*-agent; do cp src/mendari/agent_kit.py $d/mendari_agent_kit.py; done"""
    folder = EXAMPLES / name
    kit = (ROOT / "src" / "mendari" / "agent_kit.py").read_bytes()
    assert (folder / "mendari_agent_kit.py").read_bytes() == kit
    for needed in ("agent.py", "api/index.py", "requirements.txt", "vercel.json", "README.md"):
        assert (folder / needed).is_file(), needed
    requirements = (folder / "requirements.txt").read_text()
    # The starter carries the kit and never installs the whole of Mendari for it.
    assert not any(
        line.strip().split(">")[0].split("=")[0].strip() == "mendari"
        for line in requirements.splitlines()
    )


def governed(monkeypatch: pytest.MonkeyPatch, service: ModuleType) -> tuple[Decision, list[Any]]:
    """The demo agent's manifest with MENDARI_AGENT_URL pointed at the starter's app."""
    monkeypatch.setenv(ENDPOINT_ENV, URL)
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    monkeypatch.setenv("AGENT_TOKEN", "s3cret")
    monkeypatch.delenv("AGENT_ALLOW_OPEN", raising=False)
    client = TestClient(service.app)
    seen: list[httpx.Request] = []

    def to_service(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = client.post(
            "/decide",
            content=request.content,
            headers={"authorization": request.headers.get("authorization", "")},
        )
        return httpx.Response(reply.status_code, content=reply.content)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", to_service)
    return Decision(load_agent(DEMO)), seen


URGENT = (
    "It feels like an elephant is sitting on my chest.",
    "I have had chest pain since last night. Could you refill my lisinopril?",
    "I cannot catch my breath even when I am resting.",
)


@pytest.mark.parametrize("name", ["http-agent", "langgraph-agent"])
def test_the_engine_governs_the_starter_end_to_end(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if name == "langgraph-agent":
        pytest.importorskip("langgraph")
    decision, seen = governed(monkeypatch, load_starter(name))
    for body in URGENT:
        urgent = decision({"message": {"body": body}})
        assert (urgent.outcome.value, urgent.priority.value) == ("HUMAN_REVIEW", "urgent"), body
    refill = decision({"message": {"body": "Could you refill my lisinopril? I am running low."}})
    assert (refill.outcome.value, refill.recommendation) == ("AUTO", "refill")
    visit = decision({"message": {"body": "I need to book an appointment for Friday."}})
    assert (visit.outcome.value, visit.recommendation) == ("AUTO", "appointment")
    denied = decision({"message": {"body": "No chest pain. Please refill my metformin."}})
    assert (denied.outcome.value, denied.recommendation) == ("AUTO", "refill")
    both = decision({"message": {"body": "Refill my statin and book an appointment, please."}})
    assert both.outcome.value == "HUMAN_REVIEW" and both.recommendation == "other"
    assert len(seen) == len(URGENT) + 4
    assert all(r.headers["authorization"] == "Bearer s3cret" for r in seen)


def test_a_starter_without_its_token_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    decision, _ = governed(monkeypatch, load_starter("http-agent"))
    monkeypatch.setenv(TOKEN_ENV, "wrong")
    refill = decision({"message": {"body": "Could you refill my lisinopril?"}})
    assert refill.outcome.value in ("HUMAN_REVIEW", "ABSTAIN")
