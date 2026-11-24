# ADR-0025: A judge of written output is trusted by its measured agreement with clinicians

## Status
Accepted

Decided by the maintainer on 2026-11-24, before the repository was public, so no comment period ran; open to revision when it is.

Specified in [agent manifest spec 0.4](../spec/agent-manifest-0.4.md), sections 16 to 20, and in
[evaluation reports](../spec/evaluation-reports.md) (`mendari.written/1`). Built after
[ADR-0023](ADR-0023-an-agent-proposes-actions.md) (actions) and
[ADR-0024](ADR-0024-a-checked-confidence-and-a-second-signal.md), on the same spec version.

## Context
Until now every agent answered questions with labels, and an evaluation counted how often a
label was right. Agents also write: a draft reply, a summary, the text of an acknowledgement.
A text has no label to count. Two ways of judging one are in use, and each fails alone.

- **Clinicians grade it against a rubric.** It is the reference, and it does not scale: a
  site can have a few hundred texts graded, not every text of every version.
- **A model judges it.** It scales, and nobody knows what its grades are worth. A judge that
  passes nearly everything looks excellent on a set where nearly everything is fine.

The usual way out is to report the judge's grades with a caveat. A reader of a table does not
read caveats: a number that is shown is a number that is used.

## Decision

1. **One rubric, read by both.** A manifest declares what the agent writes (`outputs`) and how
   it is graded (`rubrics`). A criterion is a yes/no or a scored question, with instructions
   and an anchor for every level. The clinician and the judge read those same words. On a
   yes/no criterion `yes` passes; a scored one names the lowest level that passes.

2. **Clinicians grade blind, and the judge grades everything.** A grader sees the text, the
   input fields the rubric names and the rubric's words: never the judge's grade, another
   grader's or the agent's answers. Some texts go to two graders, and nobody is told which.
   Nothing compares anyone until a grading round closes.

3. **The judge is measured against clinicians, per criterion.** The statistic is Cohen's kappa
   with linear weights on the levels' declared scores, which is plain Cohen's kappa on a
   yes/no criterion, with a percentile-bootstrap interval over cases. Kappa, not raw
   agreement or a prevalence-adjusted coefficient: a judge that always answers "pass" scores 0
   on kappa, whatever the share of passes. Clinician-with-clinician agreement is reported
   beside it as the reference; it gates nothing.

4. **A gate with three states decides whether the judge's grades are reported.** Per criterion:
   *validated* when the lower bound of kappa is at least 0.60 and, for a criterion that must
   not fail, the exact lower bound of the share of clinician-found failures the judge also
   found is at least 0.80; *insufficient evidence* when even a perfect result could not be
   trusted (fewer than 50 outputs graded by both, fewer than 10 clinician verdicts on either
   side of the pass line, a coefficient that cannot be estimated, or, for must-not-fail, fewer
   clinician-found failures than a judge that found every one would need: 17 at 0.80); *not
   validated* otherwise. Floors are held against lower bounds, as the grading method's are, so
   a small sample fails closed.

5. **Below the floor the judge's figure is withheld.** The judge's pass share for the texts no
   clinician graded is in the report only for a validated criterion. Otherwise it is null,
   with the state and its reasons. It is not shown with a warning.

6. **The floors are the engine's.** A manifest may raise the two agreement floors and may not
   lower them, as policy may only lower automation. Every report repeats the four numbers it
   was decided on.

7. **Grades bind to the text and to the rubric's wording.** An item's id is a keyed hash of the
   case, the output's name and the exact text; a grade for another text is stale, counted and
   never used. A grades file names the hash of the rubric as a grader reads it; grades given
   under another wording are refused.

8. **Where two clinicians differ, the less favourable grade stands**, and the case is counted
   as disputed. Adjudication by a third is left for later.

9. **Texts are kept only to be graded.** A decision returns what the agent wrote and does not
   store it. A grading round keeps its sampled texts sealed under the PHI key (refused under
   PHI mode `hash`), and erases them when it closes; grades and ids remain. No report, ledger,
   log line or audit event holds a text, a context or a grade's value.

10. **A canned judge is a demonstration.** With no judge model set, grades come from a fixture
    in the agent's files, and every report made from them says so. The bundled demo agent's
    graders are simulated too, and say so wherever they appear.

11. **A judge on the agent's own model is flagged, not refused.** Models favour their own text,
    and the engine learns the agent's model only when the agent reports one.

## Consequences

- An agent's evaluation can cover what it writes without presenting an unmeasured judge as a
  measurement.
- A rare failure needs many graded cases, or a set of texts written to fail. On 100 graded
  outputs with 5% failing, almost every sample has too few failures to say anything: the
  report says "insufficient evidence", which is true.
- Each criterion is gated on its own at 95%. The report claims nothing about all criteria
  together.
- A validation holds for one run. Carrying a validated judge to another set, or to live
  decisions, is not decided here.
- Three tables are added (`written_rounds`, `written_items`, `written_assignments`) and no
  existing one changes.

## Alternatives considered

- **Quadratic weights.** They rise with the number of levels, and the report cuts the scale at
  one pass line; linear weighted kappa is a weighted average of the kappas at each cut point.
- **Gwet's AC1, or raw agreement.** Under skewed prevalence a judge that always passes scores
  near its raw agreement. The gate must fail closed.
- **Krippendorff's alpha over judge and clinicians together.** It would mix the comparison
  that gates with the one that is only a reference.
- **The hidden-set tables for grading.** They fit hand-written benchmark cases with two
  labellers each and two keys of their own, not model-written texts graded mostly once.
- **Showing a judged figure with a warning below the floor.** Rejected: see the context.
- **A run-time gate that releases a text when a validated judge passes it.** Left for a second
  phase: it needs a stored validation, stricter floors and outputs kept with live decisions.
