# The plainest agent for Mendari (HTTP starter)

One Python function from a case to its answers, served over HTTP. Start here when your agent is
a model API, a rules engine or any service you can call from Python. Mendari stays in charge: it
sends the questions its manifest declares, checks every answer, applies the policy and the risk
caps, and records the decision. This service only answers.

| File | What it is |
|---|---|
| `agent.py` | `handler(case)`: keyword rules for the demo agent's two questions. **Replace it with your model or service call.** |
| `api/index.py` | `app = create_app(agent.handler)`: `POST /decide` (bearer token) and `GET /health` |
| `mendari_agent_kit.py` | The kit, a copy of `mendari.agent_kit`, so this folder runs and deploys on its own |
| `requirements.txt`, `vercel.json` | FastAPI and uvicorn; Vercel routes every path to `api/index.py` |

## Write your handler

```python
from mendari_agent_kit import Case, respond


def handler(case: Case) -> dict:
    # case.questions: each with key, type, instructions, choices, criteria
    # case.input: the fields the manifest's state_fields send; case.text(): its text, lower-cased
    # case.instructions: the manifest's system_prompt and instructions
    return respond(
        case,
        {
            "request": ("refill", 0.95),  # (choice, confidence)
            "urgent_symptoms": {"yes": 0.2, "no": 0.8},  # or {choice: probability}
        },
        model="my-model-2026-01",
        usage={"input_tokens": 412, "output_tokens": 38},
    )
```

`respond` answers every question asked and no other, normalizes probabilities and picks the
most probable choice, so what it returns always passes Mendari's checks. An answer it cannot
build raises `AgentError` (502): Mendari fails closed and a person reads the case. The handler
may be `async def`. Never put case text in an error message or a log.

The demo policy automates from confidence 0.90 and sends a message to a nurse when urgent
symptoms are at least 30 % likely, so these rules answer a clear request at 0.95 and possible
urgent symptoms at 0.9.

## Run it

```bash
cd examples/http-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
AGENT_TOKEN=a-long-random-string .venv/bin/uvicorn api.index:app --port 8787
```

`curl http://127.0.0.1:8787/health` answers `{"status":"ok","agent_version":"0.1.0"}`. Without
`AGENT_TOKEN`, `/decide` refuses every request unless `AGENT_ALLOW_OPEN=1` (local use only).

## Point Mendari at it

From a clone of Mendari, evaluate the demo agent with this service answering:

```bash
MENDARI_AGENT_URL=http://127.0.0.1:8787/decide MENDARI_AGENT_TOKEN=a-long-random-string \
  uv run mendari eval agents/portal-message-agent --dataset test
```

On the demo agent's 600 synthetic messages these keyword rules get every request right but
detect 105 of 114 urgent messages, 3 of them automated: an everyday wording they do not know.
That is what the evaluation is for. Replace the rules with your model and run it again; compare
configurations with `mendari leaderboard` (the [authoring guide](../../docs/guides/authoring-an-agent.md#starters)).

## Deploy it

`vercel deploy` from this folder, with `AGENT_TOKEN` set as a sensitive environment variable,
then set `MENDARI_AGENT_URL` (`https://<your-deployment>/decide`) and `MENDARI_AGENT_TOKEN` where
Mendari runs. Any host that runs an ASGI app works the same way.
