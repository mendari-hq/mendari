# ADR-0026: The first release is the one baseline, and it governs agents only

## Status
Accepted (2026-11-25), with wave 6 of the plan in `tasks/todo.md`, which builds it. Decided by
the owner on 2026-11-24, before the first release (0.1.0, not yet published): what only the
reference agents of the pre-release used goes, what agents need stays, nothing made before the
first release stays readable, and old data is replaced by new synthetic data. The decisions that
settle it item by item (G1 to G16, N1 to N6, O1 to O11) are in
`notes/wave6/wave6-decisions.md`.

It supersedes four records, whose features are removed:
[ADR-0007](ADR-0007-community-agents-by-pull-request.md) (community agents),
[ADR-0011](ADR-0011-hidden-evaluation-sets.md) (hidden evaluation sets),
[ADR-0013](ADR-0013-scoreboard-and-scoped-grades.md) (the scoreboard) and
[ADR-0019](ADR-0019-the-engine-learns-and-a-person-promotes.md) (the engine learns). It amends
six where a part of their decision went (point 9). Every other record stands as it was decided.

## Context
Before its first release Mendari grew around reference agents of its own: five of them, built
on synthetic data to show the engine at work (routing a patient's portal messages, routing
referrals, checking a prior authorization before it is sent, prescreening patients for a
clinical trial, and guarding a draft to a patient). Three things grew with them.

- **A name in every layer.** The governed unit had another name, carried by modules and classes,
  JSON keys and query parameters, database columns, event kinds, a role, FHIR identifiers, the
  archive format, routes, settings and every page. Wave 1 of the plan renamed what people read
  and type, and kept the rest behind compatibility: the former routes, settings, manifest file
  name, archive extension, cache folder and console address still answered, and the names on the
  wire and in stored and signed records waited for a versioned change of format.
- **Features only those agents used.** Providers and composites for their variants, commands that
  trained and compared their models, the Da Vinci, X12 and OMOP standards their prior
  authorization and referral work spoke, code systems invented for their examples, a path for
  outside authors to contribute agents by pull request, and hidden evaluation sets with a
  scoreboard graded on them.
- **Every pre-release shape.** The stores read whatever a pre-release build had written: about
  thirty branches read an older database as it was, columns were added at open, and older keys,
  tokens, payloads and records each had a fallback. Spec versions 0.1 to 0.4 all loaded. Six
  formats were at `/2`.

Nothing has been released. No one outside the project holds a database, an archive, a report or
a signature made before 0.1.0, and the project's own deployment holds synthetic data only. A name
kept for compatibility, or a branch that reads an older shape, would ship in the first release
with no reader, and taking it out later would be a breaking change.

## Decision
1. **What Mendari governs is an agent.** The five reference agents are deleted, with their data,
   generators, FHIR records and sandbox parts. The product bundles one agent, the demo
   (`agents/portal-message-agent`, `mendari/portal-message-agent`). The tests run on synthetic
   fixture agents of their own (`tests/fixtures/agents`: `mendari-test/triage`,
   `mendari-test/clinical-review` and `mendari-test/chart-summary`), each carrying the features
   its tests need.
