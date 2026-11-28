# SPDX-License-Identifier: Apache-2.0
"""Held texts over HTTP (ADR-0027, points 4 and 5): a decision's `outputs` in the review queue
(`GET /api/review`) and in its record (`GET /api/audit/{id}`), and the review that gives each
text a verdict and releases free text by name (`POST /api/review/{id}` with `texts` and
`release_actions`).

A held text is shown by the permission that shows the input, and each such read is chained and
logged as the input's is. A review that is not well formed is refused without repeating what it
was sent: a caller may send a text where a verdict belongs. Every message and text here is
synthetic."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.server.app import create_app
from tests.conftest import (
    TRIAGE_DIR,
    encrypt_mode,
    encrypted_store,
    message,
    security_lines,
    signed_in,
)
from tests.test_held_texts import (
    ARGUMENT,
    HELD_MARKS,
    LETTER,
    PLAIN,
    RELEASE,
    REPLY,
    SENT_AS_NAME,
    SENT_AS_VERDICT,
    holding,
    of,
    resolved,
    writing,
)

AGENT = "mendari-test/actions-mini"  # the copy that writes keeps the fixture's id
IDP = "https://idp.example.org"


def served(tmp_path: Path) -> list[Path]:
    """A search path holding the copy that writes, and the triage fixture."""
    base = tmp_path / "agents"
    writing(base)
    shutil.copytree(TRIAGE_DIR, base / "triage")
    return [base]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Open mode (no sign-in) under no PHI mode, the agent's automation on hold: what would be
    automated waits for a person."""
    app = create_app(db_path=tmp_path / "open.db", agents_paths=served(tmp_path))
    with TestClient(app) as opened:
        hold = opened.post("/api/holds", json={"agent_id": AGENT, "reason": "a drill"})
        assert hold.status_code == 201, hold.text
        yield opened


def decide(client: TestClient, body: str, **options: Any) -> dict[str, Any]:
    answer = client.post(f"/api/agents/{AGENT}/decide", json=message(body), **options)
    assert answer.status_code == 200, answer.text
    decided: dict[str, Any] = answer.json()
    return decided


