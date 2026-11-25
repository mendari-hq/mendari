# SPDX-License-Identifier: Apache-2.0
"""CDS Hooks 2.0 card feedback (LP-23f, `POST /cds-services/{id}/feedback`): what a clinician did
with each card, accepted with the suggestion taken or overridden with one of the reasons the card
offered, chained on the card's decision as `card.feedback`, codes and ids only. All or nothing: a
suggestion or reason the card did not offer, a field CDS Hooks 2.0 does not define (but
`extension`, accepted and never kept) or a time with no UTC time is a `422`; a card that is not
a decision of the service's agent in the caller's organization, or, when the caller is named,
one it did not ask for, is one `404`, whichever it is. The comment is never kept. The route is a
decide route, rate limited in the decide bucket and never counted against the daily cap. The
service is the triage test fixture's (`tests/support/cds_messages.py`): one card a message."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, load_agent
from mendari.cli import main
from mendari.server.app import create_app
from mendari.standards import cds_hooks, fhir_structure, terminology
from mendari.standards.terminology import OVERRIDE_REASONS, OVERRIDE_REASONS_SYSTEM
from tests.conftest import CHART_DIR, CLINICAL_DIR, ROOT, encrypt_mode, security_lines
from tests.support.cds_messages import HOOK, SERVICE, message_hook
from tests.support.fixture_records import SHOWCASE

FEEDBACK = f"{HOOK}/feedback"
AT = "2026-10-18T12:00:00-04:00"  # US Eastern
AT_UTC = "2026-10-18T16:00:00+00:00"
COMMENT = "COMMENT-MARK-the-patient-said-something-private"


def reason(code: str = "clinical-judgement", system: str = OVERRIDE_REASONS_SYSTEM) -> Any:
    return {"system": system, "code": code, "display": OVERRIDE_REASONS.get(code, ("x",))[0]}


def overridden(card: str, code: str | None = None, comment: str | None = None) -> dict[str, Any]:
    """One card's feedback: overridden, with a reason the card offered (`code`) or none."""
    item: dict[str, Any] = {"card": card, "outcome": "overridden", "outcomeTimestamp": AT}
    if code is not None or comment is not None:
        item["overrideReason"] = {
            **({"reason": reason(code)} if code is not None else {}),
            **({"userComment": comment} if comment is not None else {}),
        }
    return item


def accepted(card: str, suggestion: str | None = None) -> dict[str, Any]:
    """One card's feedback: accepted, with its one suggestion taken."""
    taken = suggestion or cds_hooks.suggestion_id(card)
    return {
        "card": card,
        "outcome": "accepted",
        "acceptedSuggestions": [{"id": taken}],
        "outcomeTimestamp": AT,
    }


def feedback(*items: dict[str, Any]) -> dict[str, Any]:
    return {"feedback": list(items)}


def card_feedback(store: AuditStore) -> list[dict[str, Any]]:
    return [e for e in store.events() if e["event"] == "card.feedback"]


