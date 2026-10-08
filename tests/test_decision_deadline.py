# SPDX-License-Identifier: Apache-2.0
"""One wall-clock deadline across a decision's remote calls (LP-16b, `providers/deadline.py`).

Past it a remote call is not made, before any credential is fetched or any budget charged; a
call under way waits at most what remains of it; either is the provider error `timeout`, so the
decision is held for a person. Local providers always answer: a possible emergency is still
flagged, and what a pack decides offline never changes. Jev, the LLM, the CQL engine and Vercel
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
from mendari.core.registry import resolve_pack
from mendari.core.spec import Outcome, Priority
from mendari.errors import MendariError, ProviderError
from mendari.evals.pack_tests import load_cases
from mendari.parallel import parallel_map
from mendari.providers import build_provider, deadline, http
from mendari.providers import cql as cql_module
from mendari.providers.credentials import ConnectorToken
from mendari.providers.jev import JevProvider
from mendari.providers.llm import LlmProvider
from mendari.server.app import create_app
from mendari.settings import decision_deadline_s
from mendari.standards.fhir_client import GuardedHttp
from tests.conftest import INBOX_DIR, message
from tests.test_community_caps import sorter

PACKS = INBOX_DIR.parent
TRIAL = "mendari/clinical-trial-prescreener"
CQL_ENGINE = "https://cql.example.org/fhir"
URL = "https://remote.test/v1"
# A deadline that has passed before the first remote call (0, not a tiny positive number, so a
# coarse clock cannot leave it in the future).
EXPIRED = 0.0
SETTING = "MENDARI_DECISION_DEADLINE_S"


def distribution(labels: list[str], chosen: str) -> dict[str, float]:
    rest = [label for label in labels if label != chosen]
    return {chosen: 0.97, **{label: 0.03 / len(rest) for label in rest}}


class Remote:
    """Jev and an LLM behind mock transports. `calls` lists each call made, in order; `delay`
    says how long a call waits before it answers (by provider type and request text); `labels`
    is what each question is answered, with probability 0.97."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.labels = {"urgency": "routine", "destination": "nurse", "topic": "other"}
        self.delay: Callable[[str, str], float] = lambda kind, text: 0.0

    def answer(self, kind: str, request: httpx.Request) -> httpx.Response:
        self.calls.append(kind)
        time.sleep(self.delay(kind, request.content.decode()))
        body = json.loads(request.content)
        if kind == "jev":
            answers = {}
            for key, question in body["questions"].items():
                chosen = self.labels[key]
                probabilities = distribution(list(question["criteria"]), chosen)
                answers[key] = {
                    "type": "choice",
                    "choice": chosen,
                    "confidence": 0.97,
                    "probabilities": probabilities,
                }
            usage = {"input_tokens": 10, "output_tokens": 1}
            return httpx.Response(
                200, json={"model": "jev-test-model", "answers": answers, "usage": usage}
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
    """Jev and the LLM set up, every client of theirs answering through `Remote`."""
    monkeypatch.setenv("JEV_API_URL", "https://jev.test/v1/decide")
    monkeypatch.setenv("JEV_MODEL", "jev-test-model")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_CONNECTOR", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")  # no built-in default model (LP-10c-ii)
    fake = Remote()
    jev_init, llm_init = JevProvider.__init__, LlmProvider.__init__

    def client(kind: str) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda r: fake.answer(kind, r)))

    def jev(self: JevProvider, *args: Any, **kwargs: Any) -> None:
        jev_init(self, *args, **{**kwargs, "client": client("jev")})

    def llm(self: LlmProvider, *args: Any, **kwargs: Any) -> None:
        llm_init(self, *args, **{**kwargs, "client": client("llm")})

    monkeypatch.setattr(JevProvider, "__init__", jev)
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
    remote: Remote, inbox_pack: Pack
) -> None:
    """Jev, asked for the screen, answers after the whole deadline, so its answer is cut too;
    the router's LLM call would start after it, so it is not made, and nothing is charged for
    it."""
    remote.delay = lambda kind, text: 0.1 if kind == "jev" else 0.0
    budget = Counted()
    decision = Decision(
        inbox_pack,
        providers={"safety_screen": "jev", "router": "llm"},
        budget=budget,
        deadline_s=0.05,
    )
    result = decision.decide(message("A rash on my arm since Tuesday."))
    assert remote.calls == ["jev"]
    assert budget.acquired == ["jev"]
    assert result.errors == {"urgency": "timeout", "destination": "timeout"}
    assert result.outcome is Outcome.ABSTAIN  # no answer for the primary: a person decides


