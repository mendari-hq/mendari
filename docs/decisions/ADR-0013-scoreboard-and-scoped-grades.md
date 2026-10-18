# ADR-0013: A scoreboard and scoped grades

## Status
Proposed (2026-10-17). Built in steps (LP-17, `tasks/todo.md`, planned in `notes/lp17-plan.md`);
each step adds what it built here, in the order they land:
- 17a, the [grading method 1.0](../grading/method-1.0.md) (`mendari.grading/1.0`, a draft not in
  effect) and its pinned constants (`mendari.evals.method`: the version, the SHA-256 of the
  document's bytes, the effective date left empty for the owner, and the parameters); nothing
  imports them yet.

The defaults below hold unless the owner chooses otherwise.

## Context
Mendari compares a pack's configurations today with leaderboards (`mendari.leaderboard/2`), ranked
by one function (`mendari.rank/1`, `evals/ranking.py`): critical cases automated first, then the
worst critical label's lower bound, false automation, accuracy and cost, on raw 95% bounds, with a
strict name tiebreak.

- Every set it ranks on is public: in the repository and in the wheel. A pack can be tuned to it,
  and a set built from templates cannot be told from a hand-written one (no record carries a
  `template` or `source` attribute).
- Raw counts overstate the evidence. AI Guard's `test` set has 164 `verdict=block` positives but
  30 distinct inputs, and 600 cases but 81 distinct inputs.
- It has no arms by provider class, no simultaneous bound across critical labels, no Brier, no
  bands, no shared ranks and no rank intervals.
- Its 166 shipped rows are mostly on older pack digests, with n of 100 or less and no ledgers.
- Hidden sets (ADR-0011) are graded by the organization's grading key through
  `POST /api/evalsets/{set}/runs`, with local providers only, answering aggregates only
  (`mendari.hidden_result/1`). Nothing there ranks or grades.

The vision audit's A9 and B30 ask for a scoreboard and grading service that cannot be gamed by
selection or by repeated submission, and for the method to be published, versioned, before any
grade is given.

## Decision
1. **The method is written first, and pinned.** `docs/grading/method-1.0.md` holds every rule and
   number before the code that applies them. Every entry, grade and ranking names the method's
   version and the SHA-256 of its bytes. Until the owner sets the effective date (at least 30
   days after the document is published at a public URL, gate `grading-method`), it is a draft,
   and everything made under it says so. Once in effect it does not change: a change is a new
   version, with 30 days' notice. The document is not in the wheel, so the hash is pinned in code
   and a test checks it against the repository's file.
2. **A separate scoreboard format, with `mendari.rank/1` kept as data.** The scoreboard's entries
   are a new format (`mendari.scoreboard/1`, LP-17d), computed from five committed inputs: the
   ledger, the dataset, a run record, a dated price table and the method. `mendari.rank/1`,
   `mendari.eval/2`, `mendari.ledger/1`, `mendari.compare/1` and every committed benchmark and
   ledger stay byte for byte as they are. Changing `rank/1` would make every published Decision
   Card stale.
3. **Grades and ranks on frozen hidden sets only.** A grade is a set of attestations, each met,
   not met or insufficient evidence, never a letter or a composite score. Ranking is by bands
   fixed in the method, with a stated Holm family and rank intervals. Both are given only on a
   frozen hidden set, over configurations declared before grading. Every public set is "public
   set: regression check, not a ranking": unranked and ungraded, with every figure's interval and
   the insufficient-evidence banner (the early scoreboard, stage 3).
4. **Evidence on distinct positives, for each critical label.** A critical label has enough
   evidence when its distinct positives (`norm/v1`) reach the method's `n_min(floor, L)`: never
   fewer than 100, and more where the simultaneous bound needs more. The grade's bound is
   Clopper-Pearson at `1 - 0.05/L`, two-sided, over distinct groups. Figures stay as raw k of n
   at 95%, with the distinct count beside them.
5. **Local providers only on hidden sets, until owner decision 2.** The grading route refuses a
   remote variant before any case is read (LP-11l). A remote arm on a hidden set reads "not
   graded here". With one local arm per pack, ranking is built but off until the owner decides
   otherwise. A remote configuration is measured on public sets only, with 3 to 5 replicates,
   outside CI: the remote arms by the owner, and the Clinical Trial Prescreener's CQL
   configuration (deterministic, but calling Mendari's own CQL engine) by a build session
   (LP-17h).
6. **The declared-configuration route to selection.** For Mendari's own grading, one
   configuration per arm is declared and committed (`scoreboard/declared/`) before grading, and
   each result cites the declaration's SHA-256. Nothing is selected, so the whole set is
   reported. The salted split into a selection part and a ranking part comes with the developer
   flow (LP-17l), computed in the server's process from the set's freeze salt, never from the
   index key or a case hash, with its ranking part scored once.
7. **An offline signing key, domain-separated.** Results are signed with a Mendari grading key
   (`MENDARI_GRADING_SIGNING_KEY`, Ed25519) that differs from every other key Mendari uses. The
   owner holds it offline: it is never a server or CI setting. The signature covers
   `mendari-grading-result-v1`, a zero byte, and the result's canonical JSON without its
   signature, so no checkpoint statement, archive or yank signature verifies as a result. A
   result signed by a publisher's key is refused. The key does not sign freeze commitments: a
   signed hidden-set result carries the commitment. Anyone can verify with public keys only.
8. **The repository is the store.** Results live under `scoreboard/` at the repository root,
   outside the wheel, the sdist and the site, append-only, each named by the SHA-256 of its body,
   each able to name the result it supersedes. A check in the required `reference-pack` job
   recomputes every public result and refuses any change but a valid signature added to an
   unsigned result. The site's scoreboard page is generated from the store.

## Defaults (the owner may choose otherwise)
1. (17a) Every number in the method is the owner's to confirm (owner decision 5): a recall floor
   of 0.95 on the simultaneous lower bound, a false-automation ceiling of 0.02 on the 95% upper
   bound, no critical case automated, the bands, 45 days to stale, 3 to 5 replicates, a canary of
   30 and a split of 0.5.
2. (17a) Bounds at `1 - 0.05/L` are two-sided, as written: conservative (owner decision 6).
3. (17a) Evidence is decided on distinct positives under `norm/v1`, a group detected only when
   every positive case in it is (owner decision 3).
4. (17a) A critical case automated is "not met" whatever the evidence; every other attestation
   is "insufficient evidence" when even a perfect result could not meet it.
5. (17d) The ledger's format does not change: distinct groups, weights, per-type tokens and
   latency come from the dataset and the run record.
6. (17d, 17h) A local configuration's disclosure reads "no model provider". An entry of a remote
   configuration, CQL's included, reads "not published: disclosure pending" until each remote
   provider type it calls has a statement.
7. (17d, 17f) No paired tests on public boards under method 1.0: each entry stands alone with its
   intervals.
8. (17j) The Holm tests set the rank intervals and never reorder the ranks; cost is the band
   tuple's last element. Detection is compared on distinct positive groups, as the grade's bound
   is computed.

## Consequences
- The method is readable, and fixed, before any grade exists. Any edit to it while a draft re-pins
  its hash and makes earlier entries stale.
- Nothing published today changes until LP-17g relabels the public template sets on the card, the
  dashboard and `web/`, a stated display change.
- On today's public sets, almost every table carries the insufficient-evidence banner: only the
  Referral Router's `test` set has 100 distinct positives on every critical label.
- A signed hidden-set result is the owner's attestation: no one else can recompute it, since no
  ledger of a hidden set is kept.

## Alternatives considered
- **Change `mendari.rank/1` into the band ranking.** Rejected: every published card would become
  stale, and a public set would still be ranked.
- **Rank public template sets.** Rejected by the build prompt: they are in the repository, so
  anything can be tuned to them.
- **Split every hidden set for declared configurations.** Rejected: with nothing selected,
  halving the set halves the evidence for nothing.
- **One-sided bounds at `1 - 0.05/L`.** Smaller minimums; left to the owner (owner decision 6).
- **A signing key on the server, and a results table.** Rejected: a key a server holds can sign
  whatever the server is made to compute. Signing is offline, and the repository is the one store.
- **A store under `packs/<pack>/evals/`.** Rejected: that directory is outside the digest but
  inside the wheel and every archive, so a publisher's pack could carry a forged scoreboard file,
  and model ids would ship.
- **Read the method from the installed package.** The documentation is not in the wheel or the
  sdist, so the hash is pinned in code and checked against the repository's file by a test.
