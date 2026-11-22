# SPDX-License-Identifier: Apache-2.0
"""A LangGraph agent for Mendari: a `StateGraph` behind the agent contract.

The graph answers the demo agent's two questions (agents/portal-message-agent) in two steps:

    START -> screen (urgent_symptoms) -> classify (request) -> END

Each node is plain Python here (keyword rules), so the starter runs offline; each marks where a
model call would go. `handler` runs the compiled graph on the case's text and turns its final
state into Mendari's answer with the kit (`mendari_agent_kit.py`, a copy of
`mendari.agent_kit`), which also checks the request and serves it over HTTP (api/index.py).

Mendari decides what the answers mean (policy, risk caps, review); this graph only answers.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from mendari_agent_kit import Case, respond

AGENT_VERSION = "0.1.0"


class State(TypedDict, total=False):
    text: str  # the case's text, lower-cased (the input)
    urgent: dict[str, float]  # urgent_symptoms: {yes, no} probabilities
    request: tuple[str, float]  # request: (choice, confidence)
    steps: list[str]  # the nodes that ran, reported to Mendari as tool calls


URGENT = re.compile(
    r"chest|breath|slurred|drooping|numb|faint|lips look blue|side of my (face|mouth)"
)
DENIED = re.compile(r"\b(no|denies|without)\b")
REQUESTS = {
    "refill": re.compile(r"refill|renew|ran out|running low|need more"),
    "appointment": re.compile(r"appointment|reschedul|book me|schedule|cancel my visit"),
}


def screen(state: State) -> State:
    """Does the message describe symptoms that need attention today?"""
    # An LLM call goes here: ask your model for P(yes) and P(no), e.g. with structured output.
    sentences = re.split(r"(?<=[.!?])\s+", state["text"])
    urgent = any(URGENT.search(s) and not DENIED.search(s) for s in sentences)
    return {
        "urgent": {"yes": 0.9, "no": 0.1} if urgent else {"yes": 0.05, "no": 0.95},
        "steps": [*state.get("steps", []), "screen"],
    }


def classify(state: State) -> State:
    """What does the patient ask for?"""
    # An LLM call goes here: ask your model which request it is, and how sure it is.
    asked = [label for label, pattern in REQUESTS.items() if pattern.search(state["text"])]
    return {
        # One clear request, or else (nothing recognized, or several) the front desk reads it.
        "request": (asked[0], 0.95) if len(asked) == 1 else ("other", 0.75),
        "steps": [*state.get("steps", []), "classify"],
    }


def build_graph() -> Any:
    """The compiled graph: START -> screen -> classify -> END."""
    graph = StateGraph(State)
    graph.add_node("screen", screen)
    graph.add_node("classify", classify)
    graph.add_edge(START, "screen")
    graph.add_edge("screen", "classify")
    graph.add_edge("classify", END)
    return graph.compile()


GRAPH = build_graph()


def handler(case: Case) -> dict[str, Any]:
    """Mendari's answer to one case, from the graph's final state."""
    final = GRAPH.invoke({"text": case.text(), "steps": []})
    return respond(
        case,
        {"request": final["request"], "urgent_symptoms": final["urgent"]},
        model="langgraph-keyword-rules",
        agent_version=AGENT_VERSION,
        tool_calls=[{"name": step} for step in final["steps"]],
        usage={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    )
