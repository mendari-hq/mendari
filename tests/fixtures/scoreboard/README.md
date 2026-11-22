# The results store

Mendari's scoreboard results, and what each public set's result is recomputed from (LP-17f,
ADR-0013). This folder is outside the wheel, the sdist and the documentation site. The
[early scoreboard](../docs/grading/scoreboard.md) is generated from it.

| Path | What it holds |
|---|---|
| `results/<sha256>.json` | A result, named by its body's SHA-256: a public set's entry (`mendari.scoreboard/1`), or a hidden set's answer (`mendari.hidden_scoreboard/1`, LP-17k), stored as the grading route answered it, with no ledger or run record. |
| `ledgers/<sha256>.jsonl` | Each result's ledger (`mendari.ledger/1`), named by its id. |
| `runs/<sha256>.json` | Each result's run records (`mendari.run/1`, one per replicate), named by the SHA-256 of its bytes. |
| `prices/<date>.json` | A dated price table (`mendari.prices/1`) a result is priced from: none yet. |
| `canaries/<sha256>.json` | A canary (`mendari.canary/1`, LP-17h), named by its body's SHA-256: none yet. |
| `declared/<pack name>.json` | A pack's declared configurations (`mendari.declared/1`, LP-17h): per arm its remote configuration and the local one a hidden set grades, committed before any remote run or hidden-set grading. |
| `keys.json` | The public grading keys (`mendari.grading-keys/1`): none yet. |
| `disclosures.json` | The disclosures (`mendari.disclosures/1`): no statement yet, so every remote result reads "not published: disclosure pending". |

- Every file is written by `mendari scoreboard run`, `mendari scoreboard canary` or
  `mendari scoreboard add`, or signed by `mendari scoreboard sign`. Never edit one by hand. The
  declarations, the disclosures and the price tables are the owner's, and are written by hand.
- A remote configuration's results (LP-17h) are run by hand, never in CI, in 3 to 5 replicates,
  as their arm's declared configuration. The Clinical Trial Prescreener's `structured=cql` is
  measured so, on Mendari's own CQL engine; no Jev or LLM configuration is.
- The store is append-only. Nothing stored changes, except that the owner may add a valid
  signature to an unsigned result. A result out of date is run again, and the new one names the
  one it supersedes.
- CI checks it on every pull request (`scripts/check_scoreboard.py`, in the `reference-pack` job):
  each name is its content's hash, nothing is removed or changed, and each public result is what
  its ledger, its dataset, its run record and the method give, byte for byte.
- Every result is an engineering evaluation, not clinical validation. A public set's is a
  regression check, not a ranking. A hidden set's answer holds aggregates only, is checked against
  its closed schema, its own figures and, when new, its pack's declaration (it grades the `local`
  configurations of `declared/<pack name>.json`), its k and n never recomputed (no ledger of a
  hidden set is kept), and is not on the early scoreboard's page or the site.

How to run, add, check and sign: [Running the scoreboard](../docs/operations/scoreboard.md).