2. **One word, in every layer.** The old name is gone from identifiers, modules and file names,
   JSON keys and query parameters, database columns and indexes, event kinds, the role, FHIR
   identifiers, formats, routes, settings and sentences. On the wire and in records the agent is
   `agent`, `agent_id`, `agent_version`, `agent_digest` and `agents`; the event kinds are
   `store_version.*`, `registry_agent.*` and the registry chain's `agent.*`; the role is
   `agent_author`; the FHIR systems are `https://mendari.co/fhir/CodeSystem/agent-version` and
   `https://mendari.co/fhir/NamingSystem/agent-version`, and an agent's own canonical URLs start
   with `https://mendari.co/agents/`. One test holds it, `tests/test_vocabulary.py`. Over every
   text file the repository tracks, outside `notes/`, `tasks/`, `licenses/`, vendored code,
   lockfiles and binary files, it allows the old name's letters only inside the English words
   that hold them (`package`, `packet`, `unpack`, `backpack`, and Python's `struct.pack`), and it
   finds none of the five reference agents' ids. The decision records say agent too: their words
   were edited, and their decisions are left as they were written, each with a status that says
   what this record changed.
3. **Nothing from before 0.1.0 is read.** Every compatibility name is gone: the former routes,
   settings, manifest file name, archive extension, cache folder, console address, Hub address,
   and the command-line flags and commands that answered to them. A setting under a former name is
   not looked for, so it is simply not read.
4. **Each store's first schema is its baseline, and a pre-release database is refused.** The
   transaction that makes a store's schema marks it: `mendari-engine/1` in the audit store's
   `settings` table, `mendari-registry/1` in the registry's `hub_meta` (`core/db.py`,
   `Baseline`). Every open, read-only included, reads the mark before anything else. A database
   that holds the store's anchor table (`audit_events`, `hub_events`) and no mark was made by a
   pre-release build: it is refused, never read or migrated, and the refusal says to give the
   server an empty database (the incident runbook's "Starting over"). A mark that names a newer
   schema is refused too. The engine and the registry can share one database; each checks only
   its own anchor and mark. The schema is the first release's whole, with no column added at
   open, and each store's migration hook stays, empty, for the first change a release makes.
5. **Every format the first release writes is at `/1`.** The six that were at `/2` restart at
   `/1` (`mendari.eval/1`, `mendari.validation-brief/1`, `mendari.registry-version/1`,
   `mendari.leaderboard/1`, `mendari.site-report/1`, `mendari.publisher-evaluation/1`). The
   signed archive is `mendari-agent/1`, with `mendari-agent.json` and `mendari-agent.sig` and its
   files under `agent/`. Every other format stays `/1`, its keys renamed where they held the old
   name. No format written before is read.
6. **The agent and its service.** The agent is what Mendari governs: `agent_id`,
   `agent_version`, `agent_digest`. The process outside Mendari that answers for an `agent`
   variant is its service: the variant names it with `service_id`, the service reports its
   `service_version`, and the calibration file (`mendari.agent-calibration/1`) and the
   calibration report name it `service`. A request of the agent HTTP contract carries both ids,
   `agent_id` and `service_id`; its answer and the service's `/health` carry `service_version`.
   The provider type `agent`, `MENDARI_AGENT_URL`, `MENDARI_AGENT_TOKEN` and "the agent HTTP
   contract" keep their names: they read as the agent's own service.
7. **One spec version.** A manifest says `spec_version: "0.4"`, the one version the engine reads
   (`core/spec.py`, `SPEC_VERSION`); a manifest of 0.1, 0.2 or 0.3 is refused. The spec is one
   page, `docs/spec/agent-manifest.md`.
8. **The features removed with them**, by the owner's rule (what only the reference agents used
   goes, what agents need stays), applied item by item:
   - The providers `jev`, `naive_bayes`, `cql`, `criteria` and `dosing`, and the composites
     `any_of`, `cascade` and `vote` (G1, G2). The provider types are `rules`, `llm`, `agent` and
     `check`, with `repeat` and the `llm` provider's batching.
   - `mendari train`, `mendari improve` and `mendari agreement` (G3), which built, retrained and
     compared the `naive_bayes` models. Of ADR-0019, what stays is the export: `mendari feedback
     export --source reviews` writes an agent's reviewed decisions as labelled cases, each marked
     `exported_by`, and an archive or a registry refuses a file that holds the mark.
   - Da Vinci CRD, DTR and PAS, X12 and OMOP (G4), with their routes, commands, sandbox parts and
     conformance jobs (G8, G9). The standards that stay: FHIR R4, US Core 6.1.0, CDS Hooks, SMART
     App Launch and Backend Services, and Bulk Data (G5), and reading an EHR's records into an
     agent's inputs, which a hospital's agents need (G6).
   - The code systems invented for the reference agents' examples (G7). A test that needs a code
     system of its own declares it in its fixture.
   - The path for outside authors to contribute an agent by pull request (G10):
     `community-agents/`, the `community` origin, `MENDARI_COMMUNITY_AGENTS`, `mendari new
     --community` and the community check. The bounded engine it led to stays
     ([ADR-0009](ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md)), for
     agents from a registry or a store.
   - Spec versions 0.1 to 0.3, and the manifest's `guard` (with its route and the console's guard
     mode), `derive`, `coverage_information`, `questionnaire` and a policy in a file of its own
     (G12). `code_systems` stays: reading an EHR's records needs it.
   - Hidden evaluation sets and the scoreboard (G13): their keys, tables, routes and commands, the
     grading key and the `grading` role, and the grading method. The `labeller` role stays, with
     the blind grading screen that written output uses
     ([ADR-0025](ADR-0025-a-judge-is-trusted-by-measured-agreement.md)).
   - `openhealth` as an origin, which stays a reserved namespace in the registry and the store;
     the package's `server` extra; and `scripts/synthetic_cases.py` (G16).
