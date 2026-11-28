# SPDX-License-Identifier: Apache-2.0
"""Actions over HTTP (spec section 13.8): a decision's `actions` in `POST …/decide`, the
authorization route `POST /api/agents/{namespace}/{name}/actions`, and the review that releases
or denies (`POST /api/review/{id}` with `deny_actions`).

The route answers `200` whatever the disposition (a blocked action is not an HTTP error, as a
BLOCK decision is not), is idempotent, needs the permission and the scope that deciding needs,
and answers one `404` for a decision that is unknown, another organization's or another
agent's."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore
from mendari.core import actions
from mendari.core.agent import load_agent
from mendari.core.card import render_card
from mendari.server.app import create_app, declared_actions
from mendari.server.auth import permission_for
from mendari.server.limits import DailyCap, Limiter, limit_settings
from mendari.server.scopes import DECIDE, needed
from tests.conftest import (
    DEMO_AGENT_DIR,
    ROOT,
    TRIAGE_DIR,
    encrypt_mode,
    encrypted_store,
    message,
    signed_in,
)

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
AGENT = "mendari-test/actions-mini"
PLAIN = "mendari-test/triage"  # an agent that declares no actions
BASE = f"/api/agents/{AGENT}"
REFILL = "Could you refill my lisinopril?"
REPLY = "Please reply about my lisinopril."  # confident, with a free-text note and reply
VISIT = "I need to move my appointment."
ORDER = "Please renew my prescription."
UNCLEAR = "It is unclear what I want."
ABUSIVE = "This message is abusive."
ROUTE = ("POST", "/api/agents/{namespace}/{name}/actions")


def agents(tmp_path: Path) -> list[Path]:
    """A search path holding the actions fixture and the triage fixture, each a copy."""
    base = tmp_path / "agents"
    shutil.copytree(MINI, base / "actions-mini")
    shutil.copytree(TRIAGE_DIR, base / "triage")
    return [base]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Open mode (no sign-in), so canned answers automate as live ones do."""
    app = create_app(db_path=tmp_path / "open.db", agents_paths=agents(tmp_path))
    with TestClient(app) as opened:
        yield opened


def decide(client: TestClient, body: str, **options: Any) -> dict[str, Any]:
    answer = client.post(f"{BASE}/decide", json=message(body), **options)
    assert answer.status_code == 200, answer.text
    decided: dict[str, Any] = answer.json()
    return decided


def ask(
    client: TestClient, decision_id: str, name: str, arguments: Any = None, **options: Any
) -> dict[str, Any]:
    body = {"decision_id": decision_id, "name": name}
    if arguments is not None:
        body["arguments"] = arguments
    answer = client.post(f"{BASE}/actions", json=body, **options)
    assert answer.status_code == 200, answer.text
    asked: dict[str, Any] = answer.json()
    return asked


