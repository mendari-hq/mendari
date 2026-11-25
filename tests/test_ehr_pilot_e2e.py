# SPDX-License-Identifier: Apache-2.0
"""The EHR pilot end to end (LP-23m, ADR-0014): an EHR calls a CDS Hooks service with its own
signed token, Mendari reads the vendor-shaped record from the EHR's FHIR server, answers with its
card, takes the clinician's feedback on that card, and `mendari audit verify` proves the trail.

1. Sign-in on (its identity provider never called), the EHR registered as Epic publishes its
   client: keys by one pinned `jwks_uri` on the allow-list, ES384, `jku` in each token's header;
2. discovery and two `patient-view` hooks of the chart-summary test fixture over HTTP
   (`TestClient`), each call with a new token addressed to its own URL; the hooks name the FHIR
   server and its token and send no prefetch, so the record of each vendor-shaped patient
   (`tests/test_ehr_profile.py`, served by `tests/support/chart_ehr.py`) is read there;
3. each hook answers one card (a chart that needs a visit soon, and one with a routine
   follow-up), named by its decision's id, its one suggestion's id derived from it
   (`selectionBehavior: at-most-one`), and the decision is chained as asked for by the client;
4. feedback on both cards with a signed token: one accepted with its suggestion, one overridden
   with a reason the card offered, each chained as `card.feedback` on its card's decision and
   naming the client that sent it;
5. `mendari audit verify` passes; one feedback event changed behind the store's back fails it
   at that event.

On SQLite and Postgres (`db_target`). Nothing leaves the machine: the EHR's JWK Set and FHIR
server are in-process transports, a socket connection to anything but this machine is refused
(`on_this_machine`, with its positive control), and the shell's own systems are removed before
anything runs (tests/conftest.py, LIVE_SETTINGS, as tests/test_isolation.py checks). Keys are
made at run time; the patients are synthetic. It takes seconds."""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.core import clock
from mendari.core.audit import AuditStore
from mendari.sandbox import cds_client
from mendari.server.app import create_app
from mendari.standards import cds_hooks
from mendari.standards.data_coverage import FORMAT
from tests.conftest import security_lines
from tests.jwt_signers import DROP, PUBLIC_URL
from tests.support.chart_ehr import TOKEN
from tests.support.chart_fixture import CHART_ID, hook
from tests.support.fixture_records import BASE, TODAY
from tests.test_cds_auth import Served, bearer, sign_in_on
from tests.test_cds_clients import EC_EHR, JWKS_URI, entry, jwks_server, register, token
from tests.test_cds_feedback import accepted, card_feedback, feedback, overridden
from tests.test_ehr_profile import PROFILE, profile_server

