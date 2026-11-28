# ADR-0027: No free text is released unread

## Status
Accepted

Decided by the maintainer on 2026-11-27. It is the first half of the second phase
[ADR-0025](ADR-0025-a-judge-is-trusted-by-measured-agreement.md) left open, and it tightens
[ADR-0023](ADR-0023-an-agent-proposes-actions.md)'s action gate. The same spec version: no
manifest key is added. Specified in [the agent manifest spec](../spec/agent-manifest.md),
sections 13 and 15.

## Context
An agent produces free text in two places: what it writes (`outputs`, ADR-0025) and the
arguments of the actions it proposes (ADR-0023). Before this decision both could leave with no
person and no check between the agent and a patient or a chart:

- **A written output rode on any decision.** A decision returned what the agent wrote under
  every outcome, and an AUTO decision released its `write` and `contact` actions with it.
  Nothing looked at the text, and nothing stored it for anyone to look at.
- **A free-text argument was released with AUTO.** A `contact` action whose argument is the
  message itself, or a `write` action whose argument is a note for the chart, needed only an
  AUTO decision. An agent that declared no `outputs` at all could send its words this way.
- **A hold was not a reading.** A decision sent to a person was resolved by confirming its
  label, and that released every held action unless the reviewer named one to deny. The
  written text was not on the reviewer's screen, since it was kept nowhere. And an action a
  caller asked for after the reviewer had read the queue was released with the rest, by a
  reviewer who never saw it.

The only protection was an author's own `allow_auto: false`. An author who left it out
automated unread text, and a committee had to notice.

ADR-0025 foresaw a judge that clears a text at run time. A first design of that gate was
reviewed before anything was built, and the review found it unsound while these three things
stand: a text a judge held would still go out on a label confirmation, an agent could move its
words into an action's arguments, and a grading round did not measure the service, the
settings or the cases a live decision runs on. So the order is: first no free text leaves
unread, with nothing statistical in it; then a judge may clear some of it (the next decision
record), on the review this one builds.

## Decision

1. **A text is handed only to a person.** A decision's answer carries what its agent wrote
   (an output with a `slot` source) only when the decision awaits a person, HUMAN_REVIEW or
   ABSTAIN: it is that person's draft. An automated decision and a blocked one return none of
   it and keep none of it. Nobody would read it before it was used, so it is handed to nobody.

2. **The decision itself does not change.** Carrying a text does not lower an outcome: a
   label, and an action of enumerated arguments, automate as they did, and the policy's steps
   are as they were. It holds wherever a decision is served, with or without sign-in. The
   draft is carried by `POST .../decide` and by `mendari decide`. The FHIR decide route, a
   SMART launch's decide and `mendari decide --fhir` say where each text stands (`written`) and
   carry no text; a CDS Hooks card, `$decide` and `mendari mcp` carry neither. An evaluation is
   not affected: it grades every text the agent wrote, and hands none on.

3. **Free text in an action's arguments needs a person too.** An action whose arguments can
   hold free text is never released without a person when its gate would be `auto`: a `write`
   or a `contact` action, and a `read` or `draft` one of an agent nobody vouched for. Its gate
   is `person`. Arguments can hold free text unless they are a menu, a fixed set of values the
   manifest lists: every string an `enum` or a `const`, every number listed or an integer in a
   range no longer than a list may be (a hundred values), no list, and at most a million
   different arguments in all. An action with no schema at all is free text. The closed set of keywords an arguments schema may use
   (section 13.3) makes that decidable from the manifest alone. Proposing such an action turns
   AUTO into HUMAN_REVIEW, as any action never released without a person does. This is a
   fourth way the action gate tightens, beside `allow_auto: false`, an agent nobody vouched for
   and a second signal that did not agree; nothing loosens one.

   The first rule asked only that every string be enumerated, and the review of this change
   showed what that lets through: a list of one-letter values, or a property for each
   character, spells any text out of enumerated pieces, and a number nothing lists is a dose.
   A menu is judged by the values it admits, not piece by piece. A second review found that
   one integer between 1 and a million was still "listed values": a range is a list only when
   an `enum` could have held it.

