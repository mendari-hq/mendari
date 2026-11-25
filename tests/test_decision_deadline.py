# SPDX-License-Identifier: Apache-2.0
"""One wall-clock deadline across a decision's remote calls (LP-16b, `providers/deadline.py`).

Past it a remote call is not made, before any credential is fetched or any budget charged; a
call under way waits at most what remains of it; either is the provider error `timeout`, so the
decision is held for a person. Local providers always answer: a possible emergency is still
flagged, and what a pack decides offline never changes. A live agent service, the LLM and Vercel
Connect answer here through `httpx.MockTransport`s: nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from mendari import Decision, Pack, load_pack
from mendari.core import decision as decision_module
from mendari.core import patterns
from mendari.core.decision import CallBudget, DecisionResult
from mendari.core.spec import Outcome, Priority
from mendari.errors import MendariError, ProviderError
from mendari.evals.pack_tests import load_cases
from mendari.parallel import parallel_map
from mendari.providers import deadline, http
from mendari.providers.agent import AgentProvider
from mendari.providers.credentials import ConnectorToken
from mendari.providers.llm import LlmProvider
from mendari.server.app import create_app
from mendari.settings import decision_deadline_s
from tests.conftest import FIXTURE_AGENTS, message
from tests.support.engine import pulled, sorter

URL = "https://remote.test/v1"
# A deadline that has passed before the first remote call (0, not a tiny positive number, so a
# coarse clock cannot leave it in the future).
EXPIRED = 0.0
SETTING = "MENDARI_DECISION_DEADLINE_S"
# Every question of the triage fixture, both of whose slots time out.
TIMED_OUT = {"destination": "timeout", "urgency": "timeout", "abusive": "timeout"}


def distribution(labels: list[str], chosen: str) -> dict[str, float]:
    rest = [label for label in labels if label != chosen]
    return {chosen: 0.97, **{label: 0.03 / len(rest) for label in rest}}


class Remote:
    """A live agent service (`MENDARI_AGENT_URL`) and an LLM behind mock transports. `calls`
    lists each call made, in order; `delay` says how long a call waits before it answers (by
    provider type and request text); `labels` is what each question is answered, with
    confidence 0.97."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.labels = {
            "destination": "nurse",
            "urgency": "no",
            "abusive": "no",
            "topic": "other",
            "upset": "no",
        }
        self.delay: Callable[[str, str], float] = lambda kind, text: 0.0

    def answer(self, kind: str, request: httpx.Request) -> httpx.Response:
        self.calls.append(kind)
        time.sleep(self.delay(kind, request.content.decode()))
        body = json.loads(request.content)
        if kind == "agent":
            answers = {
                question["key"]: {"value": self.labels[question["key"]], "confidence": 0.97}
                for question in body["questions"]
            }
            return httpx.Response(
                200,
                json={"model": "agent-test-model", "agent_version": "2.0.0", "answers": answers},
            )
        schema = body["response_format"]["json_schema"]["schema"]["properties"]
        content = {
            key: distribution(list(question["properties"]), self.labels[key])
            for key, question in schema.items()
        }
        choice = {"finish_reason": "stop", "message": {"content": json.dumps(content)}}
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx.Response(200, json={"choices": [choice], "usage": usage})


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> Remote:
    """A live agent service and the LLM set up, every client of theirs answering through
    `Remote`."""
    monkeypatch.setenv("MENDARI_AGENT_URL", "https://agent.test/v1/decide")
    monkeypatch.delenv("MENDARI_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")  # no built-in default model (LP-10c-ii)
    fake = Remote()
    agent_init, llm_init = AgentProvider.__init__, LlmProvider.__init__

    def client(kind: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda r: fake.answer(kind, r)))

    def agent(self: AgentProvider, *args: Any, **kwargs: Any) -> None:
        agent_init(self, *args, **{**kwargs, "client": client("agent")})

    def llm(self: LlmProvider, *args: Any, **kwargs: Any) -> None:
        llm_init(self, *args, **{**kwargs, "client": client("llm")})

    monkeypatch.setattr(AgentProvider, "__init__", agent)
    monkeypatch.setattr(LlmProvider, "__init__", llm)
    return fake


