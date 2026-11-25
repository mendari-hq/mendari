# SPDX-License-Identifier: Apache-2.0
"""Notes out of the chain (Phase 4b, stage H; ADR-0005 "commit, don't copy").

A review's, a rating's or a payer's free text is kept in `notes`, sealed under PHI mode `encrypt`,
plain under `off`, refused under `hash`; the chain keeps `note: null` and a salted commitment.
Notes follow inputs (who reads them, how a read is chained, retention), can be erased while the
chain still verifies, and notes chained before this stage are hidden from readers without
`read_input`. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core import tenancy
from mendari.core.db import Session
from mendari.core.phi import PhiMode, PhiPolicy
from mendari.errors import MendariError, NoteError
from mendari.server import app as app_module
from mendari.server.app import create_app
from mendari.standards import audit_fhir, pas
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import HASH_KEY_B64, message, signed_in

KEY = os.urandom(32)
SECRET = "SECRET-NOTE Rosalind Q. Example, MRN 4821937"
URGENT = "Crushing chest pain and my left arm is numb"  # HUMAN_REVIEW: waits for a reviewer
INBOX = "mendari/patient-inbox-router"


def keeping(target: str | Path, mode: PhiMode = "encrypt") -> AuditStore:
    return AuditStore(target, phi=PhiPolicy(mode=mode, key=KEY if mode == "encrypt" else b""))


def stored(store: AuditStore) -> str:
    """Every row of every table a note could reach, as text."""
    tables = ("decisions", "audit_events", "feedback", "decision_inputs", "notes")
    with store._db.session(exclusive=False) as db:
        return json.dumps({table: db.all(f"SELECT * FROM {table}") for table in tables})


def reviewed(store: AuditStore, pack: Pack, note: str | None = SECRET) -> str:
    """A decision reviewed and rated, each with the note."""
    decision_id = Decision(pack, store=store)(message(URGENT)).id
    store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1", note=note)
    store.record_feedback(decision_id, rating="down", reporter="rn-2", note=note)
    return decision_id


def with_note(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] in ("review.resolved", "feedback.recorded")]


def audit_events(store: AuditStore) -> list[dict[str, Any]]:
    exported = audit_fhir.resources(store.iter_events())
    return [r for r in exported if r["resourceType"] == "AuditEvent"]


@pytest.mark.parametrize("mode", ["off", "encrypt"])
def test_the_chain_commits_to_a_note_and_never_copies_it(
    db_target: str, inbox_pack: Pack, mode: PhiMode
) -> None:
    store = keeping(db_target, mode)
    decision_id = reviewed(store, inbox_pack)
    with store._db.session(exclusive=False) as db:
        payloads = [row["payload"] for row in db.all("SELECT payload FROM audit_events")]
        [rating] = db.all("SELECT note FROM feedback")
        notes = db.all("SELECT * FROM notes ORDER BY created_at, event DESC")
    assert SECRET not in "".join(payloads) and rating["note"] is None
    chained = [json.loads(p) for p in payloads if '"note_ref"' in p]
    assert [p["note"] for p in chained] == [None, None]
    assert [p["note_ref"]["id"] for p in chained] == [n["id"] for n in notes]
    assert all(len(p["note_ref"]["sha256"]) == 64 for p in chained)
    assert {n["decision_id"] for n in notes} == {decision_id}
    assert (SECRET in str(notes)) is (mode == "off")  # sealed under encrypt
    exported = json.dumps(list(audit_fhir.resources(store.iter_events())))
    assert SECRET not in exported and "note_ref" not in exported
    assert store.verify(notes=True)["notes"] == {"checked": 2, "erased": 0, "unchecked": 0}


def test_a_note_is_answered_to_whoever_wrote_it(db_target: str, inbox_pack: Pack) -> None:
    store = keeping(db_target)
    decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
    review = store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1", note=SECRET)
    rating = store.record_feedback(decision_id, rating="up", note=f"  {SECRET} ")
    assert review["note"] == rating["note"] == SECRET
    assert review["note_ref"]["id"] != rating["note_ref"]["id"] == rating["id"]
    blank = store.record_feedback(decision_id, rating="down", note="   ")
    assert blank["note"] is None and blank["note_ref"] is None


def test_notes_open_for_readers_only_and_each_read_is_chained(
    db_target: str, inbox_pack: Pack
) -> None:
    store = keeping(db_target)
    decision_id = reviewed(store, inbox_pack)
    hidden = store.get(decision_id)
    assert hidden is not None
    statuses = [(e["note_status"], e["note"]) for e in with_note(hidden["events"])]
    assert statuses == [("sealed", None)] * 2
    assert hidden["events"][-1]["event"] == "feedback.recorded"  # no read without a reader
    [rating] = store.feedback()
    assert (rating["note_status"], rating["note"]) == ("sealed", None)

    shown = store.get(decision_id, reader="rn-7")
    assert shown is not None
    assert [e["note"] for e in with_note(shown["events"])] == [SECRET, SECRET]
    read = shown["events"][-1]
    note_ids = [e["payload"]["note_ref"]["id"] for e in with_note(shown["events"])]
    assert read["event"] == "input.read"
    assert read["payload"] == {"reader": "rn-7", "notes": note_ids}  # one read: input and notes
    [rating] = store.feedback(reader="rn-7")
    assert (rating["note_status"], rating["note"]) == ("open", SECRET)
    assert store.verify(notes=True)["ok"] is True


def test_without_phi_mode_notes_are_read_as_inputs_are(db_target: str, inbox_pack: Pack) -> None:
    """PHI mode off: whoever may read the decision reads its notes, and nothing is chained."""
    store = keeping(db_target, "off")
    decision_id = reviewed(store, inbox_pack)
    item = store.get(decision_id)
    assert item is not None
    assert [e["note"] for e in with_note(item["events"])] == [SECRET, SECRET]
    assert "input.read" not in [e["event"] for e in item["events"]]
    assert [f["note"] for f in store.feedback()] == [SECRET]


def test_phi_mode_hash_refuses_a_note_and_keeps_nothing(db_target: str, inbox_pack: Pack) -> None:
    store = keeping(db_target, "hash")
    decision_id = Decision(inbox_pack, store=store)(message(URGENT)).id
    with pytest.raises(NoteError, match="keeps no free text"):
        store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1", note=SECRET)
    with pytest.raises(NoteError, match="keeps no free text"):
        store.record_feedback(decision_id, rating="down", note=SECRET)
    with pytest.raises(NoteError, match="keeps no free text"):
        store.record_event(decision_id, "pas.submitted", {"outcome": "complete"}, note=SECRET)
    assert SECRET not in stored(store)
    item = store.get(decision_id)
    assert item is not None and item["review_status"] == "pending"
    assert [e["event"] for e in item["events"]] == ["decision.created"]
    store.resolve_review(decision_id, final_label="nurse", reviewer="rn-1")  # without one: fine


def test_the_api_answers_a_note_under_hash_with_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", "hash")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decided = client.post(f"/api/agents/{INBOX}/decide", json=message(URGENT)).json()
    review = {"final_label": "nurse", "reviewer": "rn-1", "note": SECRET}
    refused = client.post(f"/api/review/{decided['id']}", json=review)
    assert refused.status_code == 422 and "keeps no free text" in refused.text
    assert SECRET not in refused.text
    rating = {"rating": "down", "note": SECRET}
    assert client.post(f"/api/audit/{decided['id']}/feedback", json=rating).status_code == 422
    assert client.post(f"/api/review/{decided['id']}", json=review | {"note": None}).is_success


def test_a_sealed_note_opens_only_as_itself(db_target: str, inbox_pack: Pack) -> None:
    """The associated data names a note's kind, id and decision: a ciphertext moved to another
    note, or between notes and inputs, does not open."""
    store = keeping(db_target)
    first, second = reviewed(store, inbox_pack), reviewed(store, inbox_pack, note="other")
    with store._db.session(exclusive=True) as db:
        notes = db.all("SELECT id, decision_id, sealed FROM notes ORDER BY created_at, event DESC")
        db.all("UPDATE notes SET sealed = ? WHERE id = ?", (notes[0]["sealed"], notes[2]["id"]))
    with pytest.raises(MendariError, match="a stored note does not decrypt"):
        store.get(second, reader="rn-7")
    assert store.verify(notes=True)["ok"] is False  # verify reports it too
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE notes SET sealed = ? WHERE id = ?", (notes[2]["sealed"], notes[2]["id"]))
        [kept] = db.all("SELECT ciphertext FROM decision_inputs WHERE decision_id = ?", (first,))
        db.all("UPDATE notes SET sealed = ? WHERE id = ?", (kept["ciphertext"], notes[0]["id"]))
        db.all(
            "UPDATE decision_inputs SET ciphertext = ? WHERE decision_id = ?",
            (notes[3]["sealed"], second),
        )
    with pytest.raises(MendariError, match="a stored note does not decrypt"):
        store.get(first, reader="rn-7")  # an input as a note
    with pytest.raises(MendariError, match="a stored input does not decrypt"):
        store.reveal_inputs([{"id": second, "record": {}}], "rn-7")  # a note as an input


def test_verify_notes_finds_a_note_changed_moved_or_removed(
    db_target: str, inbox_pack: Pack
) -> None:
    store = keeping(db_target, "off")
    decision_id = reviewed(store, inbox_pack)
    other = reviewed(store, inbox_pack)
    item = store.get(decision_id)
    assert item is not None
    review = with_note(item["events"])[0]
    note_id = review["payload"]["note_ref"]["id"]
    with store._db.session(exclusive=False) as db:
        [original] = db.all("SELECT * FROM notes WHERE id = ?", (note_id,))
    edited = original["sealed"].replace("SECRET-NOTE", "secret-note")
    for change, params in (
        ("UPDATE notes SET sealed = ? WHERE id = ?", (edited, note_id)),
        ("UPDATE notes SET decision_id = ? WHERE id = ?", (other, note_id)),
        ("DELETE FROM notes WHERE id = ?", (note_id,)),
        ("UPDATE notes SET sealed = NULL WHERE id = ?", (note_id,)),  # erased, but not chained
    ):
        with store._db.session(exclusive=True) as db:
            db.all(change, params)
        assert store.verify()["ok"] is True, change  # the chain alone cannot see it
        broken = store.verify(notes=True)
        assert (broken["ok"], broken["broken_at"]) == (False, review["seq"]), change
        with store._db.session(exclusive=True) as db:
            db.all("DELETE FROM notes WHERE id = ?", (note_id,))
            db.all(
                "INSERT INTO notes (id, decision_id, event, org, created_at, sealed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                tuple(
                    original[k]
                    for k in ("id", "decision_id", "event", "org", "created_at", "sealed")
                ),
            )
        assert store.verify(notes=True)["ok"] is True


def test_an_erased_note_is_gone_and_the_chain_still_verifies(
    db_target: str, inbox_pack: Pack
) -> None:
    store = keeping(db_target)
    decision_id = reviewed(store, inbox_pack)
    item = store.get(decision_id, reader="rn-7")
    assert item is not None
    review_note, rating_note = (e["payload"]["note_ref"]["id"] for e in with_note(item["events"]))
    erased = store.erase_note(review_note, erased_by="dpo@example.org", reason="request")
    assert erased["decision_id"] == decision_id
    item = store.get(decision_id, reader="rn-7")
    assert item is not None
    statuses = [(e["note_status"], e["note"]) for e in with_note(item["events"])]
    assert statuses == [("erased", None), ("open", SECRET)]
    chained = next(e for e in item["events"] if e["event"] == "note.erased")
    assert chained["payload"] == {
        "note_id": review_note,
        "reason": "request",
        "erased_by": "dpo@example.org",
    }
    store.erase_note(rating_note, erased_by="dpo@example.org", reason="error")
    assert SECRET not in stored(store)
    result = store.verify(notes=True)
    assert result["ok"] is True and result["notes"] == {"checked": 0, "erased": 2, "unchecked": 0}
    with pytest.raises(NoteError, match="no note"):
        store.erase_note(review_note, erased_by="dpo@example.org", reason="request")
    with pytest.raises(NoteError, match="the reason is one of"):
        store.erase_note(rating_note, erased_by="dpo@example.org", reason="because")
    with pytest.raises(NoteError, match="an identifier"):
        store.erase_note(rating_note, erased_by="Dana, the DPO", reason="request")
    erasures = [r for r in audit_events(store) if r["subtype"][0]["code"] == "note.erased"]
    shown = [(r["action"], r["agent"][0]["who"]["display"]) for r in erasures]
    assert shown == [("D", "dpo@example.org")] * 2
    assert {"type": "reason", "valueString": "request"} in erasures[0]["entity"][0]["detail"]


def test_another_organization_s_note_is_not_erased(db_target: str, inbox_pack: Pack) -> None:
    """A note is erased through its decision's organization: another one's is refused as an
    unknown note is, and nothing is erased or chained."""
    store = keeping(db_target)
    decision_id = reviewed(store, inbox_pack)  # the default organization's
    item = store.get(decision_id, reader="rn-7")
    assert item is not None
    review_note = next(e["payload"]["note_ref"]["id"] for e in with_note(item["events"]))
    before = store.verify()["events"]
    with tenancy.acting_for("clinic-b"), pytest.raises(NoteError, match="no note"):
        store.erase_note(review_note, erased_by="dpo@example.org", reason="request")
    assert store.verify()["events"] == before
    item = store.get(decision_id, reader="rn-7")
    assert item is not None
    assert with_note(item["events"])[0]["note_status"] == "open"
    with tenancy.acting_for(tenancy.DEFAULT_ORG):
        erased = store.erase_note(review_note, erased_by="dpo@example.org", reason="request")
    assert erased["decision_id"] == decision_id


def test_retention_erases_old_notes_with_the_inputs(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "a.db"
    store = keeping(path)
    old, new = reviewed(store, inbox_pack), reviewed(store, inbox_pack, note="recent")
    long_ago = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE notes SET created_at = ? WHERE decision_id = ?", (long_ago, old))
        db.all("UPDATE decision_inputs SET created_at = ? WHERE decision_id = ?", (long_ago, old))
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    assert main(["phi", "purge", "--db", str(path), "--days", "365"]) == 0
    assert "1 stored input(s) deleted and 2 note(s) erased" in capsys.readouterr().out
    assert SECRET not in stored(store)
    item = store.get(old, reader="rn-7")
    assert item is not None
    purged = [e["payload"] for e in item["events"] if e["event"] == "note.erased"]
    assert [(p["reason"], p["retention_days"]) for p in purged] == [("retention", 365)] * 2
    assert [e["note_status"] for e in with_note(item["events"])] == ["erased", "erased"]
    recent = store.get(new, reader="rn-7")
    assert recent is not None
    assert [e["note"] for e in with_note(recent["events"])] == ["recent", "recent"]
    by_retention = [r for r in audit_events(store) if r["subtype"][0]["code"] == "note.erased"]
    assert [r["agent"][0]["requestor"] for r in by_retention] == [False, False]  # the policy's
    assert main(["audit", "verify", "--db", str(path), "--notes"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["notes"] == {"checked": 2, "erased": 2, "unchecked": 0}


def test_the_cli_erases_a_note_and_verify_notes_fails_on_a_change(
    tmp_path: Path,
    inbox_pack: Pack,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "a.db"
    monkeypatch.delenv("MENDARI_PHI_MODE", raising=False)
    store = keeping(path, "off")
    decision_id = reviewed(store, inbox_pack)
    item = store.get(decision_id)
    assert item is not None
    note_id = with_note(item["events"])[0]["payload"]["note_ref"]["id"]
    erase = ["notes", "erase", "--db", str(path), "--id", note_id, "--reason", "request"]
    assert main([*erase, "--by", "dpo@example.org"]) == 0
    out = capsys.readouterr().out
    assert "backups, the database's history and earlier exports still hold it" in out
    assert main([*erase, "--by", "dpo@example.org"]) == 1  # already erased
    assert "no note" in capsys.readouterr().err
    assert main(["audit", "verify", "--db", str(path), "--notes"]) == 0
    with store._db.session(exclusive=True) as db:
        db.all("UPDATE notes SET sealed = replace(sealed, 'Rosalind', 'R.')")
    assert main(["audit", "verify", "--db", str(path)]) == 0  # the chain alone is intact
    assert main(["audit", "verify", "--db", str(path), "--notes"]) == 1


def test_a_sealed_note_without_the_key_is_unchecked_and_stays_sealed(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    """After a switch from encrypt to off, a sealed note can be neither read nor checked."""
    decision_id = reviewed(keeping(tmp_path / "a.db"), inbox_pack)
    switched = keeping(tmp_path / "a.db", "off")
    item = switched.get(decision_id, reader="rn-7")
    assert item is not None
    assert [e["note_status"] for e in with_note(item["events"])] == ["sealed", "sealed"]
    assert switched.verify(notes=True)["notes"] == {"checked": 0, "erased": 0, "unchecked": 2}


# -- notes chained before this stage -----------------------------------------------------------


def legacy(store: AuditStore, pack: Pack) -> str:
    """A decision reviewed, rated and sent for prior authorization as the code before Phase 4b
    recorded them: the free text in the chained payloads and the rating's `note` column."""
    decision_id = Decision(pack, store=store)(message(URGENT)).id
    with store._db.session(exclusive=True) as db:
        db.all(
            "UPDATE decisions SET review_status = 'resolved', final_label = 'nurse' WHERE id = ?",
            (decision_id,),
        )
        review = {"final_label": "nurse", "human_override": False, "reviewer": "rn-1"}
        store._append(db, decision_id, "review.resolved", {**review, "note": SECRET})
        db.all(
            "INSERT INTO feedback (id, decision_id, created_at, pack_id, rating, reporter, note) "
            "VALUES ('f-1', ?, '2026-09-01T00:00:00.000+00:00', ?, 'down', 'rn-2', ?)",
            (decision_id, pack.id, SECRET),
        )
        rating = {"feedback_id": "f-1", "rating": "down", "reporter": "rn-2", "note": SECRET}
        store._append(db, decision_id, "feedback.recorded", {**rating, "expected": None})
        answer = {"outcome": "complete", "authorization": "AUTH-4821937", "disposition": SECRET}
        store._append(db, decision_id, "pas.submitted", {**answer, "review_actions": ["A1"]})
    return decision_id