def sign_in_on(monkeypatch: pytest.MonkeyPatch, limits: str) -> None:
    """Sign-in on (its identity provider never called), with `limits` as the rate limits."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    encrypt_mode(monkeypatch)  # sign-in never runs PHI mode off
    monkeypatch.setenv("MENDARI_RATE_LIMITS", limits)


def service_key(db: str) -> dict[str, str]:
    with AuditStore(db) as store:
        made = store.create_api_key(name="ehr", role="service", org="default", created_by="t")
    return {"authorization": f"Bearer {made['key']}"}


def hooked(client: TestClient, messages: int = 2, **headers: str) -> list[str]:
    """The card ids the hook gives for `messages` messages, one card each."""
    cards = []
    for n in range(1, messages + 1):
        answer = client.post(HOOK, json=message_hook(n), headers=headers)
        assert answer.status_code == 200, answer.text
        cards += [card["uuid"] for card in answer.json()["cards"]]
    assert len(cards) == messages
    return cards


def test_feedback_for_an_accepted_and_an_overridden_card_is_recorded_and_chained(
    db_target: str,
) -> None:
    """Each item is one `card.feedback` on its card's decision, after its `decision.created`:
    the service, the outcome, the suggestion taken, the reason as `{system, code}`, the time in
    UTC and who sent it (nobody, in open mode), and the export names the codes."""
    client = TestClient(create_app(db_path=db_target))
    first, second = hooked(client)
    sent = client.post(FEEDBACK, json=feedback(accepted(first), overridden(second, "not-relevant")))
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"recorded": 2}
    with AuditStore(db_target) as store:
        chained = card_feedback(store)
        assert store.verify()["ok"]
    assert [e["decision_id"] for e in chained] == [first, second]
    assert [e["payload"] for e in chained] == [
        {
            "service": SERVICE,
            "outcome": "accepted",
            "accepted_suggestions": [cds_hooks.suggestion_id(first)],
            "override_reason": None,
            "outcome_timestamp": AT_UTC,
            "reporter": None,
            "org": "default",
        },
        {
            "service": SERVICE,
            "outcome": "overridden",
            "accepted_suggestions": [],
            "override_reason": {"system": OVERRIDE_REASONS_SYSTEM, "code": "not-relevant"},
            "outcome_timestamp": AT_UTC,
            "reporter": None,
            "org": "default",
        },
    ]
    record = client.get(f"/api/audit/{second}").json()
    assert [e["event"] for e in record["events"]] == ["decision.created", "card.feedback"]
    bundle = client.get(f"/api/audit/{second}/fhir").json()
    (event,) = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "AuditEvent"
        and e["resource"]["subtype"][0]["code"] == "card.feedback"
    ]
    assert event["action"] == "C"
    details = {d["type"]: d["valueString"] for d in event["entity"][0]["detail"]}
    assert details["service"] == SERVICE and details["outcome"] == "overridden"
    assert details["override_reason"] == f"{OVERRIDE_REASONS_SYSTEM}|not-relevant"
    assert "accepted_suggestions" not in details  # none taken
    # Feedback may come again (the clinician changed their mind): each is chained.
    assert client.post(FEEDBACK, json=feedback(overridden(first))).status_code == 200
    with AuditStore(db_target) as store:
        assert len(card_feedback(store)) == 3


def test_feedback_naming_another_organizations_agents_or_callers_decision_answers_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sign-in on: another organization's card, a decision of another agent, a card another
    caller of the same organization asked for and an id that names nothing are each the one
    `404`, the same answer but for the id it repeats, and nothing is chained; a request naming
    one of them beside the caller's own card records neither. The caller's own card: `200`."""
    sign_in_on(monkeypatch, "off")
    db = str(tmp_path / "orgs.db")
    client = TestClient(create_app(db_path=db))
    with AuditStore(db) as store:
        keys = {
            name: store.create_api_key(name=name, role="service", org=org, created_by="t")
            for name, org in (("mine", "default"), ("other", "default"), ("theirs", "clinic-b"))
        }
    mine, other_caller, theirs = (
        {"authorization": f"Bearer {keys[name]['key']}"} for name in ("mine", "other", "theirs")
    )
    (card,) = hooked(client, 1, **mine)
    (their_card,) = hooked(client, 1, **theirs)
    chart = client.post(  # a decision of another agent with a hook
        f"/api/agents/{load_agent(CHART_DIR).id}/decide", json=SHOWCASE.input(), headers=mine
    ).json()["id"]
    nothing = str(uuid.uuid4())
    with AuditStore(db) as store:
        before = len(card_feedback(store))

    def refused(who: dict[str, str], *items: dict[str, Any], path: str = FEEDBACK) -> Any:
        answer = client.post(path, json=feedback(*items), headers=who)
        assert answer.status_code == 404, answer.text
        return answer.json()

    shown = {
        "another organization's": refused(mine, overridden(their_card)),
        "another agent's": refused(mine, overridden(chart)),
        "another caller's": refused(other_caller, overridden(card)),
        "nothing's": refused(mine, overridden(nothing)),
    }
    named = {"another organization's": their_card, "another agent's": chart}
    named |= {"another caller's": card, "nothing's": nothing}
    for which, body in shown.items():
        assert body["error"] == "UnknownCard", which
        assert body["detail"] == f"no card of this service: {named[which]}", which
    assert refused(theirs, overridden(card))["error"] == "UnknownCard"  # the reverse too
    mixed = refused(mine, accepted(card), overridden(nothing))
    assert mixed["detail"] == f"no card of this service: {nothing}"  # all or nothing
    assert refused(mine, overridden(card), path="/cds-services/nope/feedback")["error"] == (
        "AgentNotFoundError"
    )
    # An agent with no hook makes no card, so its own decisions take no card feedback either.
    reviewer = load_agent(CLINICAL_DIR)
    assert reviewer.manifest.cds_hooks is None
    example = client.get(f"/api/agents/{reviewer.id}/examples", headers=mine).json()[0]
    reviewed = client.post(
        f"/api/agents/{reviewer.id}/decide", json=example["input"], headers=mine
    ).json()["id"]
    unhooked = f"/cds-services/{reviewer.id.replace('/', '.')}/feedback"
    assert refused(mine, overridden(reviewed), path=unhooked)["error"] == "AgentNotFoundError"
    with AuditStore(db) as store:
        assert len(card_feedback(store)) == before
    sent = client.post(FEEDBACK, json=feedback(accepted(card)), headers=mine)
    assert sent.status_code == 200, sent.text
    with AuditStore(db) as store:
        (event,) = card_feedback(store)
        assert store.verify()["ok"]
    assert event["decision_id"] == card
    assert event["payload"]["reporter"] == f"key {keys['mine']['id']}"  # who sent it


