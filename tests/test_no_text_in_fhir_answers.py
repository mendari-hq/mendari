# SPDX-License-Identifier: Apache-2.0
"""No FHIR, SMART, CDS Hooks or `$decide` answer carries what an agent wrote (ADR-0027, point
2): a text is handed only to a person, by `POST …/decide` and `mendari decide`, and these
surfaces hand it to nobody. They say where each text stands (`written`) or nothing at all.

A mark is planted in every text a copy of the demo agent writes, and searched for in each
answer, for a decision that automates and for one that waits for a person: the FHIR decide
route and the Task it makes, `mendari decide --fhir`, `$decide`'s Bundle, a CDS Hooks card and
a SMART launch's decide. Every message here is synthetic."""

from __future__ import annotations

import functools
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.server import smart_routes
from mendari.server.app import create_app
from mendari.standards.fhir_client import FhirClient, GuardedHttp
from tests.conftest import DASHBOARD, DEMO_AGENT_DIR
from tests.test_smart import APP, CLIENT, EHR, PATIENT, FakeEhr, launch

DEMO = "mendari/portal-message-agent"
SERVICE = "/cds-services/mendari.portal-message-agent"
MARK = "ZQACKNOWLEDGEMENTMARK"
# What the demo agent's canned answers do with each: its routing automates, or a person reads.
MESSAGES = {
    "AUTO": "Please renew my prescription for insulin pens. Thank you.",
    "HUMAN_REVIEW": "Did my lab results come back yet?",
}
STANDS = {"AUTO": "not_released", "HUMAN_REVIEW": "held"}


def marked_agents(base: Path) -> Path:
    """A folder holding a copy of the demo agent whose every canned text ends with the mark."""
    folder = base / "portal-message-agent"
    shutil.copytree(DEMO_AGENT_DIR, folder)
    canned = folder / "fixtures" / "mock_agent.json"
    fixture = json.loads(canned.read_text(encoding="utf-8"))
    answers = [case["response"] for case in fixture["cases"]] + [fixture["default"]]
    assert all(answer["outputs"] for answer in answers)  # it writes with every message
    for answer in answers:
        answer["outputs"] = {name: f"{text} {MARK}" for name, text in answer["outputs"].items()}
    canned.write_text(json.dumps(fixture), encoding="utf-8")
    return base


def communication(body: str, rid: str = "msg-1", patient: str = "pat-1") -> dict[str, Any]:
    return {
        "resourceType": "Communication",
        "id": rid,
        "status": "completed",
        "subject": {"reference": f"Patient/{patient}"},
        "payload": [{"contentString": body}],
    }


