# The trust core, to the state of the art

Started 2026-11-27, at the owner's request ("let's make the trust core SOTA, starting with what
you recommended; review the recommendation again to see if there are other things to add").
This note is the reviewed recommendation and the order of work. Each wave has its own decision
record and its own pull request; the plan and status are in `tasks/todo.md`.

## What the trust core is

The machinery that decides when an agent may act on its own, and keeps proving that decision
still holds: a confidence that is checked and tied to a service version (ADR-0024), an
automation threshold with a stated guarantee (`evals/operating_point.py`), a second signal that
only lowers automation, a judge of written output believed only as far as it agrees with
clinicians (ADR-0025), and monitoring after deployment. The harness makes the evidence, the
wrapper enforces it on every decision, and monitoring checks that it stays true.

## Where it stood (read from the code at `c25b64b8`, 2026-11-27)

| Part | What exists | What is missing |
|---|---|---|
| Written output | Clinicians grade blind, a judge grades every text, a per-criterion gate withholds unvalidated figures | Nothing looks at a text at run time: an AUTO decision releases it, and its `write`/`contact` actions, unread |
| Monitoring | Weekly windows against the benchmark with exact intervals, drift, override rate | It re-tests a rolling window every day with no correction; it alerts and nothing else; alerts are not de-duplicated or chained; the daily job skips an organization's store versions |
| Evidence on what is automated | Outcomes and thumbs, when someone gives them | No random check of automated decisions: an AUTO decision cannot even be reviewed. "Reviewed cases are not a sample of what an agent sees" (`evals/feedback.py`) |
| Inside the agent | The answer, proposed actions, usage, the names of its tool calls | No steps: what it looked up, in what order, how long each took |
| Statistics | Exact binomial bounds, exact McNemar, fixed-sequence testing | Every bootstrap is the percentile one (slightly liberal on small samples, ADR-0025); a judged figure is all or nothing |
| Calibration | Fitted on a labelled set, tied to the service version | Nothing learns from reviewed decisions; the reported distribution is not kept, so it could not |
| Real outcomes | Synthetic data everywhere | No figure has been seen to hold on a real site's cases |

## The reviewed recommendation

The first recommendation had four items. Reading the code and the current research added four
more (marked **new**); none was dropped.

1. **new: no free text is released unread.** Found by the review of the judge gate's first
   design (`trust-core-judge-gate-draft.md`): a held text still went out when a reviewer
   confirmed the label, an agent could move its words into an action's arguments, and a
   `contact` action with a free-text message was released with AUTO. So first: a text is
   handed only to a person (an automated decision returns none of what its agent wrote),
   free-text arguments need a person, a held text is kept and shown to its reviewer, and free
   text is released by name. Nothing statistical.
   ADR-0027. *Wave T1, being built.*
2. **A judge clears a text at run time** (was item 2), with **new: a stated guarantee**: the
   share of cleared texts a clinician would fail is bounded (exact, 95%), as the operating
   point bounds false automation. It needs what the review listed: a round the server runs on
   the organization's own connection and on cases like those that would automate; the writer,
   the judge and their settings bound; a design fixed before grades exist; three graders.
   Prior work: learn-then-test and conformal risk control applied to release decisions; the
   clinical work stresses that the unattended release of a failing case is the error that
   matters, and that the count of failures seen limits what can be claimed. *Wave T2.*
3. **new: spot checks.** A random, unpredictable share of cleared texts and of automated
   decisions goes to a person, blind to why, and the verdict is recorded (T1's verdicts are
   the measure). Without it, monitoring is blind exactly where the agent acts alone, and no
   later evidence corrects a guarantee. The research is blunt on this: the share of human
   labels cannot go to zero without losing valid inference, however good the judge. *Wave T2
   for texts, T3 for labels.*
4. **The loop closes** (was item 4). A confirmed alert puts automation on hold by itself; a
   person lifts it. ADR-0010 and ADR-0019 left this as the owner's question, and the owner has
   now answered it. *Wave T3.*