def test_local_providers_answer_after_the_deadline(remote: Remote, inbox_pack: Pack) -> None:
    """The deadline only ever fails remote calls: the keyword rules beside Jev still read the
    message, so the possible-emergency rule (policy.yaml) still sends it to a nurse now."""
    decision = Decision(
        inbox_pack, providers={"safety_screen": "rules_and_jev"}, deadline_s=EXPIRED
    )
    result = decision.decide(message("Crushing chest pain since this morning"))
    assert remote.calls == []
    assert result.errors == {"urgency": "jev:timeout"}
    assert result.answers["urgency"].label == "urgent"
    assert result.answers["destination"].provider.type == "naive_bayes"  # local: answered
    assert (result.rule, result.outcome, result.priority, result.recommendation) == (
        "possible-emergency",
        Outcome.HUMAN_REVIEW,
        Priority.STAT,
        "nurse",
    )


def test_a_deadline_never_automates(remote: Remote, inbox_pack: Pack) -> None:
    """A case that automates with Jev's answers in time is held for a person once the deadline
    has passed, with `timeout` recorded."""
    providers = {"safety_screen": "rules_and_jev", "router": "jev"}
    case = message("Can you refill my lisinopril prescription? I have two days left.")
    in_time = Decision(inbox_pack, providers=providers, deadline_s=30).decide(case)
    assert in_time.outcome is Outcome.AUTO and in_time.errors == {}
    assert remote.calls == ["jev"]  # both slots' questions in one call
    late = Decision(inbox_pack, providers=providers, deadline_s=EXPIRED).decide(case)
    assert remote.calls == ["jev"]  # no call past the deadline
    assert late.outcome in (Outcome.HUMAN_REVIEW, Outcome.ABSTAIN)
    assert late.errors == {"urgency": "jev:timeout", "destination": "timeout"}


def test_the_deadline_belongs_to_one_decision(remote: Remote, inbox_pack: Pack) -> None:
    """One Decision serves every thread, as on the server. The slow decision's deadline passes
    during its Jev call, so that answer is cut and its LLM call is not made; the quick one
    starts after that deadline, during that call, and keeps its own."""
    remote.delay = lambda kind, text: 0.7 if kind == "jev" and "slow" in text else 0.0
    providers = {"safety_screen": "jev", "router": "llm"}
    shared = Decision(inbox_pack, providers=providers, deadline_s=0.5)

    def decide(text: str) -> DecisionResult:
        if text.startswith("quick"):
            time.sleep(0.55)  # after the slow decision's deadline, during its Jev call
        return shared.decide(message(text))

    slow, quick = parallel_map(decide, ["slow: a rash on my arm", "quick: a rash on my arm"], 2)
    assert slow.errors == {"urgency": "timeout", "destination": "timeout"}
    assert quick.errors == {} and quick.answers["destination"].label == "nurse"
    assert sorted(remote.calls) == ["jev", "jev", "llm"]
    assert deadline.remaining() is None  # none outside a decision