def bundle(body: str) -> dict[str, Any]:
    sender = {"resourceType": "Patient", "id": "pat-1", "birthDate": "1970-01-01"}
    entries = [{"resource": communication(body)}, {"resource": sender}]
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The marked copy, where the engine and the command line look for agents."""
    agents = marked_agents(tmp_path / "agents")
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(agents))
    return agents


@pytest.fixture
def client(tmp_path: Path, served: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db_path=tmp_path / "a.db", agents_paths=[served])) as opened:
        yield opened


def decided(answer: httpx.Response) -> Any:
    assert answer.status_code == 200, answer.text
    return answer.json()


@pytest.mark.parametrize("outcome", list(MESSAGES))
def test_the_mark_is_in_what_the_agent_writes_and_handed_only_to_a_person(
    client: TestClient, outcome: str
) -> None:
    """The control: the plain decide route hands the text, mark and all, with the decision
    that waits, and not with the one that automates. So the mark is where a text would be."""
    body = {"message": {"body": MESSAGES[outcome]}}
    answer = decided(client.post(f"/api/agents/{DEMO}/decide", json=body))
    assert answer["outcome"] == outcome
    assert answer["written"] == {"acknowledgement": {"state": STANDS[outcome]}}
    assert (MARK in json.dumps(answer)) is (outcome == "HUMAN_REVIEW")
    assert (MARK in answer.get("outputs", {}).get("acknowledgement", "")) is (
        outcome == "HUMAN_REVIEW"
    )


@pytest.mark.parametrize("outcome", list(MESSAGES))
def test_no_fhir_route_card_or_operation_carries_a_text(client: TestClient, outcome: str) -> None:
    """The FHIR decide route (the decision and its Task), `$decide` and a CDS Hooks card, for
    the same message: none holds a word of what the agent wrote, whether its decision waits
    for a person or not. The FHIR decide route says where the text stands."""
    record = bundle(MESSAGES[outcome])
    routed = decided(client.post(f"/api/agents/{DEMO}/fhir/decide", json=record))
    assert routed["decision"]["outcome"] == outcome
    assert routed["decision"]["written"] == {"acknowledgement": {"state": STANDS[outcome]}}
    assert "outputs" not in routed["decision"] and routed["task"]["resourceType"] == "Task"
    assert MARK not in json.dumps(routed)

    asked = {
        "resourceType": "Parameters",
        "parameter": [{"name": "agent", "valueString": DEMO}, {"name": "data", "resource": record}],
    }
    operation = decided(client.post("/fhir/$decide", json=asked))
    assert operation["resourceType"] == "Bundle" and operation["entry"]
    assert MARK not in json.dumps(operation)

    hook = {
        "hook": "mendari-message-received",
        "hookInstance": "6b6a1c0e-6b0c-4a6e-9f3e-0d7c1c1e7a11",
        "context": {"userId": "Practitioner/1", "patientId": "pat-1", "communicationId": "msg-1"},
        "prefetch": {
            "communication": record["entry"][0]["resource"],
            "patient": record["entry"][1]["resource"],
        },
    }
    cards = decided(client.post(SERVICE, json=hook))
    assert len(cards["cards"]) == 1
    assert MARK not in json.dumps(cards)
    # Each of the three made a decision of its own: no record of them holds the text either.
    listed = decided(client.get("/api/audit", params={"agent_id": DEMO}))
    assert len(listed) == 3 and {item["outcome"] for item in listed} == {outcome}
    for item in listed:
        kept = decided(client.get(f"/api/audit/{item['id']}"))
        assert MARK not in json.dumps(kept["record"])
        # A waiting decision's text is kept for its reviewer, as the note of its own event and
        # nowhere else in the trail; an automated one's is kept nowhere.
        elsewhere = [e for e in kept["events"] if e["event"] != "output.held"]
        assert MARK not in json.dumps(elsewhere)
        if outcome == "AUTO":
            assert MARK not in json.dumps(kept) and "outputs" not in kept
        else:
            assert [MARK in text["text"] for text in kept["outputs"]] == [True]
        task = decided(client.get(f"/fhir/Task/{item['id']}"))
        assert MARK not in json.dumps(task)


@pytest.mark.parametrize("outcome", list(MESSAGES))
def test_the_command_line_decides_on_a_fhir_record_and_prints_no_text(
    tmp_path: Path, served: Path, capsys: pytest.CaptureFixture[str], outcome: str
) -> None:
    """`mendari decide --fhir` prints the decision and its Task, and no word of the text."""
    case = tmp_path / "case.json"
    case.write_text(json.dumps(bundle(MESSAGES[outcome])), encoding="utf-8")
    assert main(["decide", DEMO, "--fhir", "--input", str(case)]) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    assert shown["decision"]["outcome"] == outcome
    assert shown["decision"]["written"] == {"acknowledgement": {"state": STANDS[outcome]}}
    assert MARK not in printed


@pytest.mark.parametrize("outcome", list(MESSAGES))
def test_a_smart_launch_decides_and_its_answer_carries_no_text(
    tmp_path: Path, served: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """A decision made from inside an EHR launch (`POST /api/smart/decide`) is answered as the
    FHIR decide route answers: where the text stands, and no word of it."""
    ehr = FakeEhr()
    ehr.resources.append(communication(MESSAGES[outcome], rid="portal-1", patient=PATIENT))
    monkeypatch.setenv("MENDARI_SMART_CLIENT_ID", CLIENT)
    monkeypatch.setenv("MENDARI_PUBLIC_URL", APP)
    monkeypatch.setenv("MENDARI_FHIR_ALLOWED_SERVERS", EHR)
    transport = httpx.MockTransport(ehr)
    monkeypatch.setattr(
        smart_routes, "GuardedHttp", functools.partial(GuardedHttp, transport=transport)
    )
    monkeypatch.setattr(
        smart_routes, "FhirClient", functools.partial(FhirClient, transport=transport)
    )
    app = create_app(db_path=tmp_path / "smart.db", agents_paths=[served])
    with TestClient(app, base_url=APP, headers=DASHBOARD) as browser:
        started = launch(browser, agent=DEMO)  # the EHR opens the agent for its patient
        assert started.status_code == 302, started.text
        code = ehr.authorize(started.headers["location"])
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        back = browser.get(
            "/smart/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert back.status_code in (302, 303), back.text
        asked = {"agent_id": DEMO, "focus": "Communication/portal-1"}
        answer = decided(browser.post("/api/smart/decide", json=asked))
    assert answer["decision"]["outcome"] == outcome
    assert answer["decision"]["written"] == {"acknowledgement": {"state": STANDS[outcome]}}
    assert MARK not in json.dumps(answer)