def events(client: TestClient, decision_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = client.get(f"/api/audit/{decision_id}").json()["events"]
    return found


# -- the first way in, over HTTP ------------------------------------------------------------------


def test_a_decision_lists_each_proposed_action_with_its_disposition(client: TestClient) -> None:
    decided = decide(client, VISIT)
    assert decided["outcome"] == "HUMAN_REVIEW"
    assert [
        (a["name"], a["effect"], a["disposition"], a["reason"]) for a in decided["actions"]
    ] == [
        ("look_up_medications", "read", "released", "with_decision"),
        ("file_note", "write", "held", "awaiting_review"),
        ("send_reply", "contact", "held", "awaiting_review"),
    ]
    for action in decided["actions"]:
        assert set(action) == {
            "id", "name", "effect", "disposition", "reason", "arguments", "arguments_hash",
        }  # fmt: skip
    assert decided["actions"][1]["arguments"] == {"note": "Asked to move a visit."}
    # An agent that declares no actions answers with no `actions` key at all.
    plain = client.post(f"/api/agents/{PLAIN}/decide", json=message("Crushing chest pain"))
    assert plain.status_code == 200 and "actions" not in plain.json()


def test_free_text_in_a_write_or_a_contact_sends_the_decision_to_a_person(
    client: TestClient,
) -> None:
    """ADR-0027, over HTTP: an answer confident enough to automate, with a free-text note and
    reply proposed. The decision goes to a person saying why, each action is held, and it is in
    the review queue."""
    decided = decide(client, REPLY)
    assert (decided["outcome"], decided["recommendation"]) == ("HUMAN_REVIEW", "refill")
    assert decided["reasons"] == [
        "confidence 0.95 >= auto threshold 0.90",
        "action 'file_note' can carry free text and is never released without a person; "
        "a person decides",
        "action 'send_reply' can carry free text and is never released without a person; "
        "a person decides",
    ]
    assert [(a["name"], a["disposition"], a["reason"]) for a in decided["actions"]] == [
        ("assign_queue", "held", "awaiting_review"),
        ("file_note", "held", "awaiting_review"),
        ("send_reply", "held", "awaiting_review"),
    ]
    queue = client.get("/api/review").json()
    (item,) = [entry for entry in queue if entry["id"] == decided["id"]]
    assert [a["disposition"] for a in item["actions"]] == ["held", "held", "held"]


def test_the_stored_decision_shows_its_actions_and_their_events(client: TestClient) -> None:
    decided = decide(client, ORDER)
    record = client.get(f"/api/audit/{decided['id']}").json()
    assert "actions" not in record["record"]
    assert [(a["name"], a["disposition"], a["arguments_status"]) for a in record["actions"]] == [
        ("look_up_medications", "released", "open"),
        ("renew_prescription", "held", "open"),
    ]
    assert [e["event"] for e in record["events"]] == [
        "decision.created",
        "action.proposed",
        "action.proposed",
    ]
    queue = client.get("/api/review").json()
    (item,) = [entry for entry in queue if entry["id"] == decided["id"]]
    assert [a["disposition"] for a in item["actions"]] == ["released", "held"]
    assert "events" not in item


def test_the_agents_detail_lists_its_actions_and_how_each_is_gated(client: TestClient) -> None:
    detail = client.get(BASE).json()
    # The gate is the tightened one (ADR-0027): a contact and a write whose argument is a free
    # string are a person's, though the manifest left `allow_auto` alone; the same effects with
    # enumerated arguments keep the gate `auto`.
    assert [(a["name"], a["effect"], a["gate"], a["allow_auto"]) for a in detail["actions"]] == [
        ("look_up_medications", "read", "decision", True),
        ("draft_reply", "draft", "decision", True),
        ("send_reply", "contact", "person", True),
        ("queue_refill", "write", "person", False),
        ("file_note", "write", "person", True),
        ("renew_prescription", "order", "person", True),
        ("send_template", "contact", "auto", True),
        ("assign_queue", "write", "auto", True),
    ]
    by_name = {a["name"]: a for a in detail["actions"]}
    assert by_name["look_up_medications"]["released"] == "With any decision that is not blocked."
    free_text = "Never without a person: its arguments can hold free text."
    assert by_name["send_reply"]["released"] == by_name["file_note"]["released"] == free_text
    assert by_name["send_template"]["released"].startswith("Only with an automated decision")
    assert by_name["assign_queue"]["released"].startswith("Only with an automated decision")
    confirmed = "Never without a person: when a reviewer confirms the decision."
    assert by_name["renew_prescription"]["released"] == confirmed  # an order: its own words
    assert by_name["queue_refill"]["released"] == confirmed  # tightened by the manifest: likewise
    assert by_name["queue_refill"]["arguments"] == ["medication", "days_supply"]
    assert by_name["renew_prescription"]["arguments"] is None  # no schema: not checked
    assert "## Actions" in detail["card_markdown"]
    assert "actions" in detail["output_schema"]["properties"]
    # An agent that declares none: its detail is what it was.
    plain = client.get(f"/api/agents/{PLAIN}").json()
    assert "actions" not in plain and "## Actions" not in plain["card_markdown"]
    assert "actions" not in plain["output_schema"]["properties"]


def test_an_unvouched_agents_free_text_reads_and_drafts_are_shown_as_a_persons() -> None:
    """What the detail lists for an agent the organization does not vouch for (one from a
    registry before its digest is approved): a read and a draft take the gate `auto`, and one
    whose arguments can hold free text is then a person's, saying why (ADR-0027). The contact
    and the write of enumerated arguments keep the gate `auto`."""
    agent = load_agent(MINI)
    unvouched = {a["name"]: a for a in declared_actions(agent, vouched=False)["actions"]}
    free_text = "Never without a person: its arguments can hold free text."
    for name in ("look_up_medications", "draft_reply", "send_reply", "file_note"):
        assert (unvouched[name]["gate"], unvouched[name]["released"]) == ("person", free_text), name
    for name in ("send_template", "assign_queue"):
        assert unvouched[name]["gate"] == "auto", name
        assert unvouched[name]["released"].startswith("Only with an automated decision")
    vouched = {a["name"]: a for a in declared_actions(agent, vouched=True)["actions"]}
    assert vouched["look_up_medications"]["gate"] == vouched["draft_reply"]["gate"] == "decision"
    # Nothing in either is looser than the other way round: vouching only ever loosens to the
    # effect's own gate, never past the free text.
    order = ["decision", "auto", "person"]
    for name, shown in vouched.items():
        assert order.index(unvouched[name]["gate"]) >= order.index(shown["gate"]), name


# -- the second way in: each row of the table -----------------------------------------------------


def test_a_blocked_action_is_a_200_and_every_disposition_is_answered(client: TestClient) -> None:
    automated = decide(client, REFILL)["id"]
    pending = decide(client, VISIT)["id"]
    abstained = decide(client, UNCLEAR)["id"]
    blocked = decide(client, ABUSIVE)["id"]
    table = [
        (automated, "look_up_medications", {}, "released", "with_decision"),
        (automated, "assign_queue", {"queue": "refill"}, "released", "automated"),
        (automated, "file_note", {"note": "asked"}, "blocked", "no_reviewer"),  # free text
        (automated, "send_reply", {"message": "asked"}, "blocked", "no_reviewer"),  # free text
        (automated, "renew_prescription", {}, "blocked", "no_reviewer"),
        (automated, "queue_refill", {"medication": "x"}, "blocked", "no_reviewer"),
        (pending, "draft_reply", {"message": "asked"}, "released", "with_decision"),
        (pending, "queue_refill", {"medication": "x"}, "held", "awaiting_review"),
        (pending, "renew_prescription", {}, "held", "awaiting_review"),
        (abstained, "draft_reply", {"message": "asked"}, "released", "with_decision"),
        (abstained, "send_reply", {"message": "asked"}, "blocked", "no_recommendation"),
        (abstained, "queue_refill", {"medication": "x"}, "blocked", "no_recommendation"),
        (blocked, "look_up_medications", {"why": "asked"}, "blocked", "decision_blocked"),
        (blocked, "send_reply", {"message": "asked"}, "blocked", "decision_blocked"),
    ]
    for decision_id, name, arguments, disposition, reason in table:
        found = ask(client, decision_id, name, arguments)
        assert (found["disposition"], found["reason"]) == (disposition, reason), (name, reason)
        assert found["decision_id"] == decision_id and found["name"] == name
        assert set(found) == {
            "id", "decision_id", "name", "effect", "disposition", "reason", "arguments_hash",
            "repeated", "notice",
        }  # fmt: skip
        assert found["repeated"] is False and "arguments" not in found
    assert client.get("/api/audit/verify").json()["ok"]


def test_review_releases_what_was_held_and_the_caller_learns_by_asking_again(
    client: TestClient,
) -> None:
    decided = decide(client, VISIT)
    note, reply = decided["actions"][1], decided["actions"][2]
    later = ask(client, decided["id"], "renew_prescription", {})
    assert later["disposition"] == "held"
    resolved = client.post(
        f"/api/review/{decided['id']}",
        json={"final_label": "appointment", "reviewer": "rn-1", "deny_actions": [reply["id"]]},
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["human_override"] is False
    assert [(a["id"], a["disposition"], a["reason"]) for a in body["actions"]] == [
        (note["id"], "released", "confirmed"),
        (reply["id"], "denied", "reviewer_denied"),
        (later["id"], "released", "confirmed"),
    ]
    released = ask(client, decided["id"], "file_note", note["arguments"])
    assert (released["id"], released["disposition"], released["repeated"]) == (
        note["id"],
        "released",
        True,
    )
    denied = ask(client, decided["id"], "send_reply", reply["arguments"])
    assert (denied["disposition"], denied["reason"]) == ("denied", "reviewer_denied")
    after = ask(client, decided["id"], "file_note", {"note": "after the review"})
    assert (after["disposition"], after["reason"]) == ("blocked", "review_closed")
    kinds = [e["event"] for e in events(client, decided["id"])]
    assert kinds.count("action.resolved") == 3 and kinds.count("review.resolved") == 1
    assert client.get("/api/audit/verify").json()["ok"]


def test_an_override_denies_and_a_wrong_id_is_a_409_that_chains_nothing(
    client: TestClient,
) -> None:
    decided = decide(client, VISIT)
    before = events(client, decided["id"])
    refused = client.post(
        f"/api/review/{decided['id']}",
        json={"final_label": "appointment", "reviewer": "rn-1", "deny_actions": ["nope"]},
    )
    assert refused.status_code == 409 and "does not hold" in refused.json()["detail"]
    assert events(client, decided["id"]) == before
    for malformed in ("nope", [""], ["x" * 101], [1]):
        body = {"final_label": "appointment", "reviewer": "rn-1", "deny_actions": malformed}
        assert client.post(f"/api/review/{decided['id']}", json=body).status_code == 422
    overridden = client.post(
        f"/api/review/{decided['id']}", json={"final_label": "refill", "reviewer": "rn-1"}
    ).json()
    assert overridden["human_override"] is True
    assert {(a["disposition"], a["reason"]) for a in overridden["actions"]} == {
        ("denied", "overridden")
    }
    again = client.post(
        f"/api/review/{decided['id']}", json={"final_label": "refill", "reviewer": "rn-1"}
    )
    assert again.status_code == 409
    # A review of a decision with nothing held answers as it always did.
    plain = decide(client, UNCLEAR)
    answer = client.post(
        f"/api/review/{plain['id']}", json={"final_label": "other", "reviewer": "rn-1"}
    ).json()
    assert "actions" not in answer


def test_asking_twice_chains_one_event_and_answers_repeated(client: TestClient) -> None:
    decided = decide(client, REFILL)
    before = len(events(client, decided["id"]))
    first = ask(client, decided["id"], "file_note", {"note": "once"})
    second = ask(client, decided["id"], "file_note", {"note": "once"})
    assert (first["repeated"], second["repeated"]) == (False, True)
    assert {k: v for k, v in second.items() if k != "repeated"} == {
        k: v for k, v in first.items() if k != "repeated"
    }
    assert len(events(client, decided["id"])) == before + 1
    # What the agent proposed with its answer is answered the same way: nothing new.
    proposed = decided["actions"][1]
    same = ask(client, decided["id"], proposed["name"], proposed["arguments"])
    assert (same["id"], same["repeated"]) == (proposed["id"], True)
    assert len(events(client, decided["id"])) == before + 1


def test_an_undeclared_or_malformed_action_is_blocked_and_never_named_in_the_trail(
    client: TestClient,
) -> None:
    decided = decide(client, REFILL)
    unknown = ask(client, decided["id"], "wipe_the_chart_sentinel", {"why": "SENTINEL-ASKED"})
    assert (unknown["disposition"], unknown["reason"]) == ("blocked", "undeclared")
    assert unknown["name"] is None and unknown["effect"] is None
    wrong = ask(client, decided["id"], "send_reply", {"message": 5})
    assert (wrong["disposition"], wrong["reason"]) == ("blocked", "invalid_arguments")
    after = ask(client, decided["id"], "assign_queue", {"queue": "refill"})
    assert (after["disposition"], after["reason"]) == ("blocked", "earlier_refusal")
    listed = client.get("/api/audit", params={"limit": 50}).text
    exported = client.get("/api/audit/export/fhir").text
    payloads = json.dumps([event["payload"] for event in events(client, decided["id"])])
    for said in (payloads, listed, exported):
        assert "wipe_the_chart_sentinel" not in said and "SENTINEL-ASKED" not in said
    # Whoever may read the decision reads the kept name and arguments, apart from the chain
    # (open mode keeps them as inputs are kept: in the clear, under no PHI mode).
    record = client.get(f"/api/audit/{decided['id']}").json()
    blocked = next(a for a in record["actions"] if a["reason"] == "undeclared")
    assert blocked["proposed_name"] == "wipe_the_chart_sentinel"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "send_reply"},  # no decision
        {"decision_id": "d", "name": "Send Reply"},
        {"decision_id": "d", "name": "send-reply"},
        {"decision_id": "d", "name": "a" * 65},
        {"decision_id": "d"},
        {"decision_id": "", "name": "send_reply"},
        {"decision_id": "d" * 101, "name": "send_reply"},
        {"decision_id": "d", "name": "send_reply", "arguments": []},
        {"decision_id": "d", "name": "send_reply", "arguments": "x"},
        {"decision_id": "d", "name": "send_reply", "arguments": {"a": "x" * 16_000}},
        {"decision_id": "d", "name": "send_reply", "arguments": {"a": [[[[[[[["deep"]]]]]]]]}},
    ],
)
def test_a_question_that_is_not_well_formed_is_a_422(client: TestClient, body: Any) -> None:
    refused = client.post(f"{BASE}/actions", json=body)
    assert refused.status_code == 422, refused.text
    assert (
        "x" * 100 not in refused.text or len(refused.text) < 2000
    )  # a value is never echoed whole


