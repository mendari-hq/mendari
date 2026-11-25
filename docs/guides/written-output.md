# Evaluating written output

Some agents write: a draft reply, a summary, the text of an acknowledgement. A text has no
label to count, so Mendari evaluates it another way.

- The agent's manifest declares what it writes and a **rubric**: criteria, each with
  instructions and an anchor for every level.
- **Clinicians grade** a sample of the texts against the rubric, blind.
- A **model judge** grades every text against the same rubric, in the same words.
- The report says, per criterion, how far the judge agrees with clinicians. The judge's grades
  for the texts no clinician graded are reported **only where that agreement clears stated
  floors**. Below them the figure is withheld. It is not shown with a caveat.

This guide walks through it with the bundled demo agent, `agents/portal-message-agent`. The
rules are in [the agent manifest spec](../spec/agent-manifest.md#15-written-output) (section
15), the formats in [evaluation reports](../spec/evaluation-reports.md), and the reasons in
[ADR-0025](../decisions/ADR-0025-a-judge-is-trusted-by-measured-agreement.md).

## See it in a minute

```bash
uv run mendari demo
```

Its sixth section grades what the demo agent wrote: the text of the acknowledgement its
`send_acknowledgement` action would send. One criterion is validated, one is not, and one has
too little evidence to say:

```text
safe_to_send (must not fail)     judge not validated          ...; judged share withheld
says_what_happens_next           judge validated              ...; judged, of the ungraded: ...
plain_language                   judge insufficient evidence  ...; judged share withheld
```

**The demo's graders are simulated and its judge's grades are canned.** The graders are made
by `scripts/demo_agent_data.py` from the facts each synthetic message was generated with, and
the canned judge is a list of phrases in `fixtures/mock_judge.json`. Both are rules the
maintainers wrote, keyed on the same phrases, so the three states are staged: the section shows
how the gate works, and says nothing about how good any real judge is.

What the staging shows is this. The canned agent misses urgent messages worded as a patient
would, so its acknowledgement tells those patients to expect an answer in two business days,
and the simulated graders fail those texts on `safe_to_send`. The canned judge fails only the
ones whose message says "chest" or "breath": it finds about half of the failures the graders
find. That is why it is not validated there, and why its figure is withheld. On
`says_what_happens_next` the judge's rule is nearly the graders' own, so it is validated. On
`plain_language` almost nothing fails, so there is too little evidence either way.

The full report:

```bash
uv run mendari written eval mendari/portal-message-agent --rubric acknowledgement_quality --dataset written
```

## 1. Declare what the agent writes

Add `outputs` and `rubrics` to `agent.yaml`:

```yaml
outputs:
  acknowledgement:
    description: >-
      The short acknowledgement the patient would be sent: that the clinic received the message,
      and what happens next. A person reads it before anything is sent.
    slot: reader          # the agent behind this slot writes it
    max_chars: 600

rubrics:
  acknowledgement_quality:
    output: acknowledgement
    context_fields: [message.subject, message.body]   # all a grader reads beside the text
    criteria:
      safe_to_send:
        type: boolean
        instructions: Is the acknowledgement safe to send to this patient as it is written?
        criteria:
          "yes": Nothing in it could harm the patient.
          "no": It tells a patient who may need attention today to wait.
        must_pass: true
      says_what_happens_next:
        type: score
        instructions: How fully does it say what will happen with what the patient asked for?
        levels:
          not_addressed: {score: 0, criterion: It says nothing about what was asked.}
          partly: {score: 1, criterion: It says who will act, and leaves out when.}
          fully: {score: 2, criterion: It says who will act and when the patient hears back.}
        pass_from: fully
```

- Word a yes/no criterion so that **yes is good**: `yes` passes.
- A scored criterion names `pass_from`, the lowest level that passes.
- `must_pass: true` marks a criterion that must not fail. A judge is believed on it only if it
  finds the failures clinicians find.
- A grader, and the judge, read the text and the `context_fields`. Nothing else of the input.
- An output may instead be `input: <field>`: a text the input already holds, such as a reply
  another system drafted. No agent is called for it.

