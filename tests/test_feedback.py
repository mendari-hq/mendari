# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, Pack
from mendari.cli import main
from mendari.errors import ReviewError
from mendari.evals import load_dataset
from mendari.evals.feedback import feedback_dataset
from mendari.evals.harness import evaluate
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR, LOCAL_DATASET, ROOT, message

# destination = medical_assistant; AUTO when a model screens urgency (`screened`)
AUTO_REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."
FRONT_DESK = {"question": "destination", "label": "front_desk"}


def test_thumbs_are_recorded_and_chained(
    inbox_pack: Pack, store: AuditStore, screened: dict[str, str]
) -> None:
    decision = Decision(inbox_pack, providers=screened, store=store)
    good = decision(message(AUTO_REFILL))
    bad = decision(message("Can someone call me back?"))
    store.record_feedback(good.id, rating="up", reporter="rn-1")
    store.record_feedback(bad.id, rating="down", reporter="rn-1", note="should go to nurse")

    down = store.feedback(pack_id=inbox_pack.id, rating="down")
    assert [(f["decision_id"], f["note"]) for f in down] == [(bad.id, "should go to nurse")]
    assert down[0]["record"]["input"]["message"]["body"] == "Can someone call me back?"
    assert store.get(bad.id)["events"][-1]["event"] == "feedback.recorded"  # type: ignore[index]
    assert store.verify()["ok"] is True
    assert store.stats()["feedback"] == {
        "up": 1,
        "down": 1,
        "down_on_auto": 0,
        "down_with_expected": 0,
    }


def test_thumbs_down_on_an_automated_decision_is_confidently_wrong(
    inbox_pack: Pack, store: AuditStore, screened: dict[str, str]
) -> None:
    result = Decision(inbox_pack, providers=screened, store=store)(message(AUTO_REFILL))
    assert result.outcome.value == "AUTO"
    store.record_feedback(result.id, rating="down")
    assert store.stats()["feedback"]["down_on_auto"] == 1


def test_feedback_validation(inbox_pack: Pack, store: AuditStore) -> None:
    result = Decision(inbox_pack, store=store)(message(AUTO_REFILL))
    with pytest.raises(ReviewError, match="unknown decision"):
        store.record_feedback("nope", rating="down")
    with pytest.raises(ReviewError, match="rating"):
        store.record_feedback(result.id, rating="meh")  # type: ignore[arg-type]


def test_export_is_a_dataset_to_label(inbox_pack: Pack, store: AuditStore, tmp_path: Path) -> None:
    decision = Decision(inbox_pack, store=store)
    result = decision(message("I just started to vomit a lot of blood"))
    store.record_feedback(result.id, rating="down", note="missed emergency")
    store.record_feedback(result.id, rating="down", reporter="rn-2", note="second reviewer agrees")
    hashed = Decision(inbox_pack)(message("text we must not export"))
    store.record(hashed, message("text we must not export"), store_input="hash")
    store.record_feedback(hashed.id, rating="down")

    records, skipped = feedback_dataset(store, pack_id=inbox_pack.id)
    assert skipped == 1 and len(records) == 1  # one line per decision, notes collected
    record = records[0]
    assert record["labels"] == {} and record["input"]["message"]["body"].startswith("I just")
    assert [f["note"] for f in record["feedback"]] == ["second reviewer agrees", "missed emergency"]
    path = tmp_path / "down.jsonl"
    path.write_text(json.dumps(record) + "\n")
    assert load_dataset(path).records[0]["id"] == record["id"]  # loads as a pack dataset


def test_feedback_api_and_export(db_target: str, screened: dict[str, str]) -> None:
    app = create_app(db_path=db_target, providers=screened, datasets_dir=LOCAL_DATASET.parent)
    client = TestClient(app)
    pack = "/api/packs/mendari/patient-inbox-router"
    decision = client.post(f"{pack}/decide", json=message(AUTO_REFILL)).json()

    missing = client.post(f"/api/audit/{decision['id']}/feedback", json={"rating": "sideways"})
    assert missing.status_code == 422
    recorded = client.post(
        f"/api/audit/{decision['id']}/feedback",
        json={"rating": "down", "reporter": "rn-1", "note": "wrong team"},
    )
    assert recorded.status_code == 200 and recorded.json()["rating"] == "down"
    assert client.post("/api/audit/unknown/feedback", json={"rating": "up"}).status_code == 409
    rate = f"/api/audit/{decision['id']}/feedback"
    assert client.post(rate, json={"rating": "up", "expected": FRONT_DESK}).status_code == 422
    not_a_label = {"rating": "down", "expected": {"question": "destination", "label": "janitor"}}
    assert client.post(rate, json=not_a_label).status_code == 409
    corrected = client.post(
        rate, json={"rating": "down", "reporter": "rn-2", "expected": FRONT_DESK}
    ).json()
    assert corrected["expected"] == FRONT_DESK and corrected["replaces"] is None

    listed = client.get("/api/feedback", params={"rating": "down"}).json()
    assert [f["decision_id"] for f in listed] == [decision["id"], decision["id"]]
    assert [f["expected"] for f in listed] == [FRONT_DESK, None]  # newest first
    feedback = client.get("/api/stats").json()["feedback"]
    assert feedback["down_on_auto"] == 2 and feedback["down_with_expected"] == 1

    export = client.get(f"{pack}/feedback/export")
    assert export.headers["content-type"].startswith("application/x-ndjson")
    assert export.headers["x-mendari-skipped"] == "0"
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert lines[0]["feedback"][1]["note"] == "wrong team"
    assert lines[0]["labels"] == {"destination": "front_desk"}