class Counted(CallBudget):
    """A budget that records each charge, with no store and no limit."""

    def __init__(self) -> None:
        super().__init__(None, {})  # type: ignore[arg-type]
        self.acquired: list[str] = []

    def acquire(self, provider_type: str) -> None:
        self.acquired.append(provider_type)


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- the decision ------------------------------------------------------------------------------


def test_a_remote_call_after_the_deadline_is_not_made_and_uses_no_budget(
    remote: Remote, triage_agent: Pack
) -> None:
    """The agent service, asked for the router (the triage fixture's first slot), answers after
    the whole deadline, so its answer is cut too; the screen's LLM call would start after it, so
    it is not made, and nothing is charged for it."""
    remote.delay = lambda kind, text: 2.5 if kind == "agent" else 0.0
    budget = Counted()
    decision = Decision(
        triage_agent,
        providers={"router": "agent", "screen": "llm"},
        budget=budget,
        deadline_s=2.0,  # ample for the call to start, even on a loaded runner
    )
    result = decision.decide(message("A rash on my arm since Tuesday."))
    assert remote.calls == ["agent"]
    assert budget.acquired == ["agent"]
    assert result.errors == TIMED_OUT
    assert result.outcome is Outcome.ABSTAIN  # no answer for the primary: a person decides


def test_local_providers_answer_after_the_deadline(remote: Remote, triage_agent: Pack) -> None:
    """The deadline only ever fails remote calls: the screen's keyword rules still read the
    message while the router's agent service is never called, so the possible-emergency rule
    still sends it to a nurse now."""
    decision = Decision(triage_agent, providers={"router": "agent"}, deadline_s=EXPIRED)
    result = decision.decide(message("I have had chest pain since this morning."))
    assert remote.calls == []
    assert result.errors == {"destination": "timeout"}
    assert result.answers["urgency"].label == "yes"
    assert result.answers["urgency"].provider.type == "rules"  # local: answered
    assert (result.rule, result.outcome, result.priority, result.recommendation) == (
        "possible-emergency",
        Outcome.HUMAN_REVIEW,
        Priority.STAT,
        "nurse",
    )


def test_a_deadline_never_automates(remote: Remote, triage_agent: Pack) -> None:
    """A case that automates with the agent service's answers in time is held for a person once
    the deadline has passed, with `timeout` recorded."""
    remote.labels["destination"] = "pharmacy"
    providers = {"router": "agent", "screen": "agent"}
    case = message("Can you refill my lisinopril prescription? I have two days left.")
    in_time = Decision(triage_agent, providers=providers, deadline_s=30).decide(case)
    assert in_time.outcome is Outcome.AUTO and in_time.errors == {}
    assert remote.calls == ["agent", "agent"]  # one call for each slot
    late = Decision(triage_agent, providers=providers, deadline_s=EXPIRED).decide(case)
    assert remote.calls == ["agent", "agent"]  # no call past the deadline
    assert late.outcome in (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN)
    assert late.errors == TIMED_OUT


def test_the_deadline_belongs_to_one_decision(remote: Remote, triage_agent: Pack) -> None:
    """One Decision serves every thread, as on the server. The slow decision's deadline passes
    during its agent call, so that answer is cut and its LLM call is not made; the quick one
    starts after that deadline, during that call, and keeps its own."""
    remote.delay = lambda kind, text: 2.8 if kind == "agent" and "slow" in text else 0.0
    providers = {"router": "agent", "screen": "llm"}
    # Each decision has 2 s: the quick one's instant calls fit in it even on a loaded runner.
    shared = Decision(triage_agent, providers=providers, deadline_s=2.0)

    def decide(text: str) -> DecisionResult:
        if text.startswith("quick"):
            time.sleep(2.2)  # after the slow decision's deadline, during its agent call
        return shared.decide(message(text))

    slow, quick = parallel_map(decide, ["slow: a rash on my arm", "quick: a rash on my arm"], 2)
    assert slow.errors == TIMED_OUT
    assert quick.errors == {} and quick.answers["destination"].label == "nurse"
    assert sorted(remote.calls) == ["agent", "agent", "llm"]
    assert deadline.remaining() is None  # none outside a decision