@pytest.mark.parametrize("mode", ["off", "encrypt"])
def test_notes_chained_before_are_shown_only_to_readers_of_inputs(
    db_target: str, inbox_pack: Pack, mode: PhiMode
) -> None:
    store = keeping(db_target, mode)
    decision_id = legacy(store, inbox_pack)
    hidden = store.get(decision_id)
    assert hidden is not None
    text = json.dumps(hidden)
    assert SECRET not in text and "AUTH-4821937" not in text
    assert [e["note_status"] for e in hidden["events"][1:]] == ["sealed"] * 3
    assert [f["note_status"] for f in store.feedback()] == ["sealed"]
    assert SECRET not in json.dumps(store.feedback())

    shown = store.get(decision_id, reader="rn-7")
    assert shown is not None
    review, rating, payer = shown["events"][1:4]
    assert review["note"] == rating["note"] == SECRET == review["payload"]["note"]
    assert json.loads(payer["note"]) == {"authorization": "AUTH-4821937", "disposition": SECRET}
    assert [f["note"] for f in store.feedback(reader="rn-7")] == [SECRET]
    reads = [e for e in shown["events"] if e["event"] == "input.read"]
    assert len(reads) == (1 if mode == "encrypt" else 0)  # off: no read is chained, as for inputs
    assert store.verify(notes=True)["ok"] is True  # the chain itself was never rewritten
    exported = json.dumps(list(audit_fhir.resources(store.iter_events())))
    assert "AUTH-4821937" not in exported and SECRET not in exported


