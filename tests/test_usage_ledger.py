# SPDX-License-Identifier: Apache-2.0
"""The usage ledger (LP-21g): each decision's and each evaluation's remote calls, by day,
organization, provider type and purpose, written with the decision (or the evaluation's
report) in its own transaction. A cost is shown only when every call was priced: unknown is
never zero and never a partial sum. On SQLite and Postgres (`db_target`)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mendari import AuditStore, CallBudget, Decision, Pack, load_pack
from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core import clock, tenancy
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.providers.jev import PRICE_PER_1K_INPUT_TOKENS_USD, JevConfig
from mendari.server.app import create_app
from tests.conftest import INBOX_DIR, SCREENED, calm_jev, message
from tests.test_llm import completion, install, pack_with_llm

TODAY = clock.today().isoformat()
JEV_CALL_USD = 100 / 1000 * PRICE_PER_1K_INPUT_TOKENS_USD  # calm_jev's 100 input tokens


def rows(store: AuditStore, **filters: Any) -> list[dict[str, Any]]:
    return store.usage_ledger(**filters)


def test_each_decision_s_remote_calls_are_counted_for_its_organization(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    with AuditStore(db_target) as store:
        screened = Decision(inbox_pack, providers=SCREENED, store=store)
        with tenancy.acting_for("clinic-a"):
            screened(message("Refill please."))
            screened(message("Can I move my appointment?"))
        with tenancy.acting_for("clinic-b"):
            screened(message("Refill please."))
        Decision(inbox_pack, store=store)(message("Refill please."))  # local providers only
        found = rows(store)
        assert [(r["org"], r["calls"], r["priced_calls"]) for r in found] == [
            ("clinic-a", 2, 2),
            ("clinic-b", 1, 1),
        ]
        first = found[0]
        assert (first["day"], first["provider_type"]) == (TODAY, "jev")
        assert first["purpose"] == "decision"
        assert (first["input_tokens"], first["output_tokens"]) == (200, 20)
        assert first["cost_usd"] == pytest.approx(2 * JEV_CALL_USD)
        assert rows(store, org="clinic-b")[0]["cost_usd"] == pytest.approx(JEV_CALL_USD)
        assert rows(store, since=TODAY, until=TODAY) == found
        assert rows(store, until="2000-01-01") == []
        tomorrow = (clock.today() + timedelta(days=1)).isoformat()
        assert rows(store, since=tomorrow) == []


def test_a_cost_is_unknown_unless_every_call_was_priced(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake gateway prices its first answer and not its second (no `cost`), then fails:
    three calls, one priced, so the cost is unknown, never the one price nor zero."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")  # no built-in default (LP-10c-ii)
    answered: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answered.append(1)
        if len(answered) == 3:
            return httpx.Response(500)
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        content = {
            question: {label: 1 / len(spec["properties"]) for label in spec["properties"]}
            for question, spec in schema["properties"].items()
        }
        body = completion(content)
        if len(answered) == 2:
            body["usage"] = {"prompt_tokens": 300, "completion_tokens": 40}
        return httpx.Response(200, json=body)

    install(monkeypatch, handler)
    pack = pack_with_llm(tmp_path)
    with AuditStore(db_target) as store:
        both = Decision(pack, providers={"router": "llm", "safety_screen": "llm"}, store=store)
        for _ in range(3):
            both(message("Refill please."))
        [row] = rows(store)
    assert len(answered) == 3  # one call a decision: its two slots share it (a batch)
    assert (row["provider_type"], row["calls"], row["priced_calls"]) == ("llm", 3, 1)
    assert (row["input_tokens"], row["output_tokens"]) == (600, 80)  # the failed call: none
    assert row["cost_usd"] is None


def test_a_call_the_budget_or_a_quota_refused_is_not_counted(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    with AuditStore(db_target) as store:
        budget = CallBudget(store, {"jev": 1}, org_limits={"decisions": 2})
        screened = Decision(inbox_pack, providers=SCREENED, store=store, budget=budget)
        for _ in range(3):  # Jev's budget refuses the second; the quota, the third decision
            screened(message("Refill please."))
        [row] = rows(store)
    assert (row["org"], row["calls"], row["priced_calls"]) == ("default", 1, 1)


def test_an_evaluation_s_calls_are_its_organization_s_evaluations(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run with workers (each decision its own meter, across threads), recorded with the report
    in one transaction, apart from the organization's decisions."""
    calm_jev(monkeypatch)
    dataset = resolve_dataset(inbox_pack, "test").head(6)
    run = evaluation(Decision(inbox_pack, providers=SCREENED), dataset, workers=3)
    assert run.usage["jev"].calls == 6 and run.usage["jev"].priced == 6
    with AuditStore(db_target) as store, tenancy.acting_for("clinic-a"):
        store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        Decision(inbox_pack, providers=SCREENED, store=store)(message("Refill please."))
        found = {r["purpose"]: r for r in rows(store, org="clinic-a")}
    assert (found["evaluation"]["calls"], found["decision"]["calls"]) == (6, 1)
    assert found["evaluation"]["cost_usd"] == pytest.approx(6 * JEV_CALL_USD)
    assert (found["evaluation"]["input_tokens"], found["evaluation"]["output_tokens"]) == (600, 60)
    assert (run.usage["jev"].input_tokens, run.usage["jev"].output_tokens) == (600, 60)