def test_reference_decisions_never_see_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every agent of the test fixtures' folder, on its default providers (local, or canned
    answers while no agent service is set), decides each of its behaviour tests exactly as
    before, with a deadline that passed before it started."""
    monkeypatch.delenv(SETTING, raising=False)
    packs = [
        load_pack(root)
        for root in sorted(FIXTURE_AGENTS.iterdir())
        if (root / "agent.yaml").exists()
    ]
    assert packs

    def comparable(result: DecisionResult) -> dict[str, Any]:
        shown = result.to_dict()
        return {k: v for k, v in shown.items() if k not in ("id", "created_at", "latency_ms")}

    for pack in packs:
        default, expired = Decision(pack), Decision(pack, deadline_s=EXPIRED)
        assert default.deadline_s == 120
        for case in load_cases(pack):
            assert comparable(expired.decide(case["input"])) == comparable(
                default.decide(case["input"])
            ), (pack.id, case.get("name"))


def test_the_cpu_budget_is_not_the_deadline(
    remote: Remote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The patterns of an agent the deployment does not own share a budget of CPU time
    (`core/patterns.py`); waiting for a remote slot asked before its rules costs it nothing, and
    a passed deadline leaves it untouched."""

    def llm_first(manifest: dict[str, Any]) -> None:
        manifest["questions"]["upset"] = {
            "type": "boolean",
            "instructions": "Does the message sound upset?",
            "criteria": {"yes": "It does.", "no": "It does not."},
        }
        screen = {
            "answers": ["upset"],
            "use": "llm",
            "variants": {"llm": {"type": "llm", "state_fields": ["message"]}},
        }
        manifest["providers"] = {"screen": screen, **manifest["providers"]}  # asked first

    pack = pulled(sorter(tmp_path, manifest=llm_first))
    assert pack.bounded
    monkeypatch.setattr(decision_module, "time_budget", lambda: patterns.time_budget(0.2))
    remote.delay = lambda kind, text: 0.5  # waited for, longer than the budget
    case = {"message": "A question about my bill"}

    waited = Decision(pack, deadline_s=30).decide(case)
    assert remote.calls == ["llm"]
    assert waited.errors == {}  # no `time_budget`: the rules ran after the wait
    assert waited.answers["topic"].label == "billing"

    late = Decision(pack, deadline_s=EXPIRED).decide(case)
    assert remote.calls == ["llm"]
    assert late.errors == {"upset": "timeout"}
    assert late.answers["topic"].label == "billing"


# -- HTTP --------------------------------------------------------------------------------------


def test_post_json_clamps_its_timeout_to_what_remains() -> None:
    seen: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    client = mock_client(handler)
    http.post_json(client, URL, {}, headers={}, timeout_s=30)
    with deadline.decision_deadline(5):
        http.post_json(client, URL, {}, headers={}, timeout_s=30)
        http.post_json(client, URL, {}, headers={}, timeout_s=2)
    assert set(seen[0].values()) == {30}  # outside a decision, as before
    assert all(0 < step <= 5 for step in seen[1].values())  # connect, read, write, pool
    assert set(seen[2].values()) == {2}
    with deadline.decision_deadline(EXPIRED), pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=30)
    assert error.value.code == "timeout" and len(seen) == 3  # no request


def test_no_retry_is_waited_past_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(http, "_sleep", waits.append)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "2"})  # http.MAX_RETRY_WAIT_S

    client = mock_client(handler)
    with deadline.decision_deadline(1.5), pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=5, retries=1)
    assert error.value.code == "rate_limited"
    assert waits == [] and len(calls) == 1
    # With time left, or outside a decision, a short Retry-After is waited out, as before.
    with deadline.decision_deadline(30), pytest.raises(ProviderError):
        http.post_json(client, URL, {}, headers={}, timeout_s=5, retries=1)
    assert waits == [2.0] and len(calls) == 3


