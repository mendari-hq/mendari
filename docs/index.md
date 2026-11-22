# Mendari

Mendari is an open governance and evaluation engine for healthcare **AI agents**. It wraps any
agent (an LLM, a LangGraph workflow, an HTTP service of your own) so that every call ends in an
explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, with a hash-chained audit record. A
governed agent is a folder of plain text:

- the questions the agent answers, in words;
- the policy that says when a person must look, capped by the agent's risk level;
- behaviour tests;
- labelled cases to evaluate it on;
- a Decision Card.

!!! warning "Research preview"
    Everything here runs on synthetic data. Mendari is not a medical device, and it must not
    be used with real patients until a deployment follows the PHI guide and the owner approves.

## Start here

| If you want to | Read |
|---|---|
| wrap your agent: its manifest (`pack.yaml`) and HTTP contract | [Authoring an agent](guides/authoring-an-agent.md) |
| call Mendari from your code, over REST or FHIR | [Using the API](guides/api.md) (on a running server: `/docs`) |
| let an EHR call the CDS Hooks services with its own signed token | [Connecting an EHR](guides/connecting-an-ehr.md) |
| evaluate an agent on your own site's cases | [Evaluating at your site](guides/evaluate-at-your-site.md) |
| run Mendari at your organization | [Deploying with real patient data](deploying-with-phi.md) |
| publish and pull signed agents | [Running a registry](operations/registry.md) |
| know exactly what a manifest may contain | [The spec](spec/agent-manifest-0.2.md), and the FHIR mapping of [0.3](spec/agent-manifest-0.3.md) |
| understand how the engine works | [Architecture](architecture.md) |
| review its security | [Threat model](security/threat-model.md) |
| see where the project is going | [Roadmap](roadmap.md) |

The decision records predate the agents-only change and keep their older wording; each says
so under its title.

## In one minute

In a clone of the [repository](https://github.com/mendari-hq/mendari) (Mendari is not on PyPI
until its first release), with [uv](https://docs.astral.sh/uv/):

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari list                                              # the bundled demo agent
uv run mendari test agents/portal-message-agent                  # its behaviour tests
uv run mendari eval agents/portal-message-agent --dataset examples
uv run mendari new triage-agent --agent --id my-clinic/triage-agent   # your own agent, in YAML
uv run mendari test triage-agent
```

The demo agent, `mendari/portal-message-agent`, routes synthetic patient portal messages and
sends anything that may describe same-day symptoms to a nurse. It runs offline on canned answers;
set `MENDARI_AGENT_URL` to your agent's endpoint and the same commands call the live agent, and
an agent that fails or times out never automates. On a server with sign-in on, canned answers
never automate either: without `MENDARI_AGENT_URL`, its decisions go to a person.

`uv sync` installs the server too (without uv: `pip install '.[server]'` in the clone); then
`uv run mendari serve` serves the demo agent, with the dashboard, on `http://127.0.0.1:8000`, and
`MENDARI_PACKS_PATH` names the folders of your own agents to serve them as well.

The code, the demo agent and the contribution guide are in the
[repository](https://github.com/mendari-hq/mendari).
