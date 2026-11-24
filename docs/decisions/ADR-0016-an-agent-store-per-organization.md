# ADR-0016: An agent store per organization, and promotion

## Status
Proposed (2026-11-10, LP-20a). Built in steps 20b to 20j (`notes/lp20-plan.md`, 2026-11-16),
off unless `MENDARI_AGENT_STORE=on`; it stays Proposed until the owner confirms the decisions
the plan lists, each built to its default. When it is accepted it gives the reserved `pack_author` role
of [ADR-0008](ADR-0008-accounts-and-memberships.md) its permission, adds the origin `store` that
[ADR-0009](ADR-0009-bounded-evaluation-for-agents-the-deployment-does-not-own.md) names, and makes
true [ADR-0004](ADR-0004-machine-access-tokens-and-scopes.md)'s line that a credential never
lists another organization's agents.

Terminology updated 2026-11-23: what this record called a pack is an agent.

## Context
A server reads its agents from the file system once, at start-up, and every organization on it is
served the same ones. So:
- An organization cannot bring an agent of its own to a hosted server, or change one, without the
  operator installing files.
- Nobody has permission to write an agent. The `pack_author` role is reserved and grants nothing.
- Nothing checks a dataset beyond its shape, nothing looks for patient identifiers in what is
  uploaded, and nothing compares an upload with the hidden evaluation sets
  ([ADR-0011](ADR-0011-hidden-evaluation-sets.md)), whose index was built for that check.
- There is no promotion. The version served is the newest on disk. The approval that exists is
  permission to automate at risk levels 2 and 3
  ([ADR-0002](ADR-0002-runtime-enforced-risk-caps.md)); it says nothing about which version an
  organization serves.
- An evaluation runs inside one request and compares an agent with its own published benchmark,
  not a draft with the version in use.

The pieces the store needs exist: the bounded engine for agents the deployment does not own
(ADR-0009), memberships (ADR-0008), exact intervals, paired tests and the per-case ledger
([ADR-0013](ADR-0013-scoreboard-and-scoped-grades.md)), and the hidden-case index (ADR-0011).

## Decision
1. **Each organization has a store of agent versions in the engine's database**, behind one
   switch (`MENDARI_AGENT_STORE`) and only in memberships mode. Unset, the engine mounts no store
   route and serves agents exactly as before.
2. **A version is keyed by its content digest and never changes.** The digest is the one the
   loader hashes from the stored files, never one a caller names. An edit is a new version that
   names its parent. Only a version's state moves (draft, held, promoted, retired, withdrawn),
   and each move is one chained event that names the organization.
3. **Nothing an agent says about itself is kept.** An uploaded card, benchmark or leaderboard is
   dropped; the server renders the card and computes every figure for the digest.
4. **A store agent is an agent the deployment does not own.** Its origin is `store`; it runs on the
   bounded engine with the per-decision budget, its provider settings are capped as a pulled
   agent's are, and it never takes a bundled agent's id and version. Its namespace is its
   organization's id, or a registry namespace that organization holds
   ([ADR-0015](ADR-0015-registry-accounts-under-the-engine.md)); a copy of a built-in agent is
   renamed on the way in.
5. **One validation function** is called by the command line, the community check, the registry
   and the store. Dataset checks are pure functions that use ADR-0011's canonical forms and
   mark a thin label "too few to estimate" instead of refusing it.
6. **Every upload passes a deterministic detector for patient identifiers** before anything is
   stored. Over its threshold the upload is refused with a report of kinds and places, never a
   value.
7. **Every upload is compared with the hidden-case index.** A match is held without a reason:
   the uploader learns only that the version is held for the operator's review, and the set and
   case ids are readable by the operator's command alone. Uploads are budgeted, because a held
   answer is still one bit. A deployment with no index key holds no hidden case and records the
   check as not applicable.
8. **Authors are people who hold `pack_author`.** They upload an archive signed by a key their
   organization registered, or copy an agent the deployment serves, then edit, validate and test a
   draft. A draft never answers a decide request.
9. **An evaluation is a stored job** that advances in slices a caller asks for, with no worker
   process, and ends in one report with its ledger. A draft is compared with the current
   version on the same cases, with the statistics the scoreboard uses and no others.
10. **Promotion is its own record.** It is bound to a digest, needed at every risk level for a
    store version, signed off by a person who authored no version in that draft's history, and
    names the evaluation reports of that digest it relied on. It is not the automation
    approval, and neither stands in for the other: at levels 2 and 3 a version needs both
    before it automates.
11. **An organization is served the agents on disk plus the versions it promoted**, and never
    another organization's. A promoted version is built once per process on first use. A store
    version never replaces an agent the deployment serves from disk.
12. **Reviewers' corrections become an improvement dataset** kept sealed in the organization,
    each label with where it came from. They are offered to a draft as proposed tests kept
    beside it, never written into an agent's files.

## Consequences
- An organization can change what it serves without the operator, and every such change has an
  author, an approver, the evidence relied on and a digest on the audit chain.
- For the first time since agents were built at start-up, which agent answers a request depends
  on the caller's organization. An organization with no promotion is served exactly as before.
- The engine's database holds other people's files: bounded per version, per organization and
  per day, and checked before they are stored.
- The hidden-set check tells an uploader one bit per upload, and a reworded hidden case passes
  it. Both are residual risks.
- The detector finds shapes and listed names. It is a filter, not a guarantee; the terms of use
  still forbid real patient data.
- A bundled agent whose patterns use lookaheads cannot be copied until they are rewritten
  (LP-16c-2), because a copy runs on the bounded engine.
- Hosted scaffolding, training and calibration are not built: an agent whose trained model
  changes is trained locally and uploaded.
- Nothing changes what a valid agent decides, and no bundled agent's digest changes.

## Alternatives Considered
- **Keep agents on disk and add a per-organization allow-list.** It gives no upload, no draft and
  no record of who chose a version, and it needs the operator for every change.
- **Reuse the automation approval as promotion.** It exists only at levels 2 and 3, expires, and
  answers a different question. A level 1 agent would be served with no sign-off at all.
- **Tell the uploader that a hidden case matched.** It turns the check into a search for the
  hidden sets, one case at a time.
- **Keep a copy's original id.** It would pass an organization's edit off as the publisher's,
  and compete with the built-in agent every organization is served.
- **Object storage for archives.** A second place to back up, to bind to an organization and to
  erase; the database already holds what the registry stores.
- **A worker process for evaluations.** The owner decided against one for now (ADR-0009); slices
  keep a job inside the limits a request already has.
- **Writing proposed tests into the agent.** Wherever the server holds real data, a corrected
  case is a patient's input, and an agent's files can be exported and published.
