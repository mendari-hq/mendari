from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

from mendari import AuditStore, CallBudget, Decision, DecisionResult, Outcome, Pack
from mendari.core import tenancy
from mendari.core.phi import PhiPolicy
from mendari.errors import ProviderError, ReviewError
from tests.conftest import message

Decide = Callable[[str], DecisionResult]

REVIEW = "hello?"  # abstains: no recommendation
EMERGENCY = "Crushing chest pain and my left arm is numb"  # stat HUMAN_REVIEW -> nurse
ROUTINE = "Can you send a refill for metformin to my pharmacy on Main Street?"  # AUTO (`decide`)


def tamper(store: AuditStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Edit the database behind the store's back, as an attacker with DB access could."""
    with store._db.session(exclusive=True) as db:
        db.all(sql, params)


@pytest.fixture
def decide(inbox_pack: Pack, store: AuditStore, screened: dict[str, str]) -> Decide:
    decision = Decision(inbox_pack, providers=screened, store=store)
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
    with pytest.raises(ReviewError, match="not one of the labels"):
        store.resolve_review(result.id, final_label="pharmacist", reviewer="rn-1")
    with pytest.raises(ReviewError, match="reviewer"):
        store.resolve_review(result.id, final_label="nurse", reviewer=" ")


def test_downstream_outcomes_feed_observed_accuracy(store: AuditStore, decide: Decide) -> None:
    result = decide(ROUTINE)
    store.record_outcome(result.id, final_label="nurse", source="staff-reroute", recorded_by="rn-9")
    assert store.stats()["observed_accuracy"] == 0.0
    assert store.stats()["labelled"] == 1  # the accuracy's n


def test_daily_review_counts_add_up_to_the_rates(store: AuditStore, decide: Decide) -> None:
    confirmed, overridden = decide(EMERGENCY), decide(EMERGENCY)
    store.resolve_review(confirmed.id, final_label="nurse", reviewer="rn-1")
    store.resolve_review(overridden.id, final_label="physician", reviewer="rn-1")
    store.resolve_review(decide(REVIEW).id, final_label="front_desk", reviewer="fd-1")  # abstained
    store.record_outcome(
        decide(ROUTINE).id, final_label="nurse", source="staff-reroute", recorded_by="rn-9"
    )
    decide(ROUTINE)
    stats = store.stats()
    [day] = stats["daily"]
    assert {k: day[k] for k in ("reviewed", "overridden", "labelled", "agreed")} == {
        "reviewed": 2,  # the abstention has no recommendation to override
        "overridden": 1,
        "labelled": 3,  # two reviews and one downstream outcome
        "agreed": 1,
    }
    assert day["overridden"] / day["reviewed"] == stats["human_override_rate"]
    assert day["agreed"] / day["labelled"] == stats["observed_accuracy"]
    assert sum(day[o.value] for o in Outcome) == stats["decisions"] == 5


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


def test_a_lifted_budget_still_counts_calls(store: AuditStore) -> None:
    for _ in range(5):
        store.consume("jev", daily_limit=None)
    assert store.usage("jev") == 5


def test_exhausted_budget_falls_back_instead_of_automating(
    inbox_pack: Pack, store: AuditStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")  # credentials fine: the budget refuses
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
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
                "WHERE application_name = 'mendari' AND pid <> pg_backend_pid()"
            ).fetchone()
        assert killed is not None and killed[0] >= 1
        assert store.stats()["decisions"] == 0  # served by a fresh connection, no error


# -- scoped, read-only reads for the FHIR routes (Task, AuditEvent, Provenance) ----------------


def test_item_is_get_without_a_reader_and_never_writes(inbox_pack: Pack, db_target: str) -> None:
    """Under PHI mode `encrypt`, `get(reader=...)` chains an `input.read`; the FHIR reads must
    not, whatever they read."""
    phi = PhiPolicy(mode="encrypt", key=os.urandom(32))
    with AuditStore(db_target, phi=phi) as store:
        decide = Decision(inbox_pack, store=store)
        result = decide(message(EMERGENCY))
        store.resolve_review(result.id, final_label="nurse", reviewer="rn-1")
        chained = store.verify()["events"]
        item = store.item(result.id)
        assert item is not None and item["record"]["input"] is None  # sealed, not decrypted
        assert store.decision_events([result.id]) == {result.id: item["events"]}
        assert store.event(result.id, item["events"][0]["seq"]) is not None
        assert store.verify()["ok"] and store.verify()["events"] == chained
        plain = store.get(result.id)  # without a reader: marked restricted, nothing chained
        assert plain is not None and plain.pop("input_restricted") is True
        assert item == plain
        assert store.verify()["events"] == chained
        assert store.item("no-such-decision") is None
        store.get(result.id, reader="rn-1")  # the control: a read of the input is chained
        assert store.verify()["events"] == chained + 1


def test_events_of_many_decisions_come_in_one_batch(store: AuditStore, decide: Decide) -> None:
    first, second, third = decide(EMERGENCY), decide(ROUTINE), decide(REVIEW)
    store.resolve_review(first.id, final_label="nurse", reviewer="rn-1")  # interleaved
    store.record_outcome(second.id, final_label="nurse", source="ehr", recorded_by="rn-9")
    found = store.decision_events([first.id, second.id, third.id, "unknown"])
    for decision in (first, second, third):
        expected = store.get(decision.id)
        assert expected is not None and found[decision.id] == expected["events"]
    assert [e["event"] for e in found[first.id]] == ["decision.created", "review.resolved"]
    assert found["unknown"] == []
    assert store.decision_events([]) == {}
    seqs = [e["seq"] for e in found[first.id]]
    assert seqs == sorted(seqs)


def test_one_event_by_decision_and_seq(store: AuditStore, decide: Decide) -> None:
    first, second = decide(EMERGENCY), decide(ROUTINE)
    created = store.events()[0]
    event = store.event(first.id, created["seq"])
    assert event == created  # the shape `events()` exports
    assert store.event(second.id, created["seq"]) is None  # another decision's event
    assert store.event(first.id, 10_000) is None


def test_another_organization_reads_nothing(store: AuditStore, inbox_pack: Pack) -> None:
    with tenancy.acting_for("clinic-a"):
        result = Decision(inbox_pack, store=store)(message(EMERGENCY))
        seq = store.events()[0]["seq"]
    with tenancy.acting_for("clinic-b"):
        assert store.item(result.id) is None
        assert store.decision_events([result.id]) == {result.id: []}
        assert store.event(result.id, seq) is None
    with tenancy.acting_for("clinic-a"):
        item = store.item(result.id)
        assert item is not None and len(item["events"]) == 1
        assert store.event(result.id, seq) is not None
    assert store.item(result.id) is not None  # open mode: no organization, no filter


def test_the_read_indexes_exist(store: AuditStore) -> None:
    """`audit_events (decision_id, seq)`: a decision's events without scanning the chain;
    `decisions (created_at, id)`: keyset pages of decisions."""
    with store._db.session(exclusive=False) as db:
        if store.backend == "sqlite":
            rows = db.all("SELECT name FROM sqlite_master WHERE type = 'index'")
        else:
            rows = db.all(
                "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()"
            )
    assert {"audit_events_decision", "decisions_created"} <= {row["name"] for row in rows}