def test_an_unknown_decision_another_agents_and_an_unknown_agent_are_404s(
    client: TestClient,
) -> None:
    decided = decide(client, REFILL)
    plain = client.post(f"/api/agents/{PLAIN}/decide", json=message("Crushing chest pain")).json()
    body = {"name": "look_up_medications", "arguments": {}}
    unknown = client.post(f"{BASE}/actions", json={"decision_id": "no-such", **body})
    other = client.post(f"{BASE}/actions", json={"decision_id": plain["id"], **body})
    assert unknown.status_code == other.status_code == 404
    assert unknown.json()["detail"] == f"no decision no-such of {AGENT}"
    assert other.json()["detail"] == f"no decision {plain['id']} of {AGENT}"
    # This agent's decision asked of another agent, and an agent nothing serves.
    crossed = client.post(
        f"/api/agents/{PLAIN}/actions", json={"decision_id": decided["id"], **body}
    )
    assert crossed.status_code == 404
    nobody = client.post(
        "/api/agents/nobody/nothing/actions", json={"decision_id": decided["id"], **body}
    )
    assert nobody.status_code == 404
    assert [e["event"] for e in events(client, decided["id"])].count("action.proposed") == 2


def test_an_agent_that_declares_no_actions_authorizes_nothing_over_http(
    client: TestClient,
) -> None:
    """An agent that declares no actions (the triage fixture):
    whatever is asked of it is blocked, as undeclared."""
    plain = client.post(f"/api/agents/{PLAIN}/decide", json=message("Crushing chest pain")).json()
    asked = client.post(
        f"/api/agents/{PLAIN}/actions",
        json={"decision_id": plain["id"], "name": "send_reply", "arguments": {}},
    )
    assert asked.status_code == 200
    assert (asked.json()["disposition"], asked.json()["reason"]) == ("blocked", "undeclared")


