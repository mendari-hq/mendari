# Mendari

**Open-source governance and evaluation for healthcare AI agents.**

Mendari wraps any AI agent (an LLM, a LangGraph workflow, an HTTP service of your own) in a
deterministic governance engine. Every call to the agent ends in one of four explicit outcomes,
`AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, decided by a policy you can read, capped by the
agent's risk level, and written to an append-only, hash-chained audit trail. The same engine
evaluates the agent on labelled cases, serves it over REST, FHIR and CDS Hooks with a review queue
for the cases a person must decide, and publishes it as a signed archive to a registry (the Agent
Hub) that other sites can pull from and verify.

> **Status: research preview** (package 0.1.0, not released). Everything here runs on synthetic
> data. It is not a medical device. Organization sign-in and PHI mode have not had a security
> review, so it must not be used with real PHI or real patients yet.

```text
your AI agent (LLM, LangGraph, HTTP service)
        │  answers the questions the manifest declares, with a confidence
        ▼
input schema ──► agent ──► policy: rules, thresholds, risk-level caps ──► AUTO | HUMAN_REVIEW | ABSTAIN | BLOCK
                                                                                   │
                                                              audit trail · review queue · FHIR Task
```

## The four outcomes

| Outcome | Meaning |
|---|---|
| `AUTO` | Acted on without a person, within the agent's risk cap and confidence policy. |
| `HUMAN_REVIEW` | A person decides, with the agent's recommendation in front of them. |
| `ABSTAIN` | No recommendation: the agent was not confident enough, or did not answer. |
| `BLOCK` | The input failed its schema or a guardrail, and the agent never saw it. |

The policy turns the agent's answers into an outcome in three steps: named rules first (in the
demo agent, a message that may describe same-day symptoms goes to a nurse as `HUMAN_REVIEW`,
priority `urgent`), then confidence thresholds on the primary question, then caps. **Caps only
lower automation**: a per-label limit, a failed or timed-out agent call, or the risk level can
turn `AUTO` into `HUMAN_REVIEW`, never the reverse. An agent that times out or returns something
outside its declared answers fails closed.

The risk level is declared in the agent's manifest and enforced by the runtime, not by the agent
([ADR-0002](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0002-runtime-enforced-risk-caps.md)):

| Level | Kind | Automation |
|---|---|---|
| 0 | Administrative (scheduling, routing, billing) | `AUTO` allowed by policy |
| 1 | Clinical information processing | `AUTO` allowed; audited |
| 2 | Clinical support | Human review by default; `AUTO` needs a recorded governance approval |
| 3 | Patient-specific prediction | As level 2, with stronger validation expected |
| 4 | Time-critical, diagnostic or treatment | Never automated |

## Install

Mendari is not on PyPI yet. Install it from a clone of this repository; you need Python 3.11+
and, for development, [uv](https://docs.astral.sh/uv/):

<!-- check: skip private-repo: CI's checkout is this clone, and the quickstart job runs uv sync in it -->
```bash
git clone https://github.com/mendari-hq/mendari.git
cd mendari
uv sync                          # the package, the server, and the test and lint tools
```

Without uv, in the clone, into any Python 3.11+ virtual environment (the `server` extra adds
what `mendari serve` needs):

<!-- check: run install -->
```bash
pip install '.[server]'
```

## Quickstart

Mendari comes with one demo agent, `mendari/portal-message-agent`
([`agents/portal-message-agent/`](https://github.com/mendari-hq/mendari/tree/main/agents/portal-message-agent)).
It reads a patient portal message, says what the patient asks for (a refill, an appointment,
something else) and whether the message describes symptoms that need attention today. It runs
offline, on canned answers in `fixtures/mock_agent.json`, so nothing here needs a key or a
network. Like everything in this repository it is a
**Synthetic example. Not clinically reviewed. Not for patient care.**

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari list                                                    # the agents on the search path
uv run mendari validate agents/portal-message-agent                    # the manifest and every file it names
uv run mendari test agents/portal-message-agent                        # its behaviour tests
uv run mendari eval agents/portal-message-agent --dataset examples     # accuracy, calibration, outcomes
```