def _broken(card: str, other: str) -> dict[str, dict[str, Any]]:
    """Feedback the card did not offer, or not CDS Hooks 2.0's shape: each a `422`."""
    stamp = {"outcomeTimestamp": AT}
    return {
        "another suggestion": accepted(card, str(uuid.uuid4())),
        "the other card's suggestion": accepted(card, cds_hooks.suggestion_id(other)),
        "accepted, nothing taken": {"card": card, "outcome": "accepted", **stamp},
        "accepted, an empty list": {
            "card": card,
            "outcome": "accepted",
            "acceptedSuggestions": [],
            **stamp,
        },
        "accepted, two taken": {
            **accepted(card),
            "acceptedSuggestions": [{"id": cds_hooks.suggestion_id(card)}] * 2,
        },
        "accepted, with a reason": {
            **accepted(card),
            "overrideReason": {"reason": reason()},
        },
        "overridden, a suggestion taken": {
            **overridden(card),
            "acceptedSuggestions": [{"id": cds_hooks.suggestion_id(card)}],
        },
        "another system's reason": {
            **overridden(card),
            "overrideReason": {"reason": reason(system="http://example.org/reasons")},
        },
        "a code not offered": {
            **overridden(card),
            "overrideReason": {"reason": reason("too-expensive"), "userComment": COMMENT},
        },
        "another outcome": {"card": card, "outcome": "ignored", **stamp},
        "no time": {"card": card, "outcome": "overridden"},
        "a time with no zone": {
            "card": card,
            "outcome": "overridden",
            "outcomeTimestamp": "2026-10-18T12:00:00",
        },
        # Valid times whose UTC time is past year 9999 or before year 1: no time to keep.
        "a time after year 9999 in UTC": {
            "card": card,
            "outcome": "overridden",
            "outcomeTimestamp": "9999-12-31T23:59:59-14:00",
        },
        "a time before year 1 in UTC": {
            "card": card,
            "outcome": "overridden",
            "outcomeTimestamp": "0001-01-01T00:00:00+14:00",
        },
        "a field CDS Hooks has not": {**overridden(card), "note": COMMENT},
        "a coding field CDS Hooks has not": {
            **overridden(card),
            "overrideReason": {"reason": {**reason(), "text": COMMENT}},
        },
        # FHIR's Coding has these; CDS Hooks 2.0's has only system, code and display.
        "a coding's version": {
            **overridden(card),
            "overrideReason": {"reason": {**reason(), "version": COMMENT}},
        },
        "a coding's userSelected": {
            **overridden(card),
            "overrideReason": {"reason": {**reason(), "userSelected": True}},
        },
        "an extension that is not an object": {**overridden(card), "extension": [COMMENT]},
    }