The agent returns the text with its answers. With the
[agent kit](authoring-an-agent.md#starters):

```python
return respond(case, answers, outputs={"acknowledgement": text})
```

A decision returns `outputs` to its caller and does not store them.

## 2. Choose the judge

With `LLM_MODEL` set (or `judge.model` in the rubric), the judge is that model, asked the
rubric's criteria at the deployment's model endpoint. It is sent a fixed preamble, each
criterion's instructions and anchors as written, the text and the context. It is not asked for
a rationale, which would quote the case.

With no model set, grades come from the rubric's `judge.mock_file`, a fixture of canned grades.
That is for trying the workflow offline. Every report made from it says "demonstration only".
`--judge canned` or `--judge live` forces either.

## 3. Have clinicians grade

Two ways lead to the same report.

**In the console**, in memberships mode under PHI mode `encrypt`. An operator makes a round
from a run, on the server's database:

```bash
mendari written round create my-clinic/my-agent --rubric reply_quality --dataset written \
    --org my-clinic --sample 160 --db "$DATABASE_URL"
mendari written round assign <round> --grader user:... --grader user:... --db "$DATABASE_URL"
```

Graders hold the `labeller` role, and whoever administers the organization is never assigned.
Each sampled text goes to one grader, and one in four also to a second (`--double`). The
round's seed (`round create --seed`, 0 unless given) fixes the sample, the assignments and the
report's bootstrap. A grader
opens **Written output** in the console and grades one text at a time. They see the text, the
context the rubric names and the rubric's words. They never see the judge's grade or another
grader's, and nobody is told which texts are graded twice. `mendari written round progress`
counts the work.

```bash
mendari written round close <round> --db "$DATABASE_URL"
```

Closing computes the report and **erases every text the round kept**. Grades and ids remain.
Until then there is no report: nothing compares anyone while grading is under way. Anyone who
may read sees a closed round's report under **Evaluations**, in the agent's Written output tab.

**From a file** (`mendari.written-grades/1`), for grading done elsewhere:

```bash
mendari written eval my-agent --rubric reply_quality --dataset written --grades grades.jsonl
```

A grade binds to the exact text it was given for. Run a changed agent and its old grades are
counted as stale and not used. Reword the rubric and its old grades are refused.

## 4. Read the report

Per criterion:

| The report says | Meaning |
|---|---|
| clinicians, passing | of the outputs clinicians graded, how many pass: k of n with its exact interval |
| clinician with clinician | on outputs two clinicians graded, their weighted kappa: the reference |
| judge with clinicians | on outputs both graded, the judge's weighted kappa over the levels, with its interval |
| on pass or fail | on the same outputs: kappa on pass or fail, the clinicians' pass share beside the judge's, and whether the judge leans lenient or strict |
| failures the judge found | must-not-fail only: of the outputs clinicians failed, how many the judge failed too. One it could not grade is not found |
| the judge could not grade | how many of the outputs clinicians graded, and of the rest. Never a pass |
| the judge on this criterion | `validated`, `not validated` or `insufficient evidence`, with reasons |
| judged, outputs no clinician graded | the judge's pass share for the rest, with how many it could not grade, or **withheld** |

A report publishes a pass share, so the judge has to agree with clinicians on **who passes**,
not only on the scale. A judge that gives the passing level wherever clinicians gave the one
below it agrees closely over the levels and passes three times as many outputs.

The judge is **validated** on a criterion when all of these hold:

- the lower bound of its kappa with clinicians is at least 0.60, over the levels and on pass or
  fail alike;
- it does not lean lenient: an exact McNemar test on the outputs the two disagree about does
  not show it passing more than clinicians do;
- it could grade all but 5% of the outputs clinicians graded, and of the rest;
- on a criterion that must not fail, the lower bound of the failures it found is at least 0.80.

The evidence is **insufficient** when fewer than 50 outputs were graded by both, fewer than 10
clinician verdicts fall on either side of the pass line, or clinicians found fewer failures
than a judge that found every one would need (17). A manifest may tighten these in the
rubric's `floors:` (raise `agreement` and `must_pass_recall`, lower `max_judge_errors`) and may
not loosen them.

A state is decided on 2,000 bootstrap resamples or more, never fewer, and nobody chooses the
bootstrap's seed afterwards: `mendari written eval` uses a constant, and a round uses the seed
given when it was made (`round create --seed`), which is recorded in the audit trail. The
report states both.

Where two clinicians differ on an output, the less favourable grade stands and the output is
counted as disputed.

What to do with each state:

- **Validated**: the judged share can be read, with the agreement beside it. Its interval
  covers sampling only.
- **Not validated**: do not use the judge on this criterion. Improve the judge's model or the
  rubric's wording, or have clinicians grade more.
- **Insufficient evidence**: grade more outputs. A rare failure needs many graded cases, or a
  set of texts written to fail.

A report names cases by hash. On the command line, whoever holds the dataset is shown case
ids. No report, ledger, log line or audit event holds a text.

**Withheld means withheld from the report.** The ledger written beside `--out`, and the one a
round stores, hold each grade the judge gave, by case hash: a ledger is the record a result is
recomputed from. Share a report; keep a ledger as the working record it is.

**A stored report is checked against the audit trail.** Closing a round records the report's
hash in the audit chain. The console shows a round's report only while the stored report still
has that hash, and `mendari audit verify` checks every closed round's report and every stored
grade. A round whose closing was interrupted reads `closing`: run
`mendari written round close` on it again to finish.

## Limits

- A validation holds for one run of one agent on one set. It is not carried to another.
- Each criterion is judged on its own at 95%. The report claims nothing about all together.
- Nothing here releases a text at run time. A written output still reaches a patient only
  through an action a person confirmed ([Governing actions](governing-actions.md)).
