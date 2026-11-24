# ADR-0020: Publisher evaluations, signed and checked

## Status
Accepted (2026-11-22, the owner's go for the north star in `tasks/todo.md`: "all evals are
published, including how they work best"). Amended 2026-11-24, below: the evaluation page may
say which configuration did best, only when one clearly did.

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

## Amendment (2026-11-24): the evaluation page says which configuration did best only when one clearly did
Point 6 stands as written for the section on the agent's page, "Published evaluations": one row
per configuration, and no row marked. Since then the Hub has an evaluation page of its own
(`/agents/<namespace>/<name>/evaluation`, built from the registry's
`mendari.agent-evaluation/1`, `hub/evaluation_page.py`). It lists a dataset's configurations in
the order of `mendari.rank/1`, which is total: some configuration is always listed first. As
first built, the page named that one as having done best whenever the first two could be told
apart. That says more than point 6 allows, and it is amended as follows.

**What the page may say, and exactly when.** For the configurations of one version on one
dataset, the registry works out the *first band*: the configuration listed first, with every
other one the counts cannot tell from it. That is one that did the same on all four measures
of the order, or one whose difference from the first, on the measure where the two first
differ, lies inside their exact 95% intervals (they overlap, or one is of no cases). The page
says that a configuration "did best on this dataset" **only when it is alone in that band**: it
is listed first, and every other configuration is behind it by more than chance explains. In
every other case no configuration is named, and the page says so in plain words:

- several in the first band: "No configuration is clearly best on this dataset", then which is
  listed first and which ones it is not clearly ahead of;
- a tie: the configurations that did the same on every measure are named together;
- one configuration alone: there is nothing to compare it with;
- nothing ranks (no critical label has 100 positive cases, or the agent names none): no band,
  and no configuration named.

The rule is worked out in one place, the registry's answer (`comparison.first_band` and
`comparison.best`), on every read, from the signed counts alone. The Hub prints what the answer
says and decides nothing; for a registry that does not say, it names none.

**Why this keeps to the decision's reason.** Point 6 marks no row best because the set is the
publisher's own and public, so a configuration can be tuned to it: a mark would be read as a
recommendation the data cannot carry. What the page says now is a statement about these counts
and not about the configuration: "did best on this dataset", never "is best", beside the same
notice that the dataset is the publisher's and can be tuned to, and with what even that
configuration let through when it automated critical cases. It is said only where the counts
leave no other reading, and withheld wherever chance could explain the order, which is where a
mark would mislead most. It compares the configurations of one agent only: no agent is ranked
against another, the badge names no configuration (it gives the fewest and the most critical
cases automated), and the section on the agent's page still marks no row.

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
