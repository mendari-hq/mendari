# ADR-0009: Bounded evaluation for agents the deployment does not own

## Status
Accepted (2026-10-07, the owner's decision: LP-16, Option A scoped). Built in steps
(`tasks/todo.md`, LP-16 build): 16a, the engine choice and the registry's check; 16b, a
wall-clock deadline per decision; 16c, the rewrites of the bundled agents' lookaround patterns.
Amends [ADR-0007](ADR-0007-community-agents-by-pull-request.md), which planned an isolated
process for agents from others.

Terminology updated 2026-11-23: what this record called a pack is an agent.

## Context
Community agents run on the B11 community engine since #133: their patterns on RE2, which never
backtracks, their settings capped at load, their schema held to keywords whose work Mendari
bounds, their inputs to 10,000 characters (10,000 values, 32 deep) and their patterns and
schema checks to one second of CPU per decision, after which the decision abstains, for a
person. Every other agent ran on Python's `re`, with no time limit, and installing it meant
trusting its author.

Agents pulled from a registry were among those: `hub/archive.py` loaded an extracted agent as
`local`, so a registry checked a publish on `re`, and a pulled agent ran on `re`. A pattern that
backtracks (`(a+)+$`) holds Python's interpreter lock for seconds on a few dozen characters.
ADR-0007, `SECURITY.md` and the threat model promised that agents from others would run in an
isolated process with memory and time limits once a public registry or uploads existed; #175
measured that option (a worker process, about 0.5 s to start).

## Decision
The owner decided (2026-10-07):

> **LP-16, Option A scoped:** agents the deployment does not own run on the B11 community
> engine: RE2 for rules and input-schema patterns, the load-time caps, the schema keyword
> allowlist, the 10,000-character input limit and the 1 s per-decision CPU budget
> (`time_budget`, then a person decides). That is registry-pulled agents now and store agents
> from LP-20; community agents already do, so B9 does not wait for LP-16. A pulled agent whose
> digest equals an agent bundled with the installed release keeps the maintained engine, so
> `hub-e2e`, the sandbox registry and `mendari pull` of a bundled agent keep working; the
> registry checks a publish on the same engine (`hub/archive.py` loads it as `local` today).
> Bundled (maintained) agents and operator-installed local agents stay on `re`, unchanged, and
> LP-16's requirement 1, its first test and gate G3-4's probe are narrowed to agents the
> deployment does not own. Accepted with it: on `re`, a crafted 10,000-character message can
> hold Python's lock, stalling its instance (rate limits bound it). A
> wall-clock deadline across one decision's providers applies to every agent. No worker process
> (Option B) for now. The bundled agents' lookaround patterns are rewritten, with a
> decision-identity proof and a rebuild, before a bundled version that differs from the
> installed one is published to a registry or copied into the store.

As built:

- **Which agents.** `Pack.bounded` (`core/origin.py` `bounded`): a community agent, or an agent of
  an origin the deployment does not own (`NOT_OWNED`: `hub`, and `store` from LP-20e) whose
  digest is not one of the installed release's bundled agents (`core/reference.py`
  `reference_digests`: the digest of each agent bundled with the release, computed once per
  process). Everything a community agent's patterns, schema and inputs are held to applies to
  it, the 10,000-character limit, the 10,000 values and 32 levels, the 100-error stop and the
  short schema reasons included. Reasons stored at run time say "an agent from a registry";
  load-time messages name both ("a community or registry agent").
- **The engine follows the bytes.** Only the digest `load_pack` hashed from the files it then
  reads chooses the engine: never the manifest (its `id`, `version` or `publisher.type`), the
  digest a registry recorded, nor the `.mendari-verified` marker. The digest covers every file
  in the agent except those under its `evals/`, any `DECISION_CARD.md`, and hidden entries and
  `__pycache__` at any depth (`agent.yaml`, `policy.yaml`, `logic/`, `fhir/`, `data/`, `models/`,
  `schemas/`, `tests/` and any other file are in it); what it leaves out is never read on the
  decide path (`Pack.read_bytes` reads only files the digest hashed, and only the bytes it
  hashed). So equal digests mean equal behaviour, and one changed byte puts a copy of a
  bundled agent on the community engine.