5. **new: monitoring that stays valid when checked every day.** A daily re-test of a rolling
   window has no bound on false alarms over a year, which an automatic hold cannot be built
   on. Confidence sequences (anytime-valid bounds) against the benchmark less the tolerance give
   one: the hold fires wrongly with a stated probability over the whole deployment. *Wave T3.*
6. **The steps inside an agent** (was item 3). The contract takes a trace (steps, tools,
   timings), kept as inputs are, and evaluations and policy rules can read it. *Wave T4.*
7. **new: sharper statistics.** Prediction-powered inference for judged figures (a valid
   interval from many judge grades and a few clinician grades, always, where today the figure
   is withheld or shown); a bias-corrected interval where the percentile bootstrap is liberal;
   adjudication where two clinicians differ. *Wave T5.*
8. **Real outcomes** (was item 1). A design-partner site and clinician labels. The owner's; no
   engineering replaces it. Waves T1 to T4 are what make a site's evidence cheap to collect
   and hard to fool.

Deferred, with the reason: **recalibration from reviewed decisions.** A calibration can raise a
confidence, so "only lowers automation" does not cover it; a refit is a new digest and resets
approvals; and the distribution a service reported is not stored. It becomes a proposal a
person promotes, after T2 keeps what it needs.

## Sources consulted (2026-11-27)

