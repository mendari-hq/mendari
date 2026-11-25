# SPDX-License-Identifier: Apache-2.0
"""What reviewers decide, as labelled cases (`evals/feedback.py` `reviewed_dataset`): a decision
with a resolved review or a recorded outcome becomes a pack-dataset record, its label the one a
person chose (or what happened downstream). Nothing here changes a pack."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.core import audit, tenancy
from mendari.evals import load_dataset
from mendari.evals.feedback import reviewed_dataset
from mendari.evals.harness import evaluate, record_problems
from mendari.server.app import create_app
from tests.conftest import (
    INBOX_DIR,
    LOCAL_DATASET,
    encrypt_mode,
    encrypted_store,
    message,
    signed_in,
)
from tests.test_hosted_data import sign_in_env

REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."
CALL_BACK = "Can someone call me back?"


def test_a_reviewed_decision_is_a_labelled_case(
    inbox_pack: Pack, store: AuditStore, tmp_path: Path
) -> None:
    decision = Decision(inbox_pack, store=store)
    kept = decision(message(REFILL))  # the pack suggests the medical assistant
    corrected = decision(message(CALL_BACK))
    assert kept.recommendation == "medical_assistant"
    store.resolve_review(kept.id, final_label="medical_assistant", reviewer="rn-1")
    store.resolve_review(corrected.id, final_label="nurse", reviewer="rn-1")

    records, skipped = reviewed_dataset(store, pack_id=inbox_pack.id)
    assert skipped == 0
    by_decision = {record["attributes"]["decision_id"]: record for record in records}
    assert set(by_decision) == {kept.id, corrected.id}
    case = by_decision[corrected.id]
    assert case["id"] == f"reviewed-{corrected.id}"
    assert case["input"] == message(CALL_BACK)
    assert case["labels"] == {"destination": "nurse"}
    assert case["attributes"]["source"] == "review"
    assert case["review"]["label"] == "nurse"
    assert case["review"]["overridden"] is (corrected.recommendation != "nurse")
    assert by_decision[kept.id]["review"]["overridden"] is False
    assert case["system"]["recommendation"] == corrected.recommendation
    assert case["system"]["pack_version"] == inbox_pack.version

    # The payoff: the file is a pack dataset, ready to evaluate and to learn from.
    path = tmp_path / "reviewed.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    dataset = load_dataset(path)
    assert record_problems(inbox_pack.manifest, dataset) == []
    assert evaluate(Decision(inbox_pack), dataset)["questions"]["destination"]["n"] == 2


def test_a_decision_still_awaiting_review_is_not_a_case(
    inbox_pack: Pack, store: AuditStore
) -> None:
    Decision(inbox_pack, store=store)(message(CALL_BACK))
    assert reviewed_dataset(store, pack_id=inbox_pack.id) == ([], 0)


def test_what_happened_downstream_is_the_label_when_it_is_known(
    inbox_pack: Pack, store: AuditStore
) -> None:
    result = Decision(inbox_pack, store=store)(message(CALL_BACK))
    store.resolve_review(result.id, final_label="front_desk", reviewer="fd-1")
    store.record_outcome(result.id, final_label="nurse", source="staff-reroute", recorded_by="rn-9")

    (record,), _ = reviewed_dataset(store, pack_id=inbox_pack.id)
    assert record["labels"] == {"destination": "nurse"}
    assert record["attributes"]["source"] == "outcome"
    assert record["review"]["label"] == "front_desk"  # what the reviewer chose stays visible
    assert record["outcome"] == {"label": "nurse"}


def test_an_automated_decision_with_an_outcome_is_a_case(
    inbox_pack: Pack, store: AuditStore, screened: dict[str, str]
) -> None:
    result = Decision(inbox_pack, providers=screened, store=store)(message(REFILL))
    assert result.outcome.value == "AUTO"
    assert reviewed_dataset(store, pack_id=inbox_pack.id) == ([], 0)  # nobody has said anything
    store.record_outcome(result.id, final_label="front_desk", source="ehr", recorded_by="fd-1")

    (record,), _ = reviewed_dataset(store, pack_id=inbox_pack.id)
    assert record["labels"] == {"destination": "front_desk"}
    assert record["attributes"]["source"] == "outcome" and "review" not in record


def test_a_decision_kept_as_a_hash_is_counted_and_not_exported(
    inbox_pack: Pack, store: AuditStore
) -> None:
    hashed = Decision(inbox_pack)(message("text we must not export"))
    store.record(hashed, message("text we must not export"), store_input="hash")
    store.resolve_review(hashed.id, final_label="nurse", reviewer="rn-1")
    assert reviewed_dataset(store, pack_id=inbox_pack.id) == ([], 1)


def test_a_case_names_no_person(inbox_pack: Pack, store: AuditStore) -> None:
    result = Decision(inbox_pack, store=store)(message(CALL_BACK))
    store.resolve_review(result.id, final_label="nurse", reviewer="rn-1", note="called the patient")
    store.record_outcome(result.id, final_label="nurse", source="ehr", recorded_by="fd-7")
    (record,), _ = reviewed_dataset(store, pack_id=inbox_pack.id)
    written = json.dumps(record)
    assert "rn-1" not in written and "fd-7" not in written and "called the patient" not in written


def test_a_case_says_it_was_exported(inbox_pack: Pack, store: AuditStore) -> None:
    """It is a site's case wherever the file goes, so it says what it is: the mark that keeps
    a pack that holds it out of an archive (`core/site_cases.py`), whoever gave its label."""
    decision = Decision(inbox_pack, store=store)
    reviewed, happened = decision(message(CALL_BACK)), decision(message(REFILL))
    for result in (reviewed, happened):
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
    store.record_outcome(happened.id, final_label="nurse", source="ehr", recorded_by="fd-7")
    records, _ = reviewed_dataset(store, pack_id=inbox_pack.id)
    assert {record["attributes"]["decision_id"]: record["attributes"] for record in records} == {
        reviewed.id: {
            "source": "review",
            "decision_id": reviewed.id,
            "exported_by": "mendari feedback export",
        },
        happened.id: {
            "source": "outcome",
            "decision_id": happened.id,
            "exported_by": "mendari feedback export",
        },
    }


def test_only_the_packs_own_cases_and_the_newest_first(store: AuditStore) -> None:
    inbox = Decision.from_path(INBOX_DIR, store=store)
    first = inbox(message(CALL_BACK))
    second = inbox(message(REFILL))
    for result in (first, second):
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
    records, _ = reviewed_dataset(store, pack_id=inbox.pack.id, limit=1)
    assert [record["attributes"]["decision_id"] for record in records] == [second.id]
    assert reviewed_dataset(store, pack_id="mendari/referral-router") == ([], 0)


def test_an_organization_exports_only_its_own_cases(inbox_pack: Pack, store: AuditStore) -> None:
    decision = Decision(inbox_pack, store=store)
    made = {}
    for org in ("north", "south"):
        with tenancy.acting_for(org):
            made[org] = decision(message(CALL_BACK))
            store.resolve_review(made[org].id, final_label="nurse", reviewer="rn-1")
    with tenancy.acting_for("north"):
        records, _ = reviewed_dataset(store, pack_id=inbox_pack.id)
    assert [record["attributes"]["decision_id"] for record in records] == [made["north"].id]


def test_a_sealed_input_is_exported_only_to_a_reader_and_the_read_is_chained(
    inbox_pack: Pack, db_target: str
) -> None:
    with encrypted_store(db_target) as store:  # PHI mode `encrypt`
        result = Decision(inbox_pack, store=store)(message(CALL_BACK))
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")

        assert reviewed_dataset(store, pack_id=inbox_pack.id) == ([], 1)  # no reader: sealed
        events = [event["event"] for event in store.get(result.id)["events"]]  # type: ignore[index]
        assert "input.read" not in events

        (record,), skipped = reviewed_dataset(store, pack_id=inbox_pack.id, reader="rn-1")
        assert skipped == 0 and record["input"] == message(CALL_BACK)
        read = [e for e in store.get(result.id)["events"] if e["event"] == "input.read"]  # type: ignore[index]
        assert len(read) == 1 and read[0]["payload"]["reader"] == "rn-1"
        assert store.verify()["ok"] is True


def test_many_decisions_are_opened_a_part_at_a_time(
    inbox_pack: Pack, db_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database binds only so many values in one statement, so inputs are opened in parts:
    every one is exported, and each read is chained once."""
    monkeypatch.setattr(audit, "_REVEALED_AT_ONCE", 2)
    with encrypted_store(db_target) as store:
        decision = Decision(inbox_pack, store=store)
        made = [decision(message(f"{CALL_BACK} It is my request number {n}.")) for n in range(5)]
        for result in made:
            store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
        records, skipped = reviewed_dataset(store, pack_id=inbox_pack.id, reader="rn-1")
        assert skipped == 0
        assert {r["attributes"]["decision_id"] for r in records} == {r.id for r in made}
        for result in made:
            events = [e["event"] for e in store.get(result.id)["events"]]  # type: ignore[index]
            assert events.count("input.read") == 1
        assert store.verify()["ok"] is True


