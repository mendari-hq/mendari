# A model-backed agent for Mendari (Claude)

The demo agent's live counterpart: a small HTTP service that answers Mendari's agent contract with
Claude. Mendari stays in charge: it sends the questions its manifest declares, checks every
answer, applies the policy and the risk caps, and records the decision. This service only
answers. Copy it to put your own model or framework behind an agent.

## The contract

Mendari `POST`s to `MENDARI_AGENT_URL` (with `Authorization: Bearer $MENDARI_AGENT_TOKEN`):

```json
{
  "agent_id": "mendari/portal-message-agent",
  "slot": "reader",
  "service_id": "starter-portal-agent",
  "questions": [
    {"key": "urgent_symptoms", "type": "boolean", "instructions": "…",
     "choices": ["yes", "no"], "criteria": {"yes": "…", "no": "…"}}
  ],
  "input": {"message": {"body": "…"}},
  "system_prompt": "…"
}
```

(`pack_id` is the agent's id: the key keeps the name it had before) and expects, for every
question asked and no other:

```json
{
  "answers": {
    "urgent_symptoms": {"value": "yes", "probabilities": {"yes": 0.95, "no": 0.05},
                        "rationale": "One short sentence."}
  },
  "model": "claude-opus-5-5",
  "service_version": "0.1.0",
  "usage": {"input_tokens": 1224, "output_tokens": 165, "cost_usd": 0.008196}
}
```

`value` must be one of the question's choices and the most probable one; probabilities sum to 1
(or send `confidence` alone). Anything else, an error status or a timeout, and Mendari fails
closed: the case goes to a person or gets no recommendation. The full rules are in
`src/mendari/providers/agent.py` (`validate_response`).

## How this one answers

`agent.py` turns the questions into a JSON schema (a probability for every choice, and a
rationale) and asks Claude Opus 5.5 for structured output at low effort. The case is sent as
data after the questions, and the system prompt tells the model to ignore instructions inside
it. A refusal, a cut-off or malformed reply, or an API error returns an HTTP error, never a
guess. Server-side refusal fallback is on.

Everything else is the agent kit (`mendari.agent_kit`): it checks the request, normalizes the
probabilities and answers with the most probable choice, so the answer always passes Mendari's
checks, and serves `POST /decide` with the bearer token and `GET /health` (`api/index.py` is
`create_app(handler)`). An unexpected error returns 502 with its type only, never its message.
It never logs a request's input. The other starters, a plain HTTP service and LangGraph, are in
[`examples/`](..).

| File | What it is |
|---|---|
| `agent.py` | `answer(case, messages)`: the prompt, the schema, the call to Claude, the cost |
| `api/index.py` | The Anthropic client and `app = create_app(handler)` |
| `mendari_agent_kit.py` | The kit, a copy of `mendari.agent_kit` |
| `requirements.txt`, `vercel.json` | The Anthropic SDK and FastAPI; Vercel routes every path to `api/index.py` |

**Why a copy of the kit.** Vercel uploads only this folder, and Mendari is not on PyPI (a bare
`mendari` requirement would install whatever else has that name). So the folder carries the kit
as one file, which needs nothing but FastAPI; `tests/test_agent_starters.py` fails if it differs
from `src/mendari/agent_kit.py` by a byte. After changing the kit, copy it again:
`cp src/mendari/agent_kit.py examples/claude-agent/mendari_agent_kit.py`.

## Run it

```bash
cd examples/claude-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
ANTHROPIC_API_KEY=… AGENT_TOKEN=a-long-random-string .venv/bin/uvicorn api.index:app --port 8787
```

Then point the demo agent at it and evaluate:

```bash
MENDARI_AGENT_URL=http://127.0.0.1:8787/decide MENDARI_AGENT_TOKEN=a-long-random-string \
  uv run mendari eval agents/portal-message-agent --dataset test --limit 50
```

Until a deployment with sign-in on sets `MENDARI_AGENT_URL`, the demo agent answers there from
its canned fixture and none of its decisions automate: each that would be `AUTO` goes to a
person, with the reason `canned answers: MENDARI_AGENT_URL is not set; a person decides`.

On Vercel, `vercel deploy` from this folder, with `ANTHROPIC_API_KEY` and `AGENT_TOKEN` set as
sensitive environment variables. Settings: `AGENT_MODEL` (default `claude-opus-5-5`) and
`AGENT_EFFORT` (default `low`).
