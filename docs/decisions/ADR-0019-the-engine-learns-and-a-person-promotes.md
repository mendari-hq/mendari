# ADR-0019: The engine learns from review, and a person promotes

## Status
Proposed (2026-11-10). Built on the branch that proposes it. Nothing in it changes what an agent
decides until a person runs `mendari improve --write` on an agent of their own and serves the
result. Merging it is the owner's decision.

Terminology updated 2026-11-23: what this record called a pack is an agent.

## Context
Mendari records what people say about its decisions and never used it:

- A reviewer who resolves a decision chooses a label, and a downstream system can record what
  really happened. Both are kept on the decision. Nothing exported them.
- `mendari feedback export` wrote thumbs-down cases for a person to label and turn into tests.
- Monitoring alerts when reviewers override more or observed accuracy falls. It changes nothing.

So an agent's built-in classifier stays what its author trained it on. Measured on a message
router of the time, whose model was trained on 1,500 synthetic template messages: it sends 99.3% of
its own test to the right team, 93.2% of the local-site example, and 73.9% of the harder draft's
held-out messages (a draft, not clinically reviewed), where 12 of its 197 answers at 0.90 or
more are wrong. Its own card says the benchmark overstates what a site will see.

Two things were measured before anything was built:

- **New labelled cases help.** With 332 of the harder draft's train cases added to the agent's
  training data, the same learner gets 81.2% of the draft's held-out split right (the two splits
  share no family, paraphrase or sentence), 98.5% of the local-site example, and 99.2% of the
  agent's own test.
- **Character features help, and a stronger learner does not.** Counting the runs of 3 to 5
  characters inside each word: 81.0% of the held-out split on the agent's data alone, 85.1% with
  the new cases, and 6 confident mistakes where the shipped model makes 12. A logistic
  regression on the shipped model's features gets 100% of the agent's test and makes 49 confident
  mistakes on the held-out split: fitted closely to the templates, it is surest where it is
  most wrong.

Two rules already stand. Nothing here changes an agent: "improvements stay reviewed and
versioned" (`evals/feedback.py`). And nothing sets an automation hold by itself
([ADR-0010](ADR-0010-operations-for-a-public-service.md)). A decision support system that
rewrote itself from its own reviewers' clicks would break both, and would change what a site
had validated without anyone deciding to.

## Decision
The engine learns, and what it learned is a proposal.

1. **Reviewed decisions are labelled cases.** `mendari feedback export --source reviews` (and
   the admin export route) writes an agent's resolved reviews and recorded outcomes as dataset
   records: the input and the primary question's label, the outcome when one was recorded, else
   the reviewer's. A record names no person and carries no note. Inputs kept as a hash are left
   out; sealed inputs open only for a reader, and each read is chained. `--org` exports one
   organization's. Every record an export writes, of ratings too, says it was exported
   (`attributes.exported_by`): it is a site's case wherever the file goes.
2. **A case is what the classifier reads of it**: its words, in order, in the fields it reads.
   The same message received twice, or with another time stamp, channel or capitals, is one
   case; given with two labels it is left out; one the agent's own data holds is never learned
   from, whatever else its record says.
3. **`mendari improve` judges before it keeps.** It sets 30% of the new cases aside by a hash
   of what the classifier reads of each (so a case held out once is held out always), learns
   from the rest with the agent's own data, and compares each candidate with the model in the
   agent on every dataset the agent declares and on the held-out cases. A candidate that fails
   the eval gate, gets fewer cases right by more than chance, is confidently wrong more often,
   or breaks a behaviour test has regressed. One that has not, and gets more of the held-out
   cases right by more than chance (exact McNemar, p < 0.05), is better.
4. **Two candidates, chosen by evidence.** The model's own features, and the other set: words
   alone, or words and runs of characters. The option lives in the model file, which names a
   format of its own when it has them (`mendari.naive_bayes/2`); the agent format is unchanged.
5. **Nothing is written unless asked, and an agent it writes to stays whole.** `--write` puts the
   model in the agent and the cases it learned from at the end of the agent's own training and
   calibration datasets, so `mendari train` rebuilds the model byte for byte. The three files
   change together or not at all, and a run is kept once.
6. **Only in an agent of one's own.** An agent bundled with the release, a community agent of the
   repository and an agent pulled from a registry are never changed in place, by the directory
   the agent is in, however its path is spelt.
7. **A person promotes.** What `--write` leaves is a changed agent with a new digest: a version
   to bump, tests and an evaluation to run, and an approval wherever a digest is approved.
   Nothing serves it, schedules it or approves it.

## Consequences
- A site can close the loop on its own agent with three commands, and see why a candidate was
  or was not kept. On that message router, with the harder draft's train split standing in for
  reviewed cases: 102 of 145 held-out messages right before, 138 after; of its confident
  answers, 10 of 71 wrong before and 1 of 116 after; and 322 to 354 of the draft's other 436
  messages, which share nothing with what it learned.