# -- the command and the route -------------------------------------------------------------------


def test_the_command_exports_one_organizations_decisions_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a pack learns from is one site's: without --org the command line reads every
    organization's decisions, as every other command does."""
    db = tmp_path / "audit.db"
    made = {}
    with AuditStore(db) as store:
        decision = Decision.from_path(INBOX_DIR, store=store)
        for org in ("north", "south"):
            with tenancy.acting_for(org):
                made[org] = decision(message(CALL_BACK))
                store.resolve_review(made[org].id, final_label="nurse", reviewer="rn-1")
    out = tmp_path / "reviewed.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    argv += ["--source", "reviews"]

    assert main([*argv, "--org", "north"]) == 0
    assert "1 reviewed decision ->" in capsys.readouterr().out
    (line,) = out.read_text().splitlines()
    assert json.loads(line)["attributes"]["decision_id"] == made["north"].id

    assert main(argv) == 0
    assert "2 reviewed decisions ->" in capsys.readouterr().out


def test_the_command_exports_reviewed_decisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        decision = Decision.from_path(INBOX_DIR, store=store)
        result = decision(message(CALL_BACK))
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
        decision(message(REFILL))  # still awaiting review
    out = tmp_path / "reviewed.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", "reviews"]) == 0
    printed = capsys.readouterr().out
    assert f"1 reviewed decision -> {out}" in printed
    assert "the label a person chose" in printed and "mendari eval --dataset" in printed
    assert "mendari improve --with" in printed
    (line,) = out.read_text().splitlines()
    assert json.loads(line)["labels"] == {"destination": "nurse"}


