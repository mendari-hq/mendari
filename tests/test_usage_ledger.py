# SPDX-License-Identifier: Apache-2.0
"""The usage ledger (LP-21g): each decision's and each evaluation's remote calls, by day,
organization, provider type and purpose, written with the decision (or the evaluation's
report) in its own transaction. A cost is shown only when every call was priced: unknown is
never zero and never a partial sum. On SQLite and Postgres (`db_target`).

The remote calls are the triage fixture's (`mendari-test/triage`): its `llm` variants, on both
its slots (`LLM`), share one call a decision (a batch), answered by a fake AI Gateway
(`gateway`) in place of the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import Agent, AuditStore, CallBudget, Decision, load_agent
from mendari.cli import main
from mendari.core import audit as audit_module
from mendari.core import clock, tenancy
from mendari.evals.harness import evaluation, resolve_dataset
from mendari.providers.llm import LlmProvider
from mendari.server.app import create_app
from tests.conftest import message
from tests.support.platform import TRIAGE

# The day the tests run on, pinned (`pinned_day`): a run that crosses local midnight still
# writes and reads one day.
TODAY = "2026-10-08"


@pytest.fixture(autouse=True)
def pinned_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "today", lambda: date.fromisoformat(TODAY))


# Both of the fixture's slots on its `llm` variants: one call a decision, their questions batched.
LLM = {"router": "llm", "screen": "llm"}
MODEL = "openai/gpt-oss-120b"
# What the fake gateway reports for each call it answers (`completion`): its tokens and price.
INPUT_TOKENS, OUTPUT_TOKENS, CALL_USD = 300, 40, 0.0012


def completion(content: Any) -> dict[str, Any]:
    """An AI Gateway chat completion holding `content`, priced (its `usage` has a `cost`)."""
    text = content if isinstance(content, str) else json.dumps(content)
    said = {"role": "assistant", "content": text}
    choice = {"index": 0, "finish_reason": "stop", "message": said}
    usage = {"prompt_tokens": INPUT_TOKENS, "completion_tokens": OUTPUT_TOKENS, "cost": CALL_USD}
    return {"model": MODEL, "choices": [choice], "usage": usage}


def even(request: httpx.Request) -> dict[str, Any]:
    """An answer to every question the request asks, each of its labels as likely as another."""
    schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
    return {
        question: {label: 1 / len(spec["properties"]) for label in spec["properties"]}
        for question, spec in schema["properties"].items()
    }


def gateway(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> None:
    """Every `llm` provider asks a fake AI Gateway, `handler`: by default, a priced answer to
    each request."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gw-test-key")
    monkeypatch.setenv("LLM_MODEL", MODEL)  # no built-in default (LP-10c-ii)
    answer = handler or (lambda request: httpx.Response(200, json=completion(even(request))))
    original = LlmProvider.__init__

    def init(self: LlmProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(answer))
        original(self, *args, **kwargs)

    monkeypatch.setattr(LlmProvider, "__init__", init)


def rows(store: AuditStore, **filters: Any) -> list[dict[str, Any]]:
    return store.usage_ledger(**filters)


