# A LangGraph agent for Mendari (starter)

A LangGraph `StateGraph` behind Mendari's agent contract. Start here when your agent is a
workflow of steps (screen, retrieve, classify, call tools). Mendari stays in charge: it sends
the questions its manifest declares, checks every answer, applies the policy and the risk caps,
and records the decision. The graph only answers.

```text
START -> screen (urgent_symptoms) -> classify (request) -> END
```

| File | What it is |
|---|---|
| `agent.py` | The graph (`build_graph`) and `handler(case)`, which runs it and returns `respond(...)` |
| `api/index.py` | `app = create_app(agent.handler)`: `POST /decide` (bearer token) and `GET /health` |
| `mendari_agent_kit.py` | The kit, a copy of `mendari.agent_kit`, so this folder runs and deploys on its own |
| `requirements.txt`, `vercel.json` | LangGraph, FastAPI and uvicorn; Vercel routes every path to `api/index.py` |

Each node is plain Python (keyword rules), so the starter runs offline, and is marked where an
LLM call goes: ask your model for a probability per choice and put it in the state. The handler
turns the final state into the answer:

```python
final = GRAPH.invoke({"text": case.text(), "steps": []})
return respond(
    case,
    # request: (choice, confidence); urgent: {choice: probability}
    {"request": final["request"], "urgent_symptoms": final["urgent"]},
    tool_calls=[{"name": step} for step in final["steps"]],
)
```

The nodes that ran are reported as `tool_calls`, kept with each answer as evidence. `respond`
makes the answer pass Mendari's checks by construction; anything it cannot build is an HTTP
error, so Mendari fails closed and a person reads the case. Checked against LangGraph 1.2.24
(`StateGraph`, `add_node`, `add_edge`, `START`, `END`, `compile`, `invoke`).

## Run it

```bash
cd examples/langgraph-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
AGENT_TOKEN=a-long-random-string .venv/bin/uvicorn api.index:app --port 8787
```

Without `AGENT_TOKEN`, `/decide` refuses every request unless `AGENT_ALLOW_OPEN=1` (local use
only).

## Point Mendari at it

From a clone of Mendari:

```bash
MENDARI_AGENT_URL=http://127.0.0.1:8787/decide MENDARI_AGENT_TOKEN=a-long-random-string \
  uv run mendari eval agents/portal-message-agent --dataset test
```

With keyword nodes it does as the [HTTP starter](../http-agent/README.md) does on the 600
synthetic messages: every request right, 105 of 114 urgent messages detected and 3 of them
automated. Put a model in `screen`, run it again, and compare.

## Deploy it

`vercel deploy` from this folder, with `AGENT_TOKEN` (and your model's credential) set as
sensitive environment variables; then set `MENDARI_AGENT_URL` (`https://<your-deployment>/decide`)
and `MENDARI_AGENT_TOKEN` where Mendari runs.
