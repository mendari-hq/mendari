# SPDX-License-Identifier: Apache-2.0
"""The EHR pilot end to end (LP-23m, ADR-0014): an EHR calls a CDS Hooks service with its own
signed token, Mendari reads the vendor-shaped record from the EHR's FHIR server, answers with its
card, takes the clinician's feedback on that card, and `mendari audit verify` proves the trail.

1. Sign-in on (its identity provider never called), the EHR registered as Epic publishes its
   client: keys by one pinned `jwks_uri` on the allow-list, ES384, `jku` in each token's header;
2. discovery and two `order-sign` hooks over HTTP (`TestClient`), each call with a new token
   addressed to its own URL; the hooks name the FHIR server and send no prefetch, so the record
   of each vendor-shaped patient (`sandbox/ehr_profile.py`) is read there;
3. each hook answers one card, named by its decision's id, its one suggestion's id derived from
   it (`selectionBehavior: at-most-one`), and the decision is chained as asked for by the client;
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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari.cli import main
from mendari.core import clock
from mendari.core.audit import AuditStore
from mendari.sandbox import cds_client, ehr_profile
from mendari.server.app import create_app
from mendari.standards import cds_hooks
from mendari.standards.data_coverage import FORMAT
from tests.conftest import security_lines
from tests.jwt_signers import DROP, PUBLIC_URL
from tests.test_cds_auth import Served, bearer, sign_in_on
from tests.test_cds_clients import EC_EHR, JWKS_URI, entry, jwks_server, register, token
from tests.test_cds_feedback import accepted, card_feedback, feedback, overridden
from tests.test_ehr_profile import TODAY, profile_server
from tests.test_fhir_client import allow_ehr
from tests.test_scoreboard_end_to_end import on_this_machine  # noqa: F401 - the socket guard

SERVICE = "mendari.referral-router"
DISCOVERY = "/cds-services"
HOOK = f"{DISCOVERY}/{SERVICE}"
FEEDBACK = f"{HOOK}/feedback"
# Two of the profile's referral patients whose order the pack decides (tests/test_ehr_profile.py,
# NOT_DECIDED names the others): one card is accepted, the other overridden.
PATIENTS = ("ehr-ref-2", "ehr-ref-4")
CLIENT = "cds epic"  # the actor a verified token of the registered client `epic` is


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
    on_this_machine: list[str],  # noqa: F811
) -> None:
    """A signed hook, its card and its feedback, chained and verified; a changed event is not."""
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    sign_in_on(monkeypatch)
    fetched = jwks_server(monkeypatch, {"keys": [EC_EHR.jwk]})
    register(monkeypatch, entry(jwks=DROP, jwks_uri=JWKS_URI))
    read: list[Any] = []
    allow_ehr(monkeypatch, profile_server(ehr_profile.resources(TODAY), read))
    profile = {patient.id: patient for patient in ehr_profile.patients(TODAY)}

    with TestClient(create_app(db_path=db_target), base_url=PUBLIC_URL) as http:
        served = Served(http, db_target)
        served.registry_is_on()  # the registry is on: a token here is verified, not ignored
        capsys.readouterr()

        listed = http.get(DISCOVERY, headers=signed(DISCOVERY))
        assert listed.status_code == 200, listed.text
        hooks = {service["id"]: service["hook"] for service in listed.json()["services"]}
        assert hooks[SERVICE] == "order-sign"

        cards = []
        for patient_id in PATIENTS:
            body = cds_client.hook_request("order-sign", profile[patient_id], ehr_profile.BASE)
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
        assert read and {request.method for request in read} == {"GET"}
        assert all(str(request.url).startswith(f"{ehr_profile.BASE}/") for request in read)

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
