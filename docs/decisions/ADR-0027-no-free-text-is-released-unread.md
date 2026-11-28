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
  written text was not on the reviewer's screen, since it was kept nowhere.

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
   are as they were. It holds everywhere a decision is served (a server with or without
   sign-in, `mendari decide`, `mendari mcp`, which returns no text at all). An evaluation is
   not affected: it grades every text the agent wrote, and hands none on.

3. **Free text in an action's arguments needs a person too.** An action whose arguments can
   hold free text is never released without a person when its gate would be `auto`: a `write`
   or a `contact` action, and a `read` or `draft` one of an agent nobody vouched for. Its gate
   is `person`. Arguments can hold free text when their schema admits a string that is not
   limited to an `enum` or a `const`, or a list of anything (no `items`), or when the action
   declares no schema at all; the closed set of keywords an arguments schema may use
   (section 13.3) makes that decidable from the manifest alone. Proposing such an action turns
   AUTO into HUMAN_REVIEW, as any action never released without a person does. This is a
   fourth way the action gate tightens, beside `allow_auto: false`, an agent nobody vouched for
   and a second signal that did not agree; nothing loosens one.

4. **A held text is kept for the person who reads it.** When a decision that carries texts is
   recorded and awaits review, each text is kept as an action's arguments are: apart from the
   record, as the note of a chained event (`output.held`), sealed under PHI mode `encrypt`,
   and only where the decision's input is kept (never under PHI mode `hash` or `store_input:
   hash`). The event holds the output's name and a salted commitment, never the text.
   Retention and erasure remove it with the input. The review queue shows it to whoever may
   read inputs, and that reading is chained as reading the input is. This replaces ADR-0025's
   "a decision returns what the agent wrote and does not store it" for a held text only: a
   released one is still kept nowhere.

5. **Free text is released by name.** Resolving a review:
   - gives each written text a verdict, `release` or `withhold`, chained as `output.resolved`
     (the output's name, the verdict, the reviewer). A review that leaves one out is refused;
   - releases a held action whose arguments can hold free text only when it names it
     (`release_actions`). Confirming the label no longer releases it;
   - when a text is withheld, releases no held action that needs an automated decision or a
     person unless it names it: a manifest does not say which action uses which text.

   A held action with no free text, on a decision none of whose texts is withheld, is resolved
   as before: released when the reviewer confirms the label, unless named in `deny_actions`.
   Where the text is not kept (point 4), the verdict is still required: the reviewer read the
   draft where the caller shows it.

6. **The record says where each text stands, and no more.** A decision whose agent wrote
   texts gains `written` in its record and its answer: each output's `state`, `held` (it
   awaits a person, who is handed it) or `not_released` (the decision automated or was
   blocked: it was handed to nobody). After review the decision's answer gives the verdict,
   `released` or `withheld`, read from the chain. No text is in a record, a reason, a log line
   or an event.

## Consequences

- No free text an agent produced reaches a patient or a chart through a decision unless a
  person released it by name. An author no longer chooses whether text is read.
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
- **More is kept.** Held texts are kept until retention or erasure removes them, sealed as
  inputs are. A deployment in PHI mode `hash` keeps none, and its reviewers give their verdict
  on a draft Mendari does not show.
- **Reviewing changes for callers.** A review of a decision that carries texts must give a
  verdict on each, and a free-text action must be named to be released. A caller that
  confirmed labels and relied on the release has to say so explicitly.
- **A rationale is still returned.** An answer's evidence (a rationale of up to 500
  characters, the names of the tools called) goes back with every decision. It is evidence
  for a reviewer and is not a text for a patient or a chart; an integration that sends it on
  is outside what this decides.
- A decision's record gains an optional key, an `action.proposed` event says when its
  arguments can hold free text, and two event kinds are added (`output.held`,
  `output.resolved`), which the FHIR audit code system lists: so this ships with a new Mendari
  version, never under a released one. No table is added: a held text is a note.

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
- **Withhold the draft from the answer of a decision that awaits a person, too.** The person
  who decides needs it, and where texts are not kept (PHI mode `hash`) the answer is the only
  place it is.
