# ADR-0022: An agent proposes actions, the runtime releases them, and the caller runs them

## Status
Accepted (2026-11-24), for wave 4 of the plan in `tasks/todo.md`. Specified in
[agent manifest spec 0.4](../spec/agent-manifest-0.4.md).

Decided by the maintainer on 2026-11-24, before the repository was public, so no comment period
ran; open to revision when it is.

## Context
Mendari governs one thing: an agent's answers to the closed questions its manifest declares. A
deterministic policy turns them into AUTO, HUMAN_REVIEW, ABSTAIN or BLOCK, and the audit chain
keeps the result. It sees nothing of what an agent does. The agent contract has `tool_calls`,
but that is a report after the fact: names the agent says it called, kept as evidence
(`tool:<name>`) and checked against nothing.

Agents worth deploying act: they queue a refill, file a note, answer a patient. Until now the
outcome said whether an answer may be used without a person, and nothing said whether a write
may happen. A site that adopted an agent built that gate itself, outside the audit trail.

Two parts of the engine already gate one thing each on a decision. The guard (spec 0.2 §9.1)
releases, holds or blocks one draft to a patient. The prior-authorization route sends a request
to a payer only for a decision that was automated or that a reviewer confirmed. Actions are the
general form of both.

What the engine holds any design to:

- Policy only lowers automation, and its limits live in the runtime, never in a manifest
  ([ADR-0002](ADR-0002-runtime-enforced-risk-caps.md)).
- The chain keeps ids, codes and hashes, never case text; what can hold patient data is kept
  apart, as the PHI mode allows ([ADR-0005](ADR-0005-hardening-before-phi.md)).
- Every chained event is about a row that `verify` finds and one organization owns.
- Only a decision that awaits review has a reviewer. An automated one has nobody to wait for.
- Mendari is not a workflow server ([ADR-0003](ADR-0003-a-served-task-is-a-decision.md)): it
  holds no credential to a chart or to a patient channel.

## Decision
1. **An agent declares its actions.** A new optional `actions` section (spec 0.4): each action
   has a name, a description a person can read, an `effect` from a closed set, and optionally a
   JSON Schema for its arguments. The effects: `read` (reads data), `draft` (prepares something
   a person sees before it takes effect), `write` (changes the chart or another system of
   record), `contact` (sends something to a patient or anyone outside the care team), `order`
   (orders, prescriptions, treatment changes).
