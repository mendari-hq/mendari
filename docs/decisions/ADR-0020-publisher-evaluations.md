# ADR-0020: Publisher evaluations, signed and checked

## Status
Accepted (2026-11-22, the owner's go for the north star in `tasks/todo.md`: "all evals are
published, including how they work best").

## Context
The Agent Hub shows only what the registry checked
([ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md)): an uploaded benchmark is
never shown, and the registry's own regression check runs only an agent's default, local
configuration. Sites share signed counts of their own evaluations
([ADR-0017](ADR-0017-a-site-network-of-signed-counts.md)). What a hospital most wants before
adopting an agent is missing: **which configurations of it work best** (the canned or rules
variant against a model-backed one, one model against another), measured on the same cases.
The publisher is the one who can run every configuration, including the ones that call a model
the registry will never call.

## Decision
A publisher may publish evaluations of a listed version, one per configuration and public
dataset, and the Hub shows them side by side. Nothing in one is shown unchecked:

1. **Counts in, rates out.** The payload is `mendari.publisher-evaluation/1`: the
   configuration (a variant for every slot), the dataset by its name in the archive and its
   SHA-256, and the evaluation as `mendari.site-report/2` counts (`evals/share.py`
   `counted_report`): no rate, no case. The registry computes every rate and interval it shows.
   Latency and cost are not counts: they are shown as the publisher's own figures, labelled so.
2. **Signed by one of the namespace's keys** (the publisher keys of `mendari keys register`),
   over the canonical JSON of the payload with its own format string, so no other Mendari
   signature passes as one; sent with the namespace's publisher token. A revoked key's
   evaluations leave the comparison and stay listed.
3. **Bound to the archive.** The version must be listed (not recalled or deleted); the
   evaluation's agent digest must be the version's; the dataset must be one the manifest
   declares, inside the signed archive, with the SHA-256 given; every slot and variant must be
   the manifest's. Anything else is refused, naming the field, never echoing a value.
4. **Re-run where the registry can.** A configuration whose every provider is local is re-run
   by the registry in the isolated child the regression check uses, and its counts must match
   exactly, or the evaluation is refused. A configuration that calls a model (an agent, Jev, an
   LLM) is not re-run: the Hub says so on its row ("signed by the publisher; not re-run: calls
   a model") and names the models the publisher reported.
5. **One live evaluation** per version, configuration and dataset. Publishing again supersedes
   it; both stay in the chain (`evaluation.published`, `evaluation.superseded`).
6. **Shown apart, never ranked.** The agent's page gets a section, "Published evaluations",
   apart from the registry's checks and from site evidence: one row per configuration, each
   figure k of n with its exact 95% interval, the critical labels' detection and automated
   misses first, then accuracy and false automation. No row is marked best; the section says
   the set is the publisher's own and public, so a configuration can be tuned to it.

## Consequences
- A hospital sees, before pulling, what each configuration of an agent did on the same cases,
  which rows the registry reproduced, and which rest on the publisher's signature alone.
- A publisher can no longer show a flattering number the registry could have checked and did
  not: local configurations are reproduced or refused.
- The model-backed rows are still the publisher's word. Site evidence (ADR-0017) and hidden
  sets (ADR-0011) remain the stronger evidence, and the page says so.

## Alternatives considered
- **Show the uploaded `evals/benchmark.json`**: refused by ADR-0012; nothing binds it to a run.
- **Have the registry call the models**: the registry would hold model credentials and pay for
  every publisher's runs; refused.
- **Treat the publisher as a site**: a site is a hospital with its own cases; mixing the two
  would blur the strongest evidence the Hub shows.