@pytest.mark.parametrize("case", list(_broken(str(uuid.UUID(int=1)), str(uuid.UUID(int=2)))))
def test_an_override_reason_or_suggestion_the_card_did_not_offer_is_refused_and_nothing_is_written(
    tmp_path: Path, case: str
) -> None:
    """Checked against what the card offered (its one suggestion's UUID 5, the codes of the
    override CodeSystem) and CDS Hooks 2.0's closed shape: a `422` that repeats nothing sent,
    nothing chained, and beside a good item, the good item is not chained either."""
    db = tmp_path / "offered.db"
    client = TestClient(create_app(db_path=db))
    card, other = hooked(client)
    broken = _broken(card, other)[case]
    for body in (feedback(broken), feedback(accepted(other), broken)):
        answer = client.post(FEEDBACK, json=body)
        assert answer.status_code == 422, (case, answer.text)
        assert COMMENT not in answer.text and card not in answer.text
    with AuditStore(db) as store:
        assert card_feedback(store) == []


def test_the_shape_of_the_request_as_a_whole_is_checked(tmp_path: Path) -> None:
    """1 to 50 cards, each named once, and nothing beside `feedback`."""
    db = tmp_path / "shape.db"
    client = TestClient(create_app(db_path=db))
    card, other = hooked(client)
    for body in (
        feedback(),
        feedback(*(overridden(str(uuid.uuid4())) for _ in range(cds_hooks.MAX_FEEDBACK + 1))),
        feedback(overridden(card), accepted(card)),
        {**feedback(overridden(card)), "hookInstance": "x"},
        {},
    ):
        assert client.post(FEEDBACK, json=body).status_code == 422
    with AuditStore(db) as store:
        assert card_feedback(store) == []
    every = [overridden(card), accepted(other)]
    assert client.post(FEEDBACK, json=feedback(*every)).json() == {"recorded": 2}