- **Every load path.** `archive.extract` takes the origin as a required keyword: the registry's
  publish, `mendari pull` (before the staged copy is renamed into the cache, so an agent the
  engine refuses is never cached nor marked verified) and `mendari archive verify` pass `hub`.
  `Decision.from_path` takes the origin of its path, so a cached agent loaded by its path is
  still an agent from a registry. The registry checks a publish against its own release's
  bundled agents. On every one of these paths, an agent from a registry with a bundled agent's
  id and a version that ties its (`version_key`) but other files is refused (`core/pack.py`
  `_shadow_problem`, LP-16 F): the cache wins a version tie, so it would be served in the
  bundled agent's place. The registry refuses its publish (`422`), `mendari pull` refuses it
  before it is cached, and one cached before is listed under `/api/health` `invalid`, with the
  directory to remove, while the bundled agent is served.
- **Little else changes for a pulled agent.** It is labelled "From a registry", may automate
  under its own policy and keeps its own BLOCK rules: community policy stays the community
  agents' (`Pack.community`). Its rules failing on the budget (`time_budget`) never leave AUTO,
  and a failed primary abstains (`core/policy.py`, unchanged).
- **Its provider configs, as a community agent's (LP-16e, amending the line above, which said it
  keeps its pinned model).** Every variant and member is checked at load against its
  provider's closed config (`providers.config_problems`), and a problem refuses the agent (an
  owned agent loads with it as a warning). Its model is the deployment's, its `max_tokens` and
  `timeout_ms` are held to the provider defaults and a `reasoning_effort` above `low` is
  dropped (`core/origin.py` `deployment_config`), and each model variant must name its
  `state_fields`. Its remote answers can change where it pinned a model; no shipped or deployed
  agent pins one.
- **No setting.** There is no setting that puts a pulled agent on `re`. An operator who trusts an
  agent copies it into `MENDARI_AGENTS_PATH`, where it is a local agent: owned, on `re`. A pulled
  agent is removed by deleting its version from the agent cache, or by not pulling it;
  `MENDARI_COMMUNITY_AGENTS` and reverting a merge reach community agents only.

## Accepted residual
On `re`, where bundled and local agents stay, a crafted 10,000-character message to a pattern
that backtracks can hold Python's lock, stalling its instance; the rate limits bound how often.

## Deadline
A wall-clock deadline across one decision's remote calls applies to every agent (built in 16b,
#184): `MENDARI_DECISION_DEADLINE_S`, whole seconds 10 to 270, 120 unless set, checked at
startup; an agent cannot set it. It is a context variable on `time.monotonic` that each decision
sets for itself at its provider loop, never stored on the `Decision` every thread shares. Jev,
the LLM and the CQL engine check it before they fetch a key or charge a budget, so a call that
would start after it is not made; under way, each HTTP attempt's timeouts are clamped to what
remains, no retry is waited past it, an answer still arriving stops at it, and waiting for
another thread's Vercel Connect key stops at it. Each is the provider error `timeout`, which
caps AUTO; local providers never look at it. Residuals (threat model D9): `httpx` bounds each
network step by what remained when it started, so headers sent a little at a time can overrun;
each decision of a request that makes several has a deadline of its own.

## Alternatives Considered
- **Option B: every agent the deployment does not own in a worker process** with memory and
  time limits (#175). About 0.5 s to start a worker, a pool to size and supervise on a
  serverless host, and the in-process limits are still needed for community agents. Not now.
- **Every agent on RE2.** Changes what bundled agents decide: lookarounds cannot run on RE2, and
  a rewrite of one is not decision-identical on every input for every pattern (16c).
- **A setting to trust a registry, or an agent.** A setting that lowers protection for content
  from outside is easy to leave on; a local copy already says "I own this".
- **Trusting the digest a registry recorded or the publisher's type.** Both are metadata a
  registry, or whoever controls one, writes.

## Consequences
- A copy of a bundled agent of this release, pulled from any registry, runs as before:
  `hub-e2e`, the sandbox registry and `mendari pull` of a bundled agent keep working, and
  `mendari pull` says which engine a pulled agent runs on.
- A bundled agent at another version than the installed one (an older release's, or a fork)
  runs on the community engine: one whose patterns use lookarounds is refused there, with a
  reason saying why and what to do (use the copy this release bundles, or a version published for this release). A
  registry and its clients on different releases see this.
- Forks of the bundled agents read at most 10,000 characters of text, though an owned agent's
  schema may allow longer notes on `re`.
- The sandbox registry seeds from the host checkout into an image built earlier: run
  `docker compose up --build` before `scripts/hub_seed.py`, or the image's bundled set is an
  older release's.
- ADR-0007's isolated process for agents from others is replaced by this decision.