def test_a_decision_of_an_earlier_digest_is_agent_changed(tmp_path: Path) -> None:
    paths = agents(tmp_path)
    db = tmp_path / "versions.db"
    with TestClient(create_app(db_path=db, agents_paths=paths)) as first:
        decided = decide(first, REFILL)
    manifest = paths[0] / "actions-mini" / "agent.yaml"
    manifest.write_text(manifest.read_text().replace("version: 0.1.0", "version: 0.1.1"))
    with TestClient(create_app(db_path=db, agents_paths=paths)) as second:
        asked = ask(second, decided["id"], "look_up_medications", {})
        assert (asked["disposition"], asked["reason"]) == ("blocked", "agent_changed")
        assert asked["name"] is None and asked["effect"] is None


def test_a_hold_set_after_an_automated_decision_is_not_automated(client: TestClient) -> None:
    decided = decide(client, REFILL)
    before = ask(client, decided["id"], "assign_queue", {"queue": "refill"})
    assert before["reason"] == "automated"
    held = client.post("/api/holds", json={"agent_id": AGENT, "reason": "a drill"})
    assert held.status_code in (200, 201), held.text
    after = ask(client, decided["id"], "assign_queue", {"queue": "scheduling"})
    assert (after["disposition"], after["reason"]) == ("blocked", "not_automated")
    assert ask(client, decided["id"], "look_up_medications", {})["disposition"] == "released"
    # And a decision made under the hold goes to a person: its write is held.
    under = decide(client, REFILL)
    assert under["outcome"] == "HUMAN_REVIEW"
    assert under["actions"][1]["disposition"] == "held"


