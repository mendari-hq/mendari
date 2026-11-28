# SPDX-License-Identifier: Apache-2.0
"""What an agent writes with its answers (spec section 15.2): the request names the outputs the
manifest declares for the slot, the response's `outputs` are held to them (an undeclared name, a
value that is not text or a text over its length fails the slot closed), the agent kit builds
and refuses the same, and `POST …/decide` returns the texts: the stored record has the shape,
and holds no word, of what was written. A text is kept only for a decision that waits for a
person, and then as a note, apart from the record (ADR-0027; tests/test_held_texts.py). Every
message and reply here is synthetic."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from mendari import Decision, load_agent
from mendari.agent_kit import AgentError, Case, Output, respond
from mendari.core import spec
from mendari.core.agent import Agent
from mendari.core.spec import AgentManifest, Outcome, WrittenOutput
from mendari.errors import AgentValidationError, ProviderError
from mendari.providers import agent as agent_provider
from mendari.providers.agent import (
    ENDPOINT_ENV,
    AgentProvider,
    clean_output,
    request_body,
    slot_outputs,
    validate_response,
)
from mendari.providers.base import ProviderInfo
from mendari.server.app import create_app
from tests.conftest import ROOT, message

MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "written-mini"
ACTIONS_MINI = ROOT / "tests" / "fixtures" / "mini-agents" / "actions-mini"
AGENT = "mendari-test/written-mini"
REFILL = "Could you refill my lisinopril?"
MARK = "PLANTED-OUTPUT-TEXT-7731"
DECLARED = {"reply_draft": WrittenOutput(description="A short reply.", slot="reader", max_chars=40)}
ANSWERS = {"request": {"value": "refill", "confidence": 0.95}}
# No output declared, both ways a caller may say it.
NONE: tuple[dict[str, WrittenOutput] | None, ...] = (None, {})


@pytest.fixture
def agent() -> Agent:
    return load_agent(MINI)


def questions(agent: Agent) -> dict[str, spec.Question]:
    return {"request": agent.manifest.questions["request"]}


def validated(agent: Agent, outputs: Any, declared: Any = DECLARED) -> dict[str, Any]:
    raw = {"answers": ANSWERS, "outputs": outputs}
    return validate_response(raw, questions(agent), outputs=declared)


# -- the response --------------------------------------------------------------------------------


def test_a_declared_output_is_read_and_one_left_out_is_no_output(agent: Agent) -> None:
    assert validated(agent, {"reply_draft": "Thank you."})["outputs"] == {
        "reply_draft": "Thank you."
    }
    assert validated(agent, {})["outputs"] == {}
    assert validated(agent, None)["outputs"] == {}
    absent = validate_response({"answers": ANSWERS}, questions(agent), outputs=DECLARED)
    assert absent["outputs"] == {}
    # A blank text is no output, as one left out is.
    for blank in ("", "   ", "\n\t \n", "\x00\x01"):
        assert validated(agent, {"reply_draft": blank})["outputs"] == {}


@pytest.mark.parametrize(
    ("outputs", "said"),
    [
        ({"summary": "A text."}, "outputs names an output this slot does not declare"),
        ({"reply_draft": "ok", MARK: "x"}, "outputs names an output this slot does not declare"),
        ({"reply_draft": 5}, "output 'reply_draft' is not text"),
        ({"reply_draft": None}, "output 'reply_draft' is not text"),
        ({"reply_draft": ["a", "b"]}, "output 'reply_draft' is not text"),
        ({"reply_draft": {"text": "a"}}, "output 'reply_draft' is not text"),
        ({"reply_draft": "x" * 41}, "output 'reply_draft' is longer than its max_chars"),
        ([], "outputs must be an object"),
        ("a text", "outputs must be an object"),
        ({"reply_draft": MARK * 9}, "output 'reply_draft' is longer than its max_chars"),
    ],
)
def test_a_response_that_breaks_the_contract_is_invalid_and_never_echoed(
    agent: Agent, outputs: Any, said: str
) -> None:
    with pytest.raises(ProviderError) as refused:
        validated(agent, outputs)
    assert refused.value.code == "invalid_response"
    assert refused.value.detail == said
    assert MARK not in str(refused.value) and MARK not in (refused.value.detail or "")


def test_the_length_limit_is_the_outputs_own(agent: Agent) -> None:
    assert validated(agent, {"reply_draft": "x" * 40})["outputs"] == {"reply_draft": "x" * 40}
    longest = {"reply_draft": WrittenOutput(description="A reply.", slot="reader", max_chars=8000)}
    assert (
        len(validated(agent, {"reply_draft": "y" * 8000}, longest)["outputs"]["reply_draft"])
        == 8000
    )
    assert spec.MAX_OUTPUT_CHARS == 8000 and spec.DEFAULT_OUTPUT_CHARS == 4000


def test_control_characters_become_a_space_and_newlines_and_tabs_stay(agent: Agent) -> None:
    text = "Line one.\nLine\ttwo.\x00\x07\x1bEnd.\r\nLast\x7f."
    cleaned = validated(agent, {"reply_draft": text})["outputs"]["reply_draft"]
    assert cleaned == "Line one.\nLine\ttwo. End. \nLast ."
    assert validated(agent, {"reply_draft": "  padded \n"})["outputs"] == {"reply_draft": "padded"}


def test_characters_that_are_not_seen_become_a_space_and_what_a_script_needs_stays(
    agent: Agent,
) -> None:
    """A reviewer releases the text they read. A character that is not seen and changes what
    is makes the screen show one thing while a channel that drops it, or a screen reader, says
    another: `Take` + a right-to-left override + `01` is drawn as "Take 10". So each becomes a
    space, as a control character does: what reorders a line (bidirectional embeddings,
    overrides, isolates and marks), zero-width and invisible characters, separators that break
    a line in some places only, and tag characters. What a script or an emoji needs stays."""
    hidden = (
        "Take \u202e01\u202c mg\u200b now\u2066.\u2069 Re\u00adfill\ufeff "
        "\U000e0041ok\u200f\u0085x\u2028y"
    )
    cleaned = validated(agent, {"reply_draft": hidden})["outputs"]["reply_draft"]
    assert cleaned == "Take  01  mg  now .  Re fill   ok x y"  # a run of them is one space
    for code in (0x202A, 0x202D, 0x2067, 0x2068, 0x061C, 0x180E, 0x2060, 0x2064, 0x206F, 0xFFF9):
        assert clean_output(f"a{chr(code)}b") == "a b", hex(code)
    for code in (0xE0001, 0xE0020, 0xE007F, 0x009B):
        assert clean_output(f"a{chr(code)}b") == "a b", hex(code)
    # Every code point Unicode says is not shown by default (Default_Ignorable_Code_Point):
    # fillers, a grapheme joiner, and the 254 variation selectors that are no presentation
    # selector, each of which could carry a byte of a text nobody sees.
    ignorable = (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180F, 0x3164, 0xFFA0, 0xFFF0)
    for code in (*ignorable, 0xFE00, 0xFE0D, 0x1BCA0, 0x1D173, 0x1D17A, 0xE0100, 0xE01EF, 0xE0FFF):
        assert clean_output(f"a{chr(code)}b") == "a b", hex(code)
    assert clean_output("a\u2800b") == "a b"  # a braille cell with no dot: blank, and no space
    hidden_bytes = "Take 10 mg" + "".join(chr(0xE0100 + byte) for byte in b"stop insulin")
    assert clean_output(hidden_bytes) == "Take 10 mg"
    # A joiner inside a word or an emoji, a variation selector, and letters of any script.
    kept = (
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 "  # Persian, a non-joiner inside
        "\U0001f469\u200d\u2695\ufe0f "  # an emoji made of two, joined, with a selector
        "a\u00f1adir \u85ac \u05e9\u05dc\u05d5\u05dd"  # Spanish, Japanese, Hebrew
    )
    assert clean_output(kept) == kept
    assert clean_output("\u202e\u200b") == ""  # nothing is left: no text at all


def test_a_rationale_and_a_tool_name_carry_no_character_that_is_not_seen(agent: Agent) -> None:
    """An answer's evidence is what a reviewer reads to judge it: a rationale and the tools
    the agent says it called. The same characters become a space there, and a line break too:
    evidence is one line."""
    called: list[Any] = ["lookup\u200e_one", {"name": "look\u2066up\u2069_two"}]
    raw = {
        "answers": {
            "request": {
                "value": "refill",
                "confidence": 0.95,
                "rationale": "Asked for \u202e01\u202c tablets\u200b.\nSecond line.",
            }
        },
        "tool_calls": called,
    }
    found = validate_response(raw, questions(agent))
    assert found["answers"]["request"]["rationale"] == "Asked for  01  tablets . Second line."
    assert agent_provider._tool_evidence(called) == [
        "tool:lookup _one",
        "tool:look up _two",
    ]


def test_with_no_output_declared_the_key_is_not_read_at_all(agent: Agent) -> None:
    """An agent whose slot declares no output answers as it always did: an `outputs` key in its
    response is not part of its contract and is not read, whatever it holds."""
    for declared in NONE:
        raw = {"answers": ANSWERS, "outputs": {"anything": 5, MARK: [MARK]}}
        assert "outputs" not in validate_response(raw, questions(agent), outputs=declared)


# -- the request ---------------------------------------------------------------------------------


def test_the_request_names_the_outputs_the_slot_writes(agent: Agent) -> None:
    declared = slot_outputs(agent.manifest, "reader", "agent")
    assert list(declared) == ["reply_draft"]  # `given_draft` is the input's, not the agent's
    body = request_body(agent.id, "reader", questions(agent), message(REFILL), outputs=declared)
    assert body["outputs"] == [
        {
            "name": "reply_draft",
            "description": agent.manifest.outputs["reply_draft"].description,
            "max_chars": 600,
        }
    ]
    for none in NONE:
        body = request_body(agent.id, "reader", questions(agent), message(REFILL), outputs=none)
        assert "outputs" not in body
    # A composite's member, another slot and a variant that is no agent write nothing.
    assert slot_outputs(agent.manifest, "reader", "agent.first") == {}
    assert slot_outputs(agent.manifest, "nobody", "agent") == {}
    older = load_agent(ACTIONS_MINI)
    assert slot_outputs(older.manifest, "reader", "agent") == {}


def provider(agent: Agent, handler: Any) -> AgentProvider:
    slot = agent.manifest.providers["reader"]
    config = {"type": "agent", **(slot.variants["agent"].model_extra or {})}
    info = ProviderInfo(slot="reader", variant="agent", type="agent")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return AgentProvider(agent, info, questions(agent), config, client=client)


def test_a_live_agent_is_asked_for_its_outputs_and_its_text_is_the_slots(
    agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, "https://agent.example.org/decide")
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", "https://agent.example.org")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": ANSWERS, "outputs": {"reply_draft": "Hello."}})

    output = provider(agent, handler).answer(message(REFILL))
    assert output.outputs == {"reply_draft": "Hello."}
    assert [item["name"] for item in seen[0]["outputs"]] == ["reply_draft"]

    def undeclared(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": ANSWERS, "outputs": {"summary": MARK}})

    with pytest.raises(ProviderError) as refused:
        provider(agent, undeclared).answer(message(REFILL))
    assert refused.value.code == "invalid_response" and MARK not in str(refused.value)


def test_a_slot_whose_output_is_malformed_fails_closed(
    agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENDPOINT_ENV, "https://agent.example.org/decide")
    monkeypatch.setenv("MENDARI_PROVIDER_ALLOWED_ORIGINS", "https://agent.example.org")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": ANSWERS, "outputs": {"reply_draft": 7}})

    decision = Decision(agent, origin_cap=False)
    decision.providers["reader"] = provider(agent, handler)
    result = decision.decide(message(REFILL))
    assert result.errors == {"request": "invalid_response"}
    assert result.outcome is not Outcome.AUTO and result.outputs == {}


# -- the canned fixture ----------------------------------------------------------------------------


def test_a_canned_response_carries_its_outputs(agent: Agent) -> None:
    decision = Decision(agent, origin_cap=False)
    written = decision.decide(message(REFILL))
    assert written.outcome is Outcome.AUTO
    assert written.outputs["reply_draft"].startswith("Thank you for your message. The pharmacy")
    # A response with no `outputs`, and one whose text is blank: no output, and no failure.
    for body in ("I have nothing to say today.", "Please send a blank reply."):
        nothing = decision.decide(message(body))
        assert nothing.outputs == {} and nothing.errors == {}
    # In memory only: not in the caller's dictionary, and not in the record.
    assert "outputs" not in written.to_dict() and "outputs" not in written.to_record()


def copied(tmp_path: Path, change: Any) -> Path:
    folder = tmp_path / "written-mini"
    shutil.copytree(MINI, folder)
    fixture = json.loads((folder / "fixtures" / "mock_agent.json").read_text())
    change(fixture)
    (folder / "fixtures" / "mock_agent.json").write_text(json.dumps(fixture))
    return folder


@pytest.mark.parametrize(
    ("outputs", "said"),
    [
        ({"summary": "A text."}, "outputs names an output this slot does not declare"),
        ({"given_draft": "A text."}, "outputs names an output this slot does not declare"),
        ({"reply_draft": 9}, "output 'reply_draft' is not text"),
        ({"reply_draft": "x" * 601}, "output 'reply_draft' is longer than its max_chars"),
    ],
)
def test_a_canned_response_is_held_to_the_contract_at_load(
    tmp_path: Path, outputs: Any, said: str
) -> None:
    def change(fixture: dict[str, Any]) -> None:
        fixture["cases"][0]["response"]["outputs"] = outputs

    folder = copied(tmp_path, change)
    with pytest.raises(AgentValidationError) as refused:
        Decision(load_agent(folder), origin_cap=False)
    assert f"cases[0].response: {said}" in str(refused.value)
    assert "providers.reader.variants.agent" in "; ".join(load_agent(folder).warnings)


def test_an_agent_that_declares_no_output_keeps_its_fixture_as_it_was(tmp_path: Path) -> None:
    folder = tmp_path / "written-mini"
    shutil.copytree(MINI, folder)
    manifest = yaml.safe_load((folder / "agent.yaml").read_text())
    for key in ("outputs", "rubrics"):
        manifest.pop(key)
    (folder / "agent.yaml").write_text(yaml.safe_dump(manifest))
    result = Decision(load_agent(folder), origin_cap=False).decide(message(REFILL))
    assert result.outcome is Outcome.AUTO and result.outputs == {}


# -- the agent kit ---------------------------------------------------------------------------------


def kit_case(agent: Agent) -> Case:
    declared = slot_outputs(agent.manifest, "reader", "agent")
    return Case.parse(
        request_body(agent.id, "reader", questions(agent), message(REFILL), outputs=declared)
    )


def test_the_kit_shows_what_is_asked_and_builds_what_the_engine_takes(agent: Agent) -> None:
    case = kit_case(agent)
    description = agent.manifest.outputs["reply_draft"].description
    assert case.outputs == (Output("reply_draft", description, 600),)
    body = respond(case, {"request": ("refill", 0.95)}, outputs={"reply_draft": "Thank you."})
    assert body["outputs"] == {"reply_draft": "Thank you."}
    declared = slot_outputs(agent.manifest, "reader", "agent")
    assert validate_response(body, questions(agent), outputs=declared)["outputs"] == body["outputs"]
    empty: dict[str, str] = {}
    for nothing in (empty, None):
        assert "outputs" not in respond(case, {"request": ("refill", 0.95)}, outputs=nothing)
    older = Case.parse(request_body(agent.id, "reader", questions(agent), message(REFILL)))
    assert older.outputs == ()


@pytest.mark.parametrize(
    ("outputs", "said"),
    [
        ({"summary": MARK}, "the agent wrote an output the case does not ask for"),
        ({"reply_draft": 5}, "the output 'reply_draft' is not text"),
        ({"reply_draft": MARK * 40}, "the output 'reply_draft' is longer than its max_chars"),
        ([("reply_draft", "x")], "outputs: {name: text} expected"),
    ],
)
def test_the_kit_refuses_what_the_engine_would(agent: Agent, outputs: Any, said: str) -> None:
    with pytest.raises(AgentError) as refused:
        respond(kit_case(agent), {"request": ("refill", 0.95)}, outputs=outputs)
    assert refused.value.status == 502 and refused.value.message == said
    assert MARK not in refused.value.message


@pytest.mark.parametrize(
    "outputs",
    [
        "reply_draft",
        [{"name": "reply_draft"}],
        [{"name": "reply_draft", "max_chars": 0}],
        [{"name": "reply_draft", "max_chars": 8001}],
        [{"name": "reply_draft", "max_chars": True}],
        [{"name": "Reply Draft", "max_chars": 10}],
        [{"name": "a", "max_chars": 10}, {"name": "a", "max_chars": 10}],
        [{"name": f"o{n}", "max_chars": 10} for n in range(9)],
    ],
)
def test_the_kit_refuses_a_request_the_engine_would_not_send(agent: Agent, outputs: Any) -> None:
    body = request_body(agent.id, "reader", questions(agent), message(REFILL))
    with pytest.raises(AgentError) as refused:
        Case.parse({**body, "outputs": outputs})
    assert refused.value.status == 400


def test_the_kits_limits_for_outputs_are_the_engines() -> None:
    from mendari import agent_kit

    assert agent_kit.MAX_OUTPUTS == spec.MAX_OUTPUTS
    assert agent_kit.MAX_OUTPUT_CHARS == spec.MAX_OUTPUT_CHARS
    assert "Output" in agent_kit.__all__


# -- over HTTP -------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    base = tmp_path / "agents"
    shutil.copytree(MINI, base / "written-mini")
    shutil.copytree(ACTIONS_MINI, base / "actions-mini")
    app = create_app(db_path=tmp_path / "open.db", agents_paths=[base])
    with TestClient(app) as opened:
        yield opened


def test_a_text_is_handed_only_to_a_person_and_kept_only_for_a_decision_that_waits(
    client: TestClient, tmp_path: Path
) -> None:
    """A text is handed only to a person (ADR-0027): `POST …/decide` returns what the agent
    wrote only with a decision that waits for one, as its draft. An automated decision returns
    none of it and keeps none of it, and still automates. Either way the answer and the stored
    record say where each text stands (`written`), and no record or chained event holds a word
    of it. A held text is kept only as a note: the trail shows it, where the decision's input is
    shown, under `outputs`. Open mode, under no PHI mode: a note is in clear there."""

    def decide(body: str) -> dict[str, Any]:
        answer = client.post(f"/api/agents/{AGENT}/decide", json=message(body))
        assert answer.status_code == 200, answer.text
        decided: dict[str, Any] = answer.json()
        return decided

    # One decision as the policy serves it, then one that waits for a person whatever the
    # policy: the agent's automation is put on hold.
    served = decide(REFILL)
    assert served["outcome"] == "AUTO"  # the text changes nothing about the decision
    assert "outputs" not in served and "pharmacy team" not in json.dumps(served)
    assert served["written"] == {"reply_draft": {"state": "not_released"}}
    hold = client.post("/api/holds", json={"agent_id": AGENT, "reason": "a drill"})
    assert hold.status_code == 201, hold.text
    waiting = decide("I need to move my appointment.")
    assert waiting["outcome"] == "HUMAN_REVIEW"
    assert waiting["outputs"]["reply_draft"].endswith("The scheduling team will contact you.")
    assert waiting["written"] == {"reply_draft": {"state": "held"}}
    with sqlite3.connect(tmp_path / "open.db") as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        tables = {
            name: json.dumps(db.execute(f'SELECT * FROM "{name}"').fetchall(), default=str)
            for name in names
        }
    assert served["id"] in tables["decisions"] and waiting["id"] in tables["decisions"]
    for decided, words in ((served, "pharmacy team will review"), (waiting, "scheduling team")):
        waits = decided["outcome"] in ("HUMAN_REVIEW", "ABSTAIN")
        stored = client.get(f"/api/audit/{decided['id']}").json()
        # The stored record says where the text stands and holds no word of it: no `outputs`
        # key, and nothing of the reply in it or in an event.
        assert "outputs" not in stored["record"]
        assert stored["record"]["written"] == decided["written"]
        assert words not in json.dumps([stored["record"], [e["payload"] for e in stored["events"]]])
        holding = sorted(name for name, text in tables.items() if words in text)
        if waits:  # held for its reviewer: a note, and what the trail shows of it
            reply = decided["outputs"]["reply_draft"]
            assert holding == ["notes"], holding
            assert stored["outputs"] == [
                {"name": "reply_draft", "state": "held", "text_status": "open", "text": reply}
            ]
        else:  # nobody waits to read it: no table holds a word of it, and no answer
            assert holding == [], holding
            assert "outputs" not in stored and words not in json.dumps(stored)
    # A case with no output, and an agent that declares none: neither key at all.
    nothing = client.post(f"/api/agents/{AGENT}/decide", json=message("I have nothing to say."))
    assert "outputs" not in nothing.json() and "written" not in nothing.json()
    other = client.post("/api/agents/mendari-test/actions-mini/decide", json=message(REFILL)).json()
    assert "outputs" not in other and "written" not in other


@pytest.mark.parametrize(
    ("outcome", "state", "handed"),
    [
        (Outcome.AUTO, "not_released", False),
        (Outcome.HUMAN_REVIEW, "held", True),
        (Outcome.ABSTAIN, "held", True),
        (Outcome.BLOCK, "not_released", False),
    ],
)
def test_where_a_text_stands_follows_whether_a_person_awaits_the_decision(
    agent: Agent, outcome: Outcome, state: str, handed: bool
) -> None:
    """`DecisionResult.written` and `drafts()` (ADR-0027): a text is `held`, and handed over as
    a draft, exactly when the decision awaits a person; with an automated or a blocked decision
    it is `not_released` and `drafts()` is empty, whatever the agent wrote. `outputs` still holds
    every text in memory: an evaluation grades them, and nothing else may hand them on."""
    decided = Decision(agent).decide(message(REFILL))
    assert decided.outputs and decided.outcome is Outcome.AUTO
    result = dataclasses.replace(decided, outcome=outcome)
    assert result.written == {"reply_draft": {"state": state}}
    assert result.drafts() == (dict(decided.outputs) if handed else {})
    assert result.to_dict()["written"] == result.written == result.to_record()["written"]
    assert "outputs" not in result.to_dict() and "outputs" not in result.to_record()
    nothing = dataclasses.replace(result, outputs={})
    assert nothing.written == {} and nothing.drafts() == {} and "written" not in nothing.to_dict()


def test_the_answers_schema_says_where_a_text_stands_and_what_a_draft_is(
    agent: Agent, tmp_path: Path
) -> None:
    """The schema an agent's detail serves for its answers (`output_schema`) describes
    `written` and `outputs` for an agent that declares outputs, and an answer of either kind
    matches it: one that waits, with its draft, and one that automated, without. An agent that
    writes nothing has a schema with neither, as it was."""
    from jsonschema import Draft202012Validator

    from mendari import AuditStore, Decision

    schema = agent.output_schema
    stands = {
        "type": "object",
        "required": ["state"],
        "properties": {"state": {"enum": ["held", "not_released"]}},
    }
    assert schema["properties"]["written"]["properties"] == dict.fromkeys(
        agent.manifest.outputs, stands
    )
    drafts = schema["properties"]["outputs"]
    assert set(drafts["properties"]) == set(agent.manifest.outputs) >= {"reply_draft"}
    assert drafts["properties"]["reply_draft"]["type"] == "string"
    assert drafts["properties"]["reply_draft"]["maxLength"] == (
        agent.manifest.outputs["reply_draft"].max_chars
    )
    assert "written" not in load_agent(ACTIONS_MINI).output_schema["properties"]
    checker = Draft202012Validator(schema)
    with AuditStore(str(tmp_path / "a.db")) as store:
        automated = Decision(agent, store=store).decide(message(REFILL))
        waiting = Decision(agent, store=store, hold_source=lambda agent: "a drill").decide(
            message(REFILL)
        )
    assert (automated.outcome, waiting.outcome) == (Outcome.AUTO, Outcome.HUMAN_REVIEW)
    checker.validate(automated.to_dict())
    checker.validate({**waiting.to_dict(), "outputs": waiting.drafts()})
    assert waiting.drafts()  # the draft is there, and the schema takes it
    wrong = {**waiting.to_dict(), "written": {"reply_draft": {"state": "released"}}}
    assert list(checker.iter_errors(wrong))  # a record says held or not released, no more


def test_the_manifest_of_every_agent_with_outputs_is_valid() -> None:
    manifest = load_agent(MINI).manifest
    assert AgentManifest.model_validate(manifest.model_dump(mode="json", by_alias=True)) == manifest


def test_the_command_line_and_mcp_hand_no_text_with_an_automated_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every surface that serves a decision keeps the rule (ADR-0027), not `POST …/decide`
    alone: `mendari decide` prints where an automated decision's text stands and not a word of
    it, and an MCP `decide` answer never carries a text at all."""
    from mendari import AuditStore
    from mendari.cli import main
    from mendari.mcp import McpServer

    base = tmp_path / "agents"
    shutil.copytree(MINI, base / "written-mini")
    monkeypatch.setenv("MENDARI_AGENTS_PATH", str(base))
    assert main(["decide", AGENT, "--text", REFILL]) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    assert shown["outcome"] == "AUTO" and "outputs" not in shown
    assert shown["written"] == {"reply_draft": {"state": "not_released"}}
    assert "pharmacy team" not in printed
    with AuditStore(":memory:") as store:
        params = {"name": "decide", "arguments": {"agent": AGENT, "input": message(REFILL)}}
        answer = McpServer(store).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )
    assert answer is not None
    told = json.dumps(answer)
    assert "AUTO" in told  # the decision automated, and its answer holds no word of the text
    assert "pharmacy team" not in told and "outputs" not in told
