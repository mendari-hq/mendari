from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openhealth import AuditStore, Decision, Pack
from openhealth.cli import main
from openhealth.errors import ReviewError
from openhealth.evals import load_dataset
from openhealth.evals.feedback import feedback_dataset
from openhealth.server.app import create_app
from tests.conftest import INBOX_DIR, LOCAL_DATASET, message

AUTO_REFILL = "Hi, I need a refill on my metoprolol. I have about a week's worth of pills left."


def test_thumbs_are_recorded_and_chained(inbox_pack: Pack, store: AuditStore) -> None:
    decision = Decision(inbox_pack, store=store)
    good = decision(message(AUTO_REFILL))
    bad = decision(message("Can someone call me back?"))
    store.record_feedback(good.id, rating="up", reporter="rn-1")
    store.record_feedback(bad.id, rating="down", reporter="rn-1", note="should go to nurse")

    down = store.feedback(pack_id=inbox_pack.id, rating="down")
    assert [(f["decision_id"], f["note"]) for f in down] == [(bad.id, "should go to nurse")]
    assert down[0]["record"]["input"]["message"]["body"] == "Can someone call me back?"
    assert store.get(bad.id)["events"][-1]["event"] == "feedback.recorded"  # type: ignore[index]
    assert store.verify()["ok"] is True
    assert store.stats()["feedback"] == {"up": 1, "down": 1, "down_on_auto": 0}


def test_thumbs_down_on_an_automated_decision_is_confidently_wrong(
    inbox_pack: Pack, store: AuditStore
) -> None:
    result = Decision(inbox_pack, store=store)(message(AUTO_REFILL))
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
    store.record_feedback(result.id, rating="down", note="second reviewer agrees")
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


def test_feedback_api_and_export(db_target: str) -> None:
    client = TestClient(create_app(db_path=db_target, datasets_dir=LOCAL_DATASET.parent))
    pack = "/api/packs/openhealth/patient-inbox-router"
    decision = client.post(f"{pack}/decide", json=message(AUTO_REFILL)).json()

    missing = client.post(f"/api/audit/{decision['id']}/feedback", json={"rating": "sideways"})
    assert missing.status_code == 422
    recorded = client.post(
        f"/api/audit/{decision['id']}/feedback",
        json={"rating": "down", "reporter": "rn-1", "note": "wrong team"},
    )
    assert recorded.status_code == 200 and recorded.json()["rating"] == "down"
    assert client.post("/api/audit/unknown/feedback", json={"rating": "up"}).status_code == 409

    listed = client.get("/api/feedback", params={"rating": "down"}).json()
    assert [f["decision_id"] for f in listed] == [decision["id"]]
    assert client.get("/api/stats").json()["feedback"]["down_on_auto"] == 1

    export = client.get(f"{pack}/feedback/export")
    assert export.headers["content-type"].startswith("application/x-ndjson")
    assert export.headers["x-openhealth-skipped"] == "0"
    lines = [json.loads(line) for line in export.text.splitlines()]
    assert lines[0]["feedback"][0]["note"] == "wrong team"


def test_cli_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "audit.db"
    with AuditStore(db) as store:
        result = Decision.from_path(INBOX_DIR, store=store)(message(AUTO_REFILL))
        store.record_feedback(result.id, rating="down")
    out = tmp_path / "down.jsonl"
    assert main(["feedback", "export", str(INBOX_DIR), "--db", str(db), "--out", str(out)]) == 0
    assert "1 rated decisions" in capsys.readouterr().out
    assert len(out.read_text().splitlines()) == 1
