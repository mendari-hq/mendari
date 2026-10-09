# ADR-0012: A registry that shows only what it checked

## Status
Proposed (2026-10-09). Built in steps (LP-18, `tasks/todo.md`); each step adds what it built
here, in the order they landed:
- 18a, the card rendered by the registry and the two groups (checked, claimed);
- 18b, the registry's chain (`hub_events`, `mendari registry verify`) and reviews.

The defaults below hold unless the owner chooses otherwise.

## Context
The registry (`src/mendari/hub/registry.py`) checks a publish thoroughly: the archive's signature
by one of the namespace's keys, every file against the signed manifest, and that the pack loads
with the signed digest on the engine its pullers run (ADR-0009). What it then shows is another
matter:

- It stores and serves the uploaded `DECISION_CARD.md`, the benchmark's accuracy and the
  leaderboard as the pack carries them. The digest leaves those files out (they are
  attestations, `Pack.attestation`), so the signature only says who uploaded them. A risk-2 pack
  whose card says risk 1 is accepted and its card shown (reproduced in the vision audit).
- The risk level, publisher name and type, summary and licence come from the manifest: the
  publisher's own words, shown beside the registry's checks with nothing to tell them apart.
- There is no way to recall a version (no yank or delete), no rate limit, no security headers,
  and no audit events for its writes.
- A pulled pack sits on the pack search path, and policy caps AUTO only by the risk level the
  pack itself declares.

## Decision
Nothing on a pack's page comes from the uploader unchecked.

1. **The registry renders the card (18a).**
   - `render_card` takes the attestations it shows as explicit inputs (`benchmark` and
     `leaderboard`; `mendari card` passes the pack's own). The registry passes none, so a card
     it renders comes from the digested files only: the manifest, policy, input schema and
     changelog.
   - An uploaded `DECISION_CARD.md`, `evals/benchmark.json` and `evals/leaderboard.json` are
     never stored as shown values. They stay in the signed archive, which a puller verifies byte
     for byte, and nothing reads them for display.
   - Figures the registry computes itself come later (18g), labelled as the publisher's own test
     set. Only the scoreboard (LP-17) ranks; until it exists the registry and the Hub rank
     nothing.
2. **Two groups on every version, in the API and on the Hub (18a).**
   - `checked`, what the registry verified itself: the signing key's id, the archive's SHA-256,
     the pack digest the signed manifest names and the pack loaded with, the engine it was
     checked on, that the card was rendered here (`card: rendered`), and the review status.
   - `claimed`, what the publisher says: the publisher's name and type, the risk level and its
     category (shown as "declared by the publisher"), the licence and the summary.
   - The status label (LP-04) is derived by the code from the manifest's provenance and only
     ever lowers what the pack claims, so it sits with the checks.
3. **Review status (18a, 18b).** A version is `unreviewed` until a registry reviewer records a
   review (18b). Until then its risk level is shown as declared by the publisher, never as
   recomputed or confirmed. A review records the risk level it found and a short note; the
   publisher's declared level stays as declared, with `confirmed` true or false beside it, and
   the newest review of a version is the one shown. Until LP-19's reviewer accounts the admin
   token records reviews.
4. **The chain (18b).** Every write (a namespace created, its publisher type set, a token
   issued, a key added, a version published, a site report shared, a review) appends a
   hash-chained event in the same transaction (`hub_events`, hashed as the audit chain is). A
   token is named by the start of its SHA-256, never itself. `RegistryStore.verify` (`mendari
   registry verify`, read-only) checks each event's hash and order, and that every row of those
   kinds is named by its event, a version with the digest and archive it was published with,
   so a row written past the API (a planted token or a `verified` namespace, a swapped archive)
   is found. A registry that held rows before it had a chain records them once in its first
   event (`registry.chain_started`).
5. **Versions published before.** They keep their stored metadata (versions are immutable),
   but their uploaded card, benchmark and leaderboard are not served: `checked.card` says
   `not_rendered`, and publishing a new version renders one. Re-rendering on read would load a
   pack per request.

## Defaults (the owner may choose otherwise)
1. (18a) The Hub shows no benchmark figure and no leaderboard until the registry computes them:
   an uploaded figure is never displayed, even with a caveat.
2. (18a) The card is rendered with the pack loaded as a pack from a registry (`origin="hub"`), so
   it shows neither a maintained nor a community source line: the registry's checks, not the
   card, say where a pack comes from.
3. (18a) In the registry's card, publisher text is plain text: each string on one line with
   Markdown's punctuation escaped, each name a code span, an evidence link only an http(s)
   address, and only the changelog's version lines as headings. So no heading, table, rule,
   image, link or bold line in it can pass for the registry's own words. The Hub renders the
   card without raw HTML or images, and drops unsafe links.
4. (18a) Where Mendari serves or shows a pack pulled from a registry (the engine's pack detail,
   `mendari card`), it shows the registry's card too, without the pack's uploaded benchmark and
   leaderboard (`card_of`).

## Consequences
- A publisher can no longer show a better benchmark than the registry measured, or a lower risk
  level in its card than the manifest declares.
- The Hub's pack page loses its benchmark fact and provider leaderboard until 18g; the
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
