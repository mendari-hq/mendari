# SPDX-License-Identifier: Apache-2.0
"""A lone surrogate, wherever a text enters: read as U+FFFD, the replacement character.

A JSON string can hold half a character (`"\\ud83d"`: half an emoji, as a system that cuts a text
at a length counted in UTF-16 units sends it), and no UTF-8 text can. Kept as it came, a
decision was recorded and then could not be served: the audit trail's list answered 500 for
every reader, and so did the review queue when the half character was in an answer's
rationale. A patient's message is not refused for it: its patterns already read one as U+FFFD
(`core/patterns.py`), and now so does everything that keeps or sends the text
(`core/text.py` `well_formed`): a decision's input, and a provider's answer. Every message here
is synthetic."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mendari import AuditStore, Decision, load_agent
from mendari.core.text import well_formed
from mendari.providers import agent as agent_provider
from mendari.providers.agent import ENDPOINT_ENV
from mendari.server.app import create_app
from tests.conftest import ROOT, message

ACTIONS_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
WRITTEN_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
ACTIONS = "mendari-test/actions-mini"
WRITTEN = "mendari-test/written-mini"
HALF = json.loads('"\\ud83d"')  # the first half of an emoji, alone
SHOWN = "�"  # how it is read: the replacement character


def raw(body: dict[str, Any]) -> str:
    """`body` as JSON whose `HALF` is written as its escape, as a caller's own encoder writes it."""
    return json.dumps(body)  # ensure_ascii: the half character as `\\ud83d`


def servable(value: Any) -> bool:
    """Whether `value` can be sent as a JSON response: UTF-8, as every route encodes one."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def test_well_formed_reads_each_lone_surrogate_as_the_replacement_character() -> None:
    whole = {"message": {"body": "A refill \U0001f600 please", "n": [1, 2.5, None, True]}}
    assert well_formed(whole) is whole  # nothing to change: the same object, in one pass
    broken = {f"k{HALF}": [f"cut {HALF}", {"deep": [[f"{HALF}{HALF}"]]}], "ok": "\U0001f600"}
    assert not servable(broken)
    made = well_formed(broken)
    assert made == {f"k{SHOWN}": [f"cut {SHOWN}", {"deep": [[SHOWN * 2]]}], "ok": "\U0001f600"}
    assert servable(made) and not servable(broken)  # a copy: what it was given is as it was
    low = json.loads('"\\udc00 tail"')  # the second half alone is no character either
    assert well_formed([low]) == [f"{SHOWN} tail"]
    # As deep as a JSON body may nest, without a deeper stack than the parser's own.
    nested: Any = f"leaf {HALF}"
    for _ in range(200):
        nested = [nested]
    flat = well_formed(nested)
    for _ in range(200):
        flat = flat[0]
    assert flat == f"leaf {SHOWN}"
    # What is no JSON value is not this function's to judge: whoever holds it to JSON says so.
    other = {"when": object()}
    assert well_formed(other) is other


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    agents = tmp_path / "agents"
    shutil.copytree(ACTIONS_MINI, agents / "actions-mini")
    app = create_app(db_path=tmp_path / "a.db", agents_paths=[agents])
    with TestClient(app, raise_server_exceptions=False) as opened:
        yield opened


def test_a_message_with_half_a_character_is_decided_and_its_record_is_served(
    client: TestClient,
) -> None:
    """The message is not refused: it is decided as the same words with the replacement
    character, kept so, and its decision can be listed, read and reviewed. Before, the decision
    was recorded and the audit trail's list answered 500 from then on."""
    said = f"I would like to move my visit {HALF}"
    sent = client.post(
        f"/api/agents/{ACTIONS}/decide",
        content=raw(message(said)),
        headers={"content-type": "application/json"},
    )
    assert sent.status_code == 200, sent.text
    decided = sent.json()
    same = client.post(f"/api/agents/{ACTIONS}/decide", json=message(said.replace(HALF, SHOWN)))
    assert (decided["outcome"], decided["recommendation"]) == (
        same.json()["outcome"],
        same.json()["recommendation"],
    )
    assert decided["input_hash"] == same.json()["input_hash"]  # one input, however it was sent
    listed = client.get("/api/audit", params={"agent_id": ACTIONS})
    assert listed.status_code == 200, listed.text
    assert decided["id"] in [item["id"] for item in listed.json()]
    kept = client.get(f"/api/audit/{decided['id']}")
    assert kept.status_code == 200, kept.text
    assert kept.json()["record"]["input"]["message"]["body"] == said.replace(HALF, SHOWN)
    assert client.get("/api/review").status_code == 200
    assert client.get(f"/fhir/Task/{decided['id']}").status_code == 200
    assert client.get("/api/audit/verify").json()["ok"]


