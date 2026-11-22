# SPDX-License-Identifier: Apache-2.0
"""`mendari mcp` (src/mendari/mcp.py): governed decisions as MCP tools for other AI agents."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mendari.core.audit import AuditStore
from mendari.mcp import PROTOCOL_VERSIONS, QUEUE_FIELDS, McpServer, serve

ROOT = Path(__file__).resolve().parents[1]
AGENT = "mendari/portal-message-agent"
URGENT = {
    "message": {"body": "Please refill my inhaler. Also I have had chest pain since last night."}
}
REFILL = {"message": {"body": "Can you send a refill of my metformin to the pharmacy?"}}


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[McpServer]:
    """The bundled demo agent on the search path (the tests' bundled agents are fixtures)."""
    monkeypatch.setenv("MENDARI_PACKS_PATH", str(ROOT / "agents"))
    with AuditStore(":memory:") as store:
        yield McpServer(store)


def call(server: McpServer, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments or {}}
    answer = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params})
    assert answer is not None and answer["id"] == 7
    result: dict[str, Any] = answer["result"]
    # The text content and the structured content say the same thing.
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result


def test_initialize_agrees_a_protocol_version_and_offers_tools_only(server: McpServer) -> None:
    for asked in PROTOCOL_VERSIONS:
        answer = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": asked},
            }
        )
        assert answer is not None
        assert answer["result"]["protocolVersion"] == asked
        assert answer["result"]["capabilities"] == {"tools": {"listChanged": False}}
    unknown = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}}
    )
    assert unknown is not None and unknown["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


def test_the_tools_listed_are_the_four_with_closed_schemas(server: McpServer) -> None:
    answer = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert answer is not None
    tools = answer["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "list_agents", "decide", "review_queue", "verify_audit",
    ]  # fmt: skip
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False


def test_list_agents_describes_the_demo_agent(server: McpServer) -> None:
    agents = call(server, "list_agents")["structuredContent"]["agents"]
    (demo,) = [agent for agent in agents if agent["id"] == AGENT]
    assert demo["risk_level"] == 0 and demo["primary"] == "request"
    assert demo["questions"]["urgent_symptoms"]["labels"] == ["yes", "no"]


def test_a_decision_is_governed_and_recorded(server: McpServer) -> None:
    urgent = call(server, "decide", {"agent": AGENT, "input": URGENT})
    assert urgent["isError"] is False
    shown = urgent["structuredContent"]
    # The policy's rule, not the agent's confidence, decides: a person reads it today.
    assert (shown["outcome"], shown["priority"]) == ("HUMAN_REVIEW", "urgent")
    assert shown["answers"]["urgent_symptoms"]["label"] == "yes"
    routine = call(server, "decide", {"agent": AGENT, "input": REFILL})["structuredContent"]
    assert (routine["outcome"], routine["recommendation"]) == ("AUTO", "refill")
    # Both are in the audit trail, and the chain holds.
    assert server.store.get(shown["id"]) is not None
    verified = call(server, "verify_audit")["structuredContent"]
    assert verified["ok"] is True and verified["events"] >= 2


def test_an_input_outside_the_schema_is_blocked_not_sent(server: McpServer) -> None:
    shown = call(server, "decide", {"agent": AGENT, "input": {"nope": 1}})["structuredContent"]
    assert shown["outcome"] == "BLOCK" and shown["answers"] == {}


def test_the_review_queue_never_shows_the_input(server: McpServer) -> None:
    call(server, "decide", {"agent": AGENT, "input": URGENT})
    pending = call(server, "review_queue", {"limit": 5})["structuredContent"]["pending"]
    assert len(pending) == 1 and set(pending[0]) == set(QUEUE_FIELDS)
    assert "chest" not in json.dumps(pending)
    assert call(server, "review_queue", {"agent": "x/none"})["structuredContent"] == {"pending": []}


@pytest.mark.parametrize(
    ("name", "arguments", "says"),
    [
        ("decide", {"agent": "x/none", "input": {}}, "x/none"),
        ("decide", {"agent": AGENT}, "input:"),
        ("decide", {"agent": AGENT, "input": [], "extra": 1}, "unknown arguments: extra"),
        ("review_queue", {"limit": 0}, "limit:"),
        ("review_queue", {"limit": True}, "limit:"),
        ("verify_audit", {"x": 1}, "unknown arguments: x"),
    ],
)
def test_a_bad_call_is_a_tool_error_the_caller_can_read(
    server: McpServer, name: str, arguments: dict[str, Any], says: str
) -> None:
    result = call(server, name, arguments)
    assert result["isError"] is True and says in result["structuredContent"]["error"]


def test_protocol_errors_are_json_rpc_errors(server: McpServer) -> None:
    def code(message: Any) -> int:
        answer = server.handle(message)
        assert answer is not None
        return int(answer["error"]["code"])

    assert code({"jsonrpc": "2.0", "id": 1, "method": "nope"}) == -32601
    assert (
        code({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}) == -32602
    )
    assert code({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}) == -32602
    assert code({"id": 1, "method": "ping"}) == -32600
    # A notification is never answered, nor is an answer sent to the server.
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None


def test_the_stdio_transport_answers_line_by_line(server: McpServer) -> None:
    lines = [
        "not json",
        "",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ]
    out = io.StringIO()
    serve(server, io.StringIO("\n".join(lines) + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a.get("error", {}).get("code") for a in answers] == [-32700, None]
    assert answers[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_mendari_mcp_runs_as_a_command(tmp_path: Path) -> None:
    """What an MCP client does: start the command, initialize, decide, read the trail after."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "decide", "arguments": {"agent": AGENT, "input": URGENT}}},
    ]  # fmt: skip
    db = tmp_path / "mcp.db"
    done = subprocess.run(
        [sys.executable, "-m", "mendari.cli", "mcp", "--db", str(db)],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "", "MENDARI_PACKS_PATH": str(ROOT / "agents"), "HOME": str(tmp_path)},
    )
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2]
    decided = answers[1]["result"]["structuredContent"]
    with AuditStore(db) as store:
        recorded = store.get(decided["id"])
        assert recorded is not None and recorded["outcome"] == "HUMAN_REVIEW"


def test_input_no_parser_should_take_is_a_parse_error_and_the_session_goes_on(
    server: McpServer,
) -> None:
    """Nested past the recursion limit, an integer past Python's digit limit, `NaN`: each was
    an uncaught exception (RecursionError, ValueError) that ended the stdio session."""
    lines = [
        "[" * 100_000,
        "1" * 5_000,
        '{"jsonrpc": "2.0", "id": NaN, "method": "ping"}',
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    ]
    out = io.StringIO()
    serve(server, io.StringIO("\n".join(lines) + "\n"), out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a.get("error", {}).get("code") for a in answers] == [-32700] * 3 + [None]
    assert answers[-1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.parametrize("ident", [{"a": 1}, [1], True])
def test_an_id_that_is_not_a_string_or_number_is_an_invalid_request(
    server: McpServer, ident: Any
) -> None:
    answer = server.handle({"jsonrpc": "2.0", "id": ident, "method": "ping"})
    assert answer == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "id must be a string or a number"},
    }


def test_an_unexpected_failure_logs_its_type_never_its_message(
    server: McpServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(message: Any) -> None:
        raise RuntimeError("patient Jane Example says chest pain")

    monkeypatch.setattr(server, "handle", broken)
    out = io.StringIO()
    serve(
        server, io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"), out
    )
    assert json.loads(out.getvalue())["error"]["code"] == -32603
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "chest pain" not in err
