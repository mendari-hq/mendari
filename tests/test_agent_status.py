# SPDX-License-Identifier: Apache-2.0
"""Every surface says what a pack is (core/status.py), and questions read as words.

The agents the tests bundle (the test fixtures') are synthetic examples, not clinically
reviewed and not for patient care. The line is derived from the manifest's `card.provenance`,
never written by the agent; without it an agent is only "not clinically reviewed". Questions and
labels may carry the names a person reads (`display`, `names`), with the key as words when they
do not."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import Agent, Decision
from mendari.core.card import render_card
from mendari.core.registry import discover
from mendari.core.spec import AgentManifest, readable
from mendari.core.status import NOT_REVIEWED, SYNTHETIC_EXAMPLE, status_label, title
from mendari.server.app import create_app
from mendari.standards import cds_hooks
from tests.conftest import ROOT

REFERENCE = sorted((p for p in discover().agents if p.origin == "maintained"), key=lambda p: p.id)
# A synthetic portal message as the triage fixture's FHIR mapping reads one: a refill, for which
# its CDS service shows a card.
COMMUNICATION: dict[str, Any] = {
    "resourceType": "Communication",
    "id": "msg-1",
    "status": "in-progress",
    "subject": {"reference": "Patient/pat-1"},
    "topic": {"text": "Refill"},
    "medium": [{"coding": [{"system": "x", "code": "portal"}]}],
    "sent": "2026-09-25T10:00:00Z",
    "payload": [{"contentString": "Please renew my prescription for metformin."}],
}
PATIENT: dict[str, Any] = {"resourceType": "Patient", "id": "pat-1", "birthDate": "1950-03-02"}


def test_the_label_says_what_the_plan_requires() -> None:
    """The launch plan's words for a reference pack (A2 step 6, B21), written once."""
    assert SYNTHETIC_EXAMPLE == "Synthetic example. Not clinically reviewed. Not for patient care."


def _manifest(agent: Agent, edit: Any) -> AgentManifest:
    raw = agent.manifest.model_dump(by_alias=True, exclude_none=True)
    edit(raw)
    return AgentManifest.model_validate(raw)


@pytest.mark.parametrize("agent", REFERENCE, ids=lambda p: p.id)
def test_every_reference_agent_says_it_is_a_synthetic_example(agent: Agent) -> None:
    assert agent.manifest.card.provenance == "synthetic"
    assert agent.status_label == SYNTHETIC_EXAMPLE
    for card in (render_card(agent), (agent.root / "DECISION_CARD.md").read_text()):
        lines = card.splitlines()
        assert lines[0].startswith("# Decision Card: ")
        assert lines[2] == f"**{SYNTHETIC_EXAMPLE}**"


def test_the_line_follows_provenance_and_nothing_else(triage_agent: Agent) -> None:
    def provenance(value: str | None) -> Any:
        def edit(raw: dict[str, Any]) -> None:
            raw["card"].pop("provenance", None)
            if value is not None:
                raw["card"]["provenance"] = value

        return edit

    assert status_label(_manifest(triage_agent, provenance("synthetic"))) == SYNTHETIC_EXAMPLE
    for value in (None, "de_identified", "site_records"):
        assert status_label(_manifest(triage_agent, provenance(value))) == NOT_REVIEWED
    with pytest.raises(ValidationError):
        _manifest(triage_agent, provenance("clinically_reviewed"))


def test_the_api_says_it(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "s.db"))
    listed = client.get("/api/agents").json()
    assert listed and {p["status_label"] for p in listed} == {SYNTHETIC_EXAMPLE}
    detail = client.get("/api/agents/mendari-test/triage").json()
    assert detail["status_label"] == SYNTHETIC_EXAMPLE


def test_cds_hooks_say_it_in_discovery_and_on_every_card(triage: Decision) -> None:
    service = cds_hooks.discovery([triage.agent])["services"][0]
    assert service["usageRequirements"].startswith(SYNTHETIC_EXAMPLE)
    response = cds_hooks.invoke(
        triage,
        {
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    assert response["cards"][0]["detail"].splitlines()[0].startswith(f"*{SYNTHETIC_EXAMPLE}*")


def test_the_readme_lists_the_reference_agents_as_synthetic_examples() -> None:
    assert SYNTHETIC_EXAMPLE in (ROOT / "README.md").read_text()


def test_display_names_are_checked(triage_agent: Agent) -> None:
    def named(**fields: Any) -> Any:
        def edit(raw: dict[str, Any]) -> None:
            raw["questions"]["destination"].update(fields)

        return edit

    manifest = _manifest(triage_agent, named(display="Team", names={"front_desk": "Reception"}))
    question = manifest.questions["destination"]
    assert title("destination", question) == "Team"
    assert question.name_of("front_desk") == "Reception"
    assert question.name_of("pharmacy") == "Pharmacy"
    with pytest.raises(ValidationError, match="does not have"):
        _manifest(triage_agent, named(names={"physician": "Physician"}))
    for display in ("", "x" * 81):
        with pytest.raises(ValidationError):
            _manifest(triage_agent, named(display=display))


def test_a_key_reads_as_words() -> None:
    assert readable("medical_assistant") == "Medical assistant"
    assert readable("yes") == "Yes"
    assert readable("egfr_30_or_more") == "Egfr 30 or more"  # why agents name their questions
