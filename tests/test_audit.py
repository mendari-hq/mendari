from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from openhealth import AuditStore, CallBudget, Decision, DecisionResult, Outcome, Pack
from openhealth.errors import ProviderError, ReviewError
from tests.conftest import message

Decide = Callable[[str], DecisionResult]

REVIEW = "hello?"  # abstains: no recommendation
EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as an attacker with DB access could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


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

    tamper(
        store,
        "UPDATE decisions SET record = replace(record, 'stat', 'routine') WHERE id = ?",
        (result.id,),
    )
    assert store.verify()["ok"] is False


def test_hash_chain_detects_deleted_events(store: AuditStore, decide: Decide) -> None:
    for body in (ROUTINE, REVIEW, EMERGENCY):
        decide(body)
    tamper(store, "DELETE FROM audit_events WHERE seq = 2")
    assert store.verify() == {"ok": False, "events": 2, "broken_at": 3}


def test_hash_only_input_storage(inbox_pack: Pack, store: AuditStore) -> None:
    result = Decision(inbox_pack)(message(ROUTINE))
    store.record(result, message(ROUTINE), store_input="hash")
    record = store.get(result.id)
    assert record is not None and record["record"]["input"] is None
    assert record["record"]["input_hash"] == result.input_hash


def test_daily_budget_is_enforced(store: AuditStore) -> None:
    for _ in range(3):
        store.consume("jev", daily_limit=3)
    with pytest.raises(ProviderError, match="budget_exhausted"):
        store.consume("jev", daily_limit=3)
    assert store.usage("jev") == 3
    assert store.usage("other") == 0


def test_exhausted_budget_falls_back_instead_of_automating(
    inbox_pack: Pack, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    decision = Decision(
        inbox_pack, providers={"router": "jev"}, budget=CallBudget(store, {"jev": 0})
    )
    result = decision(message(ROUTINE))
    assert result.outcome is Outcome.ABSTAIN
    assert result.errors == {"destination": "budget_exhausted"}


def test_unconfigured_provider_does_not_spend_budget(
    inbox_pack: Pack, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JEV_API_URL", raising=False)
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    decision = Decision(
        inbox_pack, providers={"router": "jev"}, budget=CallBudget(store, {"jev": 10})
    )
    assert decision(message(ROUTINE)).errors == {"destination": "not_configured"}
    assert store.usage("jev") == 0


def test_postgres_store_survives_server_closed_connections(db_target: str) -> None:
    """Neon suspends idle compute and closes pooled connections; the next request must work."""
    if not db_target.startswith("postgresql://"):
        pytest.skip("Postgres-only behaviour")
    import psycopg

    with AuditStore(db_target) as store:
        store.stats()  # the pool now holds an open, idle connection
        admin_url = db_target.split("?")[0]
        with psycopg.connect(admin_url, autocommit=True) as admin:
            killed = admin.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE application_name = 'openhealth' AND pid <> pg_backend_pid()"
            ).fetchone()
        assert killed is not None and killed[0] >= 1
        assert store.stats()["decisions"] == 0  # served by a fresh connection, no error
