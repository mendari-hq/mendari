# ADR-0012: A registry that shows only what it checked

## Status
Accepted (2026-11-07, the owner: "the patent gate is clear and I approve everything on my part",
which confirms each default below). Proposed 2026-10-09. Built (LP-18, 18a to 18h and the
Decision Card's approval screen, 2026-10-09 to 2026-10-17), each default below as
`tasks/todo.md` records it. Gate G3-4's prerequisite `a10-registry-hub` (`launch/gates.yaml`)
names the tests that hold it. On 2026-11-07 the owner confirmed the default for where the
registry runs: under the engine app (LP-19's option A, the build session's recommendation).
LP-19 (registry accounts, namespace claims, reviewers, the counter-signature, no shared
administrator token) is built, 19a to 19k, 2026-11-09 to 2026-11-15
([ADR-0015](ADR-0015-registry-accounts-under-the-engine.md), Accepted 2026-11-15); it amends defaults 3 and 5 below.
Turning the registry on, its signing key, the Hub's project and DNS stay the owner's, and the
gate's line for them stays pending until they are done. The steps, in the order they landed:
- 18a, the card rendered by the registry and the two groups (checked, claimed);
- 18b, the registry's chain (`hub_events`, `mendari registry verify`) and reviews;
- 18c, yank as a recall and delete (`hub/yank.py`, `mendari yank`);
- 18d, origin at pull and the cap on a pulled agent (`registry_approvals`);
- 18e, a running server honours its registries' recalls (`server/recalls.py`);
- 18f, rate limits and security headers on the registry and the Hub;
- 18g, the regression check on the publisher's own test set (`hub/regression.py`);
- the approval on the Decision Card (#240): where a pulled agent came from, a recall, the
  approval in force and earlier ones, approving through a dialog that names the digest, and
  revoking;
- 18h, the close-out: the engine no longer compares with or monitors against a pulled agent's
  uploaded benchmark (decision 1), the threat model's rows (S18, S19, T7, R3, D11, D12, E12,
  E13), the gate's tests, and the registry's runbook
  ([`docs/operations/registry.md`](../operations/registry.md)).

The defaults below hold unless the owner chooses otherwise.

Amended by [ADR-0026](ADR-0026-one-release-baseline-agents-only.md) (2026-11-25): the scoreboard
decision 1 waited for is removed. A registry's database is made with its chain and its mark
(`mendari-registry/1`), so what decisions 4 and 10 say of rows from before the chain and of
versions published before applies to none.

Terminology updated 2026-11-23 and 2026-11-25: what Mendari governs is called an agent here,
as everywhere since [ADR-0026](ADR-0026-one-release-baseline-agents-only.md).

## Context
The registry (`src/mendari/hub/registry.py`) checks a publish thoroughly: the archive's signature
by one of the namespace's keys, every file against the signed manifest, and that the agent loads
with the signed digest on the engine its pullers run (ADR-0009). What it then shows is another
matter:

- It stores and serves the uploaded `DECISION_CARD.md`, the benchmark's accuracy and the
  leaderboard as the agent carries them. The digest leaves those files out (they are
  attestations, `Agent.attestation`), so the signature only says who uploaded them. A risk-2 agent
  whose card says risk 1 is accepted and its card shown (reproduced in the vision audit).
- The risk level, publisher name and type, summary and licence come from the manifest: the
  publisher's own words, shown beside the registry's checks with nothing to tell them apart.
- There is no way to recall a version (no yank or delete), no rate limit, no security headers,
  and no audit events for its writes.
- A pulled agent sits on the agent search path, and policy caps AUTO only by the risk level the
  agent itself declares.

## Decision
Nothing on an agent's page comes from the uploader unchecked.

1. **The registry renders the card (18a).**
   - `render_card` takes the attestations it shows as explicit inputs (`benchmark` and
     `leaderboard`; `mendari card` passes the agent's own). The registry passes none, so a card
     it renders comes from the digested files only: the manifest, policy, input schema and
     changelog.
   - An uploaded `DECISION_CARD.md`, `evals/benchmark.json` and `evals/leaderboard.json` are
     never stored as shown values. They stay in the signed archive, which a puller verifies byte
     for byte, and nothing reads them for display.
   - Figures the registry computes itself come later (18g), labelled as the publisher's own test
     set. Only the scoreboard (LP-17) ranks; until it exists the registry and the Hub rank
     nothing.
   - The engine treats a pulled agent's uploaded benchmark the same way (18h): its agent detail
     and card show none, its monitor has no benchmark for it (`benchmark.reason:
     from_registry`), and its evaluation route, `mendari eval` and `mendari demo` make no
     comparison with it, saying why (a report given with `--baseline` is the caller's own and is
     compared).
2. **Two groups on every version, in the API and on the Hub (18a).**
   - `checked`, what the registry verified itself: the signing key's id, the archive's SHA-256,
     the agent digest the signed manifest names and the agent loaded with, the engine it was
     checked on, that the card was rendered here (`card: rendered`), and the review status.
   - `claimed`, what the publisher says: the publisher's name and type, the risk level and its
     category (shown as "declared by the publisher"), the licence and the summary.
   - The status label (LP-04) is derived by the code from the manifest's provenance and only
     ever lowers what the agent claims, so it sits with the checks.
3. **Review status (18a, 18b).** A version is `unreviewed` until a registry reviewer records a
   review (18b). Until then its risk level is shown as declared by the publisher, never as
   recomputed or confirmed. A review records the risk level it found and a short note; the
   publisher's declared level stays as declared, with `confirmed` true or false beside it, and
   the newest review of a version is the one shown. A registry reviewer is a person of the
   review organization under the engine, or the operator's command (`mendari registry
   review`): no shared token records reviews since LP-19g (ADR-0015).
4. **The chain (18b).** Every write (a namespace created, its publisher type set, a token
   issued, a key added, a version published, a site report shared, a review) appends a
   hash-chained event in the same transaction (`hub_events`, hashed as the audit chain is). A
   token is named by the start of its SHA-256, never itself. Each event holds the hash of the
   row its write left. `RegistryStore.verify` (`mendari registry verify`, read-only) checks
   each event's hash and order, and that every namespace, key, token, version, report and
   review is exactly the row its newest event hashed (none planted, changed or removed), that
   every archive hashes to its name and is there, and that every key is the key its id names.
   A registry that held rows before it had a chain hashes them once in its first event
   (`registry.chain_started`); it then marks itself (`hub_meta`), so a chain emptied later is
   reported, never started again. The chain is unkeyed: whoever can write the whole database
   can rewrite it whole. Its head (`GET /v1/chain/head`: the event count and the last hash),
   kept elsewhere, is what shows that; a signed head is for later.
5. **Recalls (18c).** A yank is a `mendari.yank/1` record (agent, version, digest, archive,
   reason, severity, signer, key id, time), signed over its canonical JSON. The publisher signs
   with one of the namespace's keys; the registry signs a recall it makes with its own key
   (`MENDARI_REGISTRY_SIGNING_KEY`, published at `GET /v1/registry-key`). The registry stores it
   only if it names exactly that version, its time is within ten minutes of now, and its
   signature holds; a version is recalled once. A recalled version leaves the latest version,
   search and the namespace's list, and is still served by exact version with its record
   (`GET /v1/yanks` lists every recall). Its archive is never deleted by a yank. A client shows
   the record and whether its signature holds with a key it pinned; one that does not verify
   is still a recall, since it only stops automation. An administrator may delete a version
   with a reason: its archive answers 410, its bytes go when no version that is not deleted
   needs them, and its row and a tombstone stay, so its version is never published again.
6. **Origin and the cap (18d).** A pull writes, beside the agent, where it came from
   (`mendari.origin/1`: the registry's URL, namespace, publisher type, key id, archive, digest,
   time, and any recall the registry served then; a marker from before holds the archive's
   SHA-256 alone). Served, an agent from a registry never automates until its organization
   approves its exact digest (`registry_approvals`, `POST`/`DELETE /api/registry-approvals`,
   the `approve` permission, chained `registry_agent.approved` and `.revoked`), at any risk
   level (2 and 3 need their governance approval too), and never while a recall is known
   (the one its last pull recorded; a running server checks its registry, 18e): AUTO becomes
   HUMAN_REVIEW, as under a hold, and the agent's own rule's BLOCK goes to a person too (final at
   once, it would act alone). The cap follows the origin, not the bytes: a pulled copy of a
   bundled agent is capped too, and no field of the agent lifts it. One live approval a digest
   (a second is refused, so revoking stops automation at once); an approval lasts until
   revoked, since it names one digest. `mendari test`, `mendari eval`, `leaderboard --run` and
   the server's evaluation route check the agent's own policy (`origin_cap=False`); a server and
   `mendari decide` never do. A library caller (`Decision.from_hub`) is capped unless it passes
   `origin_approval`.
7. **A running server honours recalls (18e).** For each registry its served agents came from, a
   server reads `GET /v1/yanks` in a thread of its own, over https, started by a decision for
   one of that registry's agents once the last read is five minutes old
   (`MENDARI_REGISTRY_RECALL_CHECK_S`); that decision uses the list read before. An agent it lists
   is held as an unapproved one is. What cannot be checked is treated the same: a registry not
   read in a day (`MENDARI_REGISTRY_RECALL_STALE_S`), not yet read since the server started, or
   not recorded beside the agent. A recall is honoured whether or not its signature can be
   checked here: it only lowers automation.
8. **Limits and headers (18f).** The registry limits reads per address and writes and site
   reports per token, in its own database (the engine's `Limiter` and buckets), refusing before
   a body is read. Every registry answer carries a CSP that loads nothing, and the Hub's pages
   a CSP with a fresh nonce; both refuse framing and send HSTS but on this machine. CI scans
   the Hub with OWASP ZAP's baseline.
9. **The regression check (18g).** On every publish the registry evaluates the version on
   the test set its manifest names, in a child process with no credential, a time limit and
   local providers only, and runs the eval gate against the newest earlier version's check. It
   is shown, labelled "publisher's own test set: regression check, not a ranking", and never
   refuses a version: the set is the publisher's choice, so it can show a regression, never
   rank. An agent whose default providers call a model is not run.
10. **Versions published before.** They keep their stored metadata (versions are immutable),
   but their uploaded card, benchmark and leaderboard are not served: `checked.card` says
   `not_rendered`, and publishing a new version renders one. Re-rendering on read would load an
   agent per request.

## Defaults (the owner may choose otherwise)
1. (18a) The Hub shows no benchmark figure and no leaderboard until the registry computes them:
   an uploaded figure is never displayed, even with a caveat.
2. (18a) The card is rendered with the agent loaded as an agent from a registry (`origin="hub"`), so
   it shows neither a maintained nor a community source line: the registry's checks, not the
   card, say where an agent comes from.
3. (18a) In the registry's card, publisher text is plain text: each string on one line with
   Markdown's punctuation escaped, each name a code span, an evidence link only an http(s)
   address, and only the changelog's version lines as headings. So no heading, table, rule,
   image, link or bold line in it can pass for the registry's own words. The Hub renders the
   card without raw HTML or images, and drops unsafe links.
4. (18a) Where Mendari serves or shows an agent pulled from a registry (the engine's agent detail,
   `mendari card`), it shows the registry's card too, without the agent's uploaded benchmark and
   leaderboard (`card_of`).
5. (18b) Until LP-19's reviewer accounts, a review is recorded with the admin token.
   Amended by LP-19 (ADR-0015): a review is a reviewer's, by id, or the operator's command,
   each named on the chain; the admin token is gone (19g).
6. (18c) A yank is signed by whoever makes it: the publisher with the namespace's key, the
   registry with its own key. A client honours every yank, verified or not (a forged yank only
   stops automation, the safe direction), and says whether it verified.
7. (18d) A pulled copy of a bundled agent is capped too: the cap follows the origin, not the
   bytes. `mendari test` checks the agent's own policy and says that serving caps it.
8. (18e) A server that could not check a pulled agent's registry for 24 hours holds it.
9. (18g) A regression is shown, not refused: the version is the publisher's; an agent that cannot
   be evaluated on its own test set with local providers says so.
10. (18h) The rule on uploaded benchmarks follows the origin too: a pulled copy of a reference
    agent is not monitored against or compared with the benchmark it carries.

## Consequences
- **Breaking, for deployments that serve pulled agents.** An agent pulled from a registry no
  longer automates until its organization approves its digest, at every risk level, and its own
  rule's BLOCK goes to a person until then. A registry a server cannot read for a day holds
  every agent pulled from it, and one pulled before LP-18d (a marker that names no registry) is
  held until it is pulled again.
- A pulled agent is never monitored against, or its evaluations compared with, the benchmark it
  carries; such an agent's monitor runs only the checks that need no benchmark.
- A publisher can no longer show a better benchmark than the registry measured, or a lower risk
  level in its card than the manifest declares.
- The Hub's agent page loses its benchmark fact and provider leaderboard until 18g; the
  regression check then shows the publisher's own test set, labelled as such, and LP-17 the
  scoreboard.
- Clients that read `card_markdown`, `benchmark` or `leaderboard` from the registry see the
  rendered card and `null` for the other two.

## Alternatives considered
- **Show the uploaded figures with a caveat.** Rejected by the build prompt: a caveat does not
  stop a number from being read as checked.
- **Recompute the benchmark at publish from the uploaded dataset.** That is 18g's regression
  check, labelled as the publisher's own test set: a set the publisher chose cannot rank.
- **Re-render older versions on read.** A load per request; publishing a new version is cheap.
