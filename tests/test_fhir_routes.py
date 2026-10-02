"""The `/fhir` read and search routes (server/app.py) on SQLite and Postgres: what they serve is
what the renderers make, under an absolute base; reads carry their version and are never
conditional; searches take GET and POST forms with hybrid strictness; nothing crosses an
organization, and nothing a reader does writes to the audit trail."""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Outcome
from mendari.server.app import create_app
from mendari.standards import audit_fhir, fhir_structure, task_fhir
from mendari.standards.fhir import DECISION_ID_SYSTEM
from tests.conftest import signed_in
from tests.test_fhir_structure import load_script

INBOX = "mendari/patient-inbox-router"
SERVER = "https://mendari.test"
BASE = f"{SERVER}/fhir"
FORM = {"content-type": "application/x-www-form-urlencoded"}
SECRET = "Synthetic secret: crushing chest pain since noon"


@pytest.fixture
def client(db_target: str) -> Iterator[TestClient]:
    with TestClient(create_app(db_path=db_target), base_url=SERVER) as test_client:
        yield test_client


def ok(response: Any) -> Any:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/fhir+json")
    assert response.headers["cache-control"] == "private, no-cache"
    return response.json()


def decide(client: TestClient, body: str, headers: dict[str, str] | None = None) -> str:
    answer = client.post(
        f"/api/packs/{INBOX}/decide", json={"message": {"body": body}}, headers=headers
    )
    assert answer.status_code == 200, answer.text
    return str(answer.json()["id"])


def reviewed(client: TestClient, headers: dict[str, str] | None = None) -> str:
    """A decision with three Task versions: made, overridden in review, rated (no version),
    then given what really happened downstream."""
    decision = decide(client, SECRET, headers)
    review = {"final_label": "physician", "reviewer": "rn-1", "note": "note: " + SECRET}
    calls = [
        (f"/api/review/{decision}", review),
        (f"/api/audit/{decision}/feedback", {"rating": "up", "note": SECRET}),
        (f"/api/audit/{decision}/outcome", {"final_label": "nurse", "source": "ehr"}),
    ]
    for path, body in calls:
        assert client.post(path, json=body, headers=headers).status_code == 200
    return decision


def item(db_target: str, decision: str) -> dict[str, Any]:
    with AuditStore(db_target) as store:
        found = store.item(decision)
    assert found is not None
    return found


# -- reads -------------------------------------------------------------------------------------


def test_a_task_read_is_its_latest_version_with_etag_and_last_modified(
    client: TestClient, db_target: str
) -> None:
    decision = reviewed(client)
    response = client.get(f"/fhir/Task/{decision}")
    task = ok(response)
    assert task == task_fhir.current_task(item(db_target, decision), BASE)
    assert task["meta"]["versionId"] == "3" and response.headers["etag"] == 'W/"3"'
    modified = parsedate_to_datetime(response.headers["last-modified"])
    updated = datetime.fromisoformat(task["meta"]["lastUpdated"]).astimezone(UTC)
    assert modified == updated.replace(microsecond=0)
    assert task["relevantHistory"][0]["reference"].startswith(f"{BASE}/Provenance/{decision}-p")


def test_each_version_is_read_at_its_history_url(client: TestClient, db_target: str) -> None:
    decision = reviewed(client)
    versions = task_fhir.task_versions(item(db_target, decision), BASE)
    assert len(versions) == 3  # the rating made none
    for number, expected in enumerate(versions, start=1):
        response = client.get(f"/fhir/Task/{decision}/_history/{number}")
        assert ok(response) == expected and response.headers["etag"] == f'W/"{number}"'
    for missing in ("0", "4", "01", "x", "1.0", "99999999999"):
        gone = client.get(f"/fhir/Task/{decision}/_history/{missing}")
        assert gone.status_code == 404, missing
        assert gone.json()["issue"][0]["code"] == "not-found"


def test_there_is_no_conditional_read(client: TestClient) -> None:
    """A version is derived on read and a release may render it anew: a 304 would confirm a
    copy that is no longer what the server renders, so If-None-Match always gets the body."""
    decision = decide(client, "Can you send a refill for metformin?")
    etag = client.get(f"/fhir/Task/{decision}").headers["etag"]
    again = client.get(f"/fhir/Task/{decision}", headers={"if-none-match": etag})
    assert ok(again)["id"] == decision
    since = {"if-modified-since": "Fri, 01 Jan 2100 00:00:00 GMT"}
    assert client.get(f"/fhir/Task/{decision}", headers=since).status_code == 200