`mendari test` prints one line per behaviour test:

```text
PASS  a refill request goes to the refill queue
PASS  an appointment request goes to scheduling
PASS  chest pain goes to a nurse today, whatever else the message asks

3 passed, 0 failed
```

`mendari eval` reports each figure as k of n with its exact 95% interval (on the 12 bundled
examples: 12 of 12 requests right, 7 automated, 5 sent to a person, and all 3 messages with
urgent symptoms detected and none of them automated). Twelve synthetic cases are a smoke test,
not a performance estimate.

One decision, as JSON (the outcome, the recommendation, each answer with its confidence and
evidence, and the reasons the policy gave):

<!-- check: run quickstart -->
```bash
uv run mendari decide agents/portal-message-agent --text "Could you refill my lisinopril? I am running low."
uv run mendari decide agents/portal-message-agent \
  --text "Please refill my inhaler. Also I have had chest pain since last night."
```

The first is `AUTO` (`confidence 0.95 >= auto threshold 0.90`). The second is `HUMAN_REVIEW`
with priority `urgent`: the agent is just as sure it is a refill, and the rule
`same-day-symptoms` sends it to a nurse anyway.

### Wrap your own agent

`mendari new --agent` writes a working copy of that agent under your own id:

<!-- check: run quickstart -->
```bash
uv run mendari new my-agent --agent --id my-clinic/my-agent
uv run mendari validate my-agent
uv run mendari test my-agent
uv run mendari eval my-agent --dataset examples
uv run mendari eval my-agent --dataset examples --provider reader=rules   # the keyword-rules variant, same cases
```

The folder is plain text:

```text
my-agent/
├── pack.yaml                  the agent's manifest: id, risk level, questions, the agent slot, policy
├── schemas/input.schema.json  what a valid input looks like (anything else is BLOCK)
├── fixtures/mock_agent.json   canned agent answers, used while no live agent is set
├── logic/rules.yaml           a keyword-rules variant to compare the agent with
├── data/examples.jsonl        labelled cases for `mendari eval`
└── tests/cases.yaml           behaviour tests for `mendari test`
```

