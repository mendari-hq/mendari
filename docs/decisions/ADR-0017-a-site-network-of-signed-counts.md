# ADR-0017: A site network of signed counts

## Status
Proposed (2026-11-10, LP-22a). Nothing in it is built yet. It is built in steps planned in
`notes/lp22-plan.md` (22a to 22j), most of them after
[ADR-0015](ADR-0015-registry-accounts-under-the-engine.md)'s steps, which give the registry its
organizations and reviewers. The number 0016 is left for the pack store's decision record, which
is being written at the same time.

## Context
A health system can evaluate a pack on its own chart-reviewed data and share the result with a
registry ([ADR-0012](ADR-0012-a-registry-that-shows-only-what-it-checked.md)). Since LP-07 what
it shares is a closed schema with no case in it. What the registry receives is still not
evidence:
- A site is a name typed on the command line. Any publisher token can share a report under any
  site name.
- Nothing is signed, and the same report can be sent again.
- The report carries rates the site computed, which the registry repeats.
- Only small calibration bins are suppressed. Other small cells go as they are.
- A site can run an evaluation many times and share the one it likes.
- Nothing pools sites' results or says when sites disagree.

So the registry serves every site report marked unverified, and the Hub shows none.

## Decision
1. **A site is a record in the registry**, owned by an organization and verified by a registry
   reviewer. Until it is verified it gets no ticket and shares nothing. A registry with no
   sign-in registers and verifies sites with the operator's commands, each with a reason on the
   chain.
2. **A site signs with its own Ed25519 keys.** It can hold several, add one and revoke one. A
   site key is never a publisher's key. A revocation says whether the key is thought
   compromised; if so, every report it signed leaves the pooled figures and stays listed.
3. **Counts in, rates out.** The shared payload (`mendari.site-report/2`) holds whole counts
   only. The registry computes every rate and interval it shows. Figures that are not counts
   (calibration error, Brier, latency, cost) stay with the site.
4. **One suppression function, a written rule per field, run on both sides.** A run under a
   minimum size is not shared. Cells that describe people are suppressed when small, with
   complementary suppression so no cell can be recovered from the totals. Counts of critical
   cases are shared exactly, by a stated rule, so a missed critical case is never hidden.
5. **A ticket before the run.** The site asks for a ticket bound to the site, the pack digest,
   the configuration, the dataset's size and a keyed commitment to the dataset: an HMAC under a
   secret that never leaves the site. A plain dataset hash is never sent. A ticket with no
   result is shown as unreported.
6. **One live result** per site, digest, configuration and commitment. A correction needs a new
   ticket that names the report it supersedes; both stay listed.
7. **A submission is signed and checked on receipt**: canonical JSON, a key id, a nonce, and
   formats of its own, so no other Mendari signature passes as a site's. A forged signature, an
   unregistered or revoked key, a signature over another body, a replay, and a digest that is
   not a published, listed version's are each refused. Publisher tokens no longer share reports.
8. **Deterministic by default.** A configuration that uses any provider type outside the local
   set needs the site's consent, recorded in the ticket and in the payload.
9. **Aggregation is per pack digest and configuration, never across them.** Each site's k of n
   with its exact interval comes first; then a pooled fixed-effect figure from summed counts,
   labelled as such; a random-effects estimate beside it; the range; the number of sites and
   total cases; and a homogeneity warning from an exact or seeded permutation test when any
   expected count is under 5, chi-square otherwise, at 0.05.
10. **Site evidence is shown apart.** The Hub shows it in its own section, as a forest plot
    with a table, with the site count and total cases. It never enters a hidden-set result or
    rank ([ADR-0013](ADR-0013-scoreboard-and-scoped-grades.md)).
11. **A site's own organization is not an independent site for its own pack.** Reports from the
    organization that holds the pack's namespace are listed, labelled, and left out of the pool.
12. **Review before sending, and a path with no network.** The signed submission can be written
    to a file to read first, and a ticket request and a submission can each be carried as a
    file and accepted through the same checks.

## Consequences
- A served site report names a verified site, the key that signed it and the ticket it
  answered, and anyone can check its signature.
- Selective reporting leaves a trace: unreported tickets and superseded reports are counted
  beside the figures. How a site drew its cases before it asked for a ticket is still not
  shown by anything here.
- The command line changes twice for sites: the payload's format, then a registered site and a
  ticket in place of a free name. A registry runs under the engine in production since
  2026-11-15, holding only published packs (no site report when LP-22 was built): each change
  reaches it when production redeploys, and is the owner's to approve.
- Unsigned reports from before stay stored and are no longer served.
- The registry gains statistics of its own (pooling, a random-effects estimate, a homogeneity
  test), each checked against scipy or statsmodels in tests; the product still needs neither.
- Whether a deployment may automate is unchanged. A signed report is something the owner's
  gate attestation can cite; the engine does not ask for one.
- Agreements with sites stay outside the software: they are the owner's.

## Alternatives Considered
- **Keep publisher tokens for sites.** A token belongs to a namespace and a site has none, and
  a bearer secret cannot show later which site said what.
- **A plain dataset hash.** A small or guessable dataset could be recognised from it, and two
  sites holding the same file would be linked.
- **Accept rates and round them.** A rate cannot be pooled or given an exact interval without
  its counts, and digits of a rate can say something about one case.
- **Suppress safety counts like any other cell.** It would hide exactly the cases the network
  exists to count.
- **No tickets, only signatures.** A signature shows who sent a result, not that it was the
  only run.
- **Pool across versions of a pack.** Two digests are two packs as far as evidence goes.
- **Chi-square for every comparison of sites.** It is wrong when expected counts are small,
  which is the usual case for critical labels.
- **Site evidence in the rank.** Sites choose their own cases; hidden sets are frozen and
  blind. Mixing them would let unblinded data move a rank.