class Dripping(httpx.SyncByteStream):
    """An answer that arrives a byte every 50 ms, for 5 s."""

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(100):
            time.sleep(0.05)
            yield b" "


def test_a_slow_response_stops_at_the_deadline() -> None:
    client = mock_client(lambda request: httpx.Response(200, stream=Dripping()))
    started = time.monotonic()
    with deadline.decision_deadline(0.3), pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=5)
    took = time.monotonic() - started
    assert error.value.code == "timeout"
    assert took < 2.5  # the deadline, one chunk, and room for a busy runner; not 5 s


def test_a_slow_response_stops_at_its_own_timeout_outside_a_decision() -> None:
    """httpx's timeout is per read, so an answer that drips a byte inside each wait held the
    caller (a calibration, `mendari calibrate`) for as long as the server liked: each attempt
    now ends at its timeout, whole."""
    client = mock_client(lambda request: httpx.Response(200, stream=Dripping()))
    started = time.monotonic()
    with pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=0.3)
    took = time.monotonic() - started
    assert error.value.code == "timeout"
    assert took < 2.5  # the timeout, one chunk, and room for a busy runner; not 5 s


# -- credentials -------------------------------------------------------------------------------

KEY = {"token": "connector-key", "expiresAt": 4_102_444_800_000}


def test_a_connector_token_wait_stops_at_the_deadline() -> None:
    """Another thread is fetching the key (Vercel Connect is slow): a decision waits for it at
    most until its deadline; past its deadline, no fetch starts."""
    fetching, release = threading.Event(), threading.Event()
    requests: list[httpx.Request] = []

    def connect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        fetching.set()
        release.wait(5)
        return httpx.Response(200, json=KEY)

    token = ConnectorToken("llm/my-project", client=mock_client(connect), oidc=lambda: "oidc")
    first = threading.Thread(target=token.get)
    first.start()
    try:
        assert fetching.wait(5)
        started = time.monotonic()
        with deadline.decision_deadline(0.2), pytest.raises(ProviderError) as error:
            token.get()
        assert error.value.code == "timeout"
        assert time.monotonic() - started < 2
    finally:
        release.set()
        first.join(5)
    assert token.get() == "connector-key" and len(requests) == 1  # the other thread's fetch, kept

    fresh = ConnectorToken("llm/my-project", client=mock_client(connect), oidc=lambda: "oidc")
    with deadline.decision_deadline(EXPIRED), pytest.raises(ProviderError) as error:
        fresh.get()
    assert error.value.code == "timeout" and len(requests) == 1


# -- the setting -------------------------------------------------------------------------------


# Too short, too long, not a number, not whole, and digits `int` reads or refuses that are not
# ASCII (full-width 120, a superscript 2).
NOT_ASCII = ["".join(map(chr, (0xFF11, 0xFF12, 0xFF10))), chr(0xB2)]
REFUSED = ["5", "9", "271", "300", "abc", "60s", "1.5", "-20", *NOT_ASCII]


@pytest.mark.parametrize("value", REFUSED)
def test_the_deadline_setting_is_validated(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SETTING, value)
    with pytest.raises(MendariError, match=f"{SETTING} must be a whole number of seconds, 10 to"):
        decision_deadline_s()


def test_a_bad_deadline_setting_stops_the_server_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SETTING, "300")
    with pytest.raises(MendariError, match=SETTING):
        create_app(db_path=tmp_path / "a.db")
    assert not (tmp_path / "a.db").exists()  # before anything is opened


def test_the_deadline_setting_is_120_seconds_unless_set(
    triage_agent: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTING, raising=False)
    assert decision_deadline_s() == 120 and Decision(triage_agent).deadline_s == 120
    for value in ("10", "270", " 60 "):
        monkeypatch.setenv(SETTING, value)
        assert decision_deadline_s() == int(value)
    assert Decision(triage_agent).deadline_s == 60
    assert Decision(triage_agent, deadline_s=5).deadline_s == 5  # a caller's own, e.g. a test