def test_a_key_and_a_nested_value_are_read_the_same_way(tmp_path: Path) -> None:
    """Through the engine itself, as the command line, the SDK and every route reach it: a key
    of the input and a value inside it. What is kept can be served."""
    with AuditStore(str(tmp_path / "a.db")) as store:
        decision = Decision(load_agent(ACTIONS_MINI), store=store)
        given = {"message": {"body": f"A refill, please {HALF}", f"extra{HALF}": [HALF]}}
        result = decision.decide(given)
        found = store.get(result.id)
        assert found is not None and servable(found)
        again = decision.decide(well_formed(given))
        assert result.input_hash == again.input_hash


def test_an_answer_with_half_a_character_is_read_and_its_decision_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service that cuts its text, its rationale or a tool's name mid-character: each is read
    with the replacement character, which a reviewer sees, and the decision is kept, listed and
    reviewed as any other. Before, a rationale or a tool's name so cut took the review queue and
    the audit trail's list down for that agent, and a text so cut failed the decision (500)."""
    answer = {
        "answers": {
            "request": {"value": "refill", "confidence": 0.6, "rationale": f"because {HALF}"}
        },
        "outputs": {"reply_draft": f"Take 10 mg {HALF} now"},
        "tool_calls": [f"look{HALF}up"],
    }
    body = raw(answer).encode()
    assert b"\\ud83d" in body  # as the service's own encoder wrote it: an escape

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    monkeypatch.setenv(ENDPOINT_ENV, "https://agent.example.org/decide")
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", "https://agent.example.org")
    # What the provider's own reader hands on, whichever provider asked.
    posted = agent_provider.post_json(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "https://agent.example.org/decide",
        {"x": 1},
        headers={},
        timeout_s=5,
    )
    assert servable(posted)
    assert posted["answers"]["request"]["rationale"] == f"because {SHOWN}"
    assert posted["outputs"] == {"reply_draft": f"Take 10 mg {SHOWN} now"}
    assert posted["tool_calls"] == [f"look{SHOWN}up"]
    # And through a server: the decision is made, waits, and every list of it is served.
    original = agent_provider.AgentProvider.__init__

    def init(self: agent_provider.AgentProvider, *args: Any, **kwargs: Any) -> None:
        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(agent_provider.AgentProvider, "__init__", init)
    agents = tmp_path / "agents"
    shutil.copytree(WRITTEN_MINI, agents / "written-mini")
    app = create_app(db_path=tmp_path / "live.db", agents_paths=[agents])
    with TestClient(app, raise_server_exceptions=False) as client:
        sent = client.post(
            f"/api/agents/{WRITTEN}/decide", json=message("Could you refill my lisinopril?")
        )
        assert sent.status_code == 200, sent.text
        decided = sent.json()
        assert decided["outcome"] == "HUMAN_REVIEW"  # a confidence of 0.6: a person reads it
        assert decided["outputs"] == {"reply_draft": f"Take 10 mg {SHOWN} now"}
        evidence = decided["answers"]["request"]["evidence"]
        assert f"because {SHOWN}" in " ".join(evidence) and f"tool:look{SHOWN}up" in evidence
        queue = client.get("/api/review", params={"agent_id": WRITTEN})
        assert queue.status_code == 200, queue.text
        (item,) = queue.json()
        assert item["outputs"][0]["text"] == f"Take 10 mg {SHOWN} now"
        listed = client.get("/api/audit", params={"agent_id": WRITTEN})
        assert listed.status_code == 200, listed.text
        assert client.get(f"/api/audit/{decided['id']}").status_code == 200
        assert client.get("/api/audit/verify", params={"notes": "true"}).json()["ok"]