- Prediction-powered inference for model-judge evaluation, and its limits:
  [GLIDE](https://arxiv.org/pdf/2605.31278),
  [bias and uncertainty in judge estimation](https://arxiv.org/pdf/2605.06939),
  [adaptively collected human feedback](https://arxiv.org/pdf/2611.13186),
  [anytime-valid inference from model annotations with human auditing](https://arxiv.org/pdf/2611.15197).
- Anytime-valid monitoring of deployed models:
  [statistically valid post-deployment monitoring for digital health](https://arxiv.org/html/2506.05701v2),
  [WATCH](https://arxiv.org/html/2505.04608v3),
  [anytime-valid calibration monitoring](https://arxiv.org/html/2603.13156),
  [online detection of relevant deviations](https://arxiv.org/abs/2309.15187).
- Risk control for released model output:
  [CARE, a conformal safety layer for medical summarization](https://arxiv.org/html/2606.08969),
  [when conformal risk control can certify model outputs](https://arxiv.org/html/2606.29054v2),
  [release-side risk in conformal triage](https://arxiv.org/pdf/2605.20956),
  [risk-constrained stopping for clinical diagnosis agents](https://arxiv.org/html/2609.09678).

Most are preprints; they were read for the method, not for a figure.

## Wave T1: no free text is released unread (ADR-0027)

Built on branch `trust/t1-written-gate`, test first: three core parts in parallel worktrees,
combined, then the console and the words.

| Part | Files | What |
|---|---|---|
| A1. A text is handed only to a person | `core/decision.py`, the two places an answer carried texts | `DecisionResult.drafts()` and `written`: the draft goes back only with a decision that awaits a person. (The first build sent every decision carrying a text to a person; measured, it stopped the demo agent automating anything, and was replaced.) |
| A2. Free-text arguments | `core/spec.py`, `core/policy.py`, the actions fixture | `ActionSpec.free_text` (already on the branch); `gate_of(free_text=...)`; `action_stops` |
| A3. Kept texts and the review | `core/audit.py`, `core/actions.py`, `server/app.py` (review routes) | `output.held` with the text as its note; the review's verdict on each text, `output.resolved`; free text released by name; retention, erasure and `verify` |
| B1. The console | `dashboard/review.js` and its tests | The held texts in the review, a verdict on each, free-text actions released by name |
| B2. The words | `docs/`, `core/card.py`, `core/packet.py`, `CHANGELOG.md`, `CLAUDE.md` | The spec (sections 13 and 15, the policy's steps), the guides, the threat model, the Card and the packet |

Then one engineer who built none of it reviews the combined change, the whole suite runs on
SQLite and Postgres with the browser suite, and it merges as one pull request.

### Found while the combined change was checked (2026-11-28)

- **A review released an action its reviewer never saw.** `POST …/actions` on a decision that
  waits for review holds the action with the rest, and a label confirmed from a queue read a
  moment earlier released it. The decision's own actions are chained with the decision, so a
  reviewer who sees the decision sees them; one asked for afterwards (`source: request`) is
  now released only by name. This is the same rule as for free text, for the same reason: a
  label speaks only for what its reviewer was shown. What it leaves: a caller that asked for
  an action knows its id and can name it for a reviewer it never showed it to (threat model
  E26). A review that says what it was shown (the queue as of an event) was considered and
  not built: the action's own record already tells the two cases apart.
- **The count beside "Review queue" read the queue itself**, on every view, so under PHI mode
  `encrypt` the chain recorded a read of up to 500 inputs (and now held texts) per page
  change. The read log is only evidence if a read is a read: the count now comes from
  `/api/review/summary`. Predates the wave; `server/origin.py` already named the cost of a
  queue read.
- **A CI job that no local test runs whole can break unseen.** `mendari sandbox pilot-check`
  resolved its review with a label alone, on the demo agent, whose every decision carries a
  text. The local suite passed; the job would have failed on the pull request. The lesson is
  in `tasks/lessons.md`: before a pull request, grep the product for every caller of a route
  or a store method whose contract changed, scripts and sandbox commands included, and run
  the ones CI runs in a container.

### What the independent review found (2026-11-28)

One engineer who built none of it read the whole change at `3fc9c95e` (the brief and the report
are in the thread; the reviewer ran 50 mutations of the source, of which 40 failed a test). On
fresh data, with a manifest of plain or enumerated strings, it found no way for a text or a
free-text argument to leave without a person naming it. What it did find, and what was done:

| # | Finding | Done |
|---|---|---|
| 1 | **Major.** A review that 0.1.2 left pending released free text with no name: `free_text` was chained only when true, so a proposal with no mark read as "no free text" | A declared proposal chains `free_text` either way, and a held one that says nothing is read as one that can: named. Reproduced with a 0.1.2-shaped event |
| 2 | **Major.** "Every string is enumerated" is true of each piece and not of the value: a list of one-letter enums, or a property a character, spells any text; and a number nothing lists (a dose) automated | Arguments are free text unless they are a **menu**: every string an enum or a const, every number listed or an integer between two bounds, no array, at most 1,000,000 arguments in all. Held against the real validator on random schemas |
| 3 | An action asked for after the reviewer read the queue was released unseen | Already closed when the report came (above): `source: request` is released only by name |
| 4 | No test held that the FHIR, SMART, CDS Hooks and `$decide` answers carry no text | `tests/test_no_text_in_fhir_answers.py`: a mark in every text of a copy of the demo agent, searched for in each answer; each of the six places was checked by putting the text into it |
| 5 | A manifest took an output named `reply_draft\n` (`$` matches before a final line break), and the review route refused it | The manifest's keys, id and version are held with `fullmatch` |
| 6 | A BLOCK decision returns and keeps the arguments of what its agent proposed | Said: ADR-0027's consequences and threat model E22. They are the record of what the agent tried; a blocked action is not to be run |
| 7 | Leaving the queue or closing the page inside the five seconds sent a release | It is dropped, the decision still waits, and a reviewer who left the view is told. A label that lets nothing out is still sent on leaving |
| 8 | No test held the escaping of an action's arguments, a kept note or an undeclared name, and the leak tests never read log records | Browser tests for each, checked by taking each escape out; `caplog` at every level in the leak tests, checked by logging a held text |
| 9 | A text kept a right-to-left override, a zero-width space, a tag character: the reviewer reads "Take 10" and releases "01" | Each becomes a space on arrival (texts, rationales, tool names); the console shows any in an action's arguments by its code point |
| 10 | Release mechanics: the version, and a 0.1.2 engine on a 0.2.0 database | 0.2.0 everywhere; the changelog says to stop every 0.1.2 instance first and not to go back once a text is held |

Nits done: an output named `constructor` skipped the console's verdict gate (a plain object as
a map); the Card and the packet say for each action whether its arguments can hold free text;
the answer's schema describes `written` and `outputs`; the ADR's sentence on what leaves, and
E25's "names only".

### What the re-check of the fixes found (2026-11-28)

A second engineer who wrote none of the fixes read the fourteen commits after `3fc9c95e`. No
blocker: no way for a text or a free-text argument to leave unread, and the chain verified in
every run. The fixes for a 0.1.2 decision, the menu rule, an action asked for later, names
matched whole and the console held as described: checked with the real v0.1.2 source on one
database, a fuzz of 103,554 accepted schemas against the real validator (none judged a menu
admitted a value nothing lists), 150 rounds of seven concurrent asks against one confirmation
on SQLite and Postgres, and 84 single mutations of which 70 failed a test. What it found:

| # | Finding | Done |
|---|---|---|
| 1 | **Major, and older than this wave.** One lone surrogate (half an emoji) in a rationale or a tool's name took the review queue and the audit list down for that agent; in a text it failed the decision. Looking further: the same in a decision's *input*, from any caller that may decide | Read as U+FFFD where an input and a provider's answer enter (`core/text.py` `well_formed`), as an agent's patterns already read one. A message is decided, not refused; threat model D19 |
| 2 | "Every character that is not seen" was not true: fillers and the 256 variation selectors stayed, and selectors can carry a byte each | The set is Unicode's `Default_Ignorable_Code_Point` list, but the two joiners and the two emoji selectors; the documents say which set, not "every"; a test holds the console's copy equal to the engine's |
| 3 | The console's Card view did not say whether arguments can hold free text | An agent's detail carries `free_text` per action, and the Card view prints the Decision Card's words |
| 4 | One integer between 1 and a million was "listed values only" | A range is a menu only when an `enum` could have held it: at most a hundred values |
| 5 | Ten mutations survived every test | Tests for each: the count of a range for every way a bound is given (13 mutations now caught), a question's key, markup beside an unseen character, the console's set, the text an evaluation grades, the pilot's viewer check |

Nits done: the count no longer depends on the order of the properties; `mendari new` matches an
id whole; a release is dropped when its page is hidden; a schema that names no field and takes
a text says so on the Card; the words ("enumerated arguments", "a number that is not
enumerated", what a blocked decision returns of its proposals); the ADR names what a menu
still carries (the order of keys, the spelling of a number).

Left, each older than this wave and none a way out for a text:

- **The same `$` slip elsewhere.** These patterns end in `$` and are matched with `match`, so
  each takes a final line break: `_LOCAL_MODEL` (`evals/monitoring.py`), `_DATASET_NAME`
  (`server/app.py`), `_SEGMENT` (`server/hub_routes.py`), `_ID` and `_OAUTH_ERROR`
  (`server/smart_routes.py`), `NAME` (`server/store_routes.py`), `_LINK` (`core/card.py`).
  And a slot's, a variant's, a rule's and a dataset's name in a manifest have no pattern at
  all. One sweep, with a test per name. (`SYSTEM_ID`, `NAME_RE`, `ID_PATTERN`, `_TOKEN_ID` and
  `REQUEST_ID` end in `$` too, and are already matched whole.)
- `POST /api/review/<unknown id>` answers 409, where an unknown decision is a 404 elsewhere.
- From the console's build: `text_status: "sealed"` means two things (this reader may not
  read it; no key opens it), and the 409 for a missing verdict is prose only.
- **A row an older engine wrote with half a character in it** is still not served: the fix is
  where text enters, not where it is read. None is known.
- **What an author and a caller do together** is not closable by a gate that never runs an
  action: a menu still carries a few bits, and so do the order of an object's keys and the
  spelling of a number.
