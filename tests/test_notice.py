# SPDX-License-Identifier: Apache-2.0
"""The notice (mendari.notices): one text on every surface Mendari serves, and an acknowledgement
a signed-in person gives before deciding, chained once per notice version."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import Decision, Pack, notices
from mendari.core.audit import AuditStore
from mendari.core.card import render_card
from mendari.core.registry import discover
from mendari.server.app import create_app
from mendari.server.smart_routes import error_page
from mendari.standards import cds_hooks
from tests.conftest import ROOT, message, signed_in
from tests.test_fhir import COMMUNICATION, PATIENT

INBOX = "mendari/patient-inbox-router"
REFILL = "Hi, I need a refill on my lisinopril. Thanks"
REFERENCE = sorted((p for p in discover().packs if p.origin == "maintained"), key=lambda p: p.id)


@pytest.fixture
def open_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "n.db"))


def test_every_page_and_the_openapi_description_carry_the_notice(open_client: TestClient) -> None:
    stamped = f'<p class="preview-notice" role="note">{notices.NOTICE}</p>'
    for page in ("/", "/docs", "/docs/reference"):
        assert stamped in open_client.get(page).text, page
    # The page a failed EHR launch or sign-in lands on.
    assert notices.NOTICE in bytes(error_page("the launch failed", 400).body).decode()
    assert notices.NOTICE in open_client.get("/openapi.json").json()["info"]["description"]


def test_the_hub_says_the_same_words() -> None:
    assert notices.NOTICE in (ROOT / "hub" / "app" / "layout.tsx").read_text()


@pytest.mark.parametrize("pack", REFERENCE, ids=lambda p: p.id)
def test_every_decision_card_carries_it(pack: Pack) -> None:
    for card in (render_card(pack), (pack.root / "DECISION_CARD.md").read_text()):
        assert card.splitlines()[4] == f"*{notices.NOTICE}*"


def test_cds_hooks_carry_it(router: Decision) -> None:
    service = cds_hooks.discovery([router.pack])["services"][0]
    assert notices.NOTICE in service["usageRequirements"]
    response = cds_hooks.invoke(
        router,
        {
            "hook": "mendari-message-received",
            "hookInstance": "d1577c69-dfbe-44ad-ba6d-3e05e953b2ea",
            "context": {"communicationId": "msg-1", "patientId": "pat-1"},
            "prefetch": {"communication": COMMUNICATION, "patient": PATIENT},
        },
    )
    assert notices.NOTICE in response["cards"][0]["detail"].splitlines()[0]


def test_every_decide_answer_carries_it(open_client: TestClient) -> None:
    decided = open_client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL)).json()
    assert decided["notice"] == notices.notice() and decided["outcome"]
    draft = open_client.get("/api/packs/mendari/ai-guard/examples").json()[0]["input"]
    checked = open_client.post("/api/packs/mendari/ai-guard/guard", json=draft).json()
    assert checked["notice"] == notices.notice() and checked["status"]


def test_the_notice_says_what_the_plan_requires() -> None:
    """The launch plan's notice (A4.2): its three clauses, in the one place it is written.
    Changing them is the owner's decision, with a new VERSION."""
    for clause in ("Research preview.", "Not a medical device.", "patients should call 911."):
        assert clause in notices.NOTICE


def test_the_acknowledgement_is_required_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MENDARI_NOTICE_ACKNOWLEDGEMENT")
    assert notices.mode() == "required"


@pytest.fixture
def signed_in_app(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, AuditStore]]:
    monkeypatch.delenv("MENDARI_NOTICE_ACKNOWLEDGEMENT")  # the default: required
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    with TestClient(create_app(db_path=db_target), base_url="https://mendari.test") as client:
        yield client, AuditStore(db_target)