9. **What this record amends**, each said in the record's own status:
   - [ADR-0004](ADR-0004-machine-access-tokens-and-scopes.md): the grading key that ADR-0011
     added is gone with it.
   - [ADR-0008](ADR-0008-accounts-and-memberships.md): the `grading` role is gone; `labeller`
     and `agent_author` stay.
   - [ADR-0009](ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md): there are
     no community agents. The bounded engine runs an agent of an origin the deployment does not
     own (`hub`, `store`) whose digest is not a bundled agent's.
   - [ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md): the scoreboard its
     decision 1 waited for is removed. A registry's database is made with its chain and its mark,
     so what decisions 4 and 10 say of rows from before the chain and of versions published
     before applies to none.
   - [ADR-0016](ADR-0016-an-agent-store-per-organization.md): no upload is compared with hidden
     sets and no version is held (decisions 2 and 7), and the one validation function is called
     by the command line, the registry and the store, the community check being gone (decision
     5).
   - [ADR-0018](ADR-0018-local-providers-read-plain-text.md): `rules` is the one provider it
     names that stays, and it reads its text fields in their plain form.

## Consequences
- A database, an archive, a report or a signature made before 0.1.0 is not read: each is made
  again. The project's own deployment, which holds synthetic data only, is emptied and refilled
  once wave 6 merges, and so is every preview, local and test database a pre-release build made.
- The mark stayed `mendari-engine/1` while wave 6 renamed columns, since nothing was released. A
  database made by a wave 6 build before the rename passes the mark and fails at its first
  query: it is replaced too.
- A setting under a former name turns nothing on: a feature it switched on is off until the
  setting is given its name.
- A service built on the former names of the agent HTTP contract reports `agent_version`, which
  the engine no longer reads. Mendari then sees no `service_version`, so a calibrated variant is
  capped (ADR-0024, point 2) until the service is updated. The agent kit (`mendari.agent_kit`) and
  the starters in `examples/` speak the new names; a starter deployed before wave 6 is deployed
  again.
- The tests run on three small synthetic agents and the mini agents of
  `tests/fixtures/mini-agents`. A feature no agent of the product needs is not in it.
- A word or a name that holds the old name's letters and is not the old name (an English word, a
  dependency's name) fails the vocabulary test until it is added there, with its reason.
- The decision records keep their history: the four superseded ones say so in their status, and
  the six amended ones say what changed.

## Alternatives considered
- **Keep a compatibility name for a release or two.** It is for users of a release, and there
  are none: it would ship in 0.1.0 with no reader, and be a breaking change to take out.
- **A refusal, or a warning, when a former setting name is set.** A check for old names is a
  compatibility helper by another name (O11).
- **Bump every format whose keys change.** It numbers versions that nobody ever read (O2).
- **Spec version `"1.0"` at the first release.** A separate decision. `"0.4"` stays the one
  value: the records from ADR-0023 on and the guides cite it (O3).
- **Rename first, then remove.** About 40% more renaming, and regenerating what the removed
  agents held, only to delete it (O1).
- **Delete the records of removed features.** That loses the record of why they were built
  and why they went (O4).
- **`endpoint_id` for the service's id.** An endpoint is a URL; the service is the process
  behind it (N1).
- **Rename the provider type `agent`, `MENDARI_AGENT_URL` and `MENDARI_AGENT_TOKEN` to
  `service`.** They read as the agent's own service, and every scaffold, example and guide would
  change with them (N2).
- **`agent_version.*` as the store's event kinds.** It reads as the field of that name;
  `store_version.*` matches the `store_versions` table (N3).
- **Remove reading an EHR's records with the reference agents.** It is what a hospital's agents
  need for real patient workflows, built and tested against Epic's sandbox, so it stays, on a
  fixture agent of its own (G6).
- **Rewrite the repository's history to purge the reference agents.** It would break every clone
  and pull request link (O8).
