# Mendari

**Open-source governance and evaluation for healthcare AI agents.**

Mendari wraps an AI agent (an LLM behind a prompt, a LangGraph workflow, an HTTP service of your
own) in a deterministic governance engine. Every call to the agent ends in one of four explicit
outcomes, `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or `BLOCK`, decided by a policy you can read, capped
by the agent's risk level, and written to an append-only, hash-chained audit trail. The same
engine evaluates the agent on labelled cases and publishes it, with its evaluations, as a signed
archive that anyone can pull and verify.

It is built for two groups, and for the loop between them:

- **Developers** build an agent, run it through the engine and publish every evaluation with
  it: how each configuration and model did on the same cases, so which works best is there to
  read.
- **Hospitals** adopt an agent that already comes with its evaluations and a governance
  structure, try it in shadow on their own workflows (a person decides every case, and the
  engine records what the agent would have automated), then share their results back as signed
  counts, never cases.

> **Status: research preview** (package 0.1.0, not released). Everything here runs on synthetic
> data. Mendari is not a medical device, and it must not be used with real PHI or real patients:
> its external security review has not happened yet ([Status and limits](#status-and-limits)).

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
turn `AUTO` into `HUMAN_REVIEW`, never the reverse.

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

Without uv, in the clone, into any Python 3.11+ virtual environment. One install is enough: it
brings the command line and the server (`mendari serve`), with no extra to name:

<!-- check: run install -->
```bash
pip install .
```

`mendari --help` lists the commands to start with, and `mendari help all` every one.

## Five minutes: from the demo to your own agent

### 1. Run the demo

Mendari bundles one agent, `mendari/portal-message-agent`
([`agents/portal-message-agent/`](https://github.com/mendari-hq/mendari/tree/main/agents/portal-message-agent)).
It reads a patient portal message, says what the patient asks for (a refill, an appointment,
something else) and whether the message describes symptoms that need attention today. It
answers offline from canned answers, so nothing here needs a key or a network. Like everything
in this repository it is a **Synthetic example. Not clinically reviewed. Not for patient care.**

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari demo
```