# -- through the API, signed in ------------------------------------------------------------------


@pytest.fixture
def signed_in_app(
    db_target: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, AuditStore]]:
    monkeypatch.setenv("MENDARI_PHI_MODE", "encrypt")
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    monkeypatch.setenv("MENDARI_OIDC_ISSUER", "https://idp.example.org")
    monkeypatch.setenv("MENDARI_OIDC_CLIENT_ID", "mendari")
    with TestClient(create_app(db_path=db_target), base_url="https://mendari.test") as client:
        yield client, keeping(db_target)


def test_viewers_never_see_a_note_and_reviewers_do(
    signed_in_app: tuple[TestClient, AuditStore],
) -> None:
    client, store = signed_in_app
    viewer = store.create_api_key(name="viewer", role="viewer", org="default", created_by="t")
    machine = {"authorization": f"Bearer {viewer['key']}"}
    reviewer, admin = (signed_in(store, role=role) for role in ("reviewer", "admin"))
    decided = client.post(f"/api/agents/{INBOX}/decide", json=message(URGENT), headers=reviewer)
    decision_id = decided.json()["id"]
    review = {"final_label": "nurse", "reviewer": "ignored", "note": SECRET}
    resolved = client.post(f"/api/review/{decision_id}", json=review, headers=reviewer).json()
    assert resolved["note"] == SECRET  # answered to whoever wrote it
    rating = {"rating": "down", "note": SECRET}
    client.post(f"/api/audit/{decision_id}/feedback", json=rating, headers=reviewer)

    for url in (f"/api/audit/{decision_id}", "/api/feedback", f"/api/audit/{decision_id}/fhir"):
        assert SECRET not in client.get(url, headers=machine).text, url
    assert SECRET in client.get(f"/api/audit/{decision_id}", headers=reviewer).text
    assert SECRET in client.get("/api/feedback", headers=reviewer).text
    export = client.get(f"/api/agents/{INBOX}/feedback/export", headers=admin)
    [line] = [json.loads(row) for row in export.text.splitlines()]
    assert [f["note"] for f in line["feedback"]] == [SECRET]
    fhir = client.get("/api/audit/export/fhir", headers=admin)
    assert fhir.is_success and SECRET not in fhir.text
    assert SECRET not in stored(store)  # sealed in `notes`, nowhere in the clear
    item = client.get(f"/api/audit/{decision_id}", headers=machine).json()
    readers = {e["payload"]["reader"] for e in item["events"] if e["event"] == "input.read"}
    # The chain names readers by verified email (Principal.actor), never the viewer.
    assert readers == {"reviewer@default.example.org", "admin@default.example.org"}