def test_each_decision_s_remote_calls_are_counted_for_its_organization(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision whose two slots shared one call (a batch) counts that call once."""
    gateway(monkeypatch)
    with AuditStore(db_target) as store:
        asked = Decision(triage_agent, providers=LLM, store=store)
        with tenancy.acting_for("clinic-a"):
            refill = asked(message("Refill please."))
            asked(message("Can I move my appointment?"))
        with tenancy.acting_for("clinic-b"):
            asked(message("Refill please."))
        Decision(triage_agent, store=store)(message("Refill please."))  # local providers only
        llm = refill.usage["llm"]
        assert (llm.calls, llm.priced, llm.input_tokens, llm.output_tokens) == (
            1,
            1,
            INPUT_TOKENS,
            OUTPUT_TOKENS,
        )
        found = rows(store)
        assert [(r["org"], r["calls"], r["priced_calls"]) for r in found] == [
            ("clinic-a", 2, 2),
            ("clinic-b", 1, 1),
        ]
        first = found[0]
        assert (first["day"], first["provider_type"]) == (TODAY, "llm")
        assert first["purpose"] == "decision"
        assert (first["input_tokens"], first["output_tokens"]) == (
            2 * INPUT_TOKENS,
            2 * OUTPUT_TOKENS,
        )
        assert first["cost_usd"] == pytest.approx(2 * CALL_USD)
        assert rows(store, org="clinic-b")[0]["cost_usd"] == pytest.approx(CALL_USD)
        assert rows(store, since=TODAY, until=TODAY) == found
        assert rows(store, until="2000-01-01") == []
        tomorrow = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
        assert rows(store, since=tomorrow) == []


def test_a_cost_is_unknown_unless_every_call_was_priced(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake gateway prices its first answer and not its second (no `cost`), then fails:
    three calls, one priced, so the cost is unknown, never the one price nor zero."""
    answered: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answered.append(1)
        if len(answered) == 3:
            return httpx.Response(500)
        body = completion(even(request))
        if len(answered) == 2:
            body["usage"] = {"prompt_tokens": INPUT_TOKENS, "completion_tokens": OUTPUT_TOKENS}
        return httpx.Response(200, json=body)

    gateway(monkeypatch, handler)
    with AuditStore(db_target) as store:
        both = Decision(triage_agent, providers=LLM, store=store)
        for _ in range(3):
            both(message("Refill please."))
        [row] = rows(store)
    assert len(answered) == 3  # one call a decision: its two slots share it (a batch)
    assert (row["provider_type"], row["calls"], row["priced_calls"]) == ("llm", 3, 1)
    # The failed call reported no tokens.
    assert (row["input_tokens"], row["output_tokens"]) == (2 * INPUT_TOKENS, 2 * OUTPUT_TOKENS)
    assert row["cost_usd"] is None


@pytest.mark.parametrize("status", [500, 200])
def test_a_decision_whose_sent_call_failed_has_an_unknown_cost_never_zero(
    triage_agent: Agent, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """The call was sent (and may be billed) but no answer priced it: the decision's own
    `cost_usd`, like the ledger, is unknown. It said 0.0, as if the call were free."""
    # A server error, or an answer that is not the contract (refused after it was sent).
    gateway(monkeypatch, lambda request: httpx.Response(status, json=completion("not json")))
    result = Decision(triage_agent, providers=LLM)(message("Refill please."))
    assert result.errors and result.usage["llm"].calls == 1
    assert result.usage["llm"].priced == 0
    assert result.cost_usd is None
    # Local providers alone still cost nothing, known.
    assert Decision(triage_agent)(message("Refill please.")).cost_usd == 0.0


def test_a_call_the_budget_or_a_quota_refused_is_not_counted(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway(monkeypatch)
    with AuditStore(db_target) as store:
        budget = CallBudget(store, {"llm": 1}, org_limits={"decisions": 2})
        asked = Decision(triage_agent, providers=LLM, store=store, budget=budget)
        for _ in range(3):  # the LLM's budget refuses the second; the quota, the third decision
            asked(message("Refill please."))
        [row] = rows(store)
    assert (row["org"], row["calls"], row["priced_calls"]) == ("default", 1, 1)


def test_an_evaluation_s_calls_are_its_organization_s_evaluations(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run with workers (each decision its own meter, across threads), recorded with the report
    in one transaction, apart from the organization's decisions."""
    gateway(monkeypatch)
    dataset = resolve_dataset(triage_agent, "test").head(6)
    run = evaluation(Decision(triage_agent, providers=LLM), dataset, workers=3)
    assert run.usage["llm"].calls == 6 and run.usage["llm"].priced == 6
    with AuditStore(db_target) as store, tenancy.acting_for("clinic-a"):
        store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        Decision(triage_agent, providers=LLM, store=store)(message("Refill please."))
        found = {r["purpose"]: r for r in rows(store, org="clinic-a")}
    assert (found["evaluation"]["calls"], found["decision"]["calls"]) == (6, 1)
    assert found["evaluation"]["cost_usd"] == pytest.approx(6 * CALL_USD)
    tokens = (6 * INPUT_TOKENS, 6 * OUTPUT_TOKENS)
    assert (found["evaluation"]["input_tokens"], found["evaluation"]["output_tokens"]) == tokens
    assert (run.usage["llm"].input_tokens, run.usage["llm"].output_tokens) == tokens


def test_the_evaluation_route_records_its_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway(monkeypatch)
    client = TestClient(create_app(db_path=tmp_path / "a.db", providers=LLM))
    done = client.post(f"/api/agents/{TRIAGE}/evals", json={"dataset": "test", "limit": 4})
    assert done.status_code == 200, done.text
    [row] = AuditStore(tmp_path / "a.db").usage_ledger()
    assert (row["org"], row["purpose"], row["provider_type"], row["calls"]) == (
        "default",
        "evaluation",
        "llm",
        4,
    )


def test_a_request_never_sent_is_never_counted(
    db_target: str, agent_copy: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message too large for the LLM's request (9,000 emoji: within a copy of the fixture
    that takes 10,000 characters, each escaped to 12 in JSON) is refused before it is sent: no
    call counted, no budget spent, and the day's cost stays known."""
    gateway(monkeypatch)
    root = agent_copy()
    path = root / "schemas" / "input.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["message"]["properties"]["body"]["maxLength"] = 10_000
    path.write_text(json.dumps(schema), encoding="utf-8")
    longer = load_agent(root)
    with AuditStore(db_target) as store:
        asked = Decision(
            longer, providers=LLM, store=store, budget=CallBudget(store, {"llm": None})
        )
        asked(message("Refill please."))
        refused = asked(message("\U0001f600" * 9000))
        assert "invalid_input" in refused.errors.values()
        [row] = rows(store)
        assert (row["calls"], row["priced_calls"]) == (1, 1)
        assert row["cost_usd"] == pytest.approx(CALL_USD)
        assert store.usage("llm") == 1  # nor the daily budget


def test_figures_no_call_could_report_never_reach_the_ledger(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint that reports more tokens than any call holds is refused (the decision goes to
    a person, and is recorded); a cost no call costs is not believed: unknown."""
    answers: list[dict[str, Any]] = [
        {"prompt_tokens": 2**63 - 1, "completion_tokens": 1},
        {"prompt_tokens": 300, "completion_tokens": 40, "cost": 1e308},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**completion(even(request)), "usage": answers.pop(0)})

    gateway(monkeypatch, handler)
    with AuditStore(db_target) as store:
        both = Decision(triage_agent, providers=LLM, store=store)
        first = both(message("Refill please."))
        assert set(first.errors.values()) == {"invalid_response"}
        both(message("Refill please."))
        [row] = rows(store)
        assert len(store.query()) == 2  # both recorded
    assert (row["calls"], row["priced_calls"], row["input_tokens"]) == (2, 0, 300)
    assert row["cost_usd"] is None


def test_usage_is_written_in_the_decision_s_and_the_report_s_own_transaction(
    db_target: str, triage_agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway(monkeypatch)
    dataset = resolve_dataset(triage_agent, "test").head(2)
    run = evaluation(Decision(triage_agent, providers=LLM), dataset)
    with AuditStore(db_target) as store:
        asked = Decision(triage_agent, providers=LLM, store=store)

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the write failed")

        with monkeypatch.context() as patched:  # the chain write fails: no usage either
            patched.setattr(AuditStore, "_append", broken)
            with pytest.raises(RuntimeError):
                asked(message("Refill please."))
            with pytest.raises(RuntimeError):
                store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        assert rows(store) == []
        with monkeypatch.context() as patched:  # the usage write fails: no decision, no report
            patched.setattr(audit_module, "_add_usage", broken)
            with pytest.raises(RuntimeError):
                asked(message("Refill please."))
            with pytest.raises(RuntimeError):
                store.save_report(run.report, ledger=run.ledger.dumps(), usage=run.usage)
        assert store.query() == [] and store.reports() == []


def test_mendari_eval_db_records_its_calls_as_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway(monkeypatch)
    db = tmp_path / "a.db"
    argv = ["eval", TRIAGE, "--provider", "screen=llm", "--dataset", "test", "--limit", "3",
            "--baseline", "none", "--db", str(db)]  # fmt: skip
    assert main(argv) == 0
    [row] = AuditStore(db).usage_ledger()
    assert (row["org"], row["purpose"], row["provider_type"], row["calls"]) == (
        "default",
        "evaluation",
        "llm",
        3,
    )