def test_the_evaluation_route_records_its_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = calm_jev(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db", providers=providers))
    done = client.post(
        "/api/packs/mendari/patient-inbox-router/evals",
        json={"dataset": "test", "limit": 4},
    )
    assert done.status_code == 200, done.text
    [row] = AuditStore(tmp_path / "a.db").usage_ledger()
    assert (row["org"], row["purpose"], row["provider_type"], row["calls"]) == (
        "default",
        "evaluation",
        "jev",
        4,
    )


def test_a_database_from_before_the_ledger_reads_as_having_none(
    tmp_path: Path, inbox_pack: Pack
) -> None:
    db = tmp_path / "old.db"
    with AuditStore(db) as store:
        Decision(inbox_pack, store=store)(message("Refill please."))
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DROP TABLE usage_ledger")
    assert AuditStore(db, read_only=True).usage_ledger() == []
    with AuditStore(db) as store:  # a server's store creates it
        assert store.usage_ledger() == []


def test_a_request_never_sent_is_never_counted(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message too large for Jev's request (9,000 emoji: within the pack's 10,000 characters,
    each escaped to 12 in JSON) is refused before it is sent: no call counted, no budget spent,
    and the day's cost stays known."""
    calm_jev(monkeypatch)
    with AuditStore(db_target) as store:
        screened = Decision(
            inbox_pack, providers=SCREENED, store=store, budget=CallBudget(store, {"jev": None})
        )
        screened(message("Refill please."))
        refused = screened(message("\U0001f600" * 9000))
        assert "jev:invalid_input" in refused.errors.values()
        [row] = rows(store)
        assert (row["calls"], row["priced_calls"]) == (1, 1)
        assert row["cost_usd"] == pytest.approx(JEV_CALL_USD)
        assert store.usage("jev") == 1  # nor the daily budget


def test_two_jev_slots_sharing_one_call_count_one(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    with AuditStore(db_target) as store:
        both = Decision(
            inbox_pack, providers={"router": "jev", "safety_screen": "jev"}, store=store
        )
        for _ in range(2):
            result = both(message("Refill please."))
            jev = result.usage["jev"]
            assert (jev.calls, jev.priced, jev.input_tokens, jev.output_tokens) == (1, 1, 100, 10)
        [row] = rows(store)
        assert (row["calls"], row["input_tokens"], row["output_tokens"]) == (2, 200, 20)
        assert row["cost_usd"] == pytest.approx(2 * JEV_CALL_USD)


def test_a_jev_call_without_a_price_is_unknown_never_zero(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    root = tmp_path / "pack"
    shutil.copytree(INBOX_DIR, root)
    manifest = yaml.safe_load((root / "pack.yaml").read_text())
    jev = manifest["providers"]["safety_screen"]["variants"]["rules_and_jev"]["members"]["jev"]
    jev["cost_per_1k_input_tokens_usd"] = None
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    with AuditStore(db_target) as store:
        Decision(load_pack(root), providers=SCREENED, store=store)(message("Refill please."))
        [row] = rows(store)
    assert (row["calls"], row["priced_calls"], row["input_tokens"]) == (1, 0, 100)
    assert row["cost_usd"] is None


def test_a_jev_price_is_finite_and_at_most_a_dollar_per_thousand_tokens() -> None:
    for price in (float("inf"), float("nan"), 1.5, -0.1):
        with pytest.raises(ValidationError):
            JevConfig(cost_per_1k_input_tokens_usd=price)
    assert JevConfig(cost_per_1k_input_tokens_usd=1.0).cost_per_1k_input_tokens_usd == 1.0


def test_figures_no_call_could_report_never_reach_the_ledger(
    db_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint that reports more tokens than any call holds is refused (the decision goes to
    a person, and is recorded); a cost no call costs is not believed: unknown."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")  # no built-in default (LP-10c-ii)
    answers: list[dict[str, Any]] = [
        {"prompt_tokens": 2**63 - 1, "completion_tokens": 1},
        {"prompt_tokens": 300, "completion_tokens": 40, "cost": 1e308},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        content = {
            question: {label: 1 / len(spec["properties"]) for label in spec["properties"]}
            for question, spec in schema["properties"].items()
        }
        return httpx.Response(200, json={**completion(content), "usage": answers.pop(0)})

    install(monkeypatch, handler)
    pack = pack_with_llm(tmp_path)
    with AuditStore(db_target) as store:
        both = Decision(pack, providers={"router": "llm", "safety_screen": "llm"}, store=store)
        first = both(message("Refill please."))
        assert set(first.errors.values()) == {"invalid_response"}
        both(message("Refill please."))
        [row] = rows(store)
        assert len(store.query()) == 2  # both recorded
    assert (row["calls"], row["priced_calls"], row["input_tokens"]) == (2, 0, 300)
    assert row["cost_usd"] is None


def test_usage_is_written_in_the_decision_s_and_the_report_s_own_transaction(
    db_target: str, inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    dataset = resolve_dataset(inbox_pack, "test").head(2)
    run = evaluation(Decision(inbox_pack, providers=SCREENED), dataset)
    with AuditStore(db_target) as store:
        screened = Decision(inbox_pack, providers=SCREENED, store=store)

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the write failed")

        with monkeypatch.context() as patched:  # the chain write fails: no usage either
            patched.setattr(AuditStore, "_append", broken)
            with pytest.raises(RuntimeError):
                screened(message("Refill please."))
            with pytest.raises(RuntimeError):
                store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        assert rows(store) == []
        with monkeypatch.context() as patched:  # the usage write fails: no decision, no report
            patched.setattr(audit_module, "_add_usage", broken)
            with pytest.raises(RuntimeError):
                screened(message("Refill please."))
            with pytest.raises(RuntimeError):
                store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        assert store.query() == [] and store.reports() == []


def test_mendari_eval_db_records_its_calls_as_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calm_jev(monkeypatch)
    db = tmp_path / "a.db"
    argv = ["eval", "mendari/patient-inbox-router", "--provider", "safety_screen=rules_and_jev",
            "--dataset", "test", "--limit", "3", "--baseline", "none", "--db", str(db)]  # fmt: skip
    assert main(argv) == 0
    [row] = AuditStore(db).usage_ledger()
    assert (row["org"], row["purpose"], row["provider_type"], row["calls"]) == (
        "default",
        "evaluation",
        "jev",
        3,
    )