The demo decides six messages, works the review queue, verifies the audit chain, then evaluates
the agent on 600 synthetic messages and says **not ready**: the canned answers do not detect 33
of the 114 urgent messages, those worded as a patient might, and 9 of them would have been
automated, so no person would have read them. That is on purpose: the evaluation is there to
catch it before anything decides alone. Its
[operating point](https://github.com/mendari-hq/mendari/blob/main/docs/guides/choosing-a-threshold.md)
adds that no automation threshold keeps such critical misses to 1% with 95% confidence: the fix
is the agent, not the threshold. And a
[probe](https://github.com/mendari-hq/mendari/blob/main/docs/guides/finding-failures.md)
rewrites cases in ways that keep their meaning (a typing error, a line break, an instruction
slipped in) to find the failures nobody wrote a case for.

### 2. Scaffold, validate, test and evaluate your own

<!-- check: run quickstart -->
```bash
uv run mendari new my-agent --id my-clinic/my-agent     # a working copy of the demo agent, under your id
uv run mendari validate my-agent                        # the manifest and every file it names
uv run mendari test my-agent                            # its behaviour tests
uv run mendari eval my-agent --dataset examples         # accuracy, calibration, outcomes, the operating point
uv run mendari eval my-agent --dataset examples --provider reader=rules   # the keyword-rules variant, same cases
uv run mendari probe my-agent                           # its failures; --write keeps each as a test
```

The folder is plain text:

```text
my-agent/
├── agent.yaml                 the agent's manifest: id, risk level, questions, the agent slot, policy
├── schemas/input.schema.json  what a valid input looks like (anything else is BLOCK)
├── fixtures/mock_agent.json   canned agent answers, used while no live agent is set
├── logic/rules.yaml           a keyword-rules variant to compare the agent with
├── data/examples.jsonl        labelled cases for `mendari eval`
├── tests/cases.yaml           behaviour tests for `mendari test`
└── .github/workflows/mendari.yml   the same checks on every change (no part of the agent)
```

The manifest declares **questions, not models**: each question has a fixed set of answers with
criteria written in words, and the policy is written against those answers. Edit them for your
own task: `mendari validate` says what is wrong, by file and field, and `mendari eval` gives
each figure as k of n with its exact 95% interval (twelve cases are a smoke test, not an
estimate). The
[authoring guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md)
explains every section, and
[the spec](https://github.com/mendari-hq/mendari/blob/main/docs/spec/agent-manifest-0.2.md)
specifies it.

**In CI.** The workflow uses Mendari's GitHub Action: on every pull request, and every push to
`main`, it validates, tests and evaluates the agent, writes the figures and the operating point
to the job's summary, and fails the job when a limit you state is not met (by default: a
critical label detected in under nine cases of ten, or any critical miss) or the result is
worse than a baseline. It needs no secret. The action resolves from another repository once
this one is public and a release is tagged
([Evaluating an agent on every change](https://github.com/mendari-hq/mendari/blob/main/docs/guides/evaluate-in-ci.md)).

### 3. Put a real model behind it

The answers come from a service you run: Mendari sends it one `POST` per decision, with the
questions and the input fields the manifest allows, and checks every answer. Three starters
speak that contract, each with one function to replace:
[`examples/http-agent`](https://github.com/mendari-hq/mendari/tree/main/examples/http-agent),
[`examples/langgraph-agent`](https://github.com/mendari-hq/mendari/tree/main/examples/langgraph-agent)
and [`examples/claude-agent`](https://github.com/mendari-hq/mendari/tree/main/examples/claude-agent).

<!-- check: skip long-running: the starter's server runs until stopped; tests/test_agent_starters.py serves the same app and decides through it -->
```bash
cd examples/http-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
AGENT_TOKEN=a-long-random-string .venv/bin/uvicorn api.index:app --port 8787
```

In a second terminal, in the clone, the same commands now call it instead of the canned answers:

<!-- check: skip long-running: needs the starter of the block above, which runs until stopped; tests/test_agent_starters.py makes the same call -->
```bash
MENDARI_AGENT_URL=http://127.0.0.1:8787/decide MENDARI_AGENT_TOKEN=a-long-random-string \
  uv run mendari eval my-agent --dataset examples
```

A wrong, missing or late answer is a provider failure, and the decision goes to `HUMAN_REVIEW`
or `ABSTAIN`: an unreachable agent never automates
([the contract](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md#6-the-agents-http-contract)).
Measured through Mendari on the demo agent's 600 synthetic test messages (2026-11-21, exact 95%
intervals):

| | Canned answers (offline) | `claude-agent` (live) |
|---|---|---|
| Request correct | 549 of 600 [89.0-93.6%] | 598 of 600 [98.8-99.96%] |
| Urgent messages caught | 81 of 114 [61.8-79.2%], 9 automated | 114 of 114 [96.8-100.0%], none automated |
| Automated and wrong | 9 of 600 | 0 of 600 |
| Cost per decision | $0 | $0.0075 |
| Latency, median / 95th percentile | under 1 ms | 2.2 s / 4.2 s |

The set is synthetic, built from fixed phrase lists: it shows how the evaluation tells two
configurations apart, not how either would do on real patients.

### 4. Serve it and open the console

<!-- check: skip long-running: mendari serve runs until stopped; the install check starts the same server and reads /api/health -->
```bash
MENDARI_AGENTS_PATH=my-agent uv run mendari serve --db var/demo.db   # http://127.0.0.1:8000
```

The Governance Console at `http://127.0.0.1:8000` shows each agent's Decision Card, the review
queue, the audit trail, evaluations and monitoring; the demo agent is served beside yours (set
`MENDARI_AGENT_URL` as in step 3 and the server calls your live agent). From a second terminal,
ask for a decision:

<!-- check: skip long-running: needs the server of the block above, which runs until stopped; tests/test_doc_snippets.py posts this body to the same route -->
```bash
curl -sS -X POST http://127.0.0.1:8000/api/agents/mendari/portal-message-agent/decide \
  -H 'content-type: application/json' \
  -d '{"message": {"body": "Please refill my inhaler. Also I have had chest pain since last night."}}'
```

The answer is the decision (`"outcome": "HUMAN_REVIEW"`, `"priority": "urgent"`): the rule
`same-day-symptoms` sends a refill request that mentions chest pain to a nurse. It is now in the
review queue (`GET /api/review`) and the audit trail (`GET /api/audit/{id}`). The JSON names the
agent under `pack`, the word Mendari used before: stored and signed records hold that key, so it
keeps its name. The server is open by default, for local use, and documents its API at `/docs`;
with sign-in on, canned answers never automate.

The same decision from Python:

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

Another AI agent calls the same decisions as tools: `mendari mcp --db var/mcp.db` is a
[Model Context Protocol](https://modelcontextprotocol.io) server on stdio.

## The Agent Hub: publish and pull

An agent travels as a deterministic archive signed with its publisher's Ed25519 key. A registry
keeps immutable versions and shows only what it checked itself
([ADR-0012](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md));
the Agent Hub ([`hub/`](https://github.com/mendari-hq/mendari/tree/main/hub)) is its website.
No registry address is built into the client: `mendari login` names one, and
`mendari registry serve` runs your own.

<!-- check: skip registry: needs a registry and a publisher token; CI job hub-e2e publishes to a local registry through the same client and runs mendari pull -->
```bash
mendari keys generate --out keys/                           # a publisher key pair (Ed25519)
mendari login --registry https://hub.example.org --token <publisher token>
mendari keys register --namespace my-clinic --public-key keys/publisher.pub
mendari push my-agent --key keys/publisher.key              # build, sign, publish
mendari eval my-agent --dataset test --provider reader=rules --publish --key keys/publisher.key

mendari search portal                                       # anyone, from that registry
mendari pull my-clinic/my-agent                             # download, verify, cache
```

- **Publishing.** Your organization claims a namespace and mints a publisher token. The
  registry refuses a version `mendari validate` would refuse, and lists a first one once a
  reviewer approves the namespace. `eval --publish` adds your signed evaluation of one
  configuration, on a dataset of 100 cases or more that the manifest declares: counts only, and
  the registry runs a local configuration again and refuses counts that differ
  ([ADR-0020](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0020-publisher-evaluations.md)).
  Publish one per configuration, and the Hub shows which works best.
- **Pulling.** `pull` checks the archive's digest, every file's hash and the signature before it
  caches the agent, and pins the namespace's key on first use. Served, a pulled agent never
  automates until your organization approves its exact digest, nor while its registry recalls
  it ([Running a registry](https://github.com/mendari-hq/mendari/blob/main/docs/operations/registry.md)).

## Adopting an agent at a hospital

1. **Read before you run.** An agent's page on the Hub shows its Decision Card (intended use,
   risk level, policy, known failure modes), what the registry checked, the publisher's
   evaluations of each configuration and other sites' pooled results.
2. **Pull it and measure it on your own cases**, inside your environment
   ([Evaluating at your site](https://github.com/mendari-hq/mendari/blob/main/docs/guides/evaluate-at-your-site.md)),
   and ask which automation threshold those cases support: a publisher's threshold is evidence
   about the publisher's cases
   ([Choosing an automation threshold](https://github.com/mendari-hq/mendari/blob/main/docs/guides/choosing-a-threshold.md)).
3. **Run it in shadow.** A decide call with `?shadow=true` (or the header
   `X-Mendari-Shadow: 1`), or a shadow pilot started in the console, holds automation: each
   decision goes to a person while the engine records what the agent would have automated.
   `GET /api/agents/{namespace}/{name}/validation-brief` sums it up, with no case text.
4. **Bring it to your AI governance committee.** The committee packet is one page to print per
   agent, as your organization sees it: intended use, risk level and what the runtime
   enforces, the publisher's evidence and your own, monitoring, change control, data handling
   and approvals; then everything a framework asks for that nobody has stated, and a crosswalk
   to four published frameworks. It is a reading aid, not a certification. Get it from the
   console's Overview or with `mendari packet <agent> --db <the server's database>`
   ([The committee packet](https://github.com/mendari-hq/mendari/blob/main/docs/guides/committee-packet.md)).
5. **Let it decide, under your governance.** Your organization approves the agent's exact
   digest; at risk level 2 or 3 it automates only after a named approver records a governance
   approval, and automation can be
   [held](https://github.com/mendari-hq/mendari/blob/main/docs/operations/automation-hold.md)
   for one agent or all of them at once. Sign-in, roles, tenancy and PHI mode are in
   [Deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md)
   and [Organizations and memberships](https://github.com/mendari-hq/mendari/blob/main/docs/operations/accounts.md);
   FHIR, CDS Hooks and the SMART launch in
   [Connecting an EHR](https://github.com/mendari-hq/mendari/blob/main/docs/guides/connecting-an-ehr.md);
   an organization's own versions of agents in
   [the agent store](https://github.com/mendari-hq/mendari/blob/main/docs/operations/agent-store.md).
6. **Share back.** Once your site is registered with the registry,
   `mendari eval <agent> --dataset your-cases.jsonl --share` sends it your signed counts, never
   a case; the Hub shows them beside other sites'.

## What every decision leaves

Every decision is an event in an append-only, hash-chained trail (SQLite locally, Postgres on a
server): the agent's id, version and digest, each answer with its provider and evidence, the
outcome and the reasons. A `HUMAN_REVIEW` decision waits in the review queue, ordered by
priority, and what the reviewer decides is recorded in the same trail. Reports and exports
carry ids, never case text. Anyone with the database can check the chain:

<!-- check: run cli -->
```bash
uv run mendari decide agents/portal-message-agent --text "I need to reschedule my appointment next week." --db var/readme-audit.db
uv run mendari audit verify --db var/readme-audit.db
uv run mendari card agents/portal-message-agent      # the Decision Card: intended use, risk, policy, limits
uv run mendari --version
```

The server also speaks FHIR R4 (`POST /fhir/$decide`, decisions as `Task`, the trail as
`AuditEvent` and `Provenance`) and CDS Hooks 2.0; the demo agent maps a FHIR `Communication` and
declares a hook, so both answer for it
([Using the API](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md)).

## Documentation

The pages in [`docs/`](https://github.com/mendari-hq/mendari/tree/main/docs) build into a site
with MkDocs Material (`uv sync --group docs`, then `uv run mkdocs build --strict`); it will be
hosted once the repository is public.

| If you want to | Read |
|---|---|
| write an agent's manifest | [the authoring guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-an-agent.md) |
| find an agent's failures, set its threshold, check every change | [Finding failures](https://github.com/mendari-hq/mendari/blob/main/docs/guides/finding-failures.md), [Choosing an automation threshold](https://github.com/mendari-hq/mendari/blob/main/docs/guides/choosing-a-threshold.md), [Evaluating an agent on every change](https://github.com/mendari-hq/mendari/blob/main/docs/guides/evaluate-in-ci.md) |
| contribute an agent to this repository | [Contributing an agent](https://github.com/mendari-hq/mendari/blob/main/docs/guides/contributing-an-agent.md) |
| call Mendari over REST or FHIR | [Using the API](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md) |
| read an evaluation report | [Evaluation reports and ledgers](https://github.com/mendari-hq/mendari/blob/main/docs/spec/evaluation-reports.md) |
| deploy it | [`deploy/`](https://github.com/mendari-hq/mendari/tree/main/deploy), the [incident runbook](https://github.com/mendari-hq/mendari/blob/main/docs/operations/incident-runbook.md) |
| understand how the engine works | [Architecture](https://github.com/mendari-hq/mendari/blob/main/docs/architecture.md) |
| read why it is built this way | [the decision records](https://github.com/mendari-hq/mendari/tree/main/docs/decisions) |
| review its security | [SECURITY.md](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md), [the threat model](https://github.com/mendari-hq/mendari/blob/main/docs/security/threat-model.md) |
| see where the project is going | [Roadmap](https://github.com/mendari-hq/mendari/blob/main/docs/roadmap.md) |

## Project layout

```text
agents/           the bundled demo agent (portal-message-agent)
examples/         three starters for the service behind an agent: HTTP, LangGraph, Claude
src/mendari/
├── core/         manifest spec and loading, digest, runtime, policy, audit, Decision Card, committee packet
├── providers/    what can fill an agent's slot: agent (HTTP), llm, jev, rules, naive_bayes, criteria, cql, dosing
├── evals/        metrics, the evaluation harness, the operating point, the probe, behaviour tests, reports
├── standards/    FHIR R4, CDS Hooks, SMART on FHIR, Da Vinci
├── server/       the FastAPI app: REST, FHIR, CDS Hooks, review, audit, sign-in
├── dashboard/    the Governance Console: HTML, CSS and ES modules, no build step
├── hub/          the registry service and its client (push, pull, verify)
├── community/    the checks a contributed agent must pass
├── sandbox/      `mendari demo` and the `mendari sandbox` commands, on the demo agent
├── templates/    what `mendari new` writes
├── agent_kit.py  the kit the starters answer the agent contract with
└── mcp.py        governed decisions as MCP tools
tests/            the test suite (tests/fixtures/ holds synthetic fixtures only; tests/support/ makes them)
docs/             guides, spec, operations, security, decision records (the documentation site)
hub/              the Agent Hub website (Next.js), a front end to a registry
web/              the public page, mendari.co (static HTML and CSS)
deploy/           on-premises, Kubernetes and Vercel templates
scripts/          checks and generators that CI runs
action.yml        the GitHub Action that checks an agent in its own repository's CI
sandbox/          Inferno conformance kits against the engine (inferno/); CI's compose file for the test fixtures
licenses/         licence texts and notices for what the images and NOTICE carry
notes/, tasks/    project notes, plans and lessons
```

## Development

<!-- check: skip ci: each line is a step of CI job test, or a narrower run of it -->
```bash
uv sync
uv run pytest                             # the full suite; warnings are errors
uv run pytest -m core                     # the edit loop, about 20 s: loading, decisions, policy, audit
uv run pytest tests/test_agent_provider.py   # and the file for what you changed
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy src tests
```

No real PHI, ever: test data is synthetic. How to contribute is in
[CONTRIBUTING.md](https://github.com/mendari-hq/mendari/blob/main/CONTRIBUTING.md), how the
project is run in [GOVERNANCE.md](https://github.com/mendari-hq/mendari/blob/main/GOVERNANCE.md),
and how to report a vulnerability in
[SECURITY.md](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md).

## Status and limits

- **Research preview.** The package is 0.1.0 and not released: it is not on PyPI, and the
  repository is not public yet.
- **Synthetic data only.** Every agent, dataset and figure here is synthetic: the figures show
  that the evaluation works, not how an agent does on real patients.
- **Not a medical device**, and not for emergencies: patients should call 911.
- **No real PHI yet.** Organization sign-in and PHI mode are built and have had no external
  security review: until they have, Mendari must not hold real PHI or decide about real
  patients.

## License

Apache-2.0. See [LICENSE](https://github.com/mendari-hq/mendari/blob/main/LICENSE), and
[NOTICE](https://github.com/mendari-hq/mendari/blob/main/NOTICE) for the third-party material and
terminology notices. Changes: [CHANGELOG.md](https://github.com/mendari-hq/mendari/blob/main/CHANGELOG.md).