def test_audit_events_and_provenances_are_read_by_their_ids(
    client: TestClient, db_target: str
) -> None:
    decision = reviewed(client)
    trail = item(db_target, decision)
    versions = audit_fhir.task_versions(trail["events"])
    for event in audit_fhir.joined(trail):
        audit_event = ok(client.get(f"/fhir/AuditEvent/{decision}-{event['seq']}"))
        assert audit_event == audit_fhir.audit_event(event, base=BASE)
        assert audit_event["entity"][0]["what"]["reference"] == f"{BASE}/Task/{decision}"
        response = client.get(f"/fhir/Provenance/{decision}-p{event['seq']}")
        if event["event"] not in audit_fhir.VERSION_EVENTS:  # a rating changes no result
            assert response.status_code == 404
            continue
        version = versions[event["seq"]]
        assert ok(response) == audit_fhir.provenance(event, base=BASE, version=version)
        target = response.json()["target"][0]["reference"]
        assert target == f"{BASE}/Task/{decision}/_history/{version}"
        assert response.headers["etag"] == 'W/"1"'


@pytest.mark.parametrize(
    "path",
    [
        "/fhir/Task/no-such-decision",
        "/fhir/AuditEvent/no-such-decision-1",
        "/fhir/AuditEvent/{d}-p1",  # a Provenance id is not an AuditEvent id
        "/fhir/AuditEvent/{d}-999999",
        "/fhir/AuditEvent/{d}",
        "/fhir/Provenance/{d}-1",  # nor the other way round
        "/fhir/Provenance/{d}-p99999999999999999999",
        "/fhir/AuditEvent/{d}-01",  # not the id Mendari writes: a read returns the id asked for
        "/fhir/Provenance/{d}-p01",
    ],
)
def test_unknown_ids_are_a_404_outcome(client: TestClient, path: str) -> None:
    decision = decide(client, "Can you send a refill for metformin?")
    response = client.get(path.format(d=decision))
    assert response.status_code == 404
    assert response.json()["resourceType"] == "OperationOutcome"


