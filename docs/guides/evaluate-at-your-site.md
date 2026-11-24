# Evaluate an agent at your site

A health system can measure how a published agent does on its own chart-reviewed cases, inside
its own environment, and share the result with a registry as signed counts
([ADR-0017](../decisions/ADR-0017-a-site-network-of-signed-counts.md)). No case, case id or
dataset name leaves your machines, and nothing is shared that a registry could not check.

This page is for the person who runs the evaluation at the site. The registry's side is in
[Running a registry](../operations/registry.md#sites). The same run, given a target, says which
automation threshold your own cases support
([Choosing an automation threshold](choosing-a-threshold.md#at-a-hospital-run-it-again-on-your-own-cases)),
and what your AI governance committee reads about the agent is
[the committee packet](committee-packet.md).

## What leaves your site, and what stays

What is shared is a `mendari.site-report/2`: whole counts only, counted from your run's ledger
([the format](../spec/evaluation-reports.md#shared-site-reports-mendarisite-report2)). Per
question, how many cases were labelled and right, the confusion matrix and the confidence
deciles; how many cases each outcome had; how many were automated with a wrong
recommendation; each critical label's cases, detected and automated; and the dataset's size.
Cells of 1 to 9 cases are suppressed, with whatever other cells would let them be worked out,
and a run on fewer than 100 cases is not shared at all. Counts of critical cases are shared
exactly, so a missed emergency never disappears under a privacy rule.

What stays: the cases and their ids, the dataset's name and file hash, subgroups, calibration
error, Brier scores, latency and cost, and your keys and secrets.

## Once: make this machine your site's

1. Your organization registers the site with the registry (an administrator in the dashboard,
   or the registry's operator). The registry gives the site an id.
2. On the machine that will run evaluations:

   ```bash
   mendari site init --site 0123456789abcdef
   ```

   This makes the site's private key and a commitment secret in Mendari's configuration
   directory (`~/.config/mendari`, mode 0600), and writes `site-key.json`: the public key and
   its proof that this machine holds the private one. Give that file to the registry. The
   private key and the secret never leave this machine; back them up as you back up other
   credentials. A lost commitment secret cannot be recovered: earlier commitments can then no
   longer be re-proved.
3. A registry reviewer checks, outside Mendari, that your organization is who it says, and
   verifies the site. Until then the site gets no ticket.

A new key, later: `mendari site key --new`, give the new `site-key.json` to the registry, and
revoke the old key there. The old private key is kept beside the new one
(`site_key.<hex>.pem`, the command says where); delete it once the old key is revoked. If a key may have been copied, revoke it as compromised: every
report it signed then leaves the pooled figures.

## Each evaluation: a ticket, the run, the signed report

```bash
mendari login --registry https://registry.example.org   # its address only; no token needed
mendari eval acme/intake-agent@1.2.0 --dataset reviewed-cases.jsonl --share
```

`--share` does three things, in order:

1. **Before the run it asks for a ticket**, signed with your key and bound to the agent's
   digest, the configuration, the dataset's size and a commitment to your dataset (an HMAC of
   the file under your secret: the registry can tell two runs on the same file apart from runs
   on different ones, and nobody can test a guessed dataset against it). A ticket lasts 30
   days. Every ticket with no report stays counted as unreported, for good, and a withdrawn one
   too: a second try at the same data is visible.
2. **It runs the evaluation** on the whole file (`--limit` is refused: the commitment is over
   the file).
3. **It sends the counts, signed**, as the ticket's one answer. The registry checks the
   signature, that your key is live, that the report is the ticket's, its names, and that
   suppression would change nothing.

The configuration is the deterministic arm (every provider decides on your machines) unless
you name variants with `--provider SLOT=VARIANT`. A configuration that calls a provider off
your machines (an LLM, Jev) is shared only with `--consent-remote`, recorded in the ticket and
the report; it is never pooled with a local one.

## Read before sending

```bash
mendari eval acme/intake-agent@1.2.0 --dataset reviewed-cases.jsonl --share-out submission.json
mendari site submit submission.json
```

`--share-out` takes the ticket and runs as `--share` does, then writes the signed submission
and prints a summary of its counts, and sends nothing. Read the file; `mendari site submit`
sends those exact bytes, from any machine, within the ticket's 30 days.

## With no network where the cases are

```bash
# where the cases are:
mendari site ticket acme/intake-agent@1.2.0 --dataset reviewed-cases.jsonl --out request.json
# on a machine that reaches the registry:
mendari site ticket --request request.json --out ticket.json
# back where the cases are:
mendari eval acme/intake-agent@1.2.0 --dataset reviewed-cases.jsonl --ticket ticket.json \
  --share-out submission.json
# then from any machine that reaches the registry:
mendari site submit submission.json --ticket ticket.json
```

`ticket.json` holds the ticket's one-time secret: treat it as a credential. A registry's
operator can also take a submission carried by hand (`mendari registry accept-report`); it
passes the same checks.

## Correcting a report

A ticket takes one report, once. If you find an error, ask for a new ticket that names the
report it corrects (`--supersedes REPORT_ID` on `mendari eval --share` or `mendari site
ticket`). Both reports stay listed: the old one marked superseded, and out of the pooled
figures.

## What a signed report does not show

A ticket binds a dataset before the run; it cannot show how the cases were chosen before you
asked for it. The registry shows your site's figures beside other sites', apart from any
hidden-set result or rank, and says so.