def test_an_extension_is_taken_on_any_object_and_never_kept(tmp_path: Path) -> None:
    """CDS Hooks 2.0 reserves `extension` (a JSON object) for implementations: on the request,
    an item, its override reason, the reason's Coding or a suggestion taken, it is accepted, and
    what is chained is what it is without one; it is kept nowhere."""
    db = tmp_path / "extension.db"
    client = TestClient(create_app(db_path=db))
    card, other = hooked(client)
    mark = {"org.example.ehr-note": COMMENT}
    taken = accepted(card)
    taken["acceptedSuggestions"][0]["extension"] = mark
    item = overridden(other, "not-relevant")
    item["overrideReason"]["extension"] = mark
    item["overrideReason"]["reason"]["extension"] = mark
    body = {"feedback": [{**taken, "extension": mark}, item], "extension": mark}
    sent = client.post(FEEDBACK, json=body)
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"recorded": 2} and COMMENT not in sent.text
    with AuditStore(db) as store:
        payloads = [e["payload"] for e in card_feedback(store)]
    assert [p["accepted_suggestions"] for p in payloads] == [[cds_hooks.suggestion_id(card)], []]
    assert payloads[1]["override_reason"] == {
        "system": OVERRIDE_REASONS_SYSTEM,
        "code": "not-relevant",
    }
    with closing(sqlite3.connect(db)) as raw:
        tables = [
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        held = [
            t
            for t in tables
            if COMMENT in json.dumps(raw.execute(f'SELECT * FROM "{t}"').fetchall(), default=str)
        ]
    assert tables and held == []


def test_the_comment_is_not_kept(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`userComment` is free text (owner decision 7): accepted, and kept nowhere, not even under
    PHI mode `off`: not in any table, the answer, the trail's export or the security log."""
    db = tmp_path / "comment.db"
    client = TestClient(create_app(db_path=db))
    card, other = hooked(client)
    capsys.readouterr()
    items = [overridden(card, "already-addressed", COMMENT), overridden(other, comment=COMMENT)]
    sent = client.post(FEEDBACK, json=feedback(*items))
    assert sent.status_code == 200, sent.text
    assert COMMENT not in sent.text
    exported = client.get(f"/api/audit/{card}/fhir")
    assert exported.status_code == 200 and COMMENT not in exported.text
    assert COMMENT not in client.get(f"/api/audit/{card}").text
    assert COMMENT not in capsys.readouterr().out
    with closing(sqlite3.connect(db)) as raw:
        tables = [
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        held = [
            t
            for t in tables
            if COMMENT in json.dumps(raw.execute(f'SELECT * FROM "{t}"').fetchall(), default=str)
        ]
    assert tables and held == []
    with AuditStore(db) as store:
        payloads = [e["payload"] for e in card_feedback(store)]
    assert [p["override_reason"] for p in payloads] == [
        {"system": OVERRIDE_REASONS_SYSTEM, "code": "already-addressed"},
        None,  # a comment alone gives no reason
    ]


def test_feedback_is_rate_limited_and_not_daily_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Feedback counts against the decide rate limit, per principal, as every decide route
    does, and never against the principal's daily cap: it decides nothing, so a caller at its
    cap can still say what happened to the cards it got."""
    sign_in_on(monkeypatch, "on,decide=1/min:3")
    monkeypatch.setenv("MENDARI_PRINCIPAL_DAILY_LIMIT", "1")
    db = str(tmp_path / "limited.db")
    client = TestClient(create_app(db_path=db))
    key = service_key(db)
    (card,) = hooked(client, 1, **key)  # the decide bucket's first, and the day's one decision
    health = client.get("/api/health", headers=key).json()["principal_quota"]
    assert health == {"daily_limit": 1, "used_today": 1}
    for _ in range(2):  # the bucket's second and third: past the cap, still answered
        sent = client.post(FEEDBACK, json=feedback(overridden(card)), headers=key)
        assert sent.status_code == 200, sent.text
    assert client.get("/api/health", headers=key).json()["principal_quota"]["used_today"] == 1
    capsys.readouterr()
    limited = client.post(FEEDBACK, json=feedback(overridden(card)), headers=key)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert "decide" in limited.json()["detail"]
    hits = [
        line for line in security_lines(capsys.readouterr().out) if line["event"] == "ratelimit.hit"
    ]
    assert [line["reason"] for line in hits] == ["decide"]
    with AuditStore(db) as store:
        assert len(card_feedback(store)) == 2


def test_audit_verify_passes_after_feedback(
    db_target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`mendari audit verify` over a trail holding card feedback, on both databases."""
    client = TestClient(create_app(db_path=db_target))
    card, other = hooked(client)
    items = [accepted(card), overridden(other, "clinical-judgement", COMMENT)]
    assert client.post(FEEDBACK, json=feedback(*items)).status_code == 200
    client.close()
    capsys.readouterr()
    assert main(["audit", "verify", "--db", db_target]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_the_override_reasons_are_published_with_displays(tmp_path: Path) -> None:
    """The three neutral codes (owner decision 8) are a CodeSystem of Mendari's own, served under
    `/fhir` and written for the HL7 validator (`scripts/fhir_terminology.py`, CI's
    `fhir-conformance`), each with its display and definition; a card offers exactly them, each
    Coding with the display its CodeSystem gives."""
    client = TestClient(create_app(db_path=tmp_path / "terms.db"))
    served = client.get("/fhir/CodeSystem/card-override-reasons")
    assert served.status_code == 200, served.text
    system = served.json()
    assert system["url"] == OVERRIDE_REASONS_SYSTEM and system["content"] == "complete"
    assert fhir_structure.blocking(fhir_structure.check(system)) == []
    concepts = {c["code"]: c for c in system["concept"]}
    assert list(concepts) == ["not-relevant", "already-addressed", "clinical-judgement"]
    assert all(c["display"] and c["definition"] for c in concepts.values())
    answer = client.post(HOOK, json=message_hook()).json()
    offered = answer["cards"][0]["overrideReasons"]
    assert offered == terminology.override_reasons()
    assert {(c["code"], c["display"]) for c in offered} == {
        (code, c["display"]) for code, c in concepts.items()
    }
    out = tmp_path / "terminology"
    script = ROOT / "scripts" / "fhir_terminology.py"
    subprocess.run([sys.executable, str(script), str(out)], check=True, capture_output=True)
    written = json.loads((out / "CodeSystem-card-override-reasons.json").read_text())
    assert written == system