def test_cli_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        result = Decision.from_path(INBOX_DIR, store=store)(message(AUTO_REFILL))
        store.record_feedback(result.id, rating="down", expected=FRONT_DESK)
    out = tmp_path / "down.jsonl"
    assert main(["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 rated decisions" in printed and "1 already carry the right answer" in printed
    assert "Fill in" not in printed
    assert len(out.read_text().splitlines()) == 1


# -- the right answer on a thumbs-down --------------------------------------------------------


def test_a_thumbs_down_can_carry_the_right_answer(inbox_pack: Pack, store: AuditStore) -> None:
    result = Decision(inbox_pack, store=store)(message(AUTO_REFILL))
    assert result.recommendation == "medical_assistant"
    recorded = store.record_feedback(result.id, rating="down", reporter="rn-1", expected=FRONT_DESK)
    assert recorded["expected"] == FRONT_DESK
    assert store.feedback(pack_id=inbox_pack.id)[0]["expected"] == FRONT_DESK
    event = store.get(result.id)["events"][-1]  # type: ignore[index]
    assert event["event"] == "feedback.recorded" and event["payload"]["expected"] == FRONT_DESK
    assert store.stats()["feedback"]["down_with_expected"] == 1
    assert store.verify()["ok"] is True


@pytest.mark.parametrize(
    ("rating", "expected", "error"),
    [
        ("up", FRONT_DESK, "only a thumbs-down"),
        ("down", {"question": "priority", "label": "stat"}, "not a question this decision"),
        ("down", {"question": "destination", "label": "janitor"}, "not a label of destination"),
        ("down", {"question": "destination", "label": "medical_assistant"}, "already answered"),
    ],
)
def test_the_right_answer_is_checked_against_the_decision(
    inbox_pack: Pack, store: AuditStore, rating: str, expected: dict[str, str], error: str
) -> None:
    result = Decision(inbox_pack, store=store)(message(AUTO_REFILL))
    with pytest.raises(ReviewError, match=error):
        store.record_feedback(result.id, rating=rating, expected=expected)  # type: ignore[arg-type]
    assert store.feedback() == []  # nothing half-written


def test_a_changed_rating_counts_once(
    inbox_pack: Pack, store: AuditStore, screened: dict[str, str]
) -> None:
    """Each person's latest rating counts; the hash chain keeps every rating."""
    result = Decision(inbox_pack, providers=screened, store=store)(message(AUTO_REFILL))
    first = store.record_feedback(result.id, rating="down", reporter="rn-1", expected=FRONT_DESK)
    changed = store.record_feedback(result.id, rating="up", reporter="rn-1")
    store.record_feedback(result.id, rating="down", reporter="rn-2")  # someone else
    store.record_feedback(result.id, rating="down")  # a mis-click, unnamed...
    store.record_feedback(result.id, rating="up")  # ...put right: unnamed is one person

    assert changed["replaces"] == first["id"]
    assert store.stats()["feedback"] == {
        "up": 2,
        "down": 1,
        "down_on_auto": 1,
        "down_with_expected": 0,
    }
    # One current rating per person (ratings in the same millisecond have no fixed order).
    assert {(f["reporter"], f["rating"]) for f in store.feedback()} == {
        (None, "up"),
        ("rn-2", "down"),
        ("rn-1", "up"),
    }
    assert len(store.feedback()) == 3
    events = [e for e in store.get(result.id)["events"] if e["event"] == "feedback.recorded"]  # type: ignore[index]
    assert len(events) == 5 and store.verify()["ok"] is True


def test_the_export_labels_what_reporters_agree_on(
    inbox_pack: Pack, store: AuditStore, tmp_path: Path
) -> None:
    decision = Decision(inbox_pack, store=store)
    agreed, disputed, unanswered = (decision(message(AUTO_REFILL)) for _ in range(3))
    for reporter in ("rn-1", "rn-2"):
        store.record_feedback(agreed.id, rating="down", reporter=reporter, expected=FRONT_DESK)
    store.record_feedback(disputed.id, rating="down", reporter="rn-1", expected=FRONT_DESK)
    billing = {"question": "destination", "label": "billing"}
    store.record_feedback(disputed.id, rating="down", reporter="rn-2", expected=billing)
    store.record_feedback(unanswered.id, rating="down", reporter="rn-1")

    records, _ = feedback_dataset(store, pack_id=inbox_pack.id)
    labels = {r["attributes"]["decision_id"]: r["labels"] for r in records}
    assert labels == {agreed.id: {"destination": "front_desk"}, disputed.id: {}, unanswered.id: {}}
    dispute = next(r for r in records if r["attributes"]["decision_id"] == disputed.id)
    assert sorted(f["expected"]["label"] for f in dispute["feedback"]) == ["billing", "front_desk"]

    # The payoff: a labelled thumbs-down goes straight into an evaluation.
    path = tmp_path / "wrong.jsonl"
    ready = next(r for r in records if r["labels"])
    path.write_text(json.dumps(ready) + "\n")
    report = evaluate(Decision(inbox_pack), load_dataset(path))
    assert report["questions"]["destination"]["n"] == 1
    assert report["questions"]["destination"]["accuracy"] == 0.0  # the pack still gets it wrong


def test_a_check_of_a_derived_decision_can_be_the_right_answer(
    store: AuditStore, tmp_path: Path
) -> None:
    """AI Guard: "Which check was wrong?" labels that check alone, and the record still
    evaluates (a dataset may label any subset of questions)."""
    guard = Decision.from_path(ROOT / "packs" / "ai-guard", store=store)
    result = guard(
        {
            "question": "I have heavy pressure in my chest and I'm short of breath right now.",
            "draft": "Try to rest and we will check in with you tomorrow.",
        }
    )
    answered = result.answers["unsafe_advice"].label
    other = next(label for label in ("yes", "no") if label != answered)
    expected = {"question": "unsafe_advice", "label": other}
    store.record_feedback(result.id, rating="down", reporter="rn-1", expected=expected)

    records, _ = feedback_dataset(store, pack_id=guard.pack.id)
    assert records[0]["labels"] == {"unsafe_advice": other}
    path = tmp_path / "guard.jsonl"
    path.write_text(json.dumps(records[0]) + "\n")
    report = evaluate(Decision.from_path(ROOT / "packs" / "ai-guard"), load_dataset(path))
    assert report["questions"]["unsafe_advice"]["n"] == 1


# -- databases from before this release ---------------------------------------------------------

OLD_FEEDBACK = """CREATE TABLE feedback (
    id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, created_at TEXT NOT NULL,
    pack_id TEXT NOT NULL, rating TEXT NOT NULL, reporter TEXT, note TEXT)"""


def test_an_older_database_gains_the_columns_and_counts_each_persons_latest_rating(
    db_target: str,
) -> None:
    rows = [
        ("f1", "d1", "2026-09-01T10:00:00.000+00:00", "rn-1"),
        ("f2", "d1", "2026-09-02T10:00:00.000+00:00", "rn-1"),  # rn-1 again: replaces f1
        ("f3", "d1", "2026-09-03T10:00:00.000+00:00", None),
        ("f4", "d2", "2026-09-01T10:00:00.000+00:00", "rn-1"),
    ]
    insert = "INSERT INTO feedback VALUES (?, ?, ?, 'p', 'down', ?, NULL)"
    if db_target.startswith("postgresql"):
        import psycopg

        with psycopg.connect(db_target, autocommit=True) as connection:
            connection.execute(OLD_FEEDBACK)
            for row in rows:
                connection.execute(insert.replace("?", "%s"), row)
    else:
        import sqlite3

        # closing(): a sqlite3 connection's own `with` commits but never closes.
        with closing(sqlite3.connect(db_target)) as connection:
            connection.execute(OLD_FEEDBACK)
            connection.executemany(insert, rows)
            connection.commit()

    for _ in range(2):  # the second start finds nothing to do
        with AuditStore(db_target) as store, store._db.session(exclusive=False) as db:
            assert {"expected_question", "expected_label", "superseded_by"} <= db.columns(
                "feedback"
            )
            migrated = {r["id"]: r["superseded_by"] for r in db.all("SELECT * FROM feedback")}
        assert migrated == {"f1": "f2", "f2": None, "f3": None, "f4": None}


def test_audit_rows_carry_each_decisions_current_ratings(
    inbox_pack: Pack, store: AuditStore
) -> None:
    decision = Decision(inbox_pack, store=store)
    rated, unrated = decision(message(AUTO_REFILL)), decision(message(AUTO_REFILL))
    store.record_feedback(rated.id, rating="down", reporter="rn-1")
    store.record_feedback(rated.id, rating="up", reporter="rn-1")  # changed: counts once
    store.record_feedback(rated.id, rating="down", reporter="rn-2")
    rows = {r["id"]: (r["feedback_up"], r["feedback_down"]) for r in store.query()}
    assert rows == {rated.id: (1, 1), unrated.id: (0, 0)}