# -- who may ask ----------------------------------------------------------------------------------


def test_the_route_needs_what_deciding_needs() -> None:
    path = f"/api/agents/{AGENT}/actions"
    assert permission_for("POST", path) == "decide"
    assert needed("POST", path) == DECIDE
    assert permission_for("POST", f"/api/agents/{AGENT}/decide") == "decide"
    for method in ("GET", "PUT", "DELETE"):
        assert permission_for(method, path) != "public"
    # The limit class of deciding, and never counted against the daily cap of decisions: it
    # makes none.
    with AuditStore(":memory:") as store:
        limiter = Limiter(store, limit_settings(sign_in=True))
        assert limiter.classify("POST", path, "decide") == "decide"
    assert not DailyCap.counts("POST", path)
    assert DailyCap.counts("POST", f"/api/agents/{AGENT}/decide")


def test_keys_and_people_reach_it_by_role_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    db = tmp_path / "signed.db"
    app = create_app(db_path=db, agents_paths=agents(tmp_path))
    with TestClient(app, base_url="https://mendari.test") as client, encrypted_store(db) as store:

        def key(role: str, scopes: str | None = None) -> dict[str, str]:
            made = store.create_api_key(
                name=f"{role} {scopes}", role=role, org="default", created_by="ops", scopes=scopes
            )
            return {"authorization": f"Bearer {made['key']}"}

        service = key("service")
        decided = decide(client, VISIT, headers=service)
        body = {"decision_id": decided["id"], "name": "renew_prescription", "arguments": {}}
        allowed = [
            service,
            key("service", DECIDE),
            signed_in(store, role="reviewer"),
            signed_in(store, role="admin"),
        ]
        for headers in allowed:
            answer = client.post(f"{BASE}/actions", json=body, headers=headers)
            assert answer.status_code == 200, answer.text
            assert answer.json()["disposition"] == "held"
        refused = [
            key("viewer"),
            key("service", "system/Task.rs"),  # a scope that reads Tasks reaches no decision
            signed_in(store, role="viewer"),
            signed_in(store, role="governance"),
        ]
        for headers in refused:
            assert client.post(f"{BASE}/actions", json=body, headers=headers).status_code == 403
        assert client.post(f"{BASE}/actions", json=body).status_code == 401
        # Asked four times by four principals: one event, naming who asked first.
        trail = client.get(f"/api/audit/{decided['id']}", headers=service).json()["events"]
        asked = [e for e in trail if e["payload"].get("source") == "request"]
        assert len(asked) == 1 and asked[0]["payload"]["requested_by"].startswith("key ")
        # Another organization's key: the decision is unknown to it.
        made = store.create_api_key(name="b", role="service", org="clinic-b", created_by="ops")
        theirs = client.post(
            f"{BASE}/actions", json=body, headers={"authorization": f"Bearer {made['key']}"}
        )
        assert theirs.status_code == 404
        # Sealed arguments: a service key reads none; a reviewer does, and the read is chained.
        sealed = client.get(f"/api/audit/{decided['id']}", headers=service).json()["actions"]
        assert {a["arguments_status"] for a in sealed} == {"sealed"}
        reviewer = signed_in(store, role="reviewer")
        opened = client.get(f"/api/audit/{decided['id']}", headers=reviewer).json()
        assert opened["actions"][1]["arguments"] == {"note": "Asked to move a visit."}
        assert opened["events"][-1]["event"] == "input.read"
        # A reviewer confirms; under PHI mode encrypt the trail still verifies with its notes.
        confirmed = client.post(
            f"/api/review/{decided['id']}",
            json={"final_label": "appointment", "reviewer": "ignored"},
            headers=reviewer,
        )
        assert confirmed.status_code == 200
        assert {a["disposition"] for a in confirmed.json()["actions"]} == {"released"}
        assert store.verify(notes=True)["ok"]