def test_the_command_says_when_nothing_is_reviewed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        Decision.from_path(INBOX_DIR, store=store)(message(CALL_BACK))
    out = tmp_path / "reviewed.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", "reviews"]) == 0
    printed = capsys.readouterr().out
    assert f"0 reviewed decisions -> {out}" in printed and "mendari eval" not in printed


def test_the_command_says_when_it_stopped_at_its_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        decision = Decision.from_path(INBOX_DIR, store=store)
        for body in (CALL_BACK, REFILL):
            store.resolve_review(decision(message(body)).id, final_label="nurse", reviewer="rn-1")
        hashed = Decision.from_path(INBOX_DIR)(message("kept as a hash"))
        store.record(hashed, message("kept as a hash"), store_input="hash")
        store.resolve_review(hashed.id, final_label="nurse", reviewer="rn-1")
    out = tmp_path / "reviewed.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    argv += ["--source", "reviews"]

    assert main([*argv, "--limit", "2"]) == 0  # the newest two: one of them kept as a hash
    printed = capsys.readouterr().out
    assert "1 reviewed decision ->" in printed and "(1 skipped: no input to export" in printed
    assert "the newest 2: raise --limit for more" in printed
    assert main(argv) == 0
    assert "raise --limit" not in capsys.readouterr().out
    for limit in ("0", "100001"):
        assert main([*argv, "--limit", limit]) == 1
        assert "--limit is 1 to 100,000" in capsys.readouterr().err