def test_reference_decisions_never_see_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every shipped pack on its default (local) providers decides each of its behaviour tests
    exactly as before, with a deadline that passed before it started."""
    monkeypatch.delenv(SETTING, raising=False)
    packs = [load_pack(root) for root in sorted(PACKS.iterdir()) if (root / "pack.yaml").exists()]
    assert len(packs) == 5

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
    """A community pack's patterns share a budget of CPU time (`core/patterns.py`); waiting for
    a remote member costs it nothing, and a passed deadline leaves it untouched."""

    def llm_then_rules(manifest: dict[str, Any]) -> None:
        manifest["providers"]["reader"]["variants"]["llm_then_rules"] = {
            "type": "any_of",
            "escalate": {"topic": "billing"},
            "members": {
                "llm": {"type": "llm", "state_fields": ["message"]},
                "rules": {"type": "rules", "config": "logic/rules.yaml"},
            },
        }

    pack = load_pack(sorter(tmp_path, manifest=llm_then_rules))
    assert pack.community
    monkeypatch.setattr(decision_module, "time_budget", lambda: patterns.time_budget(0.2))
    remote.delay = lambda kind, text: 0.5  # waited for, longer than the budget
    case = {"message": "A question about my bill"}
    providers = {"reader": "llm_then_rules"}

    waited = Decision(pack, providers=providers, deadline_s=30).decide(case)
    assert remote.calls == ["llm"]
    assert waited.errors == {}  # no `time_budget`: the rules ran after the wait
    assert waited.answers["topic"].label == "billing"

    late = Decision(pack, providers=providers, deadline_s=EXPIRED).decide(case)
    assert remote.calls == ["llm"]
    assert late.errors == {"topic": "llm:timeout"}
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
        return httpx.Response(429, headers={"retry-after": "1"})

    client = mock_client(handler)
    with deadline.decision_deadline(0.5), pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=5, retries=1)
    assert error.value.code == "rate_limited"
    assert waits == [] and len(calls) == 1
    # With time left, or outside a decision, a short Retry-After is waited out, as before.
    with deadline.decision_deadline(30), pytest.raises(ProviderError):
        http.post_json(client, URL, {}, headers={}, timeout_s=5, retries=1)
    assert waits == [1.0] and len(calls) == 3


class Dripping(httpx.SyncByteStream):
    """An answer that arrives a byte every 50 ms, for 2.5 s."""

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(50):
            time.sleep(0.05)
            yield b" "


def test_a_slow_response_stops_at_the_deadline() -> None:
    client = mock_client(lambda request: httpx.Response(200, stream=Dripping()))
    started = time.monotonic()
    with deadline.decision_deadline(0.3), pytest.raises(ProviderError) as error:
        http.post_json(client, URL, {}, headers={}, timeout_s=5)
    took = time.monotonic() - started
    assert error.value.code == "timeout"
    assert took < 1.0  # the deadline, one chunk, and room for a busy runner; not 2.5 s


# -- the CQL engine ----------------------------------------------------------------------------


FHIR_JSON = "application/fhir+json"


def trial_case(trial: Pack) -> dict[str, Any]:
    return dict(load_cases(trial)[0]["input"])


def test_cql_after_the_deadline_records_timeout_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = resolve_pack(TRIAL)
    monkeypatch.setenv("MENDARI_CQL_URL", CQL_ENGINE)

    def no_client(**kwargs: Any) -> GuardedHttp:
        raise AssertionError("no request to the CQL engine past the deadline")

    monkeypatch.setattr(cql_module, "GuardedHttp", no_client)
    decision = Decision(trial, providers={"structured": "cql"}, deadline_s=EXPIRED)
    result = decision.decide(trial_case(trial))
    assert not result.rejected
    structured = trial.manifest.providers["structured"].answers
    assert {result.errors[question] for question in structured} == {"timeout"}
    assert result.outcome is not Outcome.AUTO


def cql_engine(monkeypatch: pytest.MonkeyPatch, evaluate: Any) -> list[tuple[str, float]]:
    """A CQL engine that stores the Library and answers `$evaluate` with `evaluate`; the
    method and read timeout of each request it was sent."""
    seen: list[tuple[str, float]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.extensions["timeout"]["read"]))
        if request.method == "PUT":
            return httpx.Response(
                200, json={"resourceType": "Library"}, headers={"content-type": FHIR_JSON}
            )
        return evaluate(request)

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(
        cql_module,
        "GuardedHttp",
        lambda **kw: GuardedHttp(transport=transport, allowed={"https://cql.example.org"}),
    )
    return seen


def test_a_cql_request_waits_at_most_what_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Library upload and `$evaluate`, the request every decision makes once the Library
    is there, each wait at most what remains of the deadline; cut off by it, the failure is
    `timeout`. Outside a decision the same failure is `unavailable`, at the usual 10 s."""
    trial = resolve_pack(TRIAL)
    monkeypatch.setenv("MENDARI_CQL_URL", CQL_ENGINE)

    def slow(request: httpx.Request) -> httpx.Response:
        # As a real engine too slow to answer: the read times out after the timeout it was
        # given (at once outside a decision, to keep the test quick).
        waited = request.extensions["timeout"]["read"]
        time.sleep(waited + 0.05 if waited < 10 else 0)
        raise httpx.ReadTimeout("slow", request=request)

    seen = cql_engine(monkeypatch, slow)
    provider = build_provider(trial, "structured", "cql")
    with deadline.decision_deadline(0.5), pytest.raises(ProviderError) as error:
        provider.answer(trial_case(trial))
    assert error.value.code == "timeout"
    assert [method for method, _ in seen] == ["PUT", "POST"]
    assert all(0 < waited <= 0.5 for _, waited in seen)
    seen.clear()
    with deadline.decision_deadline(0.5), pytest.raises(ProviderError) as error:
        provider.answer(trial_case(trial))  # the Library is there: `$evaluate` alone
    assert error.value.code == "timeout"
    assert [method for method, _ in seen] == ["POST"] and 0 < seen[0][1] <= 0.5
    seen.clear()
    with pytest.raises(ProviderError) as error:
        provider.answer(trial_case(trial))
    assert error.value.code == "unavailable" and seen == [("POST", 10.0)]