SERVICE = CHART_ID.replace("/", ".")  # mendari-test.chart-summary, a patient-view service
DISCOVERY = "/cds-services"
HOOK = f"{DISCOVERY}/{SERVICE}"
FEEDBACK = f"{HOOK}/feedback"
# Two of the profile's patients whose chart the agent shows a card for: one needing a visit soon
# (its card accepted), one with a routine follow-up (its card overridden).
PATIENTS = ("chart-ehr-2", "chart-ehr-1")
CLIENT = "cds epic"  # the actor a verified token of the registered client `epic` is
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def on_this_machine(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every socket connection refused, and recorded, unless it stays on this machine (a Unix
    socket or the loopback address): what the test tried to reach off it."""
    tried: list[str] = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def off_the_machine(sock: socket.socket, address: Any) -> str | None:
        if sock.family == getattr(socket, "AF_UNIX", None):
            return None
        host = str(address[0] if isinstance(address, tuple) else address)
        return None if host in LOOPBACK else host

    def local_connect(sock: socket.socket, address: Any) -> None:
        if (host := off_the_machine(sock, address)) is not None:
            tried.append(host)
            raise AssertionError(f"a connection off the machine, to {host}")
        connect(sock, address)

    def local_connect_ex(sock: socket.socket, address: Any) -> int:
        if (host := off_the_machine(sock, address)) is not None:
            tried.append(host)
            raise AssertionError(f"a connection off the machine, to {host}")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    # The positive control: an address off the machine (TEST-NET-1) is refused before any packet.
    with socket.socket() as probe, pytest.raises(AssertionError, match="off the machine"):
        probe.connect(("192.0.2.1", 9))
    assert tried == ["192.0.2.1"]
    tried.clear()
    return tried


def signed(path: str) -> dict[str, str]:
    """The EHR's `Authorization` for one call to `path`: a new ES384 token, `jku` its keys."""
    return bearer(token(EC_EHR, path, jku=JWKS_URI))


def chained(db: str) -> list[dict[str, Any]]:
    """The chain's events, oldest first: seq, decision, kind and payload."""
    with AuditStore(db) as store, store._db.session(exclusive=False) as session:
        rows = session.all("SELECT seq, decision_id, event, payload FROM audit_events ORDER BY seq")
    return [{**row, "payload": json.loads(row["payload"])} for row in rows]


def test_a_signed_hook_its_card_and_its_feedback_verify_end_to_end(
    db_target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    on_this_machine: list[str],
) -> None:
    """A signed hook, its card and its feedback, chained and verified; a changed event is not."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    sign_in_on(monkeypatch)
    fetched = jwks_server(monkeypatch, {"keys": [EC_EHR.jwk]})
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    server = profile_server()
    server.install(monkeypatch)
    profile = {case.id: case for case in PROFILE}

    with TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL) as http:
        served = Served(http, db_target)
        served.registry_is_on()  # the registry is on: a token here is verified, not ignored
        capsys.readouterr()

        listed = http.get(DISCOVERY, headers=signed(DISCOVERY))
        assert listed.status_code == 200, listed.text
        hooks = {service["id"]: service["hook"] for service in listed.json()["services"]}
        assert hooks[SERVICE] == "patient-view"

        cards = []
        for patient_id in PATIENTS:
            body = hook(
                profile[patient_id], today=TODAY, fhir_server=BASE, token=TOKEN, prefetched=False
            )
            assert "prefetch" not in body  # the record is read from the EHR's FHIR server
            answer = http.post(HOOK, json=body, headers=signed(HOOK))
            assert answer.status_code == 200, answer.text
            assert cds_client.problems_of(answer.json()) == [], patient_id
            (card,) = answer.json()["cards"]
            assert card["selectionBehavior"] == "at-most-one"
            (suggestion,) = card["suggestions"]
            assert suggestion["uuid"] == cds_hooks.suggestion_id(card["uuid"])
            assert card["overrideReasons"], patient_id
            cards.append(card)
        read = server.requests
        assert read and {request.method for request in read} == {"GET"}
        assert all(str(request.url).startswith(f"{BASE}/") for request in read)

        taken, overriding = (card["uuid"] for card in cards)
        offered = cards[1]["overrideReasons"][1]["code"]  # a reason the card offered
        sent = feedback(accepted(taken), overridden(overriding, offered))
        answered = http.post(FEEDBACK, json=sent, headers=signed(FEEDBACK))
        assert answered.status_code == 200, answered.text
        assert answered.json() == {"recorded": 2}

    # Every call was the client's: the access log's caller, and one fetch of its JWK Set.
    calls = [line for line in security_lines(capsys.readouterr().out) if line["event"] == "request"]
    assert [line["actor"] for line in calls] == ["cds:epic"] * 4
    assert fetched == [JWKS_URI]

    events = chained(db_target)
    created = [e for e in events if e["event"] == "decision.created"]
    assert [e["decision_id"] for e in created] == [taken, overriding]  # a card is its decision
    assert {e["payload"]["requested_by"] for e in created} == {CLIENT}
    with AuditStore(db_target) as store:
        for decision_id in (taken, overriding):
            stored = store.get(decision_id)
            assert stored is not None
            assert stored["record"]["data_coverage"]["format"] == FORMAT
        answers = card_feedback(store)
    assert [(e["decision_id"], e["payload"]["outcome"]) for e in answers] == [
        (taken, "accepted"),
        (overriding, "overridden"),
    ]
    assert answers[0]["payload"]["accepted_suggestions"] == [cds_hooks.suggestion_id(taken)]
    assert answers[1]["payload"]["override_reason"]["code"] == offered
    assert {e["payload"]["reporter"] for e in answers} == {CLIENT}
    assert {e["payload"]["org"] for e in answers} == {"default"}
    chained_feedback = [e for e in events if e["event"] == "card.feedback"]
    assert min(e["seq"] for e in chained_feedback) > max(e["seq"] for e in created)

    assert main(["audit", "verify", "--db", db_target]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True and verified["events"] == len(events)

    # The override turned into an acceptance behind the store's back, its hash left as it was.
    (changed,) = [e for e in chained_feedback if e["decision_id"] == overriding]
    payload = {**changed["payload"], "outcome": "accepted", "override_reason": None}
    with AuditStore(db_target) as store, store._db.session(exclusive=True) as session:
        session.all(
            "UPDATE audit_events SET payload = ? WHERE seq = ?",
            (json.dumps(payload, sort_keys=True), changed["seq"]),
        )
    assert main(["audit", "verify", "--db", db_target]) == 1
    broken = json.loads(capsys.readouterr().out)
    assert broken["ok"] is False and broken["broken_at"] == changed["seq"]
    assert on_this_machine == []