def test_a_rating_filter_with_reviews_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db):
        pass
    out = tmp_path / "reviewed.jsonl"
    argv = ["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]
    assert main([*argv, "--source", "reviews", "--rating", "up"]) == 1
    assert "--rating" in capsys.readouterr().err and not out.exists()


def test_only_an_administrator_exports_and_only_their_organizations_cases(
    tmp_path: Path, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sign-in on (so PHI mode `encrypt`): the route is an administrator's, as the ratings
    export is, and answers with its own organization's cases, each read logged and chained."""
    db = tmp_path / "a.db"
    made = {}
    with encrypted_store(db) as store:
        decision = Decision(inbox_pack, store=store)
        for org in ("default", "elsewhere"):
            with tenancy.acting_for(org):
                made[org] = decision(message(CALL_BACK))
                store.resolve_review(made[org].id, final_label="nurse", reviewer="rn-1")
        encrypt_mode(monkeypatch)
        sign_in_env(monkeypatch)
        client = TestClient(create_app(db_path=db), base_url="https://mendari.test")
        url = f"/api/packs/{inbox_pack.id}/feedback/export"
        for role in ("viewer", "reviewer", "governance"):
            refused = client.get(
                url, params={"source": "reviews"}, headers=signed_in(store, role=role)
            )
            assert refused.status_code == 403, role

        export = client.get(
            url, params={"source": "reviews"}, headers=signed_in(store, role="admin")
        )
        assert export.status_code == 200
        (line,) = export.text.splitlines()
        assert json.loads(line)["attributes"]["decision_id"] == made["default"].id
        assert json.loads(line)["input"] == message(CALL_BACK)
        read = [e for e in store.get(made["default"].id)["events"] if e["event"] == "input.read"]  # type: ignore[index]
        assert [event["payload"]["reader"] for event in read] == ["admin@default.example.org"]
        with tenancy.acting_for("elsewhere"):
            theirs = store.get(made["elsewhere"].id)["events"]  # type: ignore[index]
        assert "input.read" not in [event["event"] for event in theirs]


def test_the_route_exports_reviewed_decisions(db_target: str) -> None:
    app = create_app(db_path=db_target, datasets_dir=LOCAL_DATASET.parent)
    client = TestClient(app)
    pack = "/api/packs/mendari/patient-inbox-router"
    decision = client.post(f"{pack}/decide", json=message(CALL_BACK)).json()
    assert client.get(f"{pack}/feedback/export", params={"source": "reviews"}).text == ""
    reviewed = client.post(
        f"/api/review/{decision['id']}", json={"final_label": "nurse", "reviewer": "rn-1"}
    )
    assert reviewed.status_code == 200

    export = client.get(f"{pack}/feedback/export", params={"source": "reviews"})
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("application/x-ndjson")
    assert export.headers["x-mendari-skipped"] == "0"
    assert "patient-inbox-router-reviewed.jsonl" in export.headers["content-disposition"]
    (line,) = export.text.splitlines()
    assert json.loads(line)["labels"] == {"destination": "nurse"}

    assert client.get(f"{pack}/feedback/export", params={"source": "thumbs"}).status_code == 422
    both = client.get(f"{pack}/feedback/export", params={"source": "reviews", "rating": "up"})
    assert both.status_code == 422
