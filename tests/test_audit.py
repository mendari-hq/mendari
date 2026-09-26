from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from openhealth import AuditStore, Decision, DecisionResult, Pack
from openhealth.errors import ReviewError
from tests.conftest import message

Decide = Callable[[str], DecisionResult]

REVIEW = "hello?"  # abstains: no recommendation
EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO


@pytest.fixture
def store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "audit.db")


@pytest.fixture
def decide(inbox_pack: Pack, store: AuditStore) -> Decide:
    decision = Decision(inbox_pack, store=store)
    return lambda body: decision(message(body))


def test_queue_holds_review_and_abstain_with_stat_first(store: AuditStore, decide: Decide) -> None:
    decide(ROUTINE)
    abstain = decide(REVIEW)
    stat = decide(EMERGENCY)
    queue = store.pending_reviews()
    assert [item["id"] for item in queue] == [stat.id, abstain.id]


def test_confirming_is_not_an_override(store: AuditStore, decide: Decide) -> None:
    result = decide(EMERGENCY)
    resolved = store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
    assert resolved["human_override"] is False
    assert store.stats()["human_override_rate"] == 0.0


def test_correcting_is_an_override(store: AuditStore, decide: Decide) -> None:
    result = decide(EMERGENCY)
    assert store.resolve_review(result.id, final_label="physician", reviewer="rn-1")[
        "human_override"
    ]
    with pytest.raises(ReviewError, match="not awaiting review"):
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")


def test_abstentions_resolve_without_counting_as_overrides(
    store: AuditStore, decide: Decide
) -> None:
    result = decide(REVIEW)
    assert (
        store.resolve_review(result.id, final_label="front_desk", reviewer="fd-1")["human_override"]
        is None
    )
    assert store.stats()["reviews_resolved"] == 0


def test_review_rejects_unknown_labels_and_anonymous_reviewers(
    store: AuditStore, decide: Decide
) -> None:
    result = decide(EMERGENCY)
    with pytest.raises(ReviewError, match="not a valid label"):
        store.resolve_review(result.id, final_label="pharmacist", reviewer="rn-1")
    with pytest.raises(ReviewError, match="reviewer"):
        store.resolve_review(result.id, final_label="nurse", reviewer=" ")


def test_downstream_outcomes_feed_observed_accuracy(store: AuditStore, decide: Decide) -> None:
    result = decide(ROUTINE)
    store.record_outcome(result.id, final_label="nurse", source="re-routed by staff")
    assert store.stats()["observed_accuracy"] == 0.0


def test_hash_chain_detects_tampering(store: AuditStore, decide: Decide) -> None:
    result = decide(EMERGENCY)
    store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
    decide(ROUTINE)
    assert store.verify() == {"ok": True, "events": 3, "head": store.verify()["head"]}

    with closing(sqlite3.connect(store.path, isolation_level=None)) as db:
        db.execute(
            "UPDATE decisions SET record = replace(record, 'stat', 'routine') WHERE id = ?",
            (result.id,),
        )
    assert store.verify()["ok"] is False


def test_hash_chain_detects_deleted_events(store: AuditStore, decide: Decide) -> None:
    for body in (ROUTINE, REVIEW, EMERGENCY):
        decide(body)
    with closing(sqlite3.connect(store.path, isolation_level=None)) as db:
        db.execute("DELETE FROM audit_events WHERE seq = 2")
    assert store.verify() == {"ok": False, "events": 2, "broken_at": 3}


def test_hash_only_input_storage(inbox_pack: Pack, store: AuditStore) -> None:
    result = Decision(inbox_pack)(message(ROUTINE))
    store.record(result, message(ROUTINE), store_input="hash")
    record = store.get(result.id)
    assert record is not None and record["record"]["input"] is None
    assert record["record"]["input_hash"] == result.input_hash