- A gain is not enough to be kept. The local-site example, with its true labels: both
  candidates get 98 of 99 held-out cases right where the model in the agent gets 95, which is no
  more than chance; and one of them, on the agent's hand-written emergency messages, would be
  confident about 23 answers where the model in the agent is about 7, and wrong on 4 of them.
  Nothing is kept.
- Mislabelled cases are caught when they cost the agent what it already gets right: the
  local-site example with every label set to one team loses 6 of the agent's 600 test cases (7
  with character features) and gains none, which is within the 3% the eval gate tolerates and
  more than chance, so no candidate is kept. Labels that are wrong in a way the agent's own
  datasets cannot see are not caught: the held-out cases share them.
- The held-out cases come from the same file as the learned ones. They measure messages like
  those, not every message a site will see.
- An agent that learned from a site's cases holds them (each with the id of the decision it was,
  so it can be found again and taken out), and its model holds their words. It is the site's
  data, so it is not published. A site's cases say what they are: every case a run adds says
  a run added it (`attributes.added_by` in its record: `apply` writes none that does not),
  every record an export writes says it was exported (`attributes.exported_by`), and a model
  trained on a case with either mark carries the same in its `training` record, since it holds
  their words. No archive is made of an agent with a file that holds one of those marks:
  `mendari archive build` and `mendari push` refuse it before anything is signed or sent, naming
  the files (the first five) with how many times each is marked and never a case, and a
  registry refuses one it is sent, since a client need not have looked. `mendari
  community-check` fails a community agent that holds one, before it is pushed. Every file an
  archive would hold is searched, as bytes, not only the datasets the agent declares, because an
  archive holds them all. Taking the cases out is half of it: the agent is refused until
  `mendari train` has built the model again without them. There is no way to turn it off:
  whether a site may ever publish such an agent is the owner's question.
- An export is marked (the owner's decision, 2026-11-11): it is the file of a site's cases that
  this loop makes, the one `mendari improve` reads, and the one most likely to be saved beside
  an agent. The cost is the export's use inside an agent: the file saved in an agent's folder, or its
  records copied into the agent's datasets, keeps that agent out of an archive. So a confirmed
  miss becomes a behaviour test a person writes, with no patient's words in it, and the record
  stays at the site.
- Those marks are all that is looked for. The file a run is given need carry none (an export's
  records do, cases a site labelled in a file of its own do not), and a third of its cases are
  only held out, never added, so `mendari improve` reads no cases from the agent's own folder,
  where every file goes into the archive. A site's cases that no mark is on are not
  recognised: the datasets `mendari omop dataset` and `mendari bulk dataset` build from a
  site's records (marking them would change their format: the owner's question), a file of
  cases a site labelled itself, a case retyped into an agent or copied without its `attributes`,
  a copy compressed or in another encoding. And a file that only quotes a mark is refused like
  one that holds a case.
- Reviews label the primary question only. A classifier that answers another question (a
  specialty reader, say) learns from cases labelled for that question: ratings
  that say the right answer, or a site's own labelled data.
- A run is judged with the providers that decide on the machine. A candidate's effect on a
  deployment whose safety screen includes a model is not measured, though that screen and the
  policy's `automation_requires` are unchanged by it. On this machine nothing is automated, so
  the gate's false automation rate measures nothing: the share of confident answers that are
  wrong stands in for it.
- A directory's origin is now told by the file it is, not by how its path is spelt
  (`core.registry.origin_of`). Before, on a volume that ignores case, a pulled or bundled agent
  reached through another spelling of its path was taken for a local one.

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
- **A case identified by its whole input.** Built first, and found in review: the same message
  with another time stamp counted as another case, fell on both sides of the split and was
  judged after it was learned (given three times each, every held-out case came out right), and
  a message of the agent's own test was learned from once its record differed in one field.
- **Declaring the features in `agent.yaml`.** A new key of the `naive_bayes` variant would be a
  spec 0.3 key with its own gating. The model file already records how it was trained and is
  refused by an engine that does not know its format, so the agent format is left alone.
- **Keeping the learned cases outside the agent.** `mendari train` would then rebuild the old
  model and silently undo what was learned.
- **Telling a learned case by its id** (`learned-` and a hash, as a run names it). A naming
  habit, not a statement: an author's own cases may be named so, and a tidied id would hide a
  site's case. The record says it instead, and that alone is read.
- **Leaving an export unmarked.** Built first: only the cases a run added said what they
  were, so an export saved in an agent's folder, or its records copied into the agent's
  evaluation data, went into an archive unrecognised. The owner chose the mark (2026-11-11).
- **A flag to publish such an agent anyway.** Not built. A private registry inside one site may
  be a fair place for it, and that is a decision about a site's data, so it is the owner's.