def test_the_queue_and_the_record_show_a_held_text_and_the_review_takes_its_verdict(
    client: TestClient,
) -> None:
    """`GET /api/review` and `GET /api/audit/{id}` list a decision's held texts under
    `outputs`, each with its name, where it stands and its text; `POST /api/review/{id}` takes
    a verdict on each (`texts`) and the held actions to release by name (`release_actions`),
    and answers with both."""
    decided = decide(client, LETTER)
    assert decided["outcome"] == "HUMAN_REVIEW"
    written = decided["outputs"]  # the caller's own answer: what the agent wrote, by name
    record = client.get(f"/api/audit/{decided['id']}").json()
    assert "outputs" not in record["record"]
    assert record["outputs"] == [
        {"name": name, "state": "held", "text_status": "open", "text": written[name]}
        for name in ("chart_note", "reply_draft")
    ]
    (item,) = [entry for entry in client.get("/api/review").json() if entry["id"] == decided["id"]]
    assert item["outputs"] == record["outputs"] and "events" not in item
    queue, reply = (a for a in record["actions"] if a["disposition"] == "held")
    assert "free_text" not in queue and reply["free_text"] is True
    review = {"final_label": "refill", "reviewer": "rn-1"}
    refused = client.post(f"/api/review/{decided['id']}", json=review)
    assert refused.status_code == 409
    assert "'chart_note', 'reply_draft'" in refused.json()["detail"]
    answer = client.post(
        f"/api/review/{decided['id']}",
        json={
            **review,
            "texts": {"chart_note": "release", "reply_draft": "withhold"},
            "release_actions": [queue["id"]],
        },
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["outputs"] == [
        {"name": "chart_note", "verdict": "released"},
        {"name": "reply_draft", "verdict": "withheld"},
    ]
    assert resolved(body) == [
        ("assign_queue", "released", "confirmed"),
        ("send_reply", "denied", "not_named"),
    ]
    after = client.get(f"/api/audit/{decided['id']}").json()
    assert [(o["name"], o["state"]) for o in after["outputs"]] == [
        ("chart_note", "released"),
        ("reply_draft", "withheld"),
    ]
    kinds = [e["event"] for e in after["events"]]
    assert kinds.count("output.held") == 2 and kinds.count("output.resolved") == 2
    assert client.get("/api/audit/verify").json()["ok"]
    # A decision with no held text: no `outputs` in the queue, the record or the review.
    plain = decide(client, PLAIN)
    assert "outputs" not in client.get(f"/api/audit/{plain['id']}").json()
    assert all("outputs" not in entry for entry in client.get("/api/review").json())
    done = client.post(f"/api/review/{plain['id']}", json={**review, "final_label": "other"})
    assert done.status_code == 200 and "outputs" not in done.json()


def test_an_action_asked_for_after_the_queue_was_read_is_not_released_by_the_label_alone(
    client: TestClient,
) -> None:
    """A reviewer reads the queue; a caller then asks for one more action on that decision
    (`POST …/actions`), which is held with it. The reviewer confirms the label as the queue
    showed the decision: the action they were never shown is denied (`not_named`), and the
    queue says how each action was proposed (`source`), so whoever shows a review knows which
    to name."""
    decided = decide(client, PLAIN)  # no text and no action: a label to confirm
    assert decided["outcome"] == "HUMAN_REVIEW" and decided["recommendation"] == "other"
    (seen,) = [entry for entry in client.get("/api/review").json() if entry["id"] == decided["id"]]
    assert "actions" not in seen
    asked = client.post(
        f"/api/agents/{AGENT}/actions",
        json={
            "decision_id": decided["id"],
            "name": "assign_queue",
            "arguments": {"queue": "front_desk"},
        },
    )
    assert asked.status_code == 200, asked.text
    assert (asked.json()["disposition"], asked.json()["reason"]) == ("held", "awaiting_review")
    (now,) = [entry for entry in client.get("/api/review").json() if entry["id"] == decided["id"]]
    (later,) = now["actions"]
    assert (later["source"], later["disposition"]) == ("request", "held")
    assert "free_text" not in later
    answer = client.post(
        f"/api/review/{decided['id']}", json={"final_label": "other", "reviewer": "rn-1"}
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["human_override"] is False
    assert resolved(answer.json()) == [("assign_queue", "denied", "not_named")]
    assert client.get("/api/audit/verify").json()["ok"]


def test_a_review_over_http_that_is_not_well_formed_echoes_nothing_it_was_sent(
    client: TestClient,
) -> None:
    """A verdict that is not `release` or `withhold`, a name that is none an output can have
    and an id that is no held action are refused without repeating them: a caller may send a
    text where a verdict belongs. Nothing is chained, and the decision still waits."""
    decided = decide(client, LETTER)
    url = f"/api/review/{decided['id']}"
    review = {"final_label": "refill", "reviewer": "rn-1"}
    before = client.get(f"/api/audit/{decided['id']}").json()["events"]
    malformed: list[Any] = [
        {"chart_note": "release", "reply_draft": SENT_AS_VERDICT},
        {"chart_note": "release", SENT_AS_NAME: "withhold"},
        {"chart_note": "release", "reply_draft": 5},
        {"chart_note": "release", "reply_draft": {"verdict": SENT_AS_VERDICT}},
        [SENT_AS_NAME],
        SENT_AS_VERDICT,
        {f"output_{n}": "release" for n in range(9)},  # more than an agent declares
    ]
    for texts in malformed:
        refused = client.post(url, json={**review, "texts": texts})
        assert refused.status_code == 422, refused.text
        assert SENT_AS_NAME not in refused.text and SENT_AS_VERDICT not in refused.text
    for named in ("nope", [""], ["x" * 101], [1]):
        body = {**review, "texts": RELEASE, "release_actions": named}
        assert client.post(url, json=body).status_code == 422
    # Well formed, and not this decision's: refused by the store, as plainly.
    unknown = client.post(url, json={**review, "texts": {**RELEASE, "summary": "release"}})
    assert unknown.status_code == 409 and "'chart_note', 'reply_draft'" in unknown.json()["detail"]
    wrong = client.post(url, json={**review, "texts": RELEASE, "release_actions": ["nope"]})
    assert wrong.status_code == 409 and "does not hold" in wrong.json()["detail"]
    assert client.get(f"/api/audit/{decided['id']}").json()["events"] == before
    assert client.post(url, json={**review, "texts": RELEASE}).status_code == 200


def test_a_reviewer_reads_a_sealed_held_text_and_a_key_or_a_viewer_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With sign-in on (PHI mode `encrypt`), a held text is shown by the same permission as the
    input: to a reviewer, in the queue and in the record, each read chained (`input.read`) and
    logged; to a service key or a viewer never, and their reading chains nothing. No log line
    holds a text, printed or kept as a record."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", IDP)
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    db = tmp_path / "signed.db"
    app = create_app(db_path=db, agents_paths=served(tmp_path))
    with TestClient(app, base_url="https://mendari.test") as client, encrypted_store(db) as store:
        made = store.create_api_key(name="svc", role="service", org="default", created_by="ops")
        service = {"authorization": f"Bearer {made['key']}"}
        # Canned answers never automate with sign-in on: the decision waits for a person.
        decided = decide(client, LETTER, headers=service)
        assert decided["outcome"] == "HUMAN_REVIEW" and REPLY in decided["outputs"]["reply_draft"]
        url = f"/api/audit/{decided['id']}"
        sealed = [
            {"name": "chart_note", "state": "held", "text_status": "sealed"},
            {"name": "reply_draft", "state": "held", "text_status": "sealed"},
        ]
        viewer = signed_in(store, role="viewer")
        for headers in (service, viewer):
            shown_to = client.get(url, headers=headers)
            assert shown_to.status_code == 200 and shown_to.json()["outputs"] == sealed
            for mark in (*HELD_MARKS, ARGUMENT):
                assert mark not in shown_to.text, mark
        assert sealed == next(
            entry["outputs"]
            for entry in client.get("/api/review", headers=viewer).json()
            if entry["id"] == decided["id"]
        )
        assert of(store, decided["id"], "input.read") == []
        reviewer = signed_in(store, role="reviewer")
        opened = client.get(url, headers=reviewer).json()
        assert [o["text"] for o in opened["outputs"]] == [
            decided["outputs"]["chart_note"],
            decided["outputs"]["reply_draft"],
        ]
        assert opened["events"][-1]["event"] == "input.read"
        queued = next(
            entry
            for entry in client.get("/api/review", headers=reviewer).json()
            if entry["id"] == decided["id"]
        )
        assert queued["outputs"] == opened["outputs"]
        reads = of(store, decided["id"], "input.read")
        assert len(reads) == 2 and {r["payload"]["reader"] for r in reads} == {
            "reviewer@default.example.org"
        }
        confirmed = client.post(
            f"/api/review/{decided['id']}",
            json={"final_label": "refill", "reviewer": "ignored", "texts": RELEASE},
            headers=reviewer,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert [o["verdict"] for o in confirmed.json()["outputs"]] == ["released", "released"]
        resolved_events = of(store, decided["id"], "output.resolved")
        assert {e["payload"]["reviewer"] for e in resolved_events} == {
            "reviewer@default.example.org"
        }
        assert store.verify(notes=True)["ok"]
        for mark in (*HELD_MARKS, ARGUMENT):  # sealed: no table holds a text
            assert holding(store, mark) == [], mark
    logged = capsys.readouterr()
    read = [line for line in security_lines(logged.out) if line["event"] == "phi.inputs.read"]
    assert len(read) == 2 and all(line["ids"] == [decided["id"]] for line in read)
    records = caplog.text + " ".join(repr(record.args) for record in caplog.records)
    for mark in (*HELD_MARKS, ARGUMENT):
        assert mark not in logged.out + logged.err + records, mark


def test_the_api_description_says_what_a_review_now_takes(client: TestClient) -> None:
    """`texts` and `release_actions` are described in plain words, with the two verdicts."""
    described = client.get("/openapi.json").json()
    review = described["components"]["schemas"]["ReviewRequest"]["properties"]
    assert review["texts"]["default"] == {} and review["release_actions"]["default"] == []
    verdicts = review["texts"]["additionalProperties"]
    assert verdicts["enum"] == ["release", "withhold"]
    assert "verdict" in review["texts"]["description"]
    assert "free text" in review["release_actions"]["description"]
    operation = described["paths"]["/api/review/{decision_id}"]["post"]
    assert "texts" in operation["description"] and "release_actions" in operation["description"]