def test_a_signed_in_person_acknowledges_before_deciding(
    signed_in_app: tuple[TestClient, AuditStore],
) -> None:
    client, store = signed_in_app
    reviewer = signed_in(store, role="reviewer")
    asked = client.get("/api/notice", headers=reviewer).json()
    assert asked["required"] is True and asked["acknowledged"] is False
    assert asked["text"] == notices.NOTICE and asked["acknowledgement"] == notices.ACKNOWLEDGEMENT
    refused = client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL), headers=reviewer)
    assert refused.status_code == 403 and refused.json()["error"] == "NoticeNotAcknowledged"
    assert refused.headers["Mendari-Notice-Required"] == notices.VERSION
    assert "WWW-Authenticate" not in refused.headers  # not a sign-in challenge
    # Every route that takes data in from a person: deciding, reviewing, feedback.
    for path in (
        f"/api/packs/{INBOX}/fhir/decide",
        f"/api/packs/{INBOX}/guard",
        "/api/review/any",
        "/api/audit/any/feedback",
    ):
        assert client.post(path, json={}, headers=reviewer).status_code == 403, path

    for _ in range(2):  # chained once, however often it is sent
        done = client.post("/api/notice/acknowledge", headers=reviewer).json()
        assert done["acknowledged"] is True
    assert client.get("/api/notice", headers=reviewer).json()["acknowledged"] is True
    decided = client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL), headers=reviewer)
    assert decided.status_code == 200, decided.text

    events = _events(store)
    assert len(events) == 1
    assert events[0]["payload"]["version"] == notices.VERSION
    assert events[0]["payload"]["actor"] == "reviewer@default.example.org"
    assert store.verify()["ok"] is True


def _events(store: AuditStore) -> list[dict[str, Any]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all(
            "SELECT decision_id, event, payload FROM audit_events WHERE event = ?",
            (notices.EVENT,),
        )
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def test_a_new_notice_version_asks_again(
    signed_in_app: tuple[TestClient, AuditStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = signed_in_app
    reviewer = signed_in(store, role="reviewer")
    client.post("/api/notice/acknowledge", headers=reviewer)
    monkeypatch.setattr(notices, "VERSION", "2")
    assert client.get("/api/notice", headers=reviewer).json()["acknowledged"] is False
    refused = client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL), headers=reviewer)
    assert refused.status_code == 403


def test_an_admin_cannot_mint_a_way_around_it(signed_in_app: tuple[TestClient, AuditStore]) -> None:
    client, store = signed_in_app
    admin = signed_in(store, role="admin")
    minted = client.post("/api/auth/keys", json={"name": "k", "role": "service"}, headers=admin)
    assert minted.status_code == 403 and minted.json()["error"] == "NoticeNotAcknowledged"
    client.post("/api/notice/acknowledge", headers=admin)
    assert client.post(
        "/api/auth/keys", json={"name": "k", "role": "service"}, headers=admin
    ).status_code in (200, 201)


def test_people_who_take_no_data_in_are_not_asked(
    signed_in_app: tuple[TestClient, AuditStore],
) -> None:
    client, store = signed_in_app
    viewer = signed_in(store, role="viewer")
    assert client.get("/api/notice", headers=viewer).json()["required"] is False
    health = client.get("/api/health", headers=viewer).json()
    assert health["notice"] == {"version": notices.VERSION, "acknowledgement": "required"}


def test_keys_are_never_asked(signed_in_app: tuple[TestClient, AuditStore]) -> None:
    client, store = signed_in_app
    key = store.create_api_key(name="svc", role="service", org="default", created_by="t")["key"]
    machine = {"authorization": f"Bearer {key}"}
    decided = client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL), headers=machine)
    assert decided.status_code == 200


def test_open_mode_is_never_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "required")
    client = TestClient(create_app(db_path=tmp_path / "o.db"))  # no sign-in: a local sandbox
    assert client.get("/api/notice").json()["required"] is False
    assert client.post(f"/api/packs/{INBOX}/decide", json=message(REFILL)).status_code == 200


def test_a_bad_setting_fails_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENDARI_NOTICE_ACKNOWLEDGEMENT", "maybe")
    with pytest.raises(Exception, match="required or off"):
        create_app(db_path=tmp_path / "x.db")
