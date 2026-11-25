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
| wrap your agent: its manifest (`agent.yaml`) and HTTP contract | [Authoring an agent](guides/authoring-an-agent.md) |
| find the failures nobody wrote a case for, and keep them as tests | [Finding failures](guides/finding-failures.md) |
| make an agent's confidence mean what it says, and never automate an unstable answer | [Calibration and a second signal](guides/calibration-and-a-second-signal.md) |
| know which automation threshold your cases support | [Choosing an automation threshold](guides/choosing-a-threshold.md) |
| gate what an agent does, not only what it answers | [Governing actions](guides/governing-actions.md) |
| check an agent on every change, in CI | [Evaluating an agent on every change](guides/evaluate-in-ci.md) |
| call Mendari from your code, over REST or FHIR | [Using the API](guides/api.md) (on a running server: `/docs`) |
| let an EHR call the CDS Hooks services with its own signed token | [Connecting an EHR](guides/connecting-an-ehr.md) |
| evaluate an agent on your own site's cases | [Evaluating at your site](guides/evaluate-at-your-site.md) |
| give your AI governance committee one document about an agent | [The committee packet](guides/committee-packet.md) |
| run Mendari at your organization | [Deploying with real patient data](deploying-with-phi.md) |
| publish and pull signed agents | [Running a registry](operations/registry.md) |
| know exactly what a manifest may contain | [The spec](spec/agent-manifest.md): the manifest, the FHIR mapping, actions, calibration, a second signal and written output |
| understand how the engine works | [Architecture](architecture.md) |
| review its security | [Threat model](security/threat-model.md) |
| read why it is built this way | The decision records, from [ADR-0001](decisions/ADR-0001-questions-as-the-core-abstraction.md) on |
| see where the project is going | [Roadmap](roadmap.md) |

## In one minute

In a clone of the [repository](https://github.com/mendari-hq/mendari) (Mendari is not on PyPI
yet), with [uv](https://docs.astral.sh/uv/):

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari list                                              # the bundled demo agent
uv run mendari test agents/portal-message-agent                  # its behaviour tests
uv run mendari eval agents/portal-message-agent --dataset examples
uv run mendari new triage-agent --id my-clinic/triage-agent      # your own agent, in YAML
uv run mendari test triage-agent
```

The demo agent, `mendari/portal-message-agent`, routes synthetic patient portal messages and
sends anything that may describe same-day symptoms to a nurse. It runs offline on canned answers;
set `MENDARI_AGENT_URL` to your agent's endpoint and the same commands call the live agent, and
an agent that fails or times out never automates. On a server with sign-in on, canned answers
never automate either: without `MENDARI_AGENT_URL`, its decisions go to a person.

One install brings the command line and the server (`uv sync`, or without uv `pip install .` in
the clone): `uv run mendari serve` serves the demo agent, with the dashboard, on
`http://127.0.0.1:8000`, and `MENDARI_AGENTS_PATH` names the folders of your own agents to serve
them as well. `uv run mendari demo` shows the whole loop in a minute: decisions, the review
queue, the audit trail, an evaluation with its operating point, and a probe for the failures
nobody wrote a case for.

The code, the demo agent and the contribution guide are in the
[repository](https://github.com/mendari-hq/mendari).