# -- prior authorization: the payer's words are a note --------------------------------------------


def submit_prior_authorization(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    payer_says: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """A decided prior authorization request, submitted to a mocked payer (whose ClaimResponse
    `payer_says` may change); its decision id."""
    from mendari.sandbox import payer
    from tests.test_davinci import COMPLETE, PA, request
    from tests.test_pas import (
        COVERAGE,
        INSURER,
        PATIENT,
        REQUESTER,
        REVIEW_ACTION,
        site_settings,
    )
    from tests.test_prior_auth_pack import _order

    monkeypatch.setenv("MENDARI_PAS_PAYER_URL", "https://payer.example/fhir")
    site_settings(monkeypatch)  # the X12 codes: a site's settings (Mendari ships none)

    def payer_endpoint(http_request: httpx.Request) -> httpx.Response:
        answer = payer.respond(json.loads(http_request.content), review_action=REVIEW_ACTION)
        for entry in answer["entry"]:
            if entry["resource"]["resourceType"] == "ClaimResponse":
                entry["resource"]["preAuthRef"] = "AUTH-4821937"
                if payer_says is not None:
                    payer_says(entry["resource"])
        return httpx.Response(200, json=answer, headers={"content-type": "application/fhir+json"})

    transport = httpx.MockTransport(payer_endpoint)
    monkeypatch.setattr(
        app_module,
        "GuardedHttp",
        lambda **kw: GuardedHttp(transport=transport, allowed={"https://payer.example"}),
    )
    hook = f"/cds-services/{PA.replace('/', '.')}"
    signed = client.post(hook, json=request("acme", COMPLETE, _order("mri", "FX101"))).json()
    decision_id: str = signed["cards"][0]["extension"]["mendari.decision_id"]
    body = {
        "decision_id": decision_id,
        "patient": PATIENT,
        "coverage": COVERAGE,
        "insurer": INSURER,
        "requester": REQUESTER,
        "order": _order("mri", "FX101"),
        "service_type": "62",
        "place_of_service": "22",
        "submit": True,
    }
    # The decision went to a person (no model in the notes screen): one confirms it first.
    review = {"final_label": "submit_ready", "reviewer": "pa-nurse"}
    assert client.post(f"/api/review/{decision_id}", json=review).status_code == 200
    answered = client.post(f"/api/agents/{PA}/pas", json=body).json()
    assert answered["submitted"]["authorization"] == "AUTH-4821937"  # the caller gets it
    return decision_id


@pytest.mark.parametrize("mode", ["encrypt", "hash"])
def test_a_payers_authorization_number_is_in_no_payload_or_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: PhiMode
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", mode)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decision_id = submit_prior_authorization(client, monkeypatch)
    store = keeping(tmp_path / "a.db", mode)
    assert "AUTH-4821937" not in stored(store)  # sealed (encrypt) or not kept (hash)
    record = client.get(f"/api/audit/{decision_id}").json()
    [event] = [e for e in record["events"] if e["event"] == "pas.submitted"]
    assert set(event["payload"]) == {"outcome", "review_actions", "requested_by", "note_ref"}
    assert (event["payload"]["note_ref"] is None) is (mode == "hash")
    for url in (f"/api/audit/{decision_id}", "/api/audit/export/fhir"):
        assert "AUTH-4821937" not in client.get(url).text, url
    if mode == "encrypt":
        shown = store.get(decision_id, reader="rn-7")
        assert shown is not None
        [event] = [e for e in shown["events"] if e["event"] == "pas.submitted"]
        assert json.loads(event["note"])["authorization"] == "AUTH-4821937"


PAYER_TEXT = "approved for Rosalind Q. Example, MRN 4821937"


def payer_writes_text_for_codes(claim_response: dict[str, Any]) -> None:
    """A non-conformant payer: text in `outcome` and in a review action code."""
    claim_response["outcome"] = PAYER_TEXT
    for item in claim_response["item"]:
        for adjudication in item["adjudication"]:
            for extension in adjudication.get("extension") or []:
                for part in extension.get("extension") or []:
                    if part["url"].endswith("extension-reviewActionCode"):
                        part["valueCodeableConcept"]["coding"][0]["code"] = f"A1 {PAYER_TEXT}"


@pytest.mark.parametrize("mode", ["off", "hash", "encrypt"])
def test_a_payers_text_in_place_of_a_code_is_in_no_payload_or_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: PhiMode
) -> None:
    monkeypatch.setenv("MENDARI_PHI_MODE", mode)
    monkeypatch.setenv("MENDARI_INPUT_HASH_KEY", HASH_KEY_B64)
    monkeypatch.setenv("MENDARI_PHI_KEY", base64.b64encode(KEY).decode())
    client = TestClient(create_app(db_path=tmp_path / "a.db"))
    decision_id = submit_prior_authorization(client, monkeypatch, payer_writes_text_for_codes)
    assert "MRN 4821937" not in stored(keeping(tmp_path / "a.db", mode))
    record = client.get(f"/api/audit/{decision_id}").json()
    [event] = [e for e in record["events"] if e["event"] == "pas.submitted"]
    assert (event["payload"]["outcome"], event["payload"]["review_actions"]) == (None, [])
    for url in (f"/api/audit/{decision_id}", "/api/audit/export/fhir"):
        assert "MRN 4821937" not in client.get(url).text, url