def test_references_are_under_the_public_url_when_it_is_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a proxy the request's host can be an internal one; MENDARI_PUBLIC_URL is how
    clients reach the server."""
    decision = decide(client, "Can you send a refill for metformin?")
    monkeypatch.setenv("MENDARI_PUBLIC_URL", "https://public.example.org/")
    task = ok(client.get(f"/fhir/Task/{decision}"))
    public = "https://public.example.org/fhir"
    assert task["relevantHistory"][0]["reference"].startswith(f"{public}/Provenance/")
    bundle = ok(client.get("/fhir/Task"))
    assert bundle["entry"][0]["fullUrl"] == f"{public}/Task/{decision}"
    assert bundle["link"][0]["url"].startswith(f"{public}/Task?")


# -- search ------------------------------------------------------------------------------------


def test_search_by_get_and_by_post_form_agree(client: TestClient) -> None:
    waiting = decide(client, SECRET)
    automated = decide(client, "Can you send a refill for metformin?")
    by_get = ok(client.get("/fhir/Task", params={"status": "requested"}))
    by_post = ok(client.post("/fhir/Task/_search", content="status=requested", headers=FORM))
    assert [e["resource"]["id"] for e in by_get["entry"]] == [waiting]
    assert by_post["entry"] == by_get["entry"] and by_post["link"] == by_get["link"]
    # The URL's parameters and the body's are one search (ANDed).
    both = client.post(
        "/fhir/Task/_search?status=completed", content=f"_id={automated}", headers=FORM
    )
    assert [e["resource"]["id"] for e in ok(both)["entry"]] == [automated]
    neither = client.post(
        "/fhir/Task/_search?status=requested", content=f"_id={automated}", headers=FORM
    )
    assert "entry" not in ok(neither)


def test_pages_follow_next_links_and_totals_are_given_when_asked(client: TestClient) -> None:
    made = [decide(client, f"Can you send a refill for metformin? ({n})") for n in range(5)]
    first = ok(client.get("/fhir/Task", params={"_count": "2", "_total": "accurate"}))
    assert first["total"] == 5
    seen, page = [], first
    while True:
        seen += [entry["fullUrl"] for entry in page["entry"]]
        following = [link["url"] for link in page["link"] if link["relation"] == "next"]
        if not following:
            break
        assert following[0].startswith(f"{BASE}/Task?")
        parts = urlsplit(following[0])
        page = ok(client.get(f"{parts.path}?{parts.query}"))
    assert seen == [f"{BASE}/Task/{d}" for d in reversed(made)]  # newest first, once each
    assert "total" not in ok(client.get("/fhir/Task"))  # a total costs a count: only on request
    for counted in ({"_summary": "count"}, {"_count": "0"}):
        bundle = ok(client.get("/fhir/Task", params=counted))
        assert bundle["total"] == 5 and "entry" not in bundle


def test_a_decision_s_trail_is_searched_by_its_task(client: TestClient) -> None:
    decision = reviewed(client)
    decide(client, "Can you send a refill for metformin?")
    events = ok(client.get("/fhir/AuditEvent", params={"entity": f"Task/{decision}"}))
    kinds = [e["resource"]["subtype"][0]["code"] for e in events["entry"]]
    assert kinds == [
        "decision.created",
        "review.resolved",
        "feedback.recorded",
        "outcome.recorded",
    ]
    identifier = {"target:identifier": f"{DECISION_ID_SYSTEM}|{decision}"}
    provenances = ok(client.post("/fhir/Provenance/_search", data=identifier))
    targets = [e["resource"]["target"][0]["reference"] for e in provenances["entry"]]
    assert targets == [f"{BASE}/Task/{decision}/_history/{n}" for n in (1, 2, 3)]


def test_code_systems_are_searched_by_url(client: TestClient) -> None:
    url = "https://mendari.co/fhir/CodeSystem/decision-outcome"
    bundle = ok(client.get("/fhir/CodeSystem", params={"url": url}))
    assert [e["resource"]["url"] for e in bundle["entry"]] == [url]
    assert bundle["entry"][0]["fullUrl"] == f"{BASE}/CodeSystem/decision-outcome"


@pytest.mark.parametrize(
    ("query", "prefer", "status", "ignored"),
    [
        ("stauts=x", None, 200, "stauts"),  # not an R4 parameter of Task: ignored, with a warning
        ("stauts=x", "handling=strict", 400, None),
        ("patient=Patient/p1", None, 400, None),  # standard but unsupported: never silently
        ("patient=Patient/p1", "handling=lenient", 200, "patient"),
        ("_sort=-authored-on", None, 400, None),
        ("status:not=completed", "handling=lenient", 400, None),  # modifiers: always refused
        ("authored-on=2026-10-01T10:00", None, 400, None),  # a time needs a zone
        ("authored-on=ap2026-10-01", None, 400, None),
        ("cursor=not-a-cursor", None, 400, None),
        ("_format=json&_pretty=true", "handling=strict", 200, None),  # presentation, not criteria
    ],
)
def test_strictness_on_the_wire(
    client: TestClient, query: str, prefer: str | None, status: int, ignored: str | None
) -> None:
    headers = {"prefer": prefer} if prefer else {}
    for response in (
        client.get(f"/fhir/Task?{query}", headers=headers),
        client.post("/fhir/Task/_search", content=query, headers={**FORM, **headers}),
    ):
        assert response.status_code == status, (query, response.text)
        body = response.json()
        if status == 400:
            assert body["resourceType"] == "OperationOutcome"
            assert all(issue["severity"] == "error" for issue in body["issue"])
            assert "p1" not in response.text  # parameters are named, values never echoed
            continue
        outcomes = [e for e in body.get("entry", []) if e["search"]["mode"] == "outcome"]
        if ignored is None:
            assert outcomes == []
        else:
            (outcome,) = outcomes
            issue = outcome["resource"]["issue"][0]
            assert issue["severity"] == "warning" and ignored in issue["diagnostics"]


def test_a_form_that_is_not_utf8_is_a_400(client: TestClient) -> None:
    response = client.post("/fhir/Task/_search", content=b"status=\xff", headers=FORM)
    assert response.status_code == 400
    assert response.json()["issue"][0]["code"] == "invalid"


# -- what reading never does -------------------------------------------------------------------


def test_reading_and_searching_never_write_to_the_audit_trail(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under PHI mode `encrypt`, a read of the input is chained (`input.read`); a FHIR read or
    search shows no input, so it must chain nothing: the trail is the same before and after."""
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(os.urandom(32)).decode())
    with TestClient(create_app(db_path=db_target), base_url=SERVER) as client:
        decision = reviewed(client)
        before = item(db_target, decision)["events"]
        requests = [
            f"/fhir/Task/{decision}",
            f"/fhir/Task/{decision}/_history/1",
            f"/fhir/AuditEvent/{decision}-{before[0]['seq']}",
            f"/fhir/Provenance/{decision}-p{before[0]['seq']}",
            "/fhir/Task?_total=accurate",
            "/fhir/AuditEvent",
            "/fhir/Provenance",
        ]
        answers = [client.get(path) for path in requests]
        answers.append(client.post("/fhir/Task/_search", content="_count=1", headers=FORM))
        for answer in answers:
            ok(answer)
            assert SECRET not in answer.text and "note:" not in answer.text
        assert item(db_target, decision)["events"] == before