2. **The runtime says what each effect needs**, in `core/policy.py`, as it does for risk
   levels. `read` and `draft` are released with any decision that is not BLOCK. `write` and
   `contact` are released only with an automated decision, and otherwise wait for the reviewer.
   `order` is never released without a person. A manifest may only tighten this, with
   `allow_auto: false` on an action (the key a label's policy already uses). For an agent the
   deployment has not vouched for (a community agent, or one from a registry before its digest
   is approved there, or while it is recalled) no effect is released without a person: its
   declared effects are claims.
3. **An agent proposes; it does not act.** Its answer may carry `actions` (`name`,
   `arguments`). Mendari checks each against the declaration and answers, in the decision's
   result, with a disposition: `released`, `held` or `blocked`. The caller runs released
   actions only.
4. **A proposal that cannot be released lowers the decision.** An undeclared action, arguments
   that do not match their schema, and a composite member's proposal are blocked, and an
   automated decision becomes HUMAN_REVIEW: an agent that proposes what it never declared is
   not trusted to automate. An `order`, or an `allow_auto: false` action, also makes an
   automated decision HUMAN_REVIEW, because that is the only way it gets a person. One more cap
   in `apply_policy`: it turns AUTO into HUMAN_REVIEW and changes nothing else. What the policy
   decided from keeps it, so an evaluation's operating point re-applies it exactly.
5. **Held means "waits for this decision's reviewer".** So an action is held only on a decision
   that awaits review and has a recommendation. Where no confirmation can come (an abstention,
   a decision already reviewed, an `order` asked for on an automated decision) it is blocked:
   an ABSTAIN blocks what writes, contacts or orders at once. Confirming the recommendation
   releases the held actions, an `order` among them (a person decided); overriding it denies
   them; a reviewer may deny one action and still confirm the label (`deny_actions`), so
   refusing an action never records a false override.
6. **A caller may ask again, for a decision.** `POST /api/agents/{namespace}/{name}/actions`
   and the MCP tool `authorize_action` ask whether one action with these arguments may run for
   a decision already recorded, and are answered by the same gate. Asking again for the same
   action answers its current disposition and chains nothing new, so the call is also the check
   a caller makes right before it runs a tool. There is no authorization without a decision.
7. **The chain keeps what was proposed, never what it said.** Two event kinds on the decision:
   `action.proposed` and `action.resolved`, with ids, the declared name, the effect, the
   disposition, a reason code and a keyed hash of the arguments. The arguments are kept as
   inputs are kept: sealed under PHI mode `encrypt`, apart from the chain under `off`, not at
   all under `hash`, and only for an agent that keeps its inputs. A name the manifest does not
   declare is the agent's text, so it is kept with the arguments and never chained.
8. **Mendari runs nothing.** It authorizes and records.
9. **A release is final** in this version: a hold stops new releases, not one already given.
10. **A recorded governance approval that lets a level 2 or 3 agent automate also lets its
    `write` and `contact` actions be released with an AUTO decision.** The approval names one
    digest, and that digest's Decision Card lists each action and how it is gated.

## Consequences
- A site gets one gate and one trail for what an agent does, under the rules it already
  approved for what the agent says. A hold, a missing approval, a recall or a provider failure
  stops automation of the decision and so stops releases of `write` and `contact` with it.
- The effect is the author's declaration, as the risk level is. Mendari cannot check what a
  tool does. The Decision Card lists each action with its effect and how it is gated, an
  approval names the digest that card describes, and an agent nobody here vouched for gets no
  benefit from a mild declaration.
- Mendari checks that an action is declared and well formed, not that it is right for this
  patient. That is what the decision and the reviewer are for. A `contact` released with an
  automated decision carries text a model wrote: an agent that sends free text to patients
  should set `allow_auto: false` on it, as the bundled agent does on its acknowledgement.
- No table, column or migration is added. Two event kinds join the decision's, and arguments
  use the notes that reviews already use, so retention, erasure, key rotation and
  `audit verify --notes` cover them unchanged. A database from before verifies as it did, and a
  release rolled back reads the new events as it reads any kind it does not know.
- Revoking a release, its expiry and a caller's report that it ran the action come next.
- Agents on spec 0.1 to 0.3 decide as before. An `actions` key in their agent's response is
  ignored, as it always was.
- An agent behind the contract cannot ask mid-run, before its decision exists. It proposes
  with its answer. See the alternatives.

## Alternatives considered
- **Check `tool_calls` after the fact.** Rejected: it audits and cannot gate. The write has
  happened.
- **Mendari runs the tools.** Rejected: it would hold credentials to charts and patient
  channels, a new trust boundary and a second source of truth for work the EHR owns
  (ADR-0003).
- **Let the manifest say what an effect needs.** Rejected for the reason ADR-0002 gives: the
  publisher, not the deployer, would decide what happens without a person at someone else's
  site.
- **Hold an action on an automated decision, in a queue of actions.** Rejected for now: a
  second review queue, with its own roles and views, where lowering the decision reuses the
  one that exists and keeps "policy only lowers automation" in one place.
- **A table of actions.** Rejected: nothing in this version needs one. State is the decision's
  events, read with it.
- **Authorize without a decision, for a "context" or a run in flight.** Rejected for now: a
  chained event must be about a row an organization owns, and a decision's id and row exist
  only once its providers have answered. It needs a subject of its own (a run), which is a
  table and a class of event. Before a decision exists nothing but `read` and `draft` could be
  released anyway.
- **Let a reviewer's label release an abstention's actions.** Rejected: a label the agent never
  recommended would authorize what the agent asked for.
- **A permission of its own for releasing an order.** Rejected for now: Mendari places no
  order, and the system that does enforces its own privileges. Reviewing is the permission.
- **Keep the arguments in the decision record.** Rejected: the record's hash is chained for
  good, so nothing could erase them.
- **An open set of effects, or tags.** Rejected: the runtime's table must be total, and a
  reviewer must be able to learn five words.