def test_the_chain_keeps_only_codes_from_the_payer() -> None:
    def answer(outcome: Any, *codes: Any) -> dict[str, Any]:
        system = f"{pas.X12}/306"
        actions = [
            {"url": "x/extension-reviewActionCode", "valueCodeableConcept": {"coding": [c]}}
            for c in ({"system": system, "code": code} for code in codes)
        ]
        item = {
            "adjudication": [
                {"extension": [{"url": "x/extension-reviewAction", "extension": actions}]}
            ]
        }
        response = {"resourceType": "ClaimResponse", "outcome": outcome, "item": [item]}
        return pas.outcome({"resourceType": "Bundle", "entry": [{"resource": response}]})

    kept = answer("complete", "A1", "A3", "a1", "A1 text", "ABCD", "")
    assert (kept["outcome"], kept["review_actions"]) == ("complete", ["A1", "A3"])
    for code in ("partial", "queued", "error"):
        assert answer(code)["outcome"] == code
    for text in ("approved", "Complete", "x" * 2000, {"code": "complete"}, ["complete"], 1):
        assert answer(text)["outcome"] is None, text


def test_the_payers_words_are_capped_and_must_be_text() -> None:
    response = {
        "resourceType": "ClaimResponse",
        "outcome": "complete",
        "preAuthRef": {"not": "text"},
        "disposition": "x" * 5000,
    }
    kept = pas.outcome({"resourceType": "Bundle", "entry": [{"resource": response}]})
    assert kept["authorization"] is None and len(kept["disposition"]) == pas.DISPOSITION_MAX
    assert json.loads(pas.payer_note(kept) or "") == {
        "authorization": None,
        "disposition": "x" * pas.DISPOSITION_MAX,
    }
    assert pas.payer_note({"authorization": None, "disposition": None}) is None