# -- organizations -----------------------------------------------------------------------------


@pytest.fixture
def two_orgs(db_target: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Sign-in on, a viewer key per organization (made in the store, as an admin's
    `POST /api/auth/keys` would) and a signed-in reviewer per organization."""
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    with TestClient(create_app(db_path=db_target), base_url=SERVER) as client:
        keys: dict[str, dict[str, str]] = {}
        with AuditStore(db_target) as store:
            for org in ("clinic-a", "clinic-b"):
                made = store.create_api_key(
                    name=f"{org} viewer", role="viewer", org=org, created_by="test"
                )
                keys[f"{org} viewer"] = {"authorization": f"Bearer {made['key']}"}
                keys[f"{org} reviewer"] = signed_in(store, role="reviewer", org=org)
        yield client, keys


def test_another_organization_reads_and_finds_nothing(two_orgs: Any) -> None:
    client, keys = two_orgs
    a, b = keys["clinic-a viewer"], keys["clinic-b viewer"]
    decision = reviewed(client, keys["clinic-a reviewer"])
    own = decide(client, "Can you send a refill for metformin?", keys["clinic-b reviewer"])
    trail = ok(client.get("/fhir/AuditEvent", params={"entity": decision}, headers=a))
    seq = trail["entry"][0]["resource"]["id"].removeprefix(f"{decision}-")
    for path in (
        f"/fhir/Task/{decision}",
        f"/fhir/Task/{decision}/_history/1",
        f"/fhir/AuditEvent/{decision}-{seq}",
        f"/fhir/Provenance/{decision}-p{seq}",
    ):
        assert client.get(path, headers=a).status_code == 200, path
        assert client.get(path, headers=b).status_code == 404, path  # as if unknown
    for resource in ("Task", "AuditEvent", "Provenance"):
        query = {"_total": "accurate"}
        for response in (
            client.get(f"/fhir/{resource}", params=query, headers=b),
            client.post(f"/fhir/{resource}/_search", data=query, headers=b),
            client.get(f"/fhir/{resource}", params={"_summary": "count"}, headers=b),
        ):
            found = ok(response)
            ids = {e["resource"]["id"] for e in found.get("entry", [])}
            assert all(i.startswith(own) for i in ids), resource
        assert found["total"] == {"Task": 1, "AuditEvent": 1, "Provenance": 1}[resource]


def test_a_replayed_cursor_stays_in_the_replayer_s_organization(two_orgs: Any) -> None:
    """A next link carries only a position; tenancy is applied again on every page, so another
    organization following it sees only its own rows from there on."""
    client, keys = two_orgs
    # Clinic B decides before and after clinic A, so a position inside A's rows has rows of B's
    # on both sides: newest-first Task pages reach the earlier one, chain-order pages the later.
    own = [decide(client, "refill please (1)", keys["clinic-b reviewer"])]
    theirs = [reviewed(client, keys["clinic-a reviewer"]) for _ in range(3)]
    own.append(reviewed(client, keys["clinic-b reviewer"]))
    for resource, query in (
        ("Task", "_count=1"),
        ("AuditEvent", "_count=2"),
        ("Provenance", "_count=1"),
    ):
        page = ok(client.get(f"/fhir/{resource}?{query}", headers=keys["clinic-a viewer"]))
        following = next(link["url"] for link in page["link"] if link["relation"] == "next")
        parts = urlsplit(following)
        replayed = ok(client.get(f"{parts.path}?{parts.query}", headers=keys["clinic-b viewer"]))
        assert replayed.get("entry"), resource  # B's own rows past the position, and only those
        for entry in replayed["entry"]:
            assert not any(entry["resource"]["id"].startswith(t) for t in theirs)
            assert any(entry["resource"]["id"].startswith(o) for o in own)


def test_signed_out_callers_get_nothing(two_orgs: Any) -> None:
    client, keys = two_orgs
    decision = decide(client, "Can you send a refill for metformin?", keys["clinic-a reviewer"])
    for path in (f"/fhir/Task/{decision}", "/fhir/Task", "/fhir/AuditEvent", "/fhir/CodeSystem"):
        assert client.get(path).status_code == 401, path
    assert client.post("/fhir/Task/_search", content="", headers=FORM).status_code == 401
    assert client.get("/fhir/metadata").status_code == 200  # discovery stays public


# -- what CI validates -------------------------------------------------------------------------


def test_every_sample_ci_validates_passes_the_structural_gate(client: TestClient) -> None:
    """The answers `scripts/fhir_output_sample.py` writes for the HL7 validator, made here
    through the same requests: each passes the in-process R4 structure check, and no searchset
    carries an error among its outcome entries (the validator does not look for that)."""
    script = load_script("fhir_output_sample")
    answers = script.served_reads_and_searches(client)
    assert set(answers) == {
        "served-task",
        "served-task-v2",
        "served-searchset-task",
        "served-searchset-lenient",
        "served-searchset-count",
        "served-auditevent",
        "served-searchset-auditevent",
        "served-provenance",
        "served-searchset-provenance",
        "served-searchset-codesystem",
    }
    metadata = client.get("/fhir/metadata")
    assert metadata.status_code == 200
    answers["capability"] = metadata.json()
    for name, resource in answers.items():
        assert fhir_structure.check(resource) == [], name
        for entry in resource.get("entry", []) if resource["resourceType"] == "Bundle" else []:
            if entry["search"]["mode"] == "outcome":
                severities = {issue["severity"] for issue in entry["resource"]["issue"]}
                assert severities.isdisjoint({"error", "fatal"}), name
    assert answers["served-task"]["meta"]["versionId"] == "3"
    assert answers["served-task-v2"]["meta"]["versionId"] == "2"
    assert any(link["relation"] == "next" for link in answers["served-searchset-task"]["link"])
    lenient = answers["served-searchset-lenient"]["entry"]
    assert lenient[-1]["search"]["mode"] == "outcome"
    count = answers["served-searchset-count"]
    assert count["total"] == 3 and "entry" not in count


def test_a_search_too_costly_for_the_database_is_a_400(client: TestClient) -> None:
    """Huge OR lists or repeats reached SQL and failed there (a 500); they are refused first."""
    ids = ",".join(f"d{i}-1" for i in range(101))
    too_many = client.post("/fhir/AuditEvent/_search", data={"_id": ids})
    assert too_many.status_code == 400
    assert too_many.json()["issue"][0]["code"] == "too-costly"
    repeats = tuple(("date", f"ne2026-01-{day:02d}") for day in range(1, 22))
    assert client.get("/fhir/AuditEvent", params=repeats).status_code == 400
    assert client.get("/fhir/AuditEvent", params=repeats[:20]).status_code == 200


def test_prefer_handling_is_read_from_every_prefer_field(client: TestClient) -> None:
    """RFC 7240 allows preferences in several Prefer fields."""
    fields = [("prefer", "return=minimal"), ("prefer", "handling=strict")]
    response = client.get("/fhir/Task", params={"stauts": "requested"}, headers=fields)
    assert response.status_code == 400


def test_a_policy_block_without_confidence_is_completed_not_failed(
    client: TestClient, db_target: str
) -> None:
    """Only a refused input is `failed`. A BLOCK rule on another question can leave the primary
    unanswered (no confidence); the stored `refused` flag tells the two apart, and rows from
    before the flag fall back to "BLOCK without confidence"."""
    decision = Decision.from_hub(INBOX)
    made = decision({"message": {"body": "Can you send a refill for metformin?"}})
    others = {k: v for k, v in made.answers.items() if k != made.primary}
    policy = replace(
        made, id=str(uuid.uuid4()), outcome=Outcome.BLOCK, recommendation=None, answers=others
    )
    refused = decision({"message": {"body": ""}})
    assert policy.confidence is None and not policy.rejected and refused.rejected
    with AuditStore(db_target) as store:
        store.record(policy, {"message": {"body": "x"}})
        store.record(refused, {"message": {"body": ""}})
    assert ok(client.get(f"/fhir/Task/{policy.id}"))["status"] == "completed"
    assert ok(client.get(f"/fhir/Task/{refused.id}"))["status"] == "failed"
    found = ok(client.get("/fhir/Task", params={"status": "failed"}))
    assert [e["resource"]["id"] for e in found["entry"]] == [refused.id]
    with AuditStore(db_target) as store, store._db.session(exclusive=True) as db:
        db.all("UPDATE decisions SET refused = NULL")  # as written before the column
    assert ok(client.get(f"/fhir/Task/{refused.id}"))["status"] == "failed"
