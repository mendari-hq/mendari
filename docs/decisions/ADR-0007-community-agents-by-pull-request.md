# ADR-0007: Community agents come by pull request, and where an agent lives is its label

## Status
Accepted (2026-10-04). The code, checks and docs are built; the GitHub settings (labels, the DCO
app, the agents' CI check and DCO as required checks, the code-owner ruleset) and turning community
agents on for the hosted platform are the owner's steps (`tasks/todo.md`, B8 and B9).
Amended by [ADR-0009](ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md)
(2026-10-07): agents from others run on the community engine in the server's process, not in an
isolated one.

Terminology updated 2026-11-23: what this record called a pack is an agent.

## Context
The repository is going public so outside developers can contribute agents. Until now
every agent in the repository was one Mendari maintains (`agents/`), and the loader trusted what
it found:

- every `agent.yaml` under `agents/` was served, nested ones included, and when two agents shared
  an id the newest version won silently, so a pull request could shadow a maintained agent;
- `publisher.type` is self-declared, and the maintained agents all declare `community`, so the
  manifest cannot say who stands behind an agent;
- the risk level is self-declared too, and it decides whether an agent may automate;
- an unscoped provider setting (`router=jev`) reached any agent with that slot and variant, so a
  new agent could spend the deployment's remote calls;
- the PR controlled its own evaluation bar (`degradation_tolerance`) and what its records keep
  (`store_input`).

Clinical content from an unknown author, unreviewed by a clinician, must not act on its own,
spend money or claim a trust it has not earned, and none of the existing agents or deployments
may change because community agents exist.

## Decision
The owner decided (2026-10-04):

- **Location is the label.** Community agents live in `community-agents/<name>/`, one level deep,
  with the id `community/<name>`. Only the owner's merge puts an agent there. The origin of an
  agent (`core/origin.py`: maintained, community, hub, local) comes from where it was found,
  never from its manifest. A community agent uses the `community/` namespace only, so it never
  claims `mendari/` or `openhealth/`, nor a maintained agent's id; a second claim to a community
  agent's id is refused and reported, whatever its version. Agents from anywhere else are found
  as before (the newest version of an id is served); one whose id is in `community/` gets the
  community limits wherever it was loaded from. A community agent's FHIR systems live under its
  own url (`https://mendari.co/agents/community/<name>/`), and its name, CDS title and publisher
  never name Mendari nor copy a maintained agent's name (CI).
- **Served, labelled, recommend-only.** A deployment serves community agents only with
  `MENDARI_COMMUNITY_AGENTS=on` (default off; the one switch turns them all on or off). Every
  surface labels them "Community · not clinically reviewed" (the API, `/api/health` counts,
  the dashboard picker, the overview, Try it, the Decision Card, CDS Hooks discovery and cards,
  the `/docs` landing); the maintained agents say "Maintained by Mendari"; `publisher.type` is
  shown only as self-declared. A community agent never automates: the policy caps AUTO to
  HUMAN_REVIEW (ADR-0002: policy only lowers automation), and no policy rule of its own
  blocks (refused at load; a block is final at once, so no person would confirm it). It reads
  at most 10,000 characters of text in all, the length the caps on its patterns' work are
  measured on (RE2's time grows with it).
- **Local providers only, unless the owner opts an agent in.** A community agent's default
  variants are local (`rules`, `criteria`, `dosing`, `naive_bayes`, or a composite of those);
  unscoped provider settings skip it, and only `community/<name>:slot=variant` runs it on
  something else. Its pinned `model` and `max_tokens` give way to the deployment's.
- **Risk level at most 1**, and `audit.store_input: hash`, refused at load otherwise.
- **The bar is the base branch's.** `mendari community-check` (in a CI job that runs
  on every pull request, so it can be a required check) enforces scope, identity, manifest
  policy, tests, evaluation floors (critical detection at least 0.9, no critical case
  automated), no regression within a tolerance read from the base branch (at most 0.05), a
  benchmark, card and model CI regenerates and compares, a pattern check (every pattern
  compiles on RE2, the engine community agents' patterns run on), size limits, version and
  CHANGELOG against the digest, and PHI warnings. Its limits are constants in the checker,
  which a community pull request may not change.
- **DCO.** Every commit is signed off (`git commit -s`); there is no CLA.
- **Governance.** The owner is the only merger, with a code owner's approval required. Each agent
  names its maintainers in `community-agents/MAINTAINERS.yaml`; a removed agent stays there as a
  tombstone, so its id is never reused. Clinical review is recorded in the pull request, not in
  the product.

## Consequences
- Nothing existing changes: the maintained agents decide identically, and with
  `MENDARI_COMMUNITY_AGENTS` unset a deployment serves exactly what it served before (an agent
  pulled from a registry, a local copy of a maintained agent, a path holding `agents/`), except
  that an agent whose id is in the new `community/` namespace gets the community limits. The
  community agents are not in the wheel or the images.
- An outside author sees the agent live (where community agents are on) with no power to act on
  a patient, spend the deployment's calls or look reviewed.
- The risk level is still self-declared: the reviewer is the backstop for under-declaring, and
  the level cap keeps level 2 and above (and the FDA clinical decision support question) off
  this route until a clinical reviewer group exists.
- A community agent's patterns cannot backtrack, by construction. A static check and a timed
  probe of each pattern came first; three checks of them each found new slow patterns that
  passed, and Python's `re` has no time limit. So (decided 2026-10-05, built as B11) a
  community agent's patterns run on RE2, which never backtracks: a pattern's time grows with
  the text's length (at most 10,000 characters) times its size. A pattern RE2 cannot run is
  refused at load and in CI, the settings that multiply the work are capped at load
  (`core/caps.py`), including the size of the patterns one input runs (at most 3,000
  together), and the static check and the probe are gone. The caps alone left a crafted agent
  seconds of work on adversarial text (5.5 s, 17.5 s with negated patterns), so a community
  agent's patterns and schema checks also share a time budget of one second of CPU per decision:
  past it its rules or its schema check stop and the decision abstains, for a person, so a
  decision runs them for at most the budget plus one step under way (about 3.5 s of CPU at
  worst; waiting for a model or for other requests costs nothing). An agent that runs out of time
  on ordinary text is removed: revert the merge, or turn the switch off.
- Names are checked the same way: CI fails a name, CDS Hooks title or publisher that copies a
  maintained agent's name or title, or uses the word Mendari (or OpenHealth) as whole words.
  A look-alike, which no table catches completely (the same three checks found ways past
  those too), is a warning for the owner's review. The label by origin is the protection.
- The owner is the bottleneck. The proposal issue form screens ideas before they are built,
  and CI catches the mechanical problems.
- A merged mistake with real patient data or licensed content cannot be fully taken back from a
  public history; the PHI warnings, the provenance review and the DCO reduce, not remove, that
  risk.

## Alternatives Considered
- **Community agents in `agents/`, labelled by `publisher.type`.** Rejected: the manifest is
  self-declared, the maintained agents already declare `community`, and another folder there
  changes the wheel, the images, the build and tests that count the agents.
- **Kept in the repository, served only from an owner's allow-list per agent.** Safest, but
  "available on the platform" becomes an extra owner step for every agent. The label and the
  recommend-only cap give most of its safety without it.
- **Served like maintained agents, automating by declared risk level.** Rejected: it trusts a
  self-declared level from an unknown author.
- **`<github-handle>/<name>` or organization namespaces.** Rejected for now: attribution without
  squatting, but handover is awkward and the id carries no trust signal; organizations need an
  approval step each.
- **A CLA, or nothing beyond Apache-2.0.** A CLA deters contributors and needs counsel; nothing
  leaves no record of the contributor's right to submit clinical content. The DCO is the light
  middle.
- **Uploads through the UI or API, and publishing from the registry.** Later, and only with
  isolation: each agent's rules in their own process with time and memory limits (or a regex
  engine with timeouts), so untrusted content never runs in the server's process. Until then
  the pull request is the only way in, and the owner reads everything that is merged.
  (Amended by ADR-0009: an agent pulled from a registry, and from LP-20 one from the store, runs
  on the community engine, RE2 with the caps, the input limits and a CPU budget per decision,
  in the server's process; no worker process for now.)
