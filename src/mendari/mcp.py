# SPDX-License-Identifier: Apache-2.0
"""`mendari mcp`: governed decisions as tools for other AI agents (Model Context Protocol, stdio).

An assistant or agent framework that speaks MCP starts `mendari mcp` and calls Mendari as tools.
Each `decide` call runs the named agent under its own policy, exactly as the REST API does: the
input is checked against the agent's schema, the risk level caps automation, holds and registry
approvals in the database apply, and the decision is written to the audit trail (`--db`). So a
general-purpose agent that reaches a clinical decision gets a governed outcome, never a raw
model answer.

The tools:

- `list_agents`: the agents served here, with their questions, labels and risk levels.
- `decide`: one decision by one agent: outcome, recommendation, priority, confidence, reasons.
- `review_queue`: the decisions waiting for a person, most urgent first (ids and outcomes only,
  never the input).
- `verify_audit`: whether the audit trail's hash chain is intact.

The transport is MCP's stdio: one JSON-RPC 2.0 message per line on stdin and stdout. Nothing else
is written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import IO, Any

from mendari import __version__
from mendari.core import tenancy
from mendari.core.audit import AuditStore
from mendari.core.decision import Decision, DecisionResult
from mendari.core.pack import Pack
from mendari.core.registry import discover, resolve_pack
from mendari.errors import MendariError

# The protocol revisions this server speaks, newest first. A client asking for one of them gets
# it; any other request is answered with the newest (the client then decides whether to go on).
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_QUEUE = 100
# What a review-queue item may show a tool caller: never the input, its record or a reviewer.
QUEUE_FIELDS = ("id", "pack_id", "pack_version", "outcome", "recommendation", "priority",
                "confidence", "created_at")  # fmt: skip

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

INSTRUCTIONS = (
    "Mendari governs healthcare AI agents. Call list_agents to see what can decide here, then "
    "decide with an agent's id and an input matching its schema. Treat the outcome as binding: "
    "AUTO may be acted on; HUMAN_REVIEW means a person must decide (do not act on the "
    "recommendation alone); ABSTAIN means no recommendation; BLOCK means the input was refused. "
    "Synthetic data only: this is a research preview and not a medical device."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_agents",
        "title": "List governed agents",
        "description": "The agents this Mendari serves: id, version, risk level, primary question, "
        "and each question's allowed answers.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "decide",
        "title": "Governed decision",
        "description": "Run one agent on one input under its policy. Returns the outcome (AUTO, "
        "HUMAN_REVIEW, ABSTAIN or BLOCK), the recommendation, priority, confidence and the "
        "reasons, and records the decision in the audit trail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "the agent's id, e.g. ns/name"},
                "input": {"type": "object", "description": "the input, per the agent's schema"},
            },
            "required": ["agent", "input"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "review_queue",
        "title": "Review queue",
        "description": "Decisions waiting for a person, most urgent first: ids, outcomes and "
        "recommendations only, never the input.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "only this agent's decisions"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_QUEUE, "default": 20},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "verify_audit",
        "title": "Verify the audit trail",
        "description": "Check the audit trail's hash chain end to end.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class ToolError(MendariError):
    """A tool call that cannot be done as asked: returned to the caller as a tool error."""


class McpServer:
    """The protocol, independent of the transport: `handle` takes one message, gives the answer."""

    def __init__(self, store: AuditStore):
        self.store = store
        self._decisions: dict[str, Decision] = {}
        self._tools: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "list_agents": self._list_agents,
            "decide": self._decide,
            "review_queue": self._review_queue,
            "verify_audit": self._verify_audit,
        }

    # -- JSON-RPC ------------------------------------------------------------------------------

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The answer to one message; None for a notification (or an answer sent to us)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method, ident = message.get("method"), message.get("id")
        if not isinstance(method, str):
            return (
                None
                if "result" in message or "error" in message
                else _error(ident, INVALID_REQUEST, "no method")
            )
        if "id" not in message:  # a notification (notifications/initialized, cancelled, ...)
            return None
        if isinstance(ident, bool) or not isinstance(ident, str | int | float | None):
            # JSON-RPC 2.0: an id is a string, a number or null, never repeated back otherwise.
            return _error(None, INVALID_REQUEST, "id must be a string or a number")
        params = message.get("params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(ident, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _result(ident, self._initialize(params))
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": TOOLS})
        if method == "tools/call":
            return self._call(ident, params)
        return _error(ident, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mendari", "title": "Mendari", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, ident: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name, arguments = params.get("name"), params.get("arguments")
        arguments = {} if arguments is None else arguments
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(ident, INVALID_PARAMS, f"unknown tool {name!r}")
        if not isinstance(arguments, dict):
            return _error(ident, INVALID_PARAMS, "arguments must be an object")
        try:
            content = tool(arguments)
        except MendariError as error:  # the caller's to correct: a tool error, not a crash
            return _result(ident, _content({"error": str(error)}, is_error=True))
        return _result(ident, _content(content))

    # -- tools ---------------------------------------------------------------------------------

    def _list_agents(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _no_arguments(arguments)
        agents = [_describe(pack) for pack in sorted(discover().packs, key=lambda p: p.id)]
        return {"agents": agents}

    def _decide(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _only(arguments, {"agent", "input"})
        agent, payload = arguments.get("agent"), arguments.get("input")
        if not isinstance(agent, str) or not agent:
            raise ToolError("agent: give an agent's id (list_agents shows them)")
        if not isinstance(payload, dict):
            raise ToolError("input: give an object matching the agent's input schema")
        return _shown(self._decision(agent)(payload))

    def _review_queue(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _only(arguments, {"agent", "limit"})
        agent, limit = arguments.get("agent"), arguments.get("limit", 20)
        if agent is not None and not isinstance(agent, str):
            raise ToolError("agent: an agent's id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUEUE:
            raise ToolError(f"limit: a whole number from 1 to {MAX_QUEUE}")
        items = self.store.pending_reviews(pack_id=agent, limit=limit)
        return {"pending": [{key: item.get(key) for key in QUEUE_FIELDS} for item in items]}

    def _verify_audit(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        _no_arguments(arguments)
        found = self.store.verify()
        return {"ok": bool(found["ok"]), "events": found["events"], "head": found.get("head")}

    def _decision(self, agent: str) -> Decision:
        if agent not in self._decisions:
            pack = resolve_pack(agent)
            store = self.store

            def held(pack: Pack) -> str | None:
                hold = store.hold_for(pack.id, pack.digest, tenancy.DEFAULT_ORG)
                return None if hold is None else f"hold {hold['id']}"

            def approved(pack: Pack) -> bool:
                found = store.registry_approval(pack.id, pack.digest, in_org=tenancy.DEFAULT_ORG)
                return found is not None

            self._decisions[agent] = Decision(
                pack, store=store, hold_source=held, origin_approval=approved
            )
        return self._decisions[agent]


def _describe(pack: Pack) -> dict[str, Any]:
    m = pack.manifest
    return {
        "id": pack.id,
        "version": pack.version,
        "name": m.name,
        "summary": m.summary,
        "risk_level": int(m.risk.level),
        "primary": m.policy.primary,
        "questions": {
            key: {"instructions": question.instructions, "labels": list(question.labels)}
            for key, question in m.questions.items()
        },
    }


def _shown(result: DecisionResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "agent": result.pack_id,
        "version": result.pack_version,
        "outcome": result.outcome.value,
        "recommendation": result.recommendation,
        "priority": result.priority.value,
        "confidence": result.confidence,
        "answers": {
            key: {"label": answer.label, "confidence": answer.confidence}
            for key, answer in result.answers.items()
        },
        "reasons": list(result.reasons),
        "fallback": result.fallback,
        "errors": dict(result.errors),
    }


def _only(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise ToolError(f"unknown arguments: {', '.join(extra)}")


def _no_arguments(arguments: Mapping[str, Any]) -> None:
    _only(arguments, set())


def _content(data: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": is_error,
    }


def _result(ident: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _not_json(constant: str) -> Any:
    """`NaN`, `Infinity` and `-Infinity`, which Python's parser takes and JSON does not."""
    raise ValueError(f"{constant} is not JSON")


def serve(server: McpServer, reader: IO[str], writer: IO[str]) -> None:
    """MCP's stdio transport: one JSON message per line in, one per line out, until EOF."""
    for line in reader:
        if not line.strip():
            continue
        try:
            message = json.loads(line, parse_constant=_not_json)
        except (ValueError, RecursionError):
            # Not JSON, and also JSON no answer could repeat: nested past the interpreter's
            # recursion limit, an integer past its digit limit, `NaN` or `Infinity`. Each is
            # this one message's parse error, never the end of the session.
            answer: dict[str, Any] | None = _error(None, PARSE_ERROR, "not JSON")
        else:
            try:
                answer = server.handle(message)
            except Exception as error:  # never let one message end the session
                # The type only: an exception's message may quote the input (case text).
                print(f"mendari mcp: internal error ({type(error).__name__})", file=sys.stderr)
                ident = message.get("id") if isinstance(message, dict) else None
                answer = _error(ident, -32603, "internal error")
        if answer is not None:
            writer.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n")
            writer.flush()