def test_a_body_over_the_limit_is_refused_before_it_is_read(client: TestClient) -> None:
    decided = decide(client, REFILL)
    huge = {
        "decision_id": decided["id"],
        "name": "file_note",
        "arguments": {"note": "x" * 3_000_000},
    }
    refused = client.post(f"{BASE}/actions", json=huge)
    assert refused.status_code in (413, 422)
    assert len(events(client, decided["id"])) == 3


# -- the API description and the Decision Card ----------------------------------------------------


def test_the_api_description_describes_the_route_and_the_reviews_new_field(
    client: TestClient,
) -> None:
    described = client.get("/openapi.json").json()
    operation = described["paths"]["/api/agents/{namespace}/{name}/actions"]["post"]
    assert operation["summary"] == "Authorize an action" and operation["tags"] == ["Decisions"]
    assert "runs nothing" in operation["description"]
    request = described["components"]["schemas"]["ActionRequest"]
    assert request["required"] == ["decision_id", "name"]
    assert request["properties"]["name"]["pattern"] == actions.NAME_PATTERN
    review = described["components"]["schemas"]["ReviewRequest"]["properties"]
    assert "deny_actions" in review and review["deny_actions"]["default"] == []


def test_the_decision_card_lists_the_declared_actions_and_how_each_is_gated() -> None:
    card = render_card(load_agent(MINI))
    section = card.split("## Actions")[1].split("## Providers")[0]
    assert "Mendari authorizes each action and runs none" in section
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    assert [row.split("|")[1].strip() for row in rows] == [
        f"`{name}`" for name in load_agent(MINI).manifest.actions
    ]
    by_name = {row.split("|")[1].strip().strip("`"): row for row in rows}
    assert (
        "| `read` | With any decision that is not blocked. | not checked |"
        in by_name["look_up_medications"]
    )
    # A contact and a write whose argument is a free string: the tightened gate, saying why
    # (ADR-0027). The same effects with enumerated arguments are released with AUTO.
    free_text = "Never without a person: its arguments can hold free text."
    assert f"| `contact` | {free_text} | checked: `message` |" in by_name["send_reply"]
    assert f"| `write` | {free_text} | checked: `note` |" in by_name["file_note"]
    assert "`contact` | Only with an automated decision" in by_name["send_template"]
    assert "checked: `template`" in by_name["send_template"]
    assert "`write` | Only with an automated decision" in by_name["assign_queue"]
    # A draft can hold free text and is released with the decision: a person sees it first.
    assert "`draft` | With any decision that is not blocked." in by_name["draft_reply"]
    assert (
        "`write` | Never without a person: when a reviewer" in by_name["queue_refill"]
    )  # tightened
    assert "checked: `medication`, `days_supply`" in by_name["queue_refill"]
    assert "`order` | Never without a person" in by_name["renew_prescription"]
    # The bundled agent's card lists its two; an agent that declares none has no such section.
    demo = render_card(load_agent(DEMO_AGENT_DIR))
    assert "| `assign_queue` |" in demo and "| `send_acknowledgement` |" in demo
    assert "`contact` | Never without a person" in demo
    assert "## Actions" not in render_card(load_agent(TRIAGE_DIR))