# -- batched chain writes ---------------------------------------------------------------------


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """A clock that moves 1 ms per call, and restarts when called, so two chains written alike
    are byte-identical."""
    start = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    ticks = [count()]

    def now() -> str:
        return (start + timedelta(milliseconds=next(ticks[0]))).isoformat(timespec="milliseconds")

    def restart() -> None:
        ticks[0] = count()

    monkeypatch.setattr(audit_module, "_now", now)
    return restart


def chain(store: AuditStore) -> list[tuple[Any, ...]]:
    with store._db.session(exclusive=False) as db:
        rows = db.all("SELECT * FROM audit_events ORDER BY seq")
    keys = ("decision_id", "event", "payload", "created_at", "prev_hash", "hash")
    return [tuple(row[key] for key in keys) for row in rows]


def test_append_many_writes_the_chain_n_appends_write(
    db_target: str, tmp_path: Path, ticking: Callable[[], None]
) -> None:
    entries = [(f"d-{i}", {"reader": "rn-7", "n": i}) for i in range(7)]
    one_by_one = AuditStore(tmp_path / "single.db")
    batched = AuditStore(db_target)
    ticking()
    with one_by_one._db.session(exclusive=True) as db:
        for decision_id, payload in entries:
            one_by_one._append(db, decision_id, "input.read", payload)
    ticking()
    with batched._db.session(exclusive=True) as db:
        written = batched._append_many(db, "input.read", entries)
    assert chain(batched) == chain(one_by_one)
    seqs = [event["seq"] for event in written]
    assert [event["decision_id"] for event in written] == [d for d, _ in entries]
    assert seqs == sorted(seqs) and len(set(seqs)) == 7  # seq order is insertion order
    assert batched.verify()["ok"] is True