The agent's manifest (`pack.yaml`) declares **questions, not models**: each question has a fixed
set of answers with criteria written in words, and the policy is written against those answers.
Edit the questions, the policy, the examples and the tests for your own task; `mendari validate`
says what is wrong, by file and field. Every section of the manifest is explained in the
[authoring guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md)
and specified in
[the spec](https://github.com/mendari-hq/mendari/blob/main/docs/spec/agent-manifest-0.2.md).

### Point it at a live agent

With `MENDARI_AGENT_URL` set, the same commands call your agent instead of the fixture:

<!-- check: skip placeholder: the URL is your own agent's endpoint, which the reader supplies; tests/test_agent_provider.py covers the call -->
```bash
MENDARI_AGENT_URL=https://your-agent.example/decide uv run mendari eval my-agent --dataset examples
```

Mendari sends one `POST` per decision with a JSON body: `pack_id` (the agent's id), `slot`,
`agent_id`, `system_prompt`, the `questions` (each with its `key`, `instructions`, `choices` and
`criteria`) and the `input`, limited to the fields the manifest lists under `state_fields`. Your
agent answers every question:

```json
{
  "model": "my-model-2026-01",
  "answers": {
    "request": {"value": "refill", "confidence": 0.93},
    "urgent_symptoms": {"value": "no", "confidence": 0.95}
  }
}
```

An answer outside a question's choices, a missing answer, an HTTP error or a timeout is recorded
as a provider failure, and the decision goes to `HUMAN_REVIEW` or `ABSTAIN`: an unreachable
agent never automates. `MENDARI_AGENT_TOKEN`, when set, is sent as a bearer token to that URL
only. The URL must be https (plain http only to this machine).

**A model-backed example.** [`examples/claude-agent`](https://github.com/mendari-hq/mendari/tree/main/examples/claude-agent)
is the demo agent's live counterpart: a small service that answers this contract with Claude
(Opus 5.5, low effort), copyable for your own model or framework. Measured through Mendari on the
demo agent's 600-case synthetic `test` set (2026-11-21), each proportion with its exact 95%
interval:

| | Canned answers (offline) | Claude agent (live) |
|---|---|---|
| Request correct | 549 of 600 [89.0-93.6%] | 598 of 600 [98.8-99.96%] |
| Urgent messages caught | 81 of 114 [61.8-79.2%], 9 automated | 114 of 114 [96.8-100.0%], none automated |
| Automated and wrong | 9 of 600 | 0 of 600 |
| Cost per decision | $0 | $0.0075 |
| Latency, median / 95th percentile | under 1 ms | 2.2 s / 4.2 s |

The set is synthetic and built from fixed phrase lists, so it shows how the evaluation tells
two configurations apart, not how either would do on real patients: an engineering evaluation,
not clinical validation.

### Serve it

<!-- check: skip long-running: mendari serve runs until stopped; the install check starts the same server and reads /api/health -->
```bash
uv run mendari serve --db var/demo.db           # the API and the dashboard, on http://127.0.0.1:8000
```

In a second terminal, ask for a decision (the route still says `packs`; what follows it is the
agent's id):

<!-- check: skip long-running: needs the server of the block above, which runs until stopped; tests/test_doc_snippets.py posts this body to the same route -->
```bash
curl -sS -X POST http://127.0.0.1:8000/api/packs/mendari/portal-message-agent/decide \
  -H 'content-type: application/json' \
  -d '{"message": {"body": "Please refill my inhaler. Also I have had chest pain since last night."}}'
```

The answer is the decision (`"outcome": "HUMAN_REVIEW"`, `"priority": "urgent"`). It is now in
the review queue (`GET /api/review`, and the dashboard) and in the audit trail
(`GET /api/audit/{id}`). The server is open by default, for local use on `127.0.0.1`; a running
server documents its own API at `/docs` and `/docs/reference`.

To serve your own agent, name its folder: `MENDARI_PACKS_PATH=my-agent uv run mendari serve`
(several folders are separated by `:`).

### From Python

<!-- check: run python agent -->
```python
from mendari import Decision

agent = Decision.from_hub("mendari/portal-message-agent")
result = agent({"message": {"body": "Please refill my inhaler. Also I have had chest pain since last night."}})

result.outcome          # Outcome.HUMAN_REVIEW
result.recommendation   # 'refill'
result.priority         # Priority.URGENT
result.reasons          # ('rule same-day-symptoms: The message may describe symptoms that need a nurse today.',)
```

### From another AI agent (MCP)

`mendari mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server on stdio, so an
assistant or agent framework can call governed decisions as tools: `list_agents`, `decide`,
`review_queue` and `verify_audit`. A `decide` call runs the agent under its own policy, exactly as
the REST API does, and is written to the audit trail (`--db`). The caller is told to treat the
outcome as binding: `HUMAN_REVIEW` means a person decides. A client's server entry:

```json
{
  "mcpServers": {
    "mendari": {"command": "uv", "args": ["run", "mendari", "mcp", "--db", "var/mcp.db"]}
  }
}
```

The review queue it returns holds ids, outcomes and recommendations, never the input.

## What the engine provides

**Audit trail.** Every decision is an event in an append-only, hash-chained trail (SQLite
locally, Postgres on a server): the agent's id, version and digest, each answer with its
provider and evidence, the outcome and the reasons. Reports and exports carry ids, never case
text. Anyone with the database can check the chain:

<!-- check: run cli -->
```bash
uv run mendari decide agents/portal-message-agent --text "I need to reschedule my appointment next week." --db var/readme-audit.db
uv run mendari audit verify --db var/readme-audit.db
uv run mendari card agents/portal-message-agent      # the Decision Card: intended use, risk, policy, limits
uv run mendari schema agents/portal-message-agent    # the input JSON Schema
uv run mendari --version
```

**Review queue.** A `HUMAN_REVIEW` decision waits in a queue ordered by priority, in the
dashboard and at `/api/review`, with the agent's recommendation and its reasons. What the
reviewer decides is recorded in the same trail, and feedback on a decision can be exported as
labelled cases.

**Evaluation.** `mendari eval` runs an agent on a labelled dataset (one the manifest names, or a
JSONL file of your own) and reports accuracy, per-answer precision and recall, calibration, the
share of each outcome, false automation, and detection of the answers the manifest marks
critical, each as k of n with an exact interval. `--provider SLOT=VARIANT` runs another variant
of the same slot on the same cases, so an agent, a different model behind it and plain rules are
compared under one policy. To evaluate an agent on your own site's cases, read
[Evaluating at your site](https://github.com/mendari-hq/mendari/blob/main/docs/guides/evaluate-at-your-site.md);
the report format is in
[Evaluation reports and ledgers](https://github.com/mendari-hq/mendari/blob/main/docs/spec/evaluation-reports.md).

**Sign-in and tenancy.** Set an OpenID Connect identity provider and the server requires
sign-in: people get roles (viewer, reviewer, governance, admin), machines get API keys or short-lived SMART
Backend Services tokens with scopes, and each organization sees only its own decisions, review
queue and audit trail. A level 2 or 3 agent automates only after a named approver records a
governance approval, and automation can be held for one agent or all of them at once. The
settings and the operating procedures are in
[Deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md),
[Organizations and memberships](https://github.com/mendari-hq/mendari/blob/main/docs/operations/accounts.md),
[Holding automation](https://github.com/mendari-hq/mendari/blob/main/docs/operations/automation-hold.md)
and [Revoking access](https://github.com/mendari-hq/mendari/blob/main/docs/operations/revoking-access.md).

**FHIR, CDS Hooks and SMART on FHIR.** The server has a FHIR R4 base (`/fhir/metadata`,
`POST /fhir/$decide`) that returns a decision as FHIR resources, serves each decision's work item as a
`Task`, and exports the audit trail as `AuditEvent` and `Provenance`. An agent whose manifest
maps FHIR resources to its input and names a hook is also offered as a CDS Hooks 2.0 service
(`/cds-services`), and the server can be launched from an EHR with SMART App Launch. The demo
agent takes plain JSON and declares no FHIR mapping, so it appears on none of these until you
add one. See [Using the API](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md),
[Connecting an EHR](https://github.com/mendari-hq/mendari/blob/main/docs/guides/connecting-an-ehr.md)
and the [FHIR mapping spec](https://github.com/mendari-hq/mendari/blob/main/docs/spec/agent-manifest-0.3.md).

**The registry and the Agent Hub.** An agent travels as a deterministic archive signed with its
publisher's Ed25519 key. A registry keeps immutable versions; `mendari pull` checks the archive
digest, every file hash and the signature before caching the agent, and pins the namespace's key
on first use. There is no public registry yet: you name one with `mendari login`, or run your
own with `mendari registry serve`.

<!-- check: skip registry: needs a registry and a publisher token; CI job hub-e2e publishes to a local registry through the same client and runs mendari pull -->
```bash
mendari keys generate --out keys/                           # a publisher key pair (Ed25519)
mendari login --registry https://hub.example.org --token <publisher token>
mendari keys register --namespace my-clinic --public-key keys/publisher.pub
mendari push my-agent --key keys/publisher.key              # build, sign, publish

mendari search portal                                       # anyone, from that registry
mendari pull my-clinic/my-agent                             # download, verify, cache
```

A pulled agent is not trusted like your own: served, it never automates until your organization
approves its exact digest, and never while the registry has recalled it. The registry shows only
what it checked itself
([ADR-0012](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)),
and the Hub ([`hub/`](https://github.com/mendari-hq/mendari/tree/main/hub), a Next.js site) is a
front end to it. Running a registry, publisher tokens, namespace claims, recalls and its settings
(`MENDARI_REGISTRY_SIGNING_KEY`, `MENDARI_REGISTRY_RATE_LIMITS`, `MENDARI_REGISTRY_REGRESSION`)
are in [Running a registry](https://github.com/mendari-hq/mendari/blob/main/docs/operations/registry.md).

## Deploying

The server is one FastAPI app. Container images, an on-premises Docker Compose template, a
Kubernetes manifest and the Vercel setup are in
[`deploy/`](https://github.com/mendari-hq/mendari/tree/main/deploy)
([its README](https://github.com/mendari-hq/mendari/blob/main/deploy/README.md)). Every security
setting, with its default, is in
[Deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md#8-settings-at-a-glance),
and what to do when something goes wrong in the
[incident runbook](https://github.com/mendari-hq/mendari/blob/main/docs/operations/incident-runbook.md).

## Documentation

The pages in [`docs/`](https://github.com/mendari-hq/mendari/tree/main/docs) build into a site
with MkDocs Material; it will be hosted once the repository is public.

<!-- check: skip ci: each line is a step of CI job docs, which builds the same site strictly -->
```bash
uv sync --group docs
uv run mkdocs build --strict
```

| If you want to | Read |
|---|---|
| write an agent's manifest | [the authoring guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md) |
| call Mendari over REST or FHIR | [Using the API](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md) |
| connect an EHR | [Connecting an EHR](https://github.com/mendari-hq/mendari/blob/main/docs/guides/connecting-an-ehr.md) |
| run it at your organization | [Deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md) |
| understand how the engine works | [Architecture](https://github.com/mendari-hq/mendari/blob/main/docs/architecture.md) |
| read why it is built this way | [the decision records](https://github.com/mendari-hq/mendari/tree/main/docs/decisions) |
| review its security | [SECURITY.md](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md), [the threat model](https://github.com/mendari-hq/mendari/blob/main/docs/security/threat-model.md) |
| see where the project is going | [Roadmap](https://github.com/mendari-hq/mendari/blob/main/docs/roadmap.md) |

Pages outside this README are still being moved to agent wording, and some describe example
material that is no longer bundled.

## Project layout

```text
agents/           the bundled demo agent (portal-message-agent)
src/mendari/
├── core/         manifest spec and loading, digest, runtime, policy, audit, Decision Card
├── providers/    what can fill an agent slot: agent (HTTP), llm, rules, naive_bayes, cql, ...
├── evals/        metrics, the evaluation harness, behaviour tests, reports
├── standards/    FHIR R4, CDS Hooks, SMART on FHIR
├── server/       the FastAPI app: REST, FHIR, CDS Hooks, review, audit, sign-in
├── dashboard/    the dashboard: HTML, CSS and ES modules, no build step
├── hub/          the registry service and its client (push, pull, verify)
└── templates/    what `mendari new` writes
tests/            the test suite (tests/fixtures/ holds synthetic fixtures only)
docs/             guides, spec, architecture, decision records (the documentation site)
hub/              the Agent Hub website (Next.js), a front end to a registry
web/              the public page, mendari.co (static HTML and CSS)
deploy/           on-premises, Kubernetes and Vercel templates
scripts/          checks and generators that CI runs
community-packs/  where community contributions arrive by pull request (none yet)
sandbox/inferno/  Inferno conformance kits against the engine
notes/, tasks/    project notes, plans and lessons
```

## Development

<!-- check: skip ci: each line is a step of CI job test, or a narrower run of it -->
```bash
uv sync
uv run pytest                             # the full suite; warnings are errors
uv run pytest -m core                     # the edit loop, about 20 s: loading, decisions, policy, audit
uv run pytest tests/test_agent_provider.py   # and the file for what you changed
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
```

No real PHI, ever: test data is synthetic. How to contribute is in
[CONTRIBUTING.md](https://github.com/mendari-hq/mendari/blob/main/CONTRIBUTING.md), how the
project is run in [GOVERNANCE.md](https://github.com/mendari-hq/mendari/blob/main/GOVERNANCE.md),
and how to report a vulnerability in
[SECURITY.md](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md).

## License

Apache-2.0. See [LICENSE](https://github.com/mendari-hq/mendari/blob/main/LICENSE), and
[NOTICE](https://github.com/mendari-hq/mendari/blob/main/NOTICE) for the third-party material and
terminology notices. Changes: [CHANGELOG.md](https://github.com/mendari-hq/mendari/blob/main/CHANGELOG.md).

Mendari is a research preview on synthetic data. It is not a medical device and not for
emergencies: patients should call 911.
