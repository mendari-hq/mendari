# ADR-0019: The engine learns from review, and a person promotes

## Status
Proposed (2026-11-10). Built on the branch that proposes it. Nothing in it changes what a pack
decides until a person runs `mendari improve --write` on a pack of their own and serves the
result. Merging it is the owner's decision.

## Context
Mendari records what people say about its decisions and never used it:

- A reviewer who resolves a decision chooses a label, and a downstream system can record what
  really happened. Both are kept on the decision. Nothing exported them.
- `mendari feedback export` wrote thumbs-down cases for a person to label and turn into tests.
- Monitoring alerts when reviewers override more or observed accuracy falls. It changes nothing.

So a pack's built-in classifier stays what its author trained it on. Measured on the Patient
Inbox Router, whose model was trained on 1,500 synthetic template messages: it sends 99.3% of
its own test to the right team, 93.2% of the local-site example, and 73.9% of the harder draft's
held-out messages (a draft, not clinically reviewed), where 12 of its 197 answers at 0.90 or
more are wrong. Its own card says the benchmark overstates what a site will see.

Two things were measured before anything was built:

- **New labelled cases help.** With 332 of the harder draft's train cases added to the pack's
  training data, the same learner gets 81.2% of the draft's held-out split right (the two splits
  share no family, paraphrase or sentence), 98.5% of the local-site example, and 99.2% of the
  pack's own test.
- **Character features help, and a stronger learner does not.** Counting the runs of 3 to 5
  characters inside each word: 81.0% of the held-out split on the pack's data alone, 85.1% with
  the new cases, and 6 confident mistakes where the shipped model makes 12. A logistic
  regression on the same features gets 100% of the pack's test and makes 49 confident mistakes
  on the held-out split: fitted closely to the templates, it is surest where it is most wrong.

Two rules already stand. Nothing here changes a pack: "improvements stay reviewed and
versioned" (`evals/feedback.py`). And nothing sets an automation hold by itself
([ADR-0010](ADR-0010-operations-for-a-public-service.md)). A decision support system that
rewrote itself from its own reviewers' clicks would break both, and would change what a site
had validated without anyone deciding to.

## Decision
The engine learns, and what it learned is a proposal.

1. **Reviewed decisions are labelled cases.** `mendari feedback export --source reviews` (and
   the admin export route) writes a pack's resolved reviews and recorded outcomes as dataset
   records: the input and the primary question's label, the outcome when one was recorded, else
   the reviewer's. A record names no person and carries no note. Inputs kept as a hash are left
   out; sealed inputs open only for a reader, and each read is chained.
2. **`mendari improve` judges before it keeps.** It sets 30% of the new cases aside by a hash
   of each case's own input (so a case held out once is held out always), learns from the rest
   with the pack's own data, and compares each candidate with the model in the pack on every
   dataset the pack declares and on the held-out cases. A candidate that fails the eval gate,
   gets fewer cases right by more than chance, is confidently wrong more often, or breaks a
   behaviour test has regressed. One that has not, and gets more of the held-out cases right by
   more than chance (exact McNemar, p < 0.05), is better.
3. **Two candidates, chosen by evidence.** The model's own features, and the other set: words
   alone, or words and runs of characters. The option lives in the model file, which names a
   format of its own when it has them (`mendari.naive_bayes/2`); the pack format is unchanged.
4. **Nothing is written unless asked, and a pack it writes to stays whole.** `--write` puts the
   model in the pack and the cases it learned from at the end of the pack's own training and
   calibration datasets, so `mendari train` rebuilds the model byte for byte.
5. **A person promotes.** What `--write` leaves is a changed pack with a new digest: a version
   to bump, tests and an evaluation to run, and an approval wherever a digest is approved.
   Nothing serves it, schedules it or approves it.

## Consequences
- A site can close the loop on its own pack with three commands, and see why a candidate was
  or was not kept. On the inbox router, with the harder draft's train split standing in for
  reviewed cases: 90 of 133 held-out messages right before, 124 after, confident mistakes from
  13 to 3, and 322 to 369 of the draft's other split, which shares nothing with what it learned.
- A gain is not enough to be kept. The local-site example, with its true labels: one candidate
  gets all 105 held-out cases right where the model in the pack gets 98, and is refused,
  because on the pack's hand-written emergency messages it would be confident about 23 where
  the model in the pack is about 7, and wrong on 4 of them. The other candidate's gain (7
  gained, 2 lost) is no more than chance. Nothing is kept.
- Mislabelled cases are caught when they cost the pack what it already gets right: the
  local-site example with every label set to one team loses 7 of the pack's 600 test cases
  (9 with character features) and gains none, which is within the 3% the eval gate tolerates
  and more than chance, so no candidate is kept. Labels that are wrong in a way the pack's own
  datasets cannot see are not caught: the held-out cases share them.
- The held-out cases come from the same file as the learned ones. They measure messages like
  those, not every message a site will see.
- A pack that learned from a site's cases holds them, and its model holds their words. It is
  the site's data: nothing stops `mendari push` from publishing such a pack yet.
- Reviews label the primary question only. A classifier that answers another question (the
  Referral Router's specialty reader) learns from cases labelled for that question: ratings
  that say the right answer, or a site's own labelled data.
- A run is judged with the providers that decide on the machine. A candidate's effect on a
  deployment whose safety screen includes a model is not measured, though that screen and the
  policy's `automation_requires` are unchanged by it.

## Alternatives Considered
- **Retrain and serve automatically.** Rejected: it changes what a site validated without a
  decision, moves automation without an approval, and makes a decision's digest stop naming
  what decided it.
- **An automatic hold or tighter thresholds when monitoring alerts.** It only lowers
  automation, which the policy allows, but ADR-0010 chose that nothing holds automation by
  itself. Left as the owner's question.
- **A stronger learner** (logistic regression, the same features). Measured and rejected above.
- **A cap on automation when few of a message's words are known to the model.** Measured: it
  held back most unfamiliar messages but did not tell right answers from wrong ones (on the
  draft's train split, 21 of the 67 confident answers it would still automate were wrong).
- **Declaring the features in `pack.yaml`.** A new key of the `naive_bayes` variant would be a
  spec 0.3 key with its own gating. The model file already records how it was trained and is
  refused by an engine that does not know its format, so the pack format is left alone.
- **Keeping the learned cases outside the pack.** `mendari train` would then rebuild the old
  model and silently undo what was learned.