class _Counted(Session):
    def __init__(self, inner: Session, seen: list[str]):
        self.inner, self.seen = inner, seen
        self.exclusive = inner.exclusive  # the chain is appended to in an exclusive one only

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self.seen.append(sql)
        return self.inner.all(sql, params)

    def columns(self, table: str) -> set[str]:
        return self.inner.columns(table)


def test_a_page_of_reads_takes_the_lock_for_two_round_trips(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review queue under encrypt: one `input.read` per decision, written with one read of
    the chain's head and one insert, not two statements per decision."""
    store = keeping(db_target)
    decide = Decision(inbox_pack, store=store)
    ids = [decide(message(URGENT)).id for _ in range(6)]
    backend = store._db
    real = backend.session
    locked: list[str] = []

    @contextmanager
    def session(*, exclusive: bool) -> Iterator[Session]:
        with real(exclusive=exclusive) as db:
            yield _Counted(db, locked) if exclusive else db

    monkeypatch.setattr(backend, "session", session)
    queue = store.pending_reviews(reader="rn-7")
    assert {item["id"] for item in queue} == set(ids)
    assert len(locked) == 2, locked
    with real(exclusive=False) as db:
        reads = db.all("SELECT decision_id FROM audit_events WHERE event = 'input.read'")
    assert sorted(r["decision_id"] for r in reads) == sorted(ids)
    assert store.verify()["ok"] is True