4. **A held text is kept for the person who reads it.** When a decision that carries texts is
   recorded and awaits review, each text is kept as an action's arguments are: apart from the
   record, as the note of a chained event (`output.held`), sealed under PHI mode `encrypt`,
   and only where the decision's input is kept (never under PHI mode `hash` or `store_input:
   hash`). The event holds the output's name and a salted commitment, never the text.
   Retention and erasure remove it with the input. The review queue shows it to whoever may
   read inputs, and that reading is chained as reading the input is. A held text stays a
   note after its verdict, released or withheld, until retention or erasure removes it. What
   is kept, and returned, is the text with each character Unicode says is not shown by
   default made a space, as a control character is (but the two joiners and the two emoji
   selectors a script or an emoji needs; the set is in the spec, section 15.2): with a
   right-to-left override in it, a reviewer would read one thing and release another, and
   variation selectors can carry words no eye sees. This
   replaces ADR-0025's "a decision returns what the agent wrote and does not store it" for a
   held text only: the text of an automated or a blocked decision is still kept nowhere.

5. **Free text is released by name, and so is what a reviewer may not have been shown.**
   Resolving a review:
   - gives each written text a verdict, `release` or `withhold`, chained as `output.resolved`
     (the output's name, the verdict, the reviewer). A review that leaves one out is refused;
   - releases a held action whose arguments can hold free text only when it names it
     (`release_actions`). Confirming the label no longer releases it;
   - releases a held action a caller asked for after the decision was made only when it names
     it, whatever its arguments. Its reviewer may have read the queue before it was asked
     for, so a label alone does not speak for it: the review has to name it, and a review
     screen names what it showed. Found while this was checked before release: a label
     confirmed from a queue read a moment earlier released an action asked for in between;
   - when a text is withheld, releases no held action that needs an automated decision or a
     person unless it names it: a manifest does not say which action uses which text.

   A held action the agent proposed with its answer, with no free text, on a decision none of
   whose texts is withheld, is resolved as before: released when the reviewer confirms the
   label, unless named in `deny_actions`.
   A review that overrides the recommendation denies every held action, named or not, as
   before; a text's verdict does not depend on the label. Where the text is not kept (point
   4), the verdict is still required: the reviewer read the draft where the caller shows it,
   and withholds one nobody was shown.

6. **The record says where each text stands, and no more.** A decision whose agent wrote
   texts gains `written` in its record and its answer: each output's `state`, `held` (it
   awaits a person, who is handed it) or `not_released` (the decision automated or was
   blocked: it was handed to nobody). A record is written once, so it keeps saying `held`:
   the verdict, `released` or `withheld`, is read from the chain, in the `outputs` of the
   decision as the audit trail answers it and in the review's own answer. No text is in a
   record, a reason, a log line or an event.

## Consequences

- No free text an agent produced is released to a patient or a chart through a decision
  unless a person released it by name. An author no longer chooses whether text is read. Two
  things an answer still carries are for a person and are not released: a `draft` action's
  arguments, which staff read and send themselves, and a rationale (below).
- **A proposal's arguments go back with its disposition, whatever the decision.** The answer
  of a blocked decision returns no written text and keeps none, and it still lists what the
  agent asked to do, arguments included, each `blocked`, and keeps them as each proposal's
  note where the input is kept. They are the record of what an agent tried, which is what an
  audit asks of a blocked decision, and a blocked action is not to be run.
- **An automated decision no longer comes with its text,** and `write` and `contact` actions
  with a free-text argument stop being released with AUTO. A caller that sent what an
  automated decision's agent wrote has nothing to send until a judge can clear a text (the
  next record): the way to send a text is an action a person releases, as the bundled demo
  agent's `send_acknowledgement` is. That agent's decisions are unchanged: it automates its
  routing as before, and its actions take enumerated arguments. What changes is that the
  acknowledgement it drafts with every message is returned only with the decisions a person
  reviews.
- **An identifier that is not enumerated is free text.** An arguments schema has no pattern
  keyword, so a `write` action that takes an id as a string is a person's. A spec revision may
  add an identifier type; none can loosen the rule.
- **So is a number nothing lists, and any list.** A `write` that takes a dose as a number is a
  person's; one that takes a number of days between 1 and 90 is a menu of ninety values, and
  one between 1 and 365 is not: a range counts when a list could have held it, a hundred
  values. An author who wants several of a set of options declares a yes or no for each, not
  a list.
- **More is kept.** Held texts are kept until retention or erasure removes them, sealed as
  inputs are. A deployment in PHI mode `hash` keeps none, and its reviewers give their verdict
  on a draft Mendari does not show.
- **Reviewing changes for callers.** A review of a decision that carries texts must give a
  verdict on each, and a free-text action, or one asked for after the decision was made, must
  be named to be released. A caller that confirmed labels and relied on the release has to
  say so explicitly.
- **A rationale is still returned.** An answer's evidence (a rationale of up to 500
  characters, the names of the tools called) goes back with every decision. It is evidence
  for a reviewer and is not a text for a patient or a chart; an integration that sends it on
  is outside what this decides.
- A decision's record gains an optional key, an `action.proposed` event says whether its
  arguments can hold free text, and two event kinds are added (`output.held`,
  `output.resolved`), which the FHIR audit code system lists: so this ships with a new Mendari
  version, never under a released one. No table is added: a held text is a note.
- **A decision that was waiting when the engine is upgraded.** Its proposals were recorded
  before any said whether its arguments can hold free text. A held one is read as one whose
  can, since nothing says otherwise: its review releases it only by name. No text was kept
  for it, so its review gives no verdict. An older engine does not know these rules: every
  instance is stopped before the upgrade, and none is brought back once a text is held.

## Not decided here

- **A judge that clears a text.** The next record. The review of its first design set what it
  needs: a round run by the server on the organization's own connection and on cases like the
  ones that would automate; the writer, the judge and their settings bound, not their names
  alone; a design fixed before grades exist (every sampled text graded, a seed nobody picks,
  the error level spent across rounds); at least three graders; a hold that a resubmitted case
  cannot draw again; and spot checks of what the judge clears, with this record's verdicts as
  their measure.
- **Which action uses which text.** A manifest does not say. At review, a withheld text holds
  every action that needs an automated decision or a person unless the review names it. At
  decision time nothing ties them: an automated decision may release an action of enumerated
  arguments whose text was not returned, and the caller has no text to send with it.
- **An identifier type** for arguments, and whether a judge may also clear an action's
  free-text argument.
- **What an author and a caller do together.** A menu still carries a few bits, and a caller
  that joins the arguments of many released actions into a text sends what nobody read. So do
  the order of an object's keys and the spelling of a number (`5`, `5.0`), which are kept as
  the agent sent them. Mendari authorizes and never runs: what a caller does with an answer is
  the caller's.
- **Characters that look alike.** What is taken out of a text is a list, Unicode's of what is
  not shown. A letter of another script that looks like a Latin one is a letter, and stays.

## Alternatives considered

- **Build the judge's gate first.** Reviewed and rejected for now: see the context.
- **Send every decision that carries a text to a person.** This was the first build, and it
  was measured before it was kept: the bundled demo agent drafts an acknowledgement with every
  message, and 326 of its 600 test decisions automate their routing while carrying one. It
  would have automated nothing. The text is what needs a person, not the routing.
- **Hold the `write` and `contact` actions of a decision that carries a text.** The same
  measurement: the demo agent's automated decisions put the message in a queue, which has
  nothing to do with the draft. Without the manifest saying which action uses the text, the
  hold would be a guess that costs every agent its automation.
- **Hold only texts that have a rubric.** An author would switch the hold off by declaring
  none.
- **Apply it on a server with sign-in only,** as the canned cap does. A local decision would
  then hand out a text no deployment does.
- **Treat a short string as an identifier.** A dose fits in sixteen characters.
- **Keep released texts as well.** Nothing reads them yet; spot checks will need a sample, not
  every text.
- **Have a review say what it was shown** (the queue as of some event, sent back with the
  label), to release what came with the answer and nothing newer. It asks every caller to
  carry a token for a case the action's own record already tells apart: an action that came
  with the answer is chained with the decision, so a reviewer who sees the decision sees it.
- **Withhold the draft from the answer of a decision that awaits a person, too.** The person
  who decides needs it, and where texts are not kept (PHI mode `hash`) the answer is the only
  place it is.