def test_a_cql_answer_still_arriving_stops_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine that sends its answer a little at a time is cut at the deadline, not after
    every part has come (each part alone is within the clamped timeout)."""
    trial = resolve_pack(TRIAL)
    monkeypatch.setenv("MENDARI_CQL_URL", CQL_ENGINE)
    cql_engine(
        monkeypatch,
        lambda request: httpx.Response(200, stream=Dripping(), headers={"content-type": FHIR_JSON}),
    )
    provider = build_provider(trial, "structured", "cql")
    started = time.monotonic()
    with deadline.decision_deadline(0.3), pytest.raises(ProviderError) as error:
        provider.answer(trial_case(trial))
    took = time.monotonic() - started
    assert error.value.code == "timeout"
    assert took < 1.0  # the deadline, one part, and room for a busy runner; not 2.5 s


# -- credentials -------------------------------------------------------------------------------

KEY = {"token": "jev-key", "expiresAt": 4_102_444_800_000}


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

    token = ConnectorToken("jev/my-project", client=mock_client(connect), oidc=lambda: "oidc")
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
    assert token.get() == "jev-key" and len(requests) == 1  # the other thread's fetch, kept

    fresh = ConnectorToken("jev/my-project", client=mock_client(connect), oidc=lambda: "oidc")
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
    inbox_pack: Pack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SETTING, raising=False)
    assert decision_deadline_s() == 120 and Decision(inbox_pack).deadline_s == 120
    for value in ("10", "270", " 60 "):
        monkeypatch.setenv(SETTING, value)
        assert decision_deadline_s() == int(value)
    assert Decision(inbox_pack).deadline_s == 60
    assert Decision(inbox_pack, deadline_s=5).deadline_s == 5  # a caller's own, e.g. a test
